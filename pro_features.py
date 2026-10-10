"""Persistent Pro budgets, monthly payment reminders, comparisons and AI cache."""
import calendar
from datetime import date, timedelta


def next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def previous_month(day):
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def following_due(due, anchor, today):
    month = next_month(due)
    while True:
        result = month.replace(day=min(anchor, calendar.monthrange(month.year, month.month)[1]))
        if result > today:
            return result
        month = next_month(month)


class ProFeatures:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS budgets (
                category INTEGER PRIMARY KEY REFERENCES categories(id), cents INTEGER NOT NULL CHECK(cents>0));
            CREATE TABLE IF NOT EXISTS recurring (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, cents INTEGER NOT NULL CHECK(cents>0),
                category INTEGER NOT NULL REFERENCES categories(id), due TEXT NOT NULL, anchor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS recurring_runs (
                recurring_id INTEGER NOT NULL, due TEXT NOT NULL, outcome TEXT NOT NULL,
                PRIMARY KEY(recurring_id,due));
            CREATE TABLE IF NOT EXISTS pro_notices (
                key TEXT PRIMARY KEY, text TEXT NOT NULL, action TEXT, delivered INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ai_reports (
                week TEXT PRIMARY KEY, status TEXT NOT NULL, text TEXT, created INTEGER NOT NULL);
        ''')

    def set_budget(self, category, cents):
        if category not in dict(self.store.categories()):
            raise ValueError('Category not found.')
        with self.db:
            if cents == 0:
                self.db.execute('DELETE FROM budgets WHERE category=?', (category,))
            elif cents > 0:
                self.db.execute('INSERT OR REPLACE INTO budgets VALUES(?,?)', (category, cents))
            else:
                raise ValueError('Budget must be positive.')
            self.db.execute('UPDATE pro_notices SET delivered=1 WHERE key LIKE ?',
                            (f'budget:%:{category}:%',))

    def budgets(self, today):
        return self.db.execute('''SELECT b.category,c.name,b.cents,COALESCE(SUM(e.cents),0)
            FROM budgets b JOIN categories c ON c.id=b.category
            LEFT JOIN expenses e ON e.category=b.category AND e.day>=? AND e.day<?
            GROUP BY b.category ORDER BY c.id''',
            (today.replace(day=1).isoformat(), next_month(today).isoformat())).fetchall()

    def budget_alerts(self, today, format_money):
        for category, name, limit, spent in self.budgets(today):
            for threshold in (80, 100):
                if spent * 100 >= limit * threshold:
                    key = f'budget:{today:%Y-%m}:{category}:{limit}:{threshold}'
                    text = (f'Budget alert: {name} reached {threshold}% of its monthly limit. '
                            f'Spent {format_money(spent)} of {format_money(limit)}.')
                    # Crossing 100% in one expense should not send both threshold messages.
                    delivered = int(threshold == 80 and spent >= limit)
                    with self.db:
                        self.db.execute('INSERT OR IGNORE INTO pro_notices VALUES(?,?,?,?)',
                                        (key, text, 'budgets', delivered))

    def add_recurring(self, name, cents, category, due):
        name = ' '.join(name.split())
        if not name or len(name) > 40:
            raise ValueError('Use a name with 1 to 40 characters.')
        if category not in dict(self.store.categories()) or cents <= 0:
            raise ValueError('Invalid amount or category.')
        if len(self.recurring()) >= 60:
            raise ValueError('Up to 60 recurring payments are supported.')
        with self.db:
            result = self.db.execute('INSERT INTO recurring(name,cents,category,due,anchor) VALUES(?,?,?,?,?)',
                                    (name, cents, category, due.isoformat(), due.day))
        return result.lastrowid

    def recurring(self):
        return self.db.execute('''SELECT r.id,r.name,r.cents,c.name,r.due,r.anchor,r.category
            FROM recurring r JOIN categories c ON c.id=r.category ORDER BY r.due,r.id''').fetchall()

    def settle(self, recurring_id, due, today, source, paid=True):
        with self.db:
            row = self.db.execute('SELECT cents,category,due,anchor FROM recurring WHERE id=?',
                                  (recurring_id,)).fetchone()
            if not row or row[2] != due or self.db.execute(
                    'SELECT 1 FROM recurring_runs WHERE recurring_id=? AND due=?', (recurring_id, due)).fetchone():
                return False
            if date.fromisoformat(due) > today:
                raise ValueError('This payment is not due yet.')
            if paid:
                self.db.execute('INSERT INTO expenses(cents,category,day,source) VALUES(?,?,?,?)',
                                (row[0], row[1], today.isoformat(), source))
            self.db.execute('INSERT INTO recurring_runs VALUES(?,?,?)',
                            (recurring_id, due, 'paid' if paid else 'skipped'))
            upcoming = following_due(date.fromisoformat(due), row[3], today)
            self.db.execute('UPDATE recurring SET due=? WHERE id=?', (upcoming.isoformat(), recurring_id))
            self.db.execute('UPDATE pro_notices SET delivered=1 WHERE key=?', (f'recurring:{recurring_id}:{due}',))
        return True

    def delete_recurring(self, recurring_id):
        with self.db:
            self.db.execute('DELETE FROM recurring WHERE id=?', (recurring_id,))
            self.db.execute('UPDATE pro_notices SET delivered=1 WHERE action=?', (f'recshow:{recurring_id}',))

    def recurring_alerts(self, today, format_money):
        for rid, name, cents, _, due, _, _ in self.recurring():
            if date.fromisoformat(due) <= today:
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO pro_notices VALUES(?,?,?,0)',
                        (f'recurring:{rid}:{due}', f'Payment due: {name}, {format_money(cents)}. Due date: {due}. '
                         'Nothing is charged or recorded automatically.', f'recshow:{rid}'))

    def pending_notice(self):
        return self.db.execute('SELECT key,text,action FROM pro_notices WHERE delivered=0 ORDER BY rowid LIMIT 1').fetchone()

    def mark_notice(self, key):
        with self.db:
            self.db.execute('UPDATE pro_notices SET delivered=1 WHERE key=?', (key,))

    def report(self, week):
        return self.db.execute('SELECT status,text,created FROM ai_reports WHERE week=?', (str(week),)).fetchone()

    def reserve_report(self, week, now):
        with self.db:
            result = self.db.execute('INSERT OR IGNORE INTO ai_reports VALUES(?,?,NULL,?)',
                                    (str(week), 'running', int(now)))
        return result.rowcount == 1

    def finish_report(self, week, text, success):
        with self.db:
            self.db.execute('UPDATE ai_reports SET status=?,text=? WHERE week=?',
                            ('done' if success else 'failed', text, str(week)))

    def compare(self, month, format_money):
        month = month.replace(day=1)
        before = previous_month(month)
        current = self.store.summary(month, next_month(month))
        previous = self.store.summary(before, month)
        delta = current['total_cents'] - previous['total_cents']
        def signed(value):
            return ('+' if value >= 0 else '-') + format_money(abs(value))
        if previous['total_cents']:
            change = f"{delta / previous['total_cents'] * 100:+.1f}%"
        else:
            change = 'no previous spending to compare'
        lines = [f'Month comparison: {month:%Y-%m} vs {before:%Y-%m}',
                 f"{month:%Y-%m}: {format_money(current['total_cents'])}",
                 f"{before:%Y-%m}: {format_money(previous['total_cents'])}",
                 f'Difference: {signed(delta)} ({change})', 'By category:']
        a = {row['name']: row['cents'] for row in current['categories']}
        b = {row['name']: row['cents'] for row in previous['categories']}
        for name in sorted(a.keys() | b.keys()):
            lines.append(f'{name}: {format_money(a.get(name, 0))} vs {format_money(b.get(name, 0))}; '
                         f'{signed(a.get(name, 0) - b.get(name, 0))}')
        if not a and not b:
            lines.append('No expenses in either month.')
        return '\n'.join(lines)
