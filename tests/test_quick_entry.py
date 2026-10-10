"""Quick entry regressions; no live Telegram traffic or personal data."""
import hashlib
import json
import tempfile
import time
import unittest
from datetime import date, timezone
from pathlib import Path
from unittest.mock import patch
from bot import Bot, Store, Telegram
from quick_entry import csv_rows, text_rows, MAX_BYTES


class API:
    def __init__(self):
        self.calls = []
    def call(self, method, **payload):
        self.calls.append((method, payload))
        return {}


class QuickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.api = API()
        self.bot = Bot(self.store, self.api, 123, timezone.utc)
        self.bot.today = lambda: date(2026, 10, 6)
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
    def batch(self):
        return self.store.db.execute('SELECT id FROM quick_batches ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    def count(self):
        return self.store.db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]
    def test_single_known_is_saved_and_menu_unchanged(self):
        menu = self.bot.menu()
        self.bot.text('12 coffee', 100)
        self.assertEqual(self.store.recent()[0][2:], ('Dining out', 1200))
        self.assertEqual(self.bot.menu(), menu)
        self.assertIn('Undo', str(self.api.calls[-1]))
    def test_unknown_learned_only_after_category_and_survives_restart(self):
        self.bot.text('8 Corner shop', 100)
        self.assertEqual(self.count(), 0)
        self.bot.action(f'qcat:{self.batch()}:0:1', 101)
        self.assertEqual(self.count(), 1)
        restarted = Bot(self.store, self.api, 123, timezone.utc)
        restarted.text('9 corner SHOP', 102)
        self.assertEqual(self.count(), 2)
    def test_repeat_event_and_undo_are_idempotent(self):
        self.bot.text('12 coffee', 100)
        batch = self.batch()
        self.bot.text('12 coffee', 100)
        self.assertEqual(self.count(), 1)
        self.bot.action(f'qundo:{batch}', 101)
        self.bot.action(f'qundo:{batch}', 102)
        self.bot.text('12 coffee', 100)
        self.assertEqual(self.count(), 0)
    def test_batch_requires_confirmation_and_undo_preserves_other_expenses(self):
        self.store.add(100, 1, date(2026, 10, 6), 55)
        self.bot.text('12 coffee\n8 taxi', 100)
        batch = self.batch()
        self.assertEqual(self.count(), 1)
        self.bot.action(f'qsave:{batch}', 101)
        self.assertEqual(self.count(), 3)
        self.bot.action(f'qsave:{batch}', 102)
        self.assertEqual(self.count(), 3)
        self.bot.action(f'qundo:{batch}', 103)
        self.assertEqual(self.store.recent()[0][3], 100)
        self.assertEqual(self.count(), 1)
    def test_bad_batch_does_not_save_partial_data(self):
        self.bot.text('12 coffee\nbad row', 100)
        self.assertEqual(self.count(), 0)
        self.assertIsNone(self.store.db.execute('SELECT id FROM quick_batches').fetchone())
    def test_explicit_and_relative_dates(self):
        rows = text_rows('12 coffee yesterday\n8 taxi 2026-10-01\n3 \u043a\u043e\u0444\u0435 \u0432\u0447\u0435\u0440\u0430', self.bot.today())
        self.assertEqual([r['day'] for r in rows], ['2026-10-05', '2026-10-01', '2026-10-05'])
        self.assertEqual(self.bot.quick.guess(rows[-1]['name']), 2)
        with self.assertRaises(ValueError):
            text_rows('12 coffee 2027-01-01', self.bot.today())
    def test_change_saved_category_learns_without_new_expense(self):
        self.bot.text('12 coffee', 100)
        self.bot.action(f'qcat:{self.batch()}:0:1', 101)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.recent()[0][2], 'Groceries')
        self.bot.text('5 coffee', 102)
        self.assertEqual(self.store.recent()[0][2], 'Groceries')
    def test_cancelled_draft_cannot_be_saved_or_learned(self):
        self.bot.text('12 Unknown\n5 taxi', 100)
        batch = self.batch()
        self.bot.action(f'qcancel:{batch}', 101)
        self.bot.action(f'qcat:{batch}:0:1', 102)
        self.bot.action(f'qsave:{batch}', 103)
        self.assertEqual(self.count(), 0)
        self.assertIsNone(self.bot.quick.guess('Unknown'))
    def test_csv_filters_income_and_dates_and_uses_settlement_amount(self):
        raw = b'Date,Transaction type,Description,Settlement amount,Settlement currency,Original amount,Original currency\n4 Oct 2026,Card payment,Shop,-3.50,EUR,-10,GEL\n4 Oct 2026,Outgoing transfer,Own,-9,EUR,-9,EUR\n3 Oct 2026,Card payment,Old,-2,EUR,-2,EUR\n'
        rows, skipped = csv_rows(raw, 'EUR', self.bot.today(), '2026-10-04 2026-10-06')
        self.assertEqual((len(rows), skipped, rows[0]['cents']), (1, 2, 350))
        identity = json.dumps(['2026-10-04', 'Shop', 350, '-10', 'GEL'], ensure_ascii=True)
        self.assertEqual(rows[0]['source'], -int(hashlib.sha256(identity.encode()).hexdigest()[:15], 16))
    def generic(self, value='-12', currency='EUR'):
        return f'date,description,amount,currency\n2026-10-04,coffee,{value},{currency}\n'.encode()
    def test_csv_requires_confirmation_and_repeat_import_is_skipped(self):
        rows, _ = csv_rows(self.generic(), 'EUR', self.bot.today())
        self.bot.quick.start(rows, 100, 'csv')
        self.assertEqual(self.count(), 0)
        self.bot.action(f'qsave:{self.batch()}', 101)
        self.assertEqual(self.count(), 1)
        rows, _ = csv_rows(self.generic(), 'EUR', self.bot.today())
        self.bot.quick.start(rows, 102, 'csv')
        self.bot.action(f'qsave:{self.batch()}', 103)
        self.assertEqual(self.count(), 1)
    def test_possible_manual_duplicate_is_reviewed(self):
        self.store.add(1200, 1, date(2026, 10, 4), 55)
        rows, _ = csv_rows(self.generic(), 'EUR', self.bot.today())
        self.bot.quick.start(rows, 100, 'csv')
        batch = self.batch()
        self.assertIn('Possible duplicate', str(self.api.calls[-1]))
        self.bot.action(f'qsave:{batch}', 101)
        self.assertEqual(self.count(), 1)
        self.bot.action(f'qkeep:{batch}:0', 102)
        self.bot.action(f'qsave:{batch}', 103)
        self.assertEqual(self.count(), 2)
    def test_duplicate_appearing_during_review_requires_new_decision(self):
        rows, _ = csv_rows(self.generic(), 'EUR', self.bot.today())
        self.bot.quick.start(rows, 100, 'csv')
        self.store.add(1200, 1, date(2026, 10, 4), 55)
        self.bot.action(f'qsave:{self.batch()}', 101)
        self.assertEqual(self.count(), 1)
        self.assertIn('Possible duplicate', str(self.api.calls[-1]))
    def test_currency_invalid_amount_size_and_schema_rejected(self):
        for raw in [self.generic(currency='USD'), self.generic(value='-1.001'), self.generic(value='NaN'), b'unknown,columns\n1,2', b'x'*(MAX_BYTES+1)]:
            with self.subTest(data=raw[:45]), self.assertRaises(ValueError):
                csv_rows(raw, 'EUR', self.bot.today())
    def test_separate_users_cannot_change_or_undo_each_others_rows(self):
        self.bot.text('12 coffee', 100)
        other_store = Store(Path(self.tmp.name) / 'other.sqlite3')
        try:
            other = Bot(other_store, self.api, 456, timezone.utc)
            other.action(f'qundo:{self.batch()}', 101)
            self.assertEqual(self.count(), 1)
            self.assertIsNone(other.quick.guess('Corner shop'))
        finally:
            other_store.db.close()
    def test_document_handling_is_async_and_only_previews(self):
        self.api.download_csv = lambda file_id: self.generic()
        self.bot.document({'file_id':'test', 'file_name':'test.csv'}, '', 100)
        for _ in range(100):
            if self.bot.file_result is not None:
                break
            time.sleep(.01)
        self.bot.tick()
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.bot.download_busy)
        self.assertIn('Confirm', str(self.api.calls[-1]))
    def test_download_failure_recovers(self):
        self.bot.file_result = (None, '', 1)
        self.bot.download_busy = True
        self.bot.tick()
        self.assertFalse(self.bot.download_busy)
        self.assertIn('download failed', str(self.api.calls[-1]))
    def test_deleted_expense_cannot_be_restored_by_category_edit(self):
        self.bot.text('12 coffee', 100)
        batch = self.batch()
        self.store.delete(self.store.recent()[0][0])
        self.bot.action(f'qcat:{batch}:0:1', 101)
        self.assertEqual(self.count(), 0)
    def test_download_rejects_traversal_before_http_request(self):
        api = Telegram('test')
        with patch.object(api, 'call', return_value={'file_path':'../secret'}), patch('bot.urllib.request.urlopen') as request:
            with self.assertRaises(ValueError):
                api.download_csv('id')
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
