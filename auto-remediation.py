#!/usr/bin/env python3
"""Auto-remediation daemon: tail a Telegram outbox, spawn Claude on failure alerts.

Reads an append-only outbox log line-by-line (with inode tracking so file
rotation is handled). Classifies each new line as a failure alert or not.
For failures it spawns `claude --print --permission-mode bypassPermissions`
with a remediation prompt. If Claude emits an ESCALATE: line, the daemon
forwards that escalation to the configured operator via a tg-send wrapper.
Otherwise it stays silent — the goal is to drain the noise floor, not
generate more.

Design notes:
- Allowlist classifier — only triggers on strong failure signals. False
  positives spawn unnecessary Claude sessions ($$); false negatives just
  leave the original alert in place (cheap).
- Per-(label, hash) dedup window to avoid spawn storms when the same
  failure alert fires repeatedly.
- Circuit breaker — if >N spawns inside W seconds, the daemon pauses and
  pings the operator. Protects budget if classifier ever goes wild.
- Skips its own escalation messages so it can't loop on itself.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

HOME = Path.home()
OUTBOX_PATH = Path(os.environ.get("AR_OUTBOX", str(HOME / "state/tg-outbox.md")))
STATE_DIR = Path(os.environ.get("AR_STATE_DIR", str(HOME / "state/auto-remediation")))
TG_SEND = os.environ.get("AR_TG_SEND", str(HOME / "scripts/tg-send-logged.sh"))
CLAUDE_BIN = os.environ.get("AR_CLAUDE_BIN", "claude")
CLAUDE_CWD = os.environ.get("AR_CLAUDE_CWD", str(HOME))
CLAUDE_MODEL = os.environ.get("AR_CLAUDE_MODEL", "sonnet")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEDUP_WINDOW_SEC = int(os.environ.get("AR_DEDUP_SEC", "600"))
BACKLOG_MAX_AGE_SEC = int(os.environ.get("AR_BACKLOG_SEC", "300"))
CLAUDE_TIMEOUT_SEC = int(os.environ.get("AR_CLAUDE_TIMEOUT", "900"))
POLL_INTERVAL_SEC = float(os.environ.get("AR_POLL_SEC", "2"))
MAX_CONCURRENT = int(os.environ.get("AR_MAX_CONCURRENT", "2"))

CB_WINDOW_SEC = int(os.environ.get("AR_CB_WINDOW", "300"))
CB_MAX_SPAWNS = int(os.environ.get("AR_CB_MAX", "8"))
CB_PAUSE_SEC = int(os.environ.get("AR_CB_PAUSE", "1800"))

DRY_RUN = os.environ.get("AR_DRY_RUN", "0") == "1"

LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] \[(?P<label>[^\]]+)\] "
    r"\[chat:(?P<chat>[^\]]+)\] \[msg:(?P<msg>[^\]]+)\] (?P<body>.*)$"
)

FAILURE_LABELS = re.compile(
    r"(watchdog|monitor|health|alert|sentry|guard|sentinel)",
    re.IGNORECASE,
)
FAILURE_BODY = re.compile(
    r"(?:^|\W)("
    r"ALERT:|FAIL(?:ED|URE)?\b|ERROR\b|❌|⚠️|"
    r"\bdown\b|\bunreachable\b|\bunreadable\b|\bmissing\b|"
    r"\bcrashed?\b|\binactive\b|\bdead\b|"
    r"\btimeout\b|\btimed out\b|\bstuck\b|\bhung\b|"
    r"\bnot running\b|\bnot responding\b|"
    r"\brestart failed\b|\bunable to\b|\bcannot\b"
    r")",
    re.IGNORECASE,
)
SUCCESS_HINT = re.compile(r"(✅|\bok\b|\brecovered\b|\brestored\b|\bback up\b)", re.IGNORECASE)
SKIP_LABELS = set(filter(None, os.environ.get("AR_SKIP_LABELS", "auto-remediation").split(",")))

REMEDIATION_PROMPT = """\
A failure alert just hit the operator's Telegram. Investigate and fix
if you can.

Alert:
  timestamp: {ts}
  source label: {label}
  message: {body}

Your job:
1. Identify what's failing. Check logs, service status, the file or
   resource implicated. Read CLAUDE.md (in this working directory if
   present) for operator-specific context and gating rules.
2. If the fix is bounded and reversible and does NOT touch gated items
   (payments, software installation, configuration edits flagged in
   CLAUDE.md, security exceptions, credential handling, external sends
   like emails/tweets, crypto operations), apply it. Follow whatever
   "fix bugs autonomously" rule the operator has in CLAUDE.md.
