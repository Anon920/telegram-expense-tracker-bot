"""Quick expense entry and reviewed CSV imports, isolated in each user's database."""
import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

MAX_BYTES = 2 * 1024 * 1024


def fingerprint(value):
    return -int(hashlib.sha256(value.encode()).hexdigest()[:15], 16)


def label(value):
    return ' '.join(value.casefold().split())


def cents(value):
    try:
        number = Decimal(value.strip().replace(',', '.'))
        if not number.is_finite() or number <= 0 or number > 1000000 or number * 100 != int(number * 100):
            raise ValueError()
        return int(number * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Use a positive amount with at most two decimal places.') from None


def text_rows(text, today):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 30:
        raise ValueError('Send 1 to 30 expenses, one per line.')
    rows = []
    for index, line in enumerate(lines, 1):
        match = re.fullmatch(r'(\d+(?:[.,]\d{1,2})?)\s+(?:EUR\s+|\u20ac\s*)?(.+)', line, re.I)
        if not match:
            raise ValueError(f'Line {index}: use amount and description, for example 12 coffee.')
        value, name = match.groups()
        day = today
        parts = name.rsplit(' ', 1)
        if len(parts) == 2:
            suffix = parts[-1].casefold()
            if suffix in ('yesterday', '\u0432\u0447\u0435\u0440\u0430', '\u0432\u0447\u043e\u0440\u0430'):
                name, day = parts[0], today - timedelta(days=1)
            elif suffix in ('today', '\u0441\u0435\u0433\u043e\u0434\u043d\u044f', '\u0441\u044c\u043e\u0433\u043e\u0434\u043d\u0456'):
                name = parts[0]
            elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', suffix):
                name, day = parts[0], date.fromisoformat(suffix)
        if day > today or not name.strip() or len(name) > 120:
            raise ValueError(f'Line {index}: use a description up to 120 characters and no future date.')
        rows.append(dict(day=str(day), cents=cents(value), name=name.strip(), category=None))
    return rows


def csv_rows(content, currency, today, caption=''):
    if len(content) > MAX_BYTES:
        raise ValueError('CSV files must be at most 2 MB.')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Save the CSV as UTF-8 and try again.') from None
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith('Date,Transaction type,')), None)
    zen = start is not None
    if zen:
        reader = csv.DictReader(lines[start:])
    else:
        sample = '\n'.join(lines[:5])
        delimiter = ';' if sample.count(';') > sample.count(',') else ','
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        reader.fieldnames = [v.strip().lower() for v in (reader.fieldnames or [])]
        if not {'date', 'description', 'amount', 'currency'} <= set(reader.fieldnames):
            raise ValueError('Use a ZEN account statement or CSV columns: date,description,amount,currency. Negative amounts are expenses; positive amounts are income.')
    bounds = caption.strip().split()
    if bounds:
        if len(bounds) != 2:
            raise ValueError('Optional caption: YYYY-MM-DD YYYY-MM-DD (inclusive date range).')
        lower, upper = map(date.fromisoformat, bounds)
        if lower > upper:
            raise ValueError('The start date must precede the end date.')
    else:
        lower, upper = date.min, today
    result, skipped, seen = [], 0, Counter()
    for raw in reader:
        if zen and raw.get('Transaction type') is None:
            continue
        try:
            day = datetime.strptime(raw['Date'], '%d %b %Y').date() if zen else date.fromisoformat(raw['date'].strip())
            if not lower <= day <= upper:
                skipped += 1
                continue
            if day > today:
                raise ValueError('Future dates are not supported.')
            if zen and raw['Transaction type'] != 'Card payment':
                skipped += 1
                continue
            number = Decimal(raw['Settlement amount'] if zen else raw['amount'])
            if not number.is_finite():
                raise ValueError('Invalid amount.')
            if number >= 0:
                skipped += 1
                continue
            code = raw['Settlement currency'] if zen else raw['currency']
            if code.strip().upper() != currency:
                raise ValueError(f'Only {currency} settlement amounts are supported; no automatic conversion.')
            name = raw['Description'].split(' CARD:')[0] if zen else raw['description'].strip()
            if not name or len(name) > 200:
                raise ValueError('Invalid description.')
            value = cents(str(-number))
            # Preserve identifiers used by the earlier reviewed one-time ZEN import.
            identity = json.dumps([str(day), name, value, raw['Original amount'], raw['Original currency']], ensure_ascii=True) if zen else json.dumps([str(day), name, value, currency])
            occurrence = seen[identity]
            seen[identity] += 1
            source = fingerprint(identity if not occurrence else identity + f':occurrence:{occurrence}')
            result.append(dict(day=str(day), cents=value, name=name, category=None, source=source))
            if len(result) > 200:
                raise ValueError('Import at most 200 expenses at a time; use a date range in the caption.')
        except (KeyError, InvalidOperation, TypeError):
            raise ValueError('Invalid CSV row. Check dates, descriptions and amounts.') from None
    if not result:
        raise ValueError('No outgoing purchases in the selected range. Transfers and income are excluded.')
    return result, skipped


