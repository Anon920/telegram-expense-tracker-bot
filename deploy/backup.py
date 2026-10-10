"""Create a consistent SQLite backup and retain the latest seven daily copies."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

data = Path('/opt/expenses-bot/data')
sources = [data / 'expenses.sqlite3', *data.joinpath('users').glob('*.sqlite3')]
for database in sources:
    folder = Path('/var/backups/expenses-bot') / database.relative_to(data).with_suffix('')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = folder / (datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.sqlite3')
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    destination.chmod(0o600)
    for old in sorted(folder.glob('*.sqlite3'))[:-7]:
        old.unlink()