3. Verify the fix worked (health check, port probe, log tail, whatever
   is appropriate for this alert).
4. If you cannot fix it because:
     - it touches a gated item that requires the operator's approval, OR
     - it needs information only the operator has, OR
     - it's blocked by something outside this machine,
   then your FINAL line must begin with "ESCALATE:" followed by a
   one-paragraph explanation of what's blocked and what the operator
   needs to do.
5. Otherwise stay silent. Do not post a "fixed it" confirmation. The
   operator does not want confirmation per self-heal — only escalations.

Be terse. No preamble. No summary at the end unless escalating.
"""


@dataclasses.dataclass
class Alert:
    ts: str
    label: str
    chat: str
    msg: str
    body: str
    raw: str

    def hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.label.encode())
        h.update(b"\0")
        h.update(self.body[:200].encode())
        return h.hexdigest()[:16]


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("AR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )


def is_failure(alert: Alert) -> bool:
    if alert.label in SKIP_LABELS:
        return False
    if SUCCESS_HINT.search(alert.body):
        return False
    if FAILURE_LABELS.search(alert.label):
        return True
    if FAILURE_BODY.search(alert.body):
        return True
    return False


def alert_age_sec(alert: Alert) -> float | None:
    try:
        from datetime import datetime, timezone
        ts = alert.ts.rstrip("Z")
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


class Dedup:
    def __init__(self, window: int):
        self.window = window
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            cutoff = now - self.window
            self.seen = {k: t for k, t in self.seen.items() if t > cutoff}
            if key in self.seen:
                return False
            self.seen[key] = now
            return True


class CircuitBreaker:
    def __init__(self, window: int, max_count: int, pause: int):
        self.window = window
        self.max = max_count
        self.pause = pause
        self.spawns: deque[float] = deque()
        self.paused_until: float = 0.0
        self.lock = threading.Lock()

    def can_spawn(self) -> tuple[bool, str | None]:
        now = time.time()
        with self.lock:
            if now < self.paused_until:
                return False, f"paused for {int(self.paused_until - now)}s"
            cutoff = now - self.window
            while self.spawns and self.spawns[0] < cutoff:
                self.spawns.popleft()
            if len(self.spawns) >= self.max:
                self.paused_until = now + self.pause
                self.spawns.clear()
                return False, f"tripped: {self.max} spawns in {self.window}s"
            self.spawns.append(now)
            return True, None


def escalate_to_operator(label: str, text: str) -> None:
    if not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_CHAT_ID not set; can't escalate. Would have sent: %s", text[:200])
        return
    try:
        subprocess.run(
            [TG_SEND, TELEGRAM_CHAT_ID, label, text],
            check=False,
            timeout=30,
            env={**os.environ, "TELEGRAPH_ORIGIN_JSON": json.dumps({"type": "script", "label": label})},
        )
    except Exception as e:
        logging.error("escalate_to_operator failed: %s", e)


def remediate(alert: Alert) -> None:
    prompt = REMEDIATION_PROMPT.format(ts=alert.ts, label=alert.label, body=alert.body[:1500])
    log_dir = STATE_DIR / "sessions"
    log_dir.mkdir(parents=True, exist_ok=True)
    session_log = log_dir / f"{int(time.time())}-{alert.hash()}.log"

    logging.info("spawn claude: label=%s msg=%s log=%s%s", alert.label, alert.msg, session_log.name, " [DRY_RUN]" if DRY_RUN else "")
    if DRY_RUN:
        session_log.write_text(f"# DRY_RUN\n# Alert\n{alert.raw}\n\n# Prompt would have been:\n{prompt}\n", encoding="utf-8")
        return
    cmd = [
        CLAUDE_BIN,
        "--print",
        "--permission-mode", "bypassPermissions",
        "--model", CLAUDE_MODEL,
        "--output-format", "text",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=CLAUDE_CWD,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logging.error("claude timeout for %s", alert.hash())
        escalate_to_operator("auto-remediation", f"⏱️ Remediation timeout after {CLAUDE_TIMEOUT_SEC}s.\n\nOriginal alert:\n[{alert.label}] {alert.body[:400]}")
        return
    except Exception as e:
        logging.error("claude spawn failed: %s", e)
        return

    out = result.stdout or ""
    err = result.stderr or ""
    session_log.write_text(
        f"# Alert\n{alert.raw}\n\n# Prompt\n{prompt}\n\n# Stdout\n{out}\n\n# Stderr\n{err}\n",
        encoding="utf-8",
    )

    escalate = None
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.upper().startswith("ESCALATE:"):
            escalate = line[len("ESCALATE:"):].strip()
            break

    if escalate:
        logging.info("escalating: %s", escalate[:200])
        escalate_to_operator(
            "auto-remediation",
            f"🚨 Can't self-heal — needs you.\n\nAlert: [{alert.label}] {alert.body[:300]}\n\nBlocker: {escalate[:1500]}",
        )
    else:
        logging.info("silent fix (no escalate). out=%s", out[:200].replace("\n", " "))


class Tailer:
    """Tail a file with inode tracking. Yields complete lines as they're written."""

    def __init__(self, path: Path, position_file: Path):
        self.path = path
        self.position_file = position_file
        self.fh = None
        self.inode = None

    def _save_position(self):
        if self.fh is None or self.inode is None:
            return
        try:
            self.position_file.write_text(json.dumps({"inode": self.inode, "offset": self.fh.tell()}))
        except Exception as e:
            logging.warning("could not save position: %s", e)

    def _load_position(self) -> tuple[int | None, int]:
        if not self.position_file.exists():
            return None, 0
        try:
            data = json.loads(self.position_file.read_text())
            return data.get("inode"), int(data.get("offset", 0))
        except Exception:
            return None, 0

    def _open(self):
        if not self.path.exists():
            return False
        st = self.path.stat()
        prev_inode, prev_offset = self._load_position()
        self.fh = open(self.path, "r", encoding="utf-8", errors="replace")
        self.inode = st.st_ino
        if prev_inode == st.st_ino and prev_offset <= st.st_size:
            self.fh.seek(prev_offset)
            logging.info("resumed from offset %d (inode %d)", prev_offset, st.st_ino)
        else:
            self.fh.seek(0, os.SEEK_END)
            logging.info("starting at EOF (offset %d, inode %d)", self.fh.tell(), st.st_ino)
        return True

    def _rotated(self) -> bool:
        if not self.path.exists():
            return True
        return self.path.stat().st_ino != self.inode

    def lines(self):
        while True:
            if self.fh is None:
                if not self._open():
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
            line = self.fh.readline()
            if line:
                if line.endswith("\n"):
                    yield line.rstrip("\n")
                    self._save_position()
                else:
                    self.fh.seek(self.fh.tell() - len(line.encode("utf-8")))
                    time.sleep(POLL_INTERVAL_SEC)
            else:
                if self._rotated():
                    logging.info("rotation detected, reopening")
                    try:
                        self.fh.close()
                    except Exception:
                        pass
                    self.fh = None
                    self.inode = None
                else:
                    time.sleep(POLL_INTERVAL_SEC)


