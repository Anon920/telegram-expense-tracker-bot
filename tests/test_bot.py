import tempfile
import io
import time
import threading
import unittest
from urllib.error import HTTPError
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot import Bot, CATEGORIES, LEGACY_CATEGORIES, Store, Telegram, amount, codex_analysis, configured_token, report, week_start


class TelegramTransportTests(unittest.TestCase):
    def test_short_poll_uses_short_network_timeout(self):
        with patch('bot.urllib.request.urlopen', return_value=io.BytesIO(b'{"ok":true,"result":[]}')) as request:
            self.assertEqual(Telegram('test-token').call('getUpdates', timeout=2), [])
        self.assertEqual(request.call_args.kwargs['timeout'], 5)


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, **data):
        if method == 'answerCallbackQuery':
            return []
        self.calls.append((method, data))
        return []


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.api = FakeTelegram()
        self.bot = Bot(self.store, self.api, 123, timezone.utc, analyzer=lambda *args: 'Test analysis')
        self.bot.today = lambda: date(2026, 9, 14)
        payload = self.bot.subscriptions.new_order(123)
        self.bot.subscriptions.complete(123, {'invoice_payload': payload, 'currency': 'XTR',
            'total_amount': 1, 'telegram_payment_charge_id': 'test-pro'}, int(time.time()))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def message(self, text, user=123, kind='private'):
        self.bot.handle({'update_id': 1, 'message': {'from': {'id': user}, 'chat': {'id': user, 'type': kind}, 'text': text}})

    def click(self, action, source=2):
        self.bot.handle({'update_id': source, 'callback_query': {'id': str(source), 'from': {'id': 123},
                         'message': {'chat': {'id': 123, 'type': 'private'}}, 'data': action}})

    def test_amount_exact_cents(self):
        self.assertEqual(amount(' 123,45 '), 12345)
        self.assertEqual(amount('0.01'), 1)
        self.assertEqual(amount('100'), 10000)

    def unclaimed(self):
        self.store.db.execute("DELETE FROM settings WHERE key='owner_id'")
        self.store.db.commit()
        self.bot = Bot(self.store, self.api, None, timezone.utc)

    def test_first_start_claims_owner(self):
        self.unclaimed()
        self.message('/start', user=456)
        self.assertEqual(self.bot.owner, 456)
        self.assertEqual(self.store.get('owner_id'), '456')
        self.assertEqual(self.api.calls[-1][1]['chat_id'], 456)

    def test_ownership_cannot_be_reclaimed(self):
        self.unclaimed()
        self.message('/start', user=456)
        count = len(self.api.calls)
        self.message('/start', user=789)
        self.assertEqual(len(self.api.calls), count)
        self.assertEqual(self.store.get('owner_id'), '456')

    def test_claim_requires_private_start(self):
        self.unclaimed()
        self.message('/start', kind='group')
        self.message('hello')
        self.click('menu')
        self.assertIsNone(self.bot.owner)
        self.assertIsNone(self.store.get('owner_id'))

    def test_claim_persists_after_restart(self):
        self.unclaimed()
        self.message('/start', user=456)
        self.store.db.close()
        self.store = Store(self.path)
        self.bot = Bot(self.store, self.api, None, timezone.utc)
        self.assertEqual(self.bot.owner, 456)

    def test_load_local_token_and_environment_override(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text('{"telegram_bot_token": "fake-local"}', encoding='utf-8-sig')
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(configured_token(config), 'fake-local')
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake-env'}):
            self.assertEqual(configured_token(config), 'fake-env')

    def test_missing_token_does_not_start(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(SystemExit):
            configured_token(Path(self.temp.name) / 'missing.json')

    def test_invalid_amounts(self):
        for text in ['0', '-1', 'NaN', 'inf', '1.234', '1e3', '', '1 000', '1000000000']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                amount(text)

    def test_ten_categories_and_reopen(self):
        self.assertEqual(len(self.store.categories()), 10)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.categories()), len(CATEGORIES))

    def test_legacy_category_migration_preserves_expenses(self):
        old, new = next(iter(LEGACY_CATEGORIES.items()))
        self.store.add(1250, 1, date(2026, 9, 10), 1)
        self.store.db.execute('UPDATE categories SET name=?,key=? WHERE id=1', (old, old.casefold()))
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.recent()[0][2:], (new, 1250))
        self.assertEqual(len(self.store.categories()), 10)

    def test_legacy_category_migration_merges_existing_target(self):
        old, new = next(iter(LEGACY_CATEGORIES.items()))
        self.store.category(old)
        self.store.add(1250, 11, date(2026, 9, 10), 1)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.recent()[0][2:], (new, 1250))
        self.assertEqual(len(self.store.categories()), 10)

    def test_project_text_contains_no_cyrillic(self):
        root = Path(__file__).resolve().parents[1]
        files = [*root.glob('*.py'), *root.glob('*.md'), *root.glob('*.ps1'),
                 *root.glob('*.txt'), *root.joinpath('tests').glob('*.py'), root / 'config.example.json']
        for path in files:
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(encoding='utf-8-sig'), r'[\u0400-\u052f]')

    def test_custom_category_validation(self):
        self.store.category('  My   cat ')
        self.assertEqual(self.store.categories()[-1][1], 'My cat')
        for name in ['my cat', '', 'x' * 41]:
            with self.assertRaises(ValueError):
                self.store.category(name)

    def test_add_flow_and_duplicate_click(self):
        self.click('add')
        self.message('19,99')
        self.click('cat:1', 3)
        self.click('cat:1', 3)
        self.assertEqual(len(self.store.recent()), 1)
        self.assertEqual(self.store.recent()[0][3], 1999)

    def test_expired_callback_does_not_block_action_or_next_message(self):
        original = self.api.call
        def expired(method, **payload):
            if method == 'answerCallbackQuery':
                raise HTTPError('https://example.invalid', 400, 'expired', {}, None)
            return original(method, **payload)
        with patch.object(self.api, 'call', side_effect=expired):
            self.click('week')
            self.assertIn('No expenses', self.api.calls[-1][1]['text'])
            self.message('/start')
            self.assertIn('Choose an action', self.api.calls[-1][1]['text'])

    def test_callback_network_failure_is_not_silenced(self):
        with patch.object(self.api, 'call', side_effect=HTTPError(
                'https://example.invalid', 503, 'unavailable', {}, None)):
            with self.assertRaises(HTTPError):
                self.bot.answer_callback('test')

    def test_delete_and_start_continue_while_acknowledgement_is_stalled(self):
        self.store.add(1250, 1, date(2026, 9, 10), 1)
        entered = threading.Event()
        release = threading.Event()
        original = self.api.call
        def stalled(method, **payload):
            if method == 'answerCallbackQuery':
                entered.set()
                release.wait(2)
                return []
            return original(method, **payload)
        try:
            with patch.object(self.api, 'call', side_effect=stalled):
                self.click('confirm:1')
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.store.recent(), [])
                self.assertEqual(self.api.calls[-1][1]['text'], 'Expense deleted.')
                self.message('/start')
                self.assertIn('Choose an action', self.api.calls[-1][1]['text'])
                self.assertFalse(release.is_set())
                release.set()
                self.bot.callback_queue.join()
        finally:
            release.set()

    def test_acknowledgement_timeout_does_not_prevent_deletion(self):
        self.store.add(1250, 1, date(2026, 9, 10), 1)
        original = self.api.call
        def timeout(method, **payload):
            if method == 'answerCallbackQuery':
                raise TimeoutError('simulated')
            return original(method, **payload)
        with patch.object(self.api, 'call', side_effect=timeout):
            self.click('confirm:1')
            self.bot.callback_queue.join()
            self.assertEqual(self.store.recent(), [])
            self.message('/start')
            self.assertIn('Choose an action', self.api.calls[-1][1]['text'])

    def test_storage_deduplicates_update(self):
        self.store.add(100, 1, date(2026, 9, 10), 99)
        self.store.add(100, 1, date(2026, 9, 10), 99)
        self.assertEqual(len(self.store.recent()), 1)

    def test_expenses_persist(self):
        self.store.add(100, 1, date(2026, 9, 10), 99)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.recent()[0][3], 100)

    def test_access_denied(self):
        self.message('/start', user=456)
        self.message('/start', kind='group')
        self.assertEqual(self.api.calls, [])
        self.assertIsNone(self.store.get('started'))

    def test_cancel_does_not_save(self):
        self.click('add')
        self.message('100')
        self.click('menu')
        self.click('cat:1')
        self.assertEqual(self.store.recent(), [])

    def test_invalid_amount_keeps_flow(self):
        self.click('add')
        self.message('-1')
        self.assertEqual(self.bot.state, 'amount')
        self.message('10')
        self.assertEqual(self.bot.state, ('choose', 1000))

    def test_create_category_in_telegram(self):
        self.click('new_category')
        self.message('Education')
        self.assertEqual(self.store.categories()[-1][1], 'Education')

    def test_delete_requires_confirmation(self):
        self.store.add(100, 1, date(2026, 9, 10), 1)
        self.click('del:1')
        self.assertEqual(len(self.store.recent()), 1)
        self.click('confirm:1')
        self.assertEqual(self.store.recent(), [])
        self.click('confirm:1')

    def test_period_boundaries_and_total(self):
        for i, day in enumerate([6, 7, 13, 14]):
            self.store.add(125, 1, date(2026, 9, day), i)
        data = self.store.summary(date(2026, 9, 7), date(2026, 9, 14))
        self.assertEqual(data['total_cents'], 250)
        self.assertEqual(data['categories'][0]['count'], 2)
        self.assertIn('2.50 EUR', report(data, 'EUR'))

    def test_empty_report(self):
        data = self.store.summary(date(2026, 9, 7), date(2026, 9, 14))
        self.assertIn('No expenses', report(data, 'EUR'))

    def test_week_crosses_year(self):
        self.assertEqual(week_start(date(2027, 1, 1)), date(2026, 12, 28))

    def test_menu_buttons(self):
        for action in ['menu', 'week', 'month', 'categories', 'recent', 'settings', 'ai']:
            self.click(action)
        self.assertTrue(all(len(call[1].get('text', '')) < 4096 for call in self.api.calls))

    def test_weekly_switch_persists(self):
        self.click('weekly_on')
        self.assertEqual(self.store.get('weekly'), '1')
        self.click('weekly_off')
        self.assertEqual(self.store.get('weekly'), '0')

    def test_schedule_runs_once_and_catches_up(self):
        self.store.set('started', 1)
        self.store.set('weekly', 1)
        self.bot.tick(datetime(2026, 9, 14, 8, tzinfo=timezone.utc))
        self.assertIsNone(self.store.get('last_week'))
        self.bot.tick(datetime(2026, 9, 15, 10, tzinfo=timezone.utc))
        self.assertEqual(self.store.get('last_week'), '2026-09-07')
        count = len(self.api.calls)
        self.bot.tick(datetime(2026, 9, 15, 11, tzinfo=timezone.utc))
        self.assertEqual(len(self.api.calls), count)

    def wait_result(self):
        for _ in range(100):
            if self.bot.result is not None:
                return
            time.sleep(.01)
        self.fail('Analysis worker did not complete')

    def test_analysis_worker(self):
        self.store.add(100, 1, date(2026, 9, 10), 1)
        self.bot.start_analysis()
        self.wait_result()
        self.bot.tick()
        self.assertFalse(self.bot.busy)
        self.assertIn('Test analysis', self.api.calls[-1][1]['text'])

    def test_analysis_failure_unblocks_bot(self):
        self.store.add(100, 1, date(2026, 9, 10), 1)
        def fail(*args):
            raise RuntimeError('private detail')
        self.bot.analyzer = fail
        self.bot.start_analysis()
        self.wait_result()
        self.bot.tick()
        self.assertFalse(self.bot.busy)
        self.assertGreater(self.bot.retry_after, time.time())
        self.assertNotIn('private detail', self.api.calls[-1][1]['text'])

    def test_codex_missing_is_clear(self):
        with patch.dict('os.environ', {}, clear=True), patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'not found'):
                codex_analysis({}, {}, 'EUR')

    def test_full_telegram_scenario(self):
        self.message('/start')
        self.click('new_category')
        self.message('Cat')
        self.click('add')
        self.message('12,50')
        self.click('cat:11', 10)
        self.click('week')
        self.assertIn('12.50 EUR', self.api.calls[-1][1]['text'])
        self.assertIn('Cat', self.api.calls[-1][1]['text'])
        self.click('recent')
        self.click('del:1')
        self.click('confirm:1')
        self.click('week')
        self.assertIn('No expenses', self.api.calls[-1][1]['text'])

    def test_pending_report_survives_send_failure(self):
        self.bot.busy = True
        self.bot.result = ('Completed report', '2026-09-07', True)
        with patch.object(self.api, 'call', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                self.bot.tick()
        self.assertIsNotNone(self.bot.result)
        self.assertIsNone(self.store.get('last_week'))
        self.bot.tick()
        self.assertIsNone(self.bot.result)
        self.assertEqual(self.store.get('last_week'), '2026-09-07')

    def test_long_messages_are_split(self):
        self.bot.send('A' * 8000)
        self.assertEqual(len(self.api.calls), 3)
        self.assertTrue(all(len(payload['text']) <= 3500 for _, payload in self.api.calls))

    def test_codex_invocation_and_secret_exclusion(self):
        def run(command, **kwargs):
            self.assertNotIn('TELEGRAM_BOT_TOKEN', kwargs['env'])
            self.assertIn('--ephemeral', command)
            self.assertIn('read-only', command)
            self.assertIn('--ignore-user-config', command)
            self.assertIn('EUR', kwargs['input'])
            Path(command[command.index('--output-last-message') + 1]).write_text('Check', encoding='utf-8')
            return type('Result', (), {'returncode': 0})()
        with patch.dict('os.environ', {'CODEX_BIN': 'codex.exe', 'TELEGRAM_BOT_TOKEN': 'fake-test-token'}), patch('subprocess.run', side_effect=run):
            self.assertEqual(codex_analysis({}, {}, 'EUR'), 'Check')


if __name__ == '__main__':
    unittest.main()
