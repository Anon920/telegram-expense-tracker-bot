"""Authenticated Wallet inbox and dated NBG GEL conversion. No Telegram secrets in HTTP."""
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

TBILISI = ZoneInfo('Asia/Tbilisi')


def initialize(connection):
    connection.executescript('''CREATE TABLE IF NOT EXISTS wallet_inbox(
        id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        converted TEXT,error TEXT,notified INTEGER NOT NULL DEFAULT 0,next_retry REAL NOT NULL DEFAULT 0,
        created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS wallet_rates(day TEXT PRIMARY KEY,rate TEXT NOT NULL);''')


def normalize(payload, now=None):
    if not isinstance(payload, dict):
        raise ValueError('Send a JSON object.')
    now = now or datetime.now(timezone.utc)
    event = str(payload.get('id', ''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', event):
        raise ValueError('id must be a UUID generated once for this transaction.')
    merchant = str(payload.get('merchant', '')).strip()
    if not merchant or len(merchant) > 120 or any(ord(v) < 32 for v in merchant):
        raise ValueError('merchant is required, up to 120 characters.')
    raw = str(payload.get('amount', '')).strip().upper().replace('\u00a0', ' ')
    currency = str(payload.get('currency', '')).strip().upper()
    found = re.findall(r'GEL|EUR|\u20be|\u20ac', raw)
    found = [{'\u20be':'GEL','\u20ac':'EUR'}.get(code, code) for code in found]
    if found:
        if len(set(found)) != 1 or (currency and currency != found[0]):
            raise ValueError('Amount currency does not match currency field.')
        currency = found[0]
        raw = re.sub(r'GEL|EUR|\u20be|\u20ac', '', raw).strip()
    if currency not in ('GEL', 'EUR'):
        raise ValueError('Include GEL or EUR with the amount. Currency is never guessed.')
    if not re.fullmatch(r'\d+(?:[.,]\d{1,2})?', raw):
        raise ValueError('Amount must contain a positive number with at most 2 decimals and GEL/EUR; no thousands separator.')
    amount = Decimal(raw.replace(',', '.'))
    if amount <= 0 or amount > 1000000:
        raise ValueError('Amount must be positive and no larger than 1000000.')
    try:
        timestamp = datetime.fromisoformat(str(payload['timestamp']).replace('Z', '+00:00'))
    except (ValueError, KeyError):
        raise ValueError('timestamp must be ISO 8601 with a timezone offset.') from None
    if timestamp.tzinfo is None or not now-timedelta(days=31) <= timestamp <= now+timedelta(minutes=5):
        raise ValueError('timestamp needs a timezone and must be within the last 31 days.')
    return dict(id=event, merchant=merchant, amount=str(amount), currency=currency,
                timestamp=timestamp.isoformat(), day=str(timestamp.astimezone(TBILISI).date()))


def receive(connection, payload):
    data = normalize(payload)
    encoded = json.dumps(data, sort_keys=True)
    connection.execute('BEGIN IMMEDIATE')
    try:
        existing = connection.execute('SELECT payload FROM wallet_inbox WHERE id=?', (data['id'],)).fetchone()
        if existing:
            if existing[0] != encoded:
                raise ValueError('This id already belongs to different transaction data.')
            connection.commit()
            return False
        count = connection.execute('SELECT COUNT(*) FROM wallet_inbox WHERE created>?', (time.time()-60,)).fetchone()[0]
        if count >= 30:
            raise ValueError('Too many transactions; retry later using the same id.')
        connection.execute('INSERT INTO wallet_inbox(id,payload,created) VALUES(?,?,?)', (data['id'], encoded, time.time()))
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise


def fetch_rate(day):
    url = 'https://nbg.gov.ge/gw/api/ct/monetarypolicy/currencies/en/json/?date=' + day
    with urllib.request.urlopen(url, timeout=8) as response:
        data = json.loads(response.read(200000), parse_float=Decimal)
    for group in data:
        if group.get('date', '')[:10] != day:
            continue
        for currency in group.get('currencies', []):
            if currency.get('code') == 'EUR' and currency.get('validFromDate', group['date'])[:10] == day:
                rate = Decimal(str(currency['rate'])) / Decimal(str(currency['quantity']))
                if rate.is_finite() and Decimal('0.01') < rate < Decimal('1000'):
                    return rate
    raise ValueError('No verified EUR rate for this date.')


def convert(connection, data, fetch=fetch_rate):
    rate = Decimal(1)
    if data['currency'] == 'GEL':
        cached = connection.execute('SELECT rate FROM wallet_rates WHERE day=?', (data['day'],)).fetchone()
        rate = Decimal(cached[0]) if cached else fetch(data['day'])
        if not rate.is_finite() or not Decimal('.01') < rate < Decimal('1000'):
            raise ValueError('Invalid exchange rate.')
        with connection:
            connection.execute('INSERT OR IGNORE INTO wallet_rates VALUES(?,?)', (data['day'], str(rate)))
    value = int((Decimal(data['amount']) / rate * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if value <= 0:
        raise ValueError('Converted value is less than one cent.')
    return dict(day=data['day'], cents=value, name=data['merchant'], category=None,
                wallet_original=f"{data['amount']} {data['currency']}", wallet_rate=str(rate),
                source=-int(hashlib.sha256(('wallet:'+data['id']).encode()).hexdigest()[:15],16))


class WalletInbox:
    def __init__(self, database, config, start_worker=True):
        self.path, self.config = str(database), config
        self.stop = threading.Event()
        with closing(sqlite3.connect(self.path)) as connection:
            initialize(connection)
        if start_worker:
            threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        while not self.stop.wait(1):
            try:
                self.convert_one()
            except Exception:
                # No payloads, URLs or credentials in logs.
                print('Wallet conversion worker retrying.', flush=True)

    def convert_one(self, fetch=fetch_rate):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            row = connection.execute("SELECT id,payload FROM wallet_inbox WHERE status='pending' AND next_retry<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return
            try:
                converted = convert(connection, json.loads(row[1]), fetch)
                with connection:
                    connection.execute("UPDATE wallet_inbox SET status='ready',converted=?,error=NULL WHERE id=?", (json.dumps(converted),row[0]))
            except Exception:
                with connection:
                    connection.execute('UPDATE wallet_inbox SET error=?,next_retry=? WHERE id=?', ('Conversion unavailable; waiting for a verified rate. No expense has been saved.', time.time()+300,row[0]))

    def deliver(self, bot):
        connection = bot.db.db
        failed = connection.execute("SELECT id,error FROM wallet_inbox WHERE status='pending' AND error IS NOT NULL AND notified=0 LIMIT 1").fetchone()
        if failed:
            bot.send('Wallet: ' + failed[1])
            with connection:
                connection.execute('UPDATE wallet_inbox SET notified=1 WHERE id=?',(failed[0],))
        row = connection.execute("SELECT id,converted FROM wallet_inbox WHERE status='ready' ORDER BY created LIMIT 1").fetchone()
        if row:
            item = json.loads(row[1])
            bot.quick.start([item], 'wallet:'+row[0], 'wallet')
            with connection:
                connection.execute("UPDATE wallet_inbox SET status='delivered' WHERE id=?", (row[0],))

    def setup(self, bot):
        bot.send('Wallet connection (private; keep this key secret):\nURL: '+self.config['url']+
                 '\nMethod: POST\nHeader Authorization: Bearer '+self.config['key']+
                 '\nJSON fields: id (UUID), amount (text including GEL/EUR), merchant (Merchant), timestamp (ISO 8601). '
                 'Do not send card numbers. First test with JSON {"probe": true}; this creates no expense.')


def make_handler(database, key):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self.reply(200 if self.path=='/health' else 404, {'ok':self.path=='/health'})
        def do_POST(self):
            if self.path != '/wallet':
                self.reply(404, {'error':'Not found'})
                return
            if not hmac.compare_digest(self.headers.get('Authorization','').encode(), ('Bearer '+key).encode()):
                self.reply(401, {'error':'Unauthorized'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Body must be 1-4096 bytes with Content-Length.')
                payload = json.loads(self.rfile.read(size))
                if payload == {'probe':True}:
                    self.reply(200, {'ok':True,'probe':True,'expense_created':False})
                    return
                with closing(sqlite3.connect(database, timeout=5)) as connection:
                    inserted = receive(connection, payload)
                self.reply(202, {'ok':True,'queued':inserted,'duplicate':not inserted})
            except (ValueError, UnicodeError) as error:
                self.reply(400, {'error':str(error)[:240]})
            except Exception:
                self.reply(503, {'error':'Temporarily unavailable. Retry with the same id.'})
    return Handler


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    config = json.loads((root/'data'/'wallet-config.json').read_text())
    assert len(config['key']) >= 32
    database = str(root/'data'/'expenses.sqlite3')
    with closing(sqlite3.connect(database)) as connection:
        initialize(connection)
    HTTPServer(('127.0.0.1', 8787), make_handler(database, config['key'])).serve_forever()
