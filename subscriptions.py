"""Telegram Stars orders and payment ledger. No entitlement before successful_payment."""
import secrets
import time

PRICE = 1
# A non-expiring test entitlement. NULL avoids inventing an expiry date.


class Subscriptions:
    def __init__(self, connection):
        self.db = connection
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS star_orders (
                payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                created INTEGER NOT NULL, checkout_id TEXT, checkout_until INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS star_payments (
                charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, payload TEXT NOT NULL,
                paid_at INTEGER NOT NULL,
                refunded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS subscription_menus (
                user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY(user_id, message_id));
        ''')

    def active(self, user_id, now=None):
        if self.db.execute('SELECT 1 FROM settings WHERE key=? AND value=?',
                           (f'pro_paused:{user_id}', '1')).fetchone():
            return False
        return self.db.execute('SELECT 1 FROM star_payments WHERE user_id=? AND refunded=0 LIMIT 1',
                               (user_id,)).fetchone() is not None

    def new_order(self, user_id, now=None):
        now = int(time.time() if now is None else now)
        if self.db.execute('SELECT 1 FROM settings WHERE key=? AND value=?',
                           (f'pro_paused:{user_id}', '1')).fetchone():
            raise ValueError('Pro is temporarily paused for testing. Contact /paysupport to restore access without paying again.')
        if self.active(user_id, now):
            raise ValueError('Your subscription is already active.')
        # Reuse a recent invoice payload to prevent multiple overlapping checkouts.
        row = self.db.execute('''SELECT payload FROM star_orders WHERE user_id=? AND created>?
            AND payload NOT IN (SELECT payload FROM star_payments) ORDER BY created DESC LIMIT 1''',
                              (user_id, now - 3600)).fetchone()
        if row:
            return row[0]
        payload = 'lifetime:' + secrets.token_urlsafe(24)
        with self.db:
            self.db.execute('INSERT INTO star_orders(payload,user_id,created) VALUES(?,?,?)',
                            (payload, user_id, now))
        return payload

    def valid(self, user_id, payment):
        row = self.db.execute('SELECT user_id FROM star_orders WHERE payload=?',
                              (payment.get('invoice_payload', ''),)).fetchone()
        return bool(row and row[0] == user_id and payment.get('currency') == 'XTR'
                    and payment.get('total_amount') == PRICE)

    def checkout(self, query, now=None):
        now = int(time.time() if now is None else now)
        user_id = query.get('from', {}).get('id')
        if not self.valid(user_id, query):
            return 'Invalid payment details. Please open a new invoice from the bot.'
        if self.active(user_id, now):
            return 'Your subscription is already active.'
        payload = query['invoice_payload']
        row = self.db.execute('SELECT created,checkout_id,checkout_until FROM star_orders WHERE payload=?',
                              (payload,)).fetchone()
        if now - row[0] > 3600:
            return 'This invoice has expired. Please open a new one.'
        paid = self.db.execute('SELECT 1 FROM star_payments WHERE payload=?', (payload,)).fetchone()
        if paid:
            return 'This invoice has already been paid.'
        if row[2] > now and row[1] != query['id']:
            return 'Another checkout is in progress. Please wait a minute.'
        with self.db:
            self.db.execute('UPDATE star_orders SET checkout_id=?,checkout_until=? WHERE payload=?',
                            (query['id'], now + 60, payload))
        return None

    def complete(self, user_id, payment, paid_at):
        if not self.valid(user_id, payment) or not payment.get('telegram_payment_charge_id'):
            raise ValueError('Unrecognized payment. Please use /paysupport.')
        charge = payment['telegram_payment_charge_id']
        with self.db:
            if self.db.execute('SELECT 1 FROM star_payments WHERE charge_id=?', (charge,)).fetchone():
                return False
            self.db.execute('INSERT INTO star_payments VALUES(?,?,?,?,0)',
                            (charge, user_id, payment['invoice_payload'], int(paid_at)))
        return True

    def refund_record(self, user_id, charge):
        with self.db:
            self.db.execute('UPDATE star_payments SET refunded=1 WHERE user_id=? AND charge_id=?',
                            (user_id, charge))

    def latest_charge(self, user_id):
        row = self.db.execute('''SELECT charge_id FROM star_payments WHERE user_id=? AND refunded=0
            ORDER BY paid_at DESC LIMIT 1''', (user_id,)).fetchone()
        return row[0] if row else None

    def remember_menu(self, user_id, message_id):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO subscription_menus VALUES(?,?)', (user_id, message_id))

    def menus(self, user_id):
        return [row[0] for row in self.db.execute(
            'SELECT message_id FROM subscription_menus WHERE user_id=?', (user_id,))]

    def forget_menu(self, user_id, message_id):
        with self.db:
            self.db.execute('DELETE FROM subscription_menus WHERE user_id=? AND message_id=?', (user_id, message_id))
