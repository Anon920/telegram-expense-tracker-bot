# Telegram Expense Tracker Bot

A multi-user Telegram bot for tracking personal expenses, built with **Python standard library + SQLite** (no frameworks, one runtime dependency). It runs in production on a Linux server under systemd and is covered by **123 automated tests**.

## What it does

- **Fast entry:** type `12 coffee` or `8 taxi yesterday`; known words map to categories automatically (EN/RU/UA keywords), unknown descriptions are learned per user.
- **Batch and CSV import:** up to 30 lines per message, or a bank-statement CSV (generic or ZEN format) with a review step before anything is saved.
- **Reports:** current week / month, last 10 expenses, budgets and monthly recurring-payment reminders.
- **AI weekly analysis (Pro):** aggregated category totals are passed to a sandboxed `codex exec` run; results are cached so each week is analysed once.
- **Payments:** Pro access through Telegram Stars; entitlement is granted only after `successful_payment`.
- **Wallet bridge (pilot):** an authenticated HTTPS endpoint receives iPhone Wallet transactions and converts GEL to EUR using the dated National Bank of Georgia rate.

## Architecture

```
Telegram Bot API (long polling)
        │
     bot.py ── Bot / PublicBot: routing, inline keyboards, per-user state
        │
        ├── quick_entry.py   free-text + CSV parsing, review drafts, undo
        ├── pro_features.py  budgets, reminders, month comparisons, AI cache
        ├── subscriptions.py Telegram Stars orders and payment ledger
        └── wallet_bridge.py localhost HTTP inbox (behind Caddy), NBG rates
        │
     SQLite: one database per user (data/users/<telegram-id>.sqlite3)
```

## Engineering decisions

- **Data isolation:** each Telegram user gets a separate SQLite file; the HTTP bridge never accepts a user ID or file path from the caller.
- **Money as integer cents** in a fixed currency per database, so totals never mix currencies.
- **Idempotent imports:** transaction identities are stored; repeated CSV rows or retried Wallet events cannot create duplicates; ambiguous same-day/same-amount matches require a user decision.
- **Secrets stay out of code and Git:** the token comes from `TELEGRAM_BOT_TOKEN` or an ignored `config.json`; the Wallet key is separate and HMAC-compared; the AI subprocess runs with the token removed from its environment and a read-only sandbox.
- **Hardened deployment:** dedicated system user, `NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`, `UMask=0077`; daily consistent SQLite backups via the `sqlite3` backup API with 7-day retention (`deploy/`).
- **Resilience:** background workers for downloads and rate fetching, retries for missing exchange rates, state that survives restarts.

## Run locally

```bash
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=<token from @BotFather>
python bot.py
```

Windows: `powershell -ExecutionPolicy Bypass -File .\start.ps1` (runs the test suite before starting).

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases and simulated Telegram / Codex / HTTP responses; they never send messages or touch real data.

## Deployment

`deploy/setup.sh` prepares an Ubuntu server (service account, venv, systemd units, test run). Full operating notes: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Stack

Python 3.11+ · SQLite · Telegram Bot API · systemd · Caddy · unittest
