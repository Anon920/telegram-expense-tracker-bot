"""Wallet bridge tests use synthetic transactions and mock exchange rates."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
from bot import Bot, Store
from wallet_bridge import WalletInbox, initialize, normalize, receive, convert, fetch_rate, make_handler


class API:
    def __init__(self): self.calls=[]
    def call(self, method, **payload):
        self.calls.append((method,payload))
        return {}


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.sqlite3'
        self.store=Store(self.path)
        initialize(self.store.db)
        self.api=API()
        self.bot=Bot(self.store,self.api,123,timezone.utc)
        self.inbox=WalletInbox(self.path,{'key':'test-key-only-01234567890123456789','url':'https://example.invalid/wallet'},False)
        self.payload=dict(id='test-event-00000001',merchant='coffee',amount='27,80 GEL',timestamp=datetime.now(timezone.utc).isoformat())
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
    def test_requires_currency_and_valid_timestamp(self):
        for change in [dict(amount='27.80'),dict(amount='27 GEL',currency='EUR'),dict(amount='-2 GEL'),dict(amount='NaN GEL'),dict(amount='1.222 GEL'),dict(timestamp='2026-10-06'),dict(id='short')]:
            with self.subTest(change=change),self.assertRaises(ValueError): normalize(dict(self.payload,**change))
    def test_amount_formats(self):
        self.assertEqual(normalize(self.payload)['amount'],'27.80')
        self.assertEqual(normalize(dict(self.payload,amount='27.80',currency='GEL'))['currency'],'GEL')
        self.assertEqual(normalize(dict(self.payload,amount='\u20ac 27,80'))['currency'],'EUR')
    def test_timezone_conversion(self):
        p=dict(self.payload,timestamp='2026-10-05T22:30:00+00:00')
        self.assertEqual(normalize(p,datetime(2026,10,6,tzinfo=timezone.utc))['day'],'2026-10-06')
    def test_idempotency_and_conflict(self):
        self.assertTrue(receive(self.store.db,self.payload))
        self.assertFalse(receive(self.store.db,self.payload))
        with self.assertRaises(ValueError): receive(self.store.db,dict(self.payload,amount='28 GEL'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM wallet_inbox').fetchone()[0],1)
    def test_conversion_rounding_and_cache(self):
        data=normalize(self.payload)
        calls=[]
        def rate(day): calls.append(day); return Decimal('3')
        result=convert(self.store.db,data,rate)
        self.assertEqual(result['cents'],927)
        self.assertEqual(result['wallet_original'],'27.80 GEL')
        convert(self.store.db,data,rate)
        self.assertEqual(len(calls),1)
    def test_eur_does_not_fetch_rate(self):
        result=convert(self.store.db,normalize(dict(self.payload,amount='27.80 EUR')),lambda _:self.fail('No rate needed'))
        self.assertEqual(result['cents'],2780)
    def test_failed_rate_stays_pending_without_expense(self):
        receive(self.store.db,self.payload)
        def unavailable(day): raise TimeoutError()
        self.inbox.convert_one(unavailable)
        self.inbox.deliver(self.bot)
        self.inbox.deliver(self.bot)
        self.assertEqual(self.store.recent(),[])
        self.assertEqual(len(self.api.calls),1)
        self.assertEqual(self.store.db.execute('SELECT status FROM wallet_inbox').fetchone()[0],'pending')
    def test_queued_event_saved_once_and_undo(self):
        receive(self.store.db,self.payload)
        self.inbox.convert_one(lambda _:Decimal('3'))
        self.inbox.deliver(self.bot)
        self.inbox.deliver(self.bot)
        self.assertEqual(len(self.store.recent()),1)
        self.assertIn('approximately 9.27 EUR',str(self.api.calls[-1]))
        batch=self.store.db.execute('SELECT id FROM quick_batches').fetchone()[0]
        self.bot.action('qundo:'+batch,1)
        self.assertEqual(self.store.recent(),[])
        receive(self.store.db,self.payload)
        self.inbox.convert_one(lambda _:Decimal('3'))
        self.inbox.deliver(self.bot)
        self.assertEqual(self.store.recent(),[])
    def test_restart_after_save_before_delivery_mark_does_not_duplicate(self):
        receive(self.store.db,self.payload)
        self.inbox.convert_one(lambda _:Decimal('3'))
        self.inbox.deliver(self.bot)
        self.store.db.execute("UPDATE wallet_inbox SET status='ready'")
        self.store.db.commit()
        WalletInbox(self.path,{},False).deliver(self.bot)
        self.assertEqual(len(self.store.recent()),1)
    def test_unknown_merchant_waits_for_category(self):
        receive(self.store.db,dict(self.payload,merchant='Unknown test shop'))
        self.inbox.convert_one(lambda _:Decimal('3'))
        self.inbox.deliver(self.bot)
        self.assertEqual(self.store.recent(),[])
        batch=self.store.db.execute('SELECT id FROM quick_batches').fetchone()[0]
        self.bot.action(f'qcat:{batch}:0:7',1)
        self.assertEqual(self.store.recent()[0][2],'Shopping')
    def test_other_accounts_do_not_receive_setup_key(self):
        self.bot.text('/wallet_setup')
        self.assertNotIn('test-key-only',str(self.api.calls))
    def test_http_auth_probe_validation_and_idempotency(self):
        server=HTTPServer(('127.0.0.1',0),make_handler(str(self.path),'test-secret'))
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        url=f'http://127.0.0.1:{server.server_port}/wallet'
        def send(payload,secret='test-secret'):
            request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
            return urllib.request.urlopen(request,timeout=3)
        try:
            with self.assertRaises(urllib.error.HTTPError) as error: send(self.payload,'wrong')
            self.assertEqual(error.exception.code,401)
            error.exception.close()
            with send({'probe':True}) as reply:
                self.assertFalse(json.load(reply)['expense_created'])
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM wallet_inbox').fetchone()[0],0)
            with send(self.payload) as reply: self.assertTrue(json.load(reply)['queued'])
            with send(self.payload) as reply: self.assertTrue(json.load(reply)['duplicate'])
        finally:
            server.shutdown();server.server_close();worker.join()
    def test_nbg_rejects_wrong_day_and_scales_quantity(self):
        import io
        payload=[{'date':'2026-10-06T00:00:00Z','currencies':[{'code':'EUR','rate':30,'quantity':10,'validFromDate':'2026-10-06T00:00:00Z'}]}]
        with patch('wallet_bridge.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(fetch_rate('2026-10-06'),Decimal('3'))
        with patch('wallet_bridge.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaises(ValueError):fetch_rate('2026-10-05')


if __name__=='__main__': unittest.main()
