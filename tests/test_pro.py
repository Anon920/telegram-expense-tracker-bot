"""Pro access, spending limits, recurring payments and shared AI quota tests."""
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot import AI_SLOT, Bot, PublicBot, Store, money
from pro_features import ProFeatures, following_due, next_month, previous_month


class API:
    def __init__(self):
        self.calls = []

    def call(self, method, **payload):
        if method != 'answerCallbackQuery':
            self.calls.append((method, payload))
        return True


def grant(bot, charge='test-pro'):
    payload = bot.subscriptions.new_order(bot.owner)
    bot.subscriptions.complete(bot.owner, {'invoice_payload': payload, 'currency': 'XTR',
        'total_amount': 1, 'telegram_payment_charge_id': charge}, int(time.time()))


class ProTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.api = API()
        self.calls = 0
        def analyzer(*args):
            self.calls += 1
            return 'Saved AI report'
        self.bot = Bot(self.store, self.api, 123, timezone.utc, analyzer=analyzer)
        self.bot.today = lambda: date(2026, 9, 14)
        self.pro = self.bot.pro

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def wait_result(self):
        for _ in range(300):
            if self.bot.result is not None:
                return
            time.sleep(.01)
        self.fail('AI worker did not finish')

    def test_free_user_cannot_bypass_any_pro_callback(self):
        for action in ['ai', 'run_ai', 'settings', 'weekly_on', 'budgets', 'budgetcat:1',
                       'budgetoff:1', 'recurring', 'recnew', 'recshow:1', 'recpaid:1:2026-09-14',
                       'recdelok:1', 'compare', 'cmp:2026-08-01']:
            self.bot.action(action, 1)
            self.assertIn('Pro includes', self.api.calls[-1][1]['text'])
        self.assertFalse(self.bot.busy)
        self.assertEqual(self.pro.budgets(self.bot.today()), [])

    def test_owner_has_no_free_ai_bypass(self):
        self.bot.start_analysis()
        self.assertEqual(self.calls, 0)

    def test_paid_menu_contains_all_pro_features(self):
        grant(self.bot)
        actions = {b['callback_data'] for row in self.bot.menu()['inline_keyboard'] for b in row}
        self.assertTrue({'ai', 'settings', 'budgets', 'recurring', 'compare'} <= actions)
        self.assertNotIn('subscribe', actions)

    def test_free_tracking_is_unchanged(self):
        self.bot.action('add', 1)
        self.bot.text('12.50')
        self.bot.action('cat:1', 2)
        self.assertEqual(self.store.recent()[0][3], 1250)
        self.bot.action('month', 3)
        self.assertIn('12.50 EUR', self.api.calls[-1][1]['text'])

    def test_refund_blocks_pending_pro_input(self):
        grant(self.bot)
        self.bot.action('budgetcat:1', 1)
        self.bot.subscriptions.refund_record(123, 'test-pro')
        self.bot.text('100')
        self.assertEqual(self.pro.budgets(self.bot.today()), [])

    def test_budget_counts_only_selected_month(self):
        self.pro.set_budget(1, 10000)
        for source, day in enumerate([date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1)]):
            self.store.add(1000, 1, day, source)
        self.assertEqual(self.pro.budgets(date(2026, 9, 14))[0][3], 2000)

    def test_expense_confirmation_shows_remaining_category_budget(self):
        grant(self.bot)
        self.pro.set_budget(1, 10000)
        self.store.add(2000, 1, self.bot.today(), 1)
        self.store.add(9000, 2, self.bot.today(), 2)
        self.store.add(8000, 1, date(2026, 8, 31), 3)
        self.bot.state = ('choose', 3000)
        self.bot.action('cat:1', 4)
        text = self.api.calls[-1][1]['text']
        self.assertIn('Monthly budget: 50.00 EUR / 100.00 EUR', text)
        self.assertIn('Remaining this month: 50.00 EUR', text)

    def test_expense_confirmation_handles_exact_and_exceeded_budget(self):
        grant(self.bot)
        self.pro.set_budget(1, 1000)
        self.bot.state = ('choose', 1000)
        self.bot.action('cat:1', 1)
        self.assertIn('Remaining this month: 0.00 EUR', self.api.calls[-1][1]['text'])
        self.bot.state = ('choose', 500)
        self.bot.action('cat:1', 2)
        self.assertIn('Over budget: 5.00 EUR', self.api.calls[-1][1]['text'])

    def test_expense_confirmation_omits_budget_without_pro_or_category_limit(self):
        self.pro.set_budget(1, 10000)
        self.bot.state = ('choose', 500)
        self.bot.action('cat:1', 1)
        self.assertNotIn('budget', self.api.calls[-1][1]['text'].lower())
        grant(self.bot)
        self.bot.state = ('choose', 500)
        self.bot.action('cat:2', 2)
        self.assertNotIn('budget', self.api.calls[-1][1]['text'].lower())

    def test_budget_thresholds_notify_once_across_restart(self):
        self.pro.set_budget(1, 10000)
        self.store.add(8000, 1, self.bot.today(), 1)
        fmt = lambda value: money(value, 'EUR')
        self.pro.budget_alerts(self.bot.today(), fmt)
        notice = self.pro.pending_notice()
        self.assertIn('80%', notice[1])
        self.pro.mark_notice(notice[0])
        self.pro = ProFeatures(self.store)
        self.pro.budget_alerts(self.bot.today(), fmt)
        self.assertIsNone(self.pro.pending_notice())
        self.store.add(2000, 1, self.bot.today(), 2)
        self.pro.budget_alerts(self.bot.today(), fmt)
        self.assertIn('100%', self.pro.pending_notice()[1])

    def test_jump_over_budget_sends_only_100_percent(self):
        self.pro.set_budget(1, 100)
        self.store.add(150, 1, self.bot.today(), 1)
        self.pro.budget_alerts(self.bot.today(), str)
        notice = self.pro.pending_notice()
        self.assertIn('100%', notice[1])
        self.pro.mark_notice(notice[0])
        self.assertIsNone(self.pro.pending_notice())

    def test_budget_ui_and_disable(self):
        grant(self.bot)
        self.bot.action('budgetcat:1', 1)
        self.bot.text('125.50')
        self.assertEqual(self.pro.budgets(self.bot.today())[0][2], 12550)
        self.bot.action('budgetoff:1', 2)
        self.assertEqual(self.pro.budgets(self.bot.today()), [])

    def test_month_math_and_anchor_day(self):
        self.assertEqual(next_month(date(2026, 12, 31)), date(2027, 1, 1))
        self.assertEqual(previous_month(date(2027, 1, 1)), date(2026, 12, 1))
        feb = following_due(date(2028, 1, 31), 31, date(2028, 1, 31))
        self.assertEqual(feb, date(2028, 2, 29))
        self.assertEqual(following_due(feb, 31, feb), date(2028, 3, 31))

    def test_recurring_payment_paid_once_and_next_due_advances(self):
        rid = self.pro.add_recurring('Rent', 10000, 1, self.bot.today())
        self.assertTrue(self.pro.settle(rid, '2026-09-14', self.bot.today(), 1))
        self.assertFalse(self.pro.settle(rid, '2026-09-14', self.bot.today(), 2))
        self.assertEqual(len(self.store.recent()), 1)
        self.assertEqual(self.pro.recurring()[0][4], '2026-10-14')

    def test_skip_does_not_record_expense_or_duplicate_missed_months(self):
        rid = self.pro.add_recurring('Rent', 10000, 1, date(2026, 1, 31))
        self.pro.settle(rid, '2026-01-31', self.bot.today(), 1, paid=False)
        self.assertFalse(self.store.recent())
        self.assertEqual(self.pro.recurring()[0][4], '2026-09-30')

    def test_future_payment_cannot_be_recorded_early(self):
        rid = self.pro.add_recurring('Rent', 10000, 1, date(2026, 10, 1))
        with self.assertRaises(ValueError):
            self.pro.settle(rid, '2026-10-01', self.bot.today(), 1)

    def test_delete_reminder_keeps_recorded_expenses(self):
        rid = self.pro.add_recurring('Rent', 10000, 1, self.bot.today())
        self.pro.settle(rid, '2026-09-14', self.bot.today(), 1)
        self.pro.delete_recurring(rid)
        self.assertFalse(self.pro.recurring())
        self.assertEqual(len(self.store.recent()), 1)

    def test_recurring_ui(self):
        grant(self.bot)
        self.bot.action('recnew', 1)
        self.bot.text('Music')
        self.bot.text('9.99')
        self.bot.action('reccat:1', 2)
        self.bot.text('2026-09-14')
        self.bot.action('recshow:1', 3)
        self.assertIn('Mark paid', str(self.api.calls[-1][1]['reply_markup']))
        self.bot.action('recpaid:1:2026-09-14', 4)
        self.assertEqual(self.store.recent()[0][3], 999)

    def test_due_reminder_is_persisted_and_only_sent_once(self):
        self.pro.add_recurring('Music', 999, 1, self.bot.today())
        self.pro.recurring_alerts(self.bot.today(), str)
        key, text, action = self.pro.pending_notice()
        self.assertIn('Music', text)
        self.assertEqual(action, 'recshow:1')
        self.pro.mark_notice(key)
        self.pro.recurring_alerts(self.bot.today(), str)
        self.assertIsNone(self.pro.pending_notice())

    def test_comparison_uses_complete_months_and_signed_changes(self):
        self.store.add(2000, 1, date(2026, 7, 1), 1)
        self.store.add(1000, 1, date(2026, 8, 1), 2)
        self.store.add(9000, 1, date(2026, 9, 1), 3)
        text = self.pro.compare(date(2026, 8, 1), lambda n: money(n, 'EUR'))
        self.assertIn('-10.00 EUR (-50.0%)', text)
        self.assertNotIn('90.00', text)

    def test_comparison_handles_no_previous_spending(self):
        self.store.add(1000, 1, date(2026, 8, 1), 1)
        self.assertIn('no previous spending', self.pro.compare(date(2026, 8, 1), str))

    def test_disabled_budget_cancels_pending_alert(self):
        self.pro.set_budget(1, 1000)
        self.store.add(900, 1, self.bot.today(), 1)
        self.pro.budget_alerts(self.bot.today(), str)
        self.assertIsNotNone(self.pro.pending_notice())
        self.pro.set_budget(1, 0)
        self.assertIsNone(self.pro.pending_notice())

    def test_notice_does_not_replace_scheduled_week_key(self):
        grant(self.bot)
        self.store.set('started', 1)
        self.store.set('weekly', 1)
        self.store.set('last_week', '2026-09-07')
        self.pro.add_recurring('Music', 999, 1, self.bot.today())
        with patch.object(self.bot, 'start_analysis') as analysis:
            self.bot.tick(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
        analysis.assert_not_called()

    def test_manual_and_automatic_analysis_share_persisted_result(self):
        grant(self.bot)
        self.store.add(1000, 1, date(2026, 9, 10), 1)
        self.bot.start_analysis()
        self.wait_result()
        self.bot.tick()
        self.bot.start_analysis(scheduled=True)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.store.get('last_week'), '2026-09-07')
        restarted = Bot(self.store, self.api, 123, timezone.utc,
                        analyzer=lambda *args: self.fail('Cache should prevent another AI call'))
        restarted.today = self.bot.today
        restarted.start_analysis()
        self.assertIn('Saved AI report', self.api.calls[-1][1]['text'])

    def test_failed_attempt_is_not_repeated(self):
        grant(self.bot)
        self.store.add(1000, 1, date(2026, 9, 10), 1)
        def fail(*args):
            self.calls += 1
            raise RuntimeError('test')
        self.bot.analyzer = fail
        self.bot.start_analysis()
        self.wait_result()
        self.bot.tick()
        self.bot.start_analysis()
        self.assertEqual(self.calls, 1)

    def test_global_busy_slot_does_not_consume_weekly_quota(self):
        grant(self.bot)
        self.store.add(1000, 1, date(2026, 9, 10), 1)
        with AI_SLOT:
            self.bot.start_analysis()
        self.assertIsNone(self.pro.report('2026-09-07'))

    def test_refund_disables_notifications_and_analysis(self):
        grant(self.bot)
        self.pro.add_recurring('Music', 999, 1, self.bot.today())
        self.bot.subscriptions.refund_record(123, 'test-pro')
        self.bot.tick(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
        self.assertFalse(self.api.calls)

    def test_background_reminders_load_persisted_users_after_restart(self):
        users = Path(self.temp.name) / 'users'
        router = PublicBot(self.store, self.api, 123, timezone.utc, users_path=users)
        other = router.user_bot(456)
        grant(other)
        other.pro.add_recurring('Rent', 100, 1, date(2020, 1, 1))
        other.db.db.close()
        router.users.clear()
        with patch('bot.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
            clock.combine = datetime.combine
            clock.min = datetime.min
            router.tick()
        self.assertTrue(any(payload.get('chat_id') == 456 and 'Payment due' in payload.get('text', '')
                            for _, payload in self.api.calls))
        for bot in router.users.values():
            bot.db.db.close()


if __name__ == '__main__':
    unittest.main()
