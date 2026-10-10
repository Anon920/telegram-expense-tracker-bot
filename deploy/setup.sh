#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv ca-certificates
id expensesbot >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/expensesbot --shell /bin/bash expensesbot
install -d -o expensesbot -g expensesbot -m 700 /opt/expenses-bot/data /var/backups/expenses-bot
python3 -m venv /opt/expenses-bot/.venv
/opt/expenses-bot/.venv/bin/pip install --disable-pip-version-check -q -r /opt/expenses-bot/requirements.txt
chown -R expensesbot:expensesbot /opt/expenses-bot
chmod 700 /opt/expenses-bot
install -m 644 /opt/expenses-bot/deploy/expenses-bot.service /etc/systemd/system/
install -m 644 /opt/expenses-bot/deploy/expenses-bot-backup.service /etc/systemd/system/
install -m 644 /opt/expenses-bot/deploy/expenses-bot-backup.timer /etc/systemd/system/
systemctl daemon-reload
cd /opt/expenses-bot
runuser -u expensesbot -- .venv/bin/python -m unittest discover -s tests -v
runuser -u expensesbot -- .venv/bin/python -m compileall -q bot.py tests deploy
echo 'Server preparation complete; bot not started yet.'
