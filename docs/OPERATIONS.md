# Personal expenses bot

A Telegram expenses bot built with Python and SQLite, with separate data for each user. All daily interactions happen
in a private Telegram chat using inline buttons. Enter amounts and custom category names
as messages. The interface, reports and Codex analysis are in English.

Features include ten default categories, custom categories, current-week and current-month
reports, the ten most recent expenses, and deletion with confirmation. Expenses use today's
date in Europe/Kyiv. The default currency is EUR; amounts are stored as integer cents.

## Faster entry without extra menu buttons

Send `12 coffee` or `8 taxi yesterday` directly in the chat. English, Russian and Ukrainian
common category words are recognized without AI. Append `YYYY-MM-DD` for an earlier date.
A recognized single expense is saved immediately. For an unknown description, choose its
category once: your exact description is remembered for future entries, separately per user.
Use **Change category** to correct an entry and its remembered category, or **Undo** to remove
the expenses created by that entry. Main-menu buttons remain unchanged. These shortcuts are
available to free and Pro accounts; Pro confirmations also show applicable budget balances.

Send up to 30 lines, such as `12 coffee` followed by `8 taxi`, to review a batch before confirming.
An invalid line prevents the entire message from being saved. Drafts and saved entry controls
persist across restarts. Undo removes saved expenses but retains learned category preferences.