WORDS = {
    'Groceries': ['groceries', 'nikora', 'hypermarket', '\u043f\u0440\u043e\u0434\u0443\u043a\u0442\u044b', '\u043f\u0440\u043e\u0434\u0443\u043a\u0442\u0438'],
    'Dining out': ['coffee', 'cafe', 'restaurant', 'bolt food', '\u043a\u043e\u0444\u0435', '\u043a\u0430\u0432\u0430', '\u043a\u0430\u0444\u0435'],
    'Transport': ['taxi', 'ridenow', '\u0442\u0430\u043a\u0441\u0438', '\u0442\u0430\u043a\u0441\u0456'],
    'Utilities': ['kyivstar', 'lifecell', 'magticom'],
}


class QuickEntry:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS quick_aliases(name TEXT PRIMARY KEY,category INTEGER NOT NULL REFERENCES categories(id));
            CREATE TABLE IF NOT EXISTS quick_batches(id TEXT PRIMARY KEY,rows TEXT NOT NULL,status TEXT NOT NULL,kind TEXT NOT NULL);
        ''')

    def guess(self, name):
        key = label(name)
        saved = self.db.execute('SELECT category FROM quick_aliases WHERE name=?', (key,)).fetchone()
        if saved:
            return saved[0]
        names = {label(name): cid for cid, name in self.bot.db.categories()}
        if key in names:
            return names[key]
        for category, words in WORDS.items():
            if any(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', key) for word in words):
                return names.get(label(category))
        if key.startswith('bolt.eu/d/'):
            return names.get('dining out')
        return None

    def load(self, batch):
        found = self.db.execute('SELECT rows,status,kind FROM quick_batches WHERE id=?', (batch,)).fetchone()
        if not found:
            raise ValueError('This entry is no longer available.')
        return json.loads(found[0]), found[1], found[2]

    def save(self, batch, rows, status, kind):
        self.db.execute('INSERT OR REPLACE INTO quick_batches VALUES(?,?,?,?)', (batch, json.dumps(rows), status, kind))

    def send(self, text, rows):
        self.bot.send(text, {'inline_keyboard': [[{'text': title, 'callback_data': action} for title, action in row] for row in rows]})

    def start(self, rows, event, kind='text', skipped=0):
        batch = hashlib.sha256(f'{kind}:{event}'.encode()).hexdigest()[:12]
        if self.db.execute('SELECT 1 FROM quick_batches WHERE id=?', (batch,)).fetchone():
            self.show(batch)
            return
        for index, row in enumerate(rows):
            row['category'] = self.guess(row['name'])
            row['source'] = row.get('source', fingerprint(f'quick:{event}:{index}'))
            row['skip'] = bool(self.db.execute('SELECT 1 FROM expenses WHERE source=?', (row['source'],)).fetchone())
            row['duplicate'] = not row['skip'] and kind in ('csv', 'wallet') and bool(self.db.execute('SELECT 1 FROM expenses WHERE day=? AND cents=?', (row['day'], row['cents'])).fetchone())
        with self.db:
            self.save(batch, rows, 'draft', kind)
        self.bot.state = None
        if kind in ('text', 'wallet') and len(rows) == 1 and rows[0]['category'] and not rows[0]['duplicate']:
            self.commit(batch)
        else:
            if kind == 'csv':
                self.bot.send(f'CSV review: {len(rows)} purchases; {skipped} other/out-of-range rows excluded. Nothing saved yet.', {'inline_keyboard': []})
            self.show(batch)

    def show(self, batch):
        rows, status, kind = self.load(batch)
        if status == 'undone' or status == 'cancelled':
            self.send('This entry was cancelled.', [[('Menu', 'menu')]])
            return
        if status == 'saved':
            self.send('Saved. You can change a category or undo this entry.', [[('Change category', f'qedit:{batch}')], [('Undo', f'qundo:{batch}'), ('Menu', 'menu')]])
            return
        for i, row in enumerate(rows):
            if row.get('skip'):
                continue
            if row.get('duplicate'):
                self.send(f"Possible duplicate: {row['day']} {row['name']} {row['cents']/100:.2f} {self.bot.currency}. An expense with this date and amount already exists.", [[('Skip', f'qskip:{batch}:{i}'), ('Keep both', f'qkeep:{batch}:{i}')], [('Cancel', f'qcancel:{batch}')]])
                return
            if row['category'] is None:
                self.categories(batch, i)
                return
        names = dict(self.bot.db.categories())
        selected = [(i, r) for i, r in enumerate(rows) if not r.get('skip')]
        total = sum(r['cents'] for _, r in selected)
        text = '\n'.join(f"{i+1}. {r['day']} {r['name']} - {r['cents']/100:.2f} {self.bot.currency} - {names[r['category']]}" for i, r in selected)
        self.send(f"Review before saving ({len(rows)-len(selected)} skipped):\n{text}\nTotal: {total/100:.2f} {self.bot.currency}", [[('Confirm', f'qsave:{batch}'), ('Change category', f'qedit:{batch}')], [('Cancel', f'qcancel:{batch}')]])

    def categories(self, batch, index):
        rows, status, _ = self.load(batch)
        row = rows[index]
        buttons = [(name, f'qcat:{batch}:{index}:{cid}') for cid, name in self.bot.db.categories()]
        navigation = [('Back', f'qview:{batch}')]
        if status == 'draft':
            navigation.append(('Cancel', f'qcancel:{batch}'))
        self.send(f"Category for {row['name']} ({row['cents']/100:.2f} {self.bot.currency}):", [buttons[i:i+2] for i in range(0, len(buttons), 2)] + [navigation])

    def commit(self, batch):
        rows, status, kind = self.load(batch)
        if status != 'draft':
            self.show(batch)
            return
        if any((r['category'] is None or r.get('duplicate')) and not r.get('skip') for r in rows):
            self.show(batch)
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for row in rows:
                if row.get('skip'):
                    continue
                if self.db.execute('SELECT 1 FROM expenses WHERE source=?', (row['source'],)).fetchone():
                    row['skip'] = True
                    continue
                # Recheck candidates because another entry may have been saved during review.
                if kind in ('csv', 'wallet') and not row.get('keep') and self.db.execute('SELECT 1 FROM expenses WHERE day=? AND cents=?', (row['day'], row['cents'])).fetchone():
                    row['duplicate'] = True
                    self.db.rollback()
                    with self.db:
                        self.save(batch, rows, 'draft', kind)
                    self.show(batch)
                    return
                cursor = self.db.execute('INSERT INTO expenses(day,cents,category,source) VALUES(?,?,?,?)', (row['day'], row['cents'], row['category'], row['source']))
                row['expense'] = cursor.lastrowid
                if row.get('learn'):
                    self.db.execute('INSERT OR REPLACE INTO quick_aliases VALUES(?,?)', (label(row['name']), row['category']))
            self.save(batch, rows, 'saved', kind)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        saved = [r for r in rows if r.get('expense') and not r.get('skip')]
        text = f"Saved: {len(saved)} expenses, {sum(r['cents'] for r in saved)/100:.2f} {self.bot.currency}."
        if saved and saved[0].get('wallet_original'):
            item = saved[0]
            text += f"\n{item['name']}: {item['wallet_original']} -> approximately {item['cents']/100:.2f} EUR."
            if item['wallet_original'].endswith('GEL'):
                text += f"\nNBG rate for {item['day']}: 1 EUR = {item['wallet_rate']} GEL. Bank settlement may differ."
        if self.bot.subscriptions.active(self.bot.owner):
            for cid, name, limit, spent in self.bot.pro.budgets(self.bot.today()):
                if any(r['category'] == cid and r['day'][:7] == str(self.bot.today())[:7] for r in saved):
                    text += f"\n{name}: " + (f'{(limit-spent)/100:.2f} {self.bot.currency} remaining this month.' if spent <= limit else f'{(spent-limit)/100:.2f} {self.bot.currency} over budget.')
            self.bot.next_notice_check = 0
        self.send(text, [[('Change category', f'qedit:{batch}'), ('Undo', f'qundo:{batch}')], [('Menu', 'menu')]])

    def action(self, action):
        if not action.startswith(('qview:', 'qedit:', 'qrow:', 'qcat:', 'qsave:', 'qcancel:', 'qundo:', 'qskip:', 'qkeep:')):
            return False
        try:
            parts = action.split(':')
            verb, batch = parts[:2]
            rows, status, kind = self.load(batch)
            if verb == 'qview':
                self.show(batch)
            elif verb == 'qsave':
                self.commit(batch)
            elif verb in ('qcancel', 'qundo'):
                if (verb == 'qcancel' and status == 'draft') or (verb == 'qundo' and status == 'saved'):
                    with self.db:
                        if status == 'saved':
                            for row in rows:
                                if row.get('expense') and not row.get('skip'):
                                    self.db.execute('DELETE FROM expenses WHERE id=? AND source=?', (row['expense'], row['source']))
                        self.save(batch, rows, 'undone' if status == 'saved' else 'cancelled', kind)
                self.show(batch)
            elif status not in ('draft', 'saved'):
                self.show(batch)
            elif verb == 'qedit':
                choices = [(i, r) for i, r in enumerate(rows) if not r.get('skip')]
                if len(choices) == 1:
                    self.categories(batch, choices[0][0])
                else:
                    page = max(0, int(parts[2])) if len(parts) > 2 else 0
                    buttons = [[(f"{i+1}. {r['name'][:35]} {r['cents']/100:.2f}", f'qrow:{batch}:{i}')] for i, r in choices[page*20:(page+1)*20]]
                    navigation = []
                    if page:
                        navigation.append(('Previous', f'qedit:{batch}:{page-1}'))
                    if (page+1)*20 < len(choices):
                        navigation.append(('Next', f'qedit:{batch}:{page+1}'))
                    self.send('Choose an expense:', buttons + ([navigation] if navigation else []) + [[('Back', f'qview:{batch}')]])
            else:
                index = int(parts[2])
                if index < 0 or index >= len(rows) or rows[index].get('skip'):
                    raise ValueError('This expense is no longer editable.')
                row = rows[index]
                if verb == 'qrow':
                    self.categories(batch, index)
                elif verb in ('qskip', 'qkeep') and status == 'draft':
                    row['duplicate'] = False
                    row['skip'] = verb == 'qskip'
                    row['keep'] = verb == 'qkeep'
                    with self.db:
                        self.save(batch, rows, status, kind)
                    self.show(batch)
                elif verb == 'qcat':
                    cid = int(parts[3])
                    if cid not in dict(self.bot.db.categories()):
                        raise ValueError('Category no longer exists.')
                    row['category'], row['learn'] = cid, True
                    with self.db:
                        if status == 'saved':
                            changed = self.db.execute('UPDATE expenses SET category=? WHERE id=? AND source=?', (cid, row.get('expense'), row['source'])).rowcount
                            if not changed:
                                raise ValueError('This expense has been deleted.')
                            self.db.execute('INSERT OR REPLACE INTO quick_aliases VALUES(?,?)', (label(row['name']), cid))
                        for other in rows:
                            if status == 'draft' and label(other['name']) == label(row['name']):
                                other['category'], other['learn'] = cid, True
                        self.save(batch, rows, status, kind)
                    if status == 'draft' and kind in ('text', 'wallet') and len(rows) == 1:
                        self.commit(batch)
                    else:
                        self.show(batch)
        except (ValueError, IndexError) as error:
            self.bot.send(str(error))
        return True
