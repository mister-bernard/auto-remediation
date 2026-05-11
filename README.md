# auto-remediation

Tail a Telegram outbox log, classify failure alerts, spawn a Claude CLI
session to fix them. Stay silent on success — only re-escalate when a
fix is blocked.


## Why

A lot of services on a busy VPS write failure alerts to Telegram —
service watchdogs, disk monitors, VPN health checks, nginx error
trackers. Most are routine and self-fixable (restart a service, rotate
a stuck poller, clear a temp file), but no one is in the loop, so they
all dump straight to the operator's inbox.

This daemon catches alerts at the outbox layer, hands each one to a
Claude CLI session running locally with `--permission-mode
bypassPermissions`, and only forwards the alert to the operator when
the fix is genuinely blocked (gated by `CLAUDE.md` rules — payments,
software install, sensitive config edits, credential handling, etc.).

## How

```
┌────────────────────┐
│ any script/service │
└──────────┬─────────┘
           │  tg-send wrapper writes to outbox + Telegram
           ▼
┌────────────────────┐
│   outbox log file  │  ← append-only log of every TG message
└──────────┬─────────┘
           │  tail (inode-tracked)
           ▼
┌────────────────────┐  failure?  ┌──────────────────────────┐
│ auto-remediation   │────yes────►│ claude --print           │
│  classifier        │            │  --permission-mode       │
│  dedup (10m)       │            │  bypassPermissions       │
│  circuit breaker   │            │  --model sonnet          │
└────────────────────┘            └─────────────┬────────────┘
                                                │
                              ESCALATE: <text>  │  silent (fixed it)
                                                ▼
                                  forward one message to operator
                                  via tg-send wrapper
```

## Classifier

Allowlist — only triggers on strong failure signals. False positives
cost a Claude session; false negatives just leave the original alert
in place.

- **Labels** matching `watchdog|monitor|health|alert|sentry|guard|sentinel`
- **Body** matching `ALERT:|FAIL|ERROR|❌|⚠️|down|unreachable|missing|crashed|inactive|timeout|stuck|hung|not running|restart failed`
- **Skip** if body contains `✅|ok|recovered|restored|back up`
- **Skip** labels in `AR_SKIP_LABELS` (default: `auto-remediation` so it can't loop on itself)

## Safety

- **Dedup**: same `(label, body[:200])` won't trigger again for 10 minutes
- **Circuit breaker**: >8 spawns in 5 minutes pauses the daemon for 30 minutes and pings the operator
- **Stale skip**: alerts older than 5 minutes (e.g. backlog after daemon restart) are skipped
- **Inode tracking**: handles outbox rotation without re-processing old lines
- **Concurrency cap**: at most 2 Claude sessions running at once
- **Session logs**: full prompt + Claude output saved to `$AR_STATE_DIR/sessions/`
- **`AR_DRY_RUN=1`**: classify and log, don't actually spawn Claude

## Outbox line format

The daemon expects lines like:

```
[2026-05-11T12:00:00Z] [service-watchdog] [chat:123456] [msg:7890] ALERT: foo (:1234/health) unreachable
```

This is the format written by a `tg-send-logged.sh`-style wrapper. If
your wrapper uses a different format, override the `LINE_RE` pattern
in `auto-remediation.py`.

## Install

Requirements:

- Python 3.10+
- The `claude` CLI on `PATH` (or override `AR_CLAUDE_BIN`)
- A `tg-send`-compatible wrapper script that writes to an outbox log
- A `systemd --user` instance

```bash
git clone https://github.com/<your-org>/auto-remediation ~/projects/auto-remediation
cd ~/projects/auto-remediation
TELEGRAM_CHAT_ID=123456789 bash install.sh
```

The installer:

1. Writes `~/.config/auto-remediation/env` with your Telegram chat ID (mode 600, **not** committed)
2. Drops the systemd unit at `~/.config/systemd/user/auto-remediation.service`
3. Enables and starts the service

## Configure

All knobs are env vars. Defaults assume your home dir holds the outbox.

| Var | Default | Meaning |
|---|---|---|
| `TELEGRAM_CHAT_ID` | *(required for escalations)* | chat to forward blocked alerts to |
| `AR_OUTBOX` | `$HOME/state/tg-outbox.md` | outbox path |
| `AR_STATE_DIR` | `$HOME/state/auto-remediation` | dedup + session logs |
| `AR_TG_SEND` | `$HOME/scripts/tg-send-logged.sh` | escalation wrapper |
| `AR_CLAUDE_BIN` | `claude` | claude CLI binary |
| `AR_CLAUDE_CWD` | `$HOME` | working dir for spawned session |
| `AR_CLAUDE_MODEL` | `sonnet` | model alias |
| `AR_DEDUP_SEC` | `600` | dedup window |
| `AR_BACKLOG_SEC` | `300` | skip alerts older than this |
| `AR_CLAUDE_TIMEOUT` | `900` | per-session timeout |
| `AR_MAX_CONCURRENT` | `2` | concurrent sessions |
| `AR_CB_WINDOW` | `300` | circuit breaker window |
| `AR_CB_MAX` | `8` | spawns allowed in window |
| `AR_CB_PAUSE` | `1800` | pause duration after trip |
| `AR_SKIP_LABELS` | `auto-remediation` | comma-separated labels to ignore |
| `AR_DRY_RUN` | `0` | `1` = classify + log, don't spawn Claude |

Put any of these in `~/.config/auto-remediation/env` to override them
in the systemd context.

## Operate

```bash
systemctl --user status auto-remediation
journalctl --user -u auto-remediation -f
ls ~/state/auto-remediation/sessions/   # per-alert Claude transcripts
```

To silence temporarily: `systemctl --user stop auto-remediation`.

## License

MIT.