def parse_line(line: str) -> Alert | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    return Alert(
        ts=m.group("ts"),
        label=m.group("label"),
        chat=m.group("chat"),
        msg=m.group("msg"),
        body=m.group("body"),
        raw=line,
    )


def main() -> int:
    setup_logging()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    position_file = STATE_DIR / "outbox.pos"

    dedup = Dedup(DEDUP_WINDOW_SEC)
    breaker = CircuitBreaker(CB_WINDOW_SEC, CB_MAX_SPAWNS, CB_PAUSE_SEC)
    active = threading.BoundedSemaphore(MAX_CONCURRENT)

    def handle(alert: Alert) -> None:
        with active:
            remediate(alert)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    logging.info("auto-remediation watching %s", OUTBOX_PATH)
    tailer = Tailer(OUTBOX_PATH, position_file)

    for line in tailer.lines():
        if stop.is_set():
            break
        alert = parse_line(line)
        if not alert:
            continue
        if not is_failure(alert):
            continue
        age = alert_age_sec(alert)
        if age is not None and age > BACKLOG_MAX_AGE_SEC:
            logging.info("skipping stale alert (age=%.0fs) label=%s", age, alert.label)
            continue
        if not dedup.check(alert.hash()):
            logging.info("dedup skip label=%s", alert.label)
            continue
        ok, reason = breaker.can_spawn()
        if not ok:
            logging.warning("circuit breaker %s; skipping label=%s", reason, alert.label)
            if reason and reason.startswith("tripped"):
                escalate_to_operator(
                    "auto-remediation",
                    f"🛑 Circuit breaker tripped ({reason}). Pausing remediation for {CB_PAUSE_SEC//60}m. Triggering alert: [{alert.label}] {alert.body[:200]}",
                )
            continue
        threading.Thread(target=handle, args=(alert,), daemon=True).start()

    logging.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