Attach a UTF-8 CSV (up to 2 MB and 200 selected purchases). Supported formats are ZEN account
statements and columns `date,description,amount,currency`, with ISO dates, negative expense
amounts and positive income amounts. Generic CSV supports comma or semicolon delimiters.
Use an optional caption such as `2026-09-29 2026-10-04` to select an inclusive date range;
without a caption, all supported purchases through today are reviewed. ZEN imports use EUR
settlement amounts and statement dates; transfers, income and embedded transfer fees are excluded.
Other currencies are rejected rather than silently converted. CSV files are downloaded using
the [Telegram getFile API](https://core.telegram.org/bots/api#getfile) in a background worker.

CSV never saves immediately: review descriptions, dates, categories and totals, then confirm.
Previously imported transaction identities are skipped, including the earlier reviewed ZEN
import. Same-date/same-amount matches with existing expenses require a Skip/Keep both decision;
the bot cannot prove that such a match is a duplicate. It rechecks before committing. Changed
descriptions or dates can prevent identity matching, so review the preview carefully. Identical
CSV rows are tracked by occurrence, since separate purchases can have the same date and amount.
Raw files are not retained by the application; selected descriptions are kept in private SQLite
entry records so correction and undo work. No bank credentials or paid AI are involved.

## Apple Wallet connection (operator pilot)

The operator can connect a personal iPhone Transaction automation without changing the bot menu.
This receives new transactions supplied by Shortcuts, not the Wallet history or bank statements.
Other users cannot request the operator's connection key. Availability of merchant/amount data
depends on the device and card; a real iPhone end-to-end test is required after setup.

The server uses `wallet_bridge.py`, bound only to `127.0.0.1:8787`, behind Caddy HTTPS.
Templates are in `deploy/expenses-wallet.service` and `deploy/Wallet.Caddyfile`. The current
pilot hostname uses [sslip.io](https://sslip.io/), a free external DNS dependency; it can be
replaced with an owned domain. Caddy manages certificate renewal. Ports 80/443 must be reachable.
Generate a separate random key of at least 32 characters and save it with the public HTTPS
URL in private, Git-ignored `data/wallet-config.json` (mode 600, owned by expensesbot).
The key is unrelated to the Telegram token. Restart both services after key rotation.
The bot only enables Wallet processing if that file exists and the account uses EUR.

In the private operator chat, `/wallet_setup` returns the HTTPS URL and Authorization header.
Keep this header private; do not include it in screenshots or publicly shared shortcuts.
In Shortcuts, first add **Get Contents of URL**, set **Method** to **POST**, add the
**Authorization** header supplied by the bot, and choose **Request Body: JSON**.
For the initial connection check, add just `probe` as a **Boolean** set to true, then show the
response with **Show Content**. A successful response includes `expense_created: false`.
This probe does not create an expense or queue record.

After the probe works, remove `probe`. Use these fields for actual transaction delivery:

| Field | Shortcuts value | Type |
| --- | --- | --- |
| `id` | Generate UUID once for the event; reuse it for retries | Text |
| `amount` | Text containing Shortcut Input > Amount, including GEL or EUR | Text |
| `merchant` | Shortcut Input > Merchant | Text |
| `timestamp` | Current Date formatted as ISO 8601, e.g. `yyyy-MM-dd'T'HH:mm:ssXXX` | Text |

If Amount provides only a number, supply a separate `currency` field extracted from the actual
transaction. Do not hardcode GEL for a card also used in other currencies. Unknown currency is
rejected, never guessed. Use the purchase timestamp if available; Current Date is the capture
time, so run the automation immediately. Events must have a timezone and be no older than 31 days.
Each payload is limited to 4 KB. The endpoint accepts up to 30 new events per minute and never
accepts a user ID or database path from the caller. The separate key grants operator-only writes.

GEL amounts are divided by the dated EUR/GEL rate from the
[National Bank of Georgia](https://nbg.gov.ge/en/page/foreign-exchange-market), with cent rounding.
The exact requested effective date must match the response. Daily rates are cached; missing
rates leave the event pending and trigger a Telegram notice, with retries every five minutes.
Rate fetching runs outside the Telegram polling thread. No amounts or merchant data are sent to NBG.
Wallet dates use Asia/Tbilisi; existing manual-entry/report schedules remain Europe/Kyiv.

Known categories save automatically; unknown merchants ask for a category. Confirmations retain
the original currency/amount and show the approximate EUR amount and rate. Change category and
Undo reuse the existing entry controls. To correct a settled amount, undo and re-enter the exact
EUR expense. Queue records survive restarts. Reusing an event ID cannot duplicate the expense;
reusing it with different data is rejected. New IDs with the same date/EUR amount require review.
CSV matching cannot reliably reconcile later bank settlement amounts or dates with estimates:
review bank imports and skip/correct corresponding Wallet entries to avoid double counting.

Operations: `systemctl status expenses-wallet caddy expenses-bot`. `/health` is a public readiness
probe; transaction POSTs require the private key. Daily SQLite backups include the Wallet queue
and cached rates. Back up the private Wallet configuration separately when moving servers.

## First launch on Windows

1. Create a bot with [the official @BotFather](https://t.me/BotFather) using `/newbot`.
2. The original operator account keeps its existing data. Other private-chat users receive isolated databases.
3. Open PowerShell in this directory and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start.ps1
   ```

4. Enter the token when prompted. Input is hidden and saved in local `config.json`,
   which is excluded from Git. Existing configuration is reused on subsequent launches.
5. Open your bot in Telegram and press Start.

The launcher uses an installed Python or the bundled Codex Python runtime on this computer.
Otherwise, install Python 3.11+ from python.org and enable Add Python to PATH.
For a separate installation, run `python -m pip install -r requirements.txt`.
All tests run before every launch. The bot does not start if tests fail.

Anyone can use the bot in a private chat. Each user has separate expenses, categories and input state.
Pro subscribers can run Codex analysis or enable its schedule. Run one instance at a time.
Keep the computer powered on, the process running and the internet connected. Press Ctrl+C
to stop a foreground instance. Automatic startup after reboot is not configured.

## Codex and weekly analysis

The bot uses local `codex exec` with your Codex account rather than a separate OpenAI API key.
Install Codex CLI and run `codex login`. If the executable cannot be found, set `CODEX_BIN`
to its full path. On a computer with Codex Desktop, use `Get-Command codex` to locate it.

Use the Analyze last week button for a manual report. Enable automatic reports
through the Auto weekly reports settings. Scheduling is disabled by default.
Once enabled, the bot analyzes the previous complete Monday-to-Sunday week after 09:00
Europe/Kyiv on Mondays, comparing it with the week before that.

If the computer was off, the bot catches up with the most recent completed week when restarted.
It does not send every historical missed report. Each Pro user gets one generation attempt per
completed week, shared by manual and automatic analysis. Results are saved in SQLite and reused
after restarts. Failed or interrupted attempts are not automatically repeated, to prevent duplicate
costs. Only one AI process runs at a time; a busy server does not consume another user's attempt.
Current-week expenses become eligible for analysis on the following Monday.

Only category names, aggregate amounts and counts are passed to Codex. Analysis uses your
account allowance and requires a valid login and available usage limits. Codex runs in a
separate temporary directory, with a read-only sandbox, without user configuration or
persisted session files. The Telegram token is excluded from its environment.
Suggestions should be reviewed: the bot does not know your income or financial obligations.

## Data and configuration

The operator database is `data/expenses.sqlite3`; other accounts use `data/users/<telegram-id>.sqlite3`. Keep it private and
exclude `data/` from shared note synchronization if necessary. To back it up, stop the bot
and copy the database. To reset the test completely, stop the bot and remove `data/`;
this deletes the expense history and owner link. Restart unfinished expense entry after a restart.

The token is stored in `config.json`; see `config.example.json` for its structure.
Never publish the real configuration. Supported environment variables:

- `TELEGRAM_BOT_TOKEN`: overrides the token in the configuration file.
- `TELEGRAM_OWNER_ID`: optional initial owner assignment; an existing database link takes precedence.
- `CODEX_BIN`: optional path to the Codex executable.
- `CURRENCY`: defaults to EUR and applies only when the database is first created.

The database keeps its original currency to prevent mixed-currency totals.
Existing default categories are migrated to English automatically while preserving their
expense links. User-created categories are preserved as entered.

## Verification after changes

```powershell
python -m unittest discover -s tests -v
python -m compileall -q bot.py tests
```

Tests use temporary databases and simulated Telegram/Codex responses. They do not send
messages, use paid analysis or touch personal data. They also check that project text files
contain no Cyrillic characters. Legacy database labels use escaped migration identifiers.

For a live smoke test: Start, Add expense, enter 12.50, choose a category, open This week,
then delete the test expense. Run `python check_connection.py` for a read-only Telegram
connection check that never prints the token. Live Codex analysis needs completed-week data.

Official references: [Telegram Bot API](https://core.telegram.org/bots/api) and
[Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/).

## Linux server deployment

The current server runs Ubuntu 24.04 with the application at `/opt/expenses-bot` under
the dedicated `expensesbot` account. Windows launch scripts are not used on the server.
The `deploy/` directory contains systemd units, an installation script and a SQLite backup script.
Run `deploy/setup.sh` as root after uploading the project files; it installs dependencies,
creates the service account, installs the units and runs tests without starting the bot.
Install Codex separately with `python3 deploy/install_codex.py`; this verifies the official
release asset digest before installing the x86_64 Linux binary.

For cutover, stop the local instance, create a consistent SQLite backup, then securely transfer
that database and `config.json` over SSH. Give both files to `expensesbot` with mode 600.
Keep the owner link in the migrated database. Never run local and remote polling simultaneously.

Server operations:

```sh
systemctl status expenses-bot
systemctl restart expenses-bot
journalctl -u expenses-bot -n 50 --no-pager
systemctl enable --now expenses-bot expenses-bot-backup.timer
systemctl start expenses-bot-backup.service
runuser -u expensesbot -- /usr/local/bin/codex login --device-auth
```

The service starts automatically after reboot and restarts after a crash. Daily backups run
at 03:00 UTC, keep the latest seven copies and live in `/var/backups/expenses-bot`.
These are on the same server: they do not protect against loss of the entire server.
Codex account login must be completed separately before AI analysis is available.
Ordinary Telegram expense tracking and reports do not depend on Codex login.

## Lifetime test subscription using Telegram Stars

Unsubscribed users see `Subscribe - 1 Star` in the main menu. The offer explains the terms;
`Agree and pay 1 Star` opens a Telegram Stars invoice. This is a real one-time charge of
exactly 1 Star (XTR), not a recurring subscription. Subscriber status has no expiry.
Existing active subscriptions unlock Pro automatically without another payment.
Expense entry, deletion, categories, current-week and current-month totals remain free.
Pro includes weekly AI analysis (manual and scheduled), category budgets, recurring payment
reminders and month comparisons. Refunding the subscription disables Pro actions and notifications;
expense history is preserved.

## Pro tools

- **Category budgets:** set a monthly EUR limit per category. Limits repeat every month until
  changed or disabled. View spent and remaining amounts; notifications appear at 80% and 100%.
  Thresholds are recorded to avoid repeated alerts for the same month and limit.
  After saving an expense, Pro users also see that category's monthly spending, limit and
  remaining budget (or the amount over budget) directly in the confirmation.
- **Recurring payments:** enter a name, amount, category and next due date (`YYYY-MM-DD`).
  Payments repeat monthly, preserving the original day; short months use their last day.
  Reminders appear from 09:00 Europe/Kyiv on or after the due date. **Mark paid** creates one
  expense dated today; repeated clicks cannot duplicate it. **Skip** advances without recording
  an expense. Both move to the next future occurrence, skipping missed months. Deleting a reminder
  keeps recorded expenses. No bank charges or automatic expenses are created.
- **Compare months:** compare two completed calendar months, including category totals and
  absolute/percentage changes. Previous/next buttons browse older months. No AI call is required.

Budgets, reminders, entitlements and cached analysis are isolated per user and survive restarts.
Background checks include users who have not opened the bot since its last restart. Reminder
delivery can be delayed when Telegram is unavailable. Savings goals and receipt input are not included.

Subscription status is granted only after a validated `successful_payment` update.
Pre-checkout validates currency, amount, invoice ownership, age and duplicate checkout state.
Payment charge IDs are persisted for idempotency and refunds. After payment, the Subscribe
button is omitted from new menus and removed from tracked old menus in the background.
Old menus that Telegram can no longer edit may retain a stale button, but cannot charge an
active subscriber again. Status survives server restarts and is included in daily backups.

Commands:

- `/subscription`: view the offer or your active subscriber status.
- `/terms`: view the test purchase terms.
- `/paysupport`: open a self-service refund with confirmation.
- `/paysupport <question>`: save a payment support request for the operator.
- `/support_requests`: operator-only view of recent payment support requests.

A confirmed refund returns the Star through Telegram and revokes that payment's entitlement.
A Telegram refund event also updates local status. User databases are private, ignored by Git
and included in daily backups. Keep all user databases when migrating or restoring the service.

Payment tests simulate Telegram events and never spend Stars. A live invoice can be created
without charging anyone; a real purchase still requires the user to approve it in Telegram.
Reference: https://core.telegram.org/bots/payments-stars
