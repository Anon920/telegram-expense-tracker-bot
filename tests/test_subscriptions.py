"""Payment and multi-user regression tests. No live payments or external calls."""
import sqlite3
import tempfile
import time
import unittest
from datetime import date, timezone
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from bot import Bot, PublicBot, Store
from subscriptions import Subscriptions


class API:
    def __init__(self):
        self.calls = []
        self.message_id = 0

    def call(self, method, **data):
        if method == 'answerCallbackQuery':
            return True
        self.calls.append((method, data))
        self.message_id += 1
        return {'message_id': self.message_id}


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'expenses.sqlite3'
        self.db = Store(self.path)
        self.api = API()
        self.bot = Bot(self.db, self.api, 123, timezone.utc)
        self.ledger = self.bot.subscriptions

    def tearDown(self):
        self.db.db.close()
        self.temp.cleanup()

    def payment(self, **changes):
        result = {'currency': 'XTR', 'total_amount': 1,
                  'invoice_payload': self.ledger.new_order(123), 'telegram_payment_charge_id': 'test-charge'}
        result.update(changes)
        return result

    def checkout(self, payment=None, user=123, query_id='q1'):
        query = dict(payment or self.payment(), id=query_id)
        query['from'] = {'id': user}
        self.bot.handle({'update_id': 10, 'pre_checkout_query': query})
        return self.api.calls[-1][1]

    def success(self, payment):
        self.bot.handle({'update_id': 20, 'message': {'from': {'id': 123},
                         'chat': {'id': 123, 'type': 'private'}, 'date': int(time.time()),
                         'successful_payment': payment}})

    def has_subscribe(self):
        return any(button.get('callback_data') == 'subscribe'
                   for row in self.bot.menu()['inline_keyboard'] for button in row)

    def test_temporary_free_mode_preserves_payment_and_can_be_reversed(self):
        self.success(self.payment())
        self.db.set('pro_paused:123', '1')
        self.assertFalse(self.ledger.active(123))
        self.assertTrue(self.has_subscribe())
        self.assertEqual(self.db.db.execute('SELECT refunded FROM star_payments').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'temporarily paused'):
            self.ledger.new_order(123)
        self.bot.action('run_ai', 30)
        self.assertFalse(self.bot.busy)
        self.db.set('pro_paused:123', '0')
        self.assertTrue(self.ledger.active(123))

    def test_lifetime_invoice_costs_exactly_one_star(self):
        self.bot.action('buy_subscription', 1)
        method, invoice = self.api.calls[-1]
        self.assertEqual(method, 'sendInvoice')
        self.assertEqual(invoice['currency'], 'XTR')
        self.assertEqual(invoice['prices'][0]['amount'], 1)
        self.assertEqual(len(invoice['prices']), 1)
        self.assertNotIn('subscription_period', invoice)
        self.assertFalse(self.ledger.active(123))

    def test_offer_discloses_test_status_and_requires_acceptance(self):
        self.bot.action('subscribe', 1)
        self.assertEqual(self.api.calls[-1][0], 'sendMessage')
        self.assertIn('Pro includes weekly AI analysis', self.api.calls[-1][1]['text'])
        self.assertEqual(self.api.calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['text'], 'Agree and pay 1 Star')

    def test_precheckout_alone_does_not_activate(self):
        self.assertTrue(self.checkout()['ok'])
        self.assertFalse(self.ledger.active(123))
        self.assertTrue(self.has_subscribe())

    def test_invalid_checkout_details_are_rejected(self):
        for changes in ({'currency': 'EUR'}, {'total_amount': 2}, {'invoice_payload': 'unknown'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.checkout(self.payment(**changes))['ok'])
        self.assertFalse(self.checkout(self.payment(), user=456)['ok'])

    def test_parallel_checkout_is_rejected_but_retry_is_accepted(self):
        payment = self.payment()
        self.assertTrue(self.checkout(payment)['ok'])
        self.assertTrue(self.checkout(payment)['ok'])
        self.assertFalse(self.checkout(payment, query_id='q2')['ok'])

    def test_expired_invoice_is_rejected(self):
        payment = self.payment()
        query = dict(payment, id='q1', **{'from': {'id': 123}})
        self.assertIn('expired', self.ledger.checkout(query, now=int(time.time()) + 4000))

    def test_success_activates_without_expiry_and_removes_button(self):
        self.assertTrue(self.has_subscribe())
        self.success(self.payment())
        self.assertTrue(self.ledger.active(123, now=99999999999))
        self.assertFalse(self.has_subscribe())

    def test_duplicate_payment_is_idempotent(self):
        payment = self.payment()
        self.success(payment)
        self.success(payment)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM star_payments').fetchone()[0], 1)

    def test_invalid_success_does_not_activate(self):
        self.success(self.payment(total_amount=2))
        self.assertFalse(self.ledger.active(123))

    def test_another_users_payment_does_not_activate(self):
        payment = self.payment(invoice_payload=self.ledger.new_order(456))
        self.success(payment)
        self.assertFalse(self.ledger.active(123))

    def test_active_user_cannot_buy_twice(self):
        payment = self.payment()
        self.success(payment)
        self.assertFalse(self.checkout(payment)['ok'])
        self.bot.action('buy_subscription', 1)
        self.assertNotEqual(self.api.calls[-1][0], 'sendInvoice')

    def test_subscription_survives_restart(self):
        self.success(self.payment())
        self.db.db.close()
        self.db = Store(self.path)
        self.bot = Bot(self.db, self.api, 123, timezone.utc)
        self.assertFalse(self.has_subscribe())

    def test_old_menus_are_updated_after_success(self):
        self.bot.send('Menu')
        previous_id = self.api.calls[-1][1]['chat_id']
        self.assertEqual(previous_id, 123)
        self.success(self.payment())
        self.bot.tick()
        method, data = self.api.calls[-1]
        self.assertEqual(method, 'editMessageReplyMarkup')
        self.assertFalse(any(b.get('callback_data') == 'subscribe'
                             for row in data['reply_markup']['inline_keyboard'] for b in row))
        self.assertEqual(self.ledger.menus(123), [])

    def test_refund_is_confirmed_and_restores_subscription_button(self):
        self.success(self.payment())
        self.bot.action('refund', 1)
        self.assertTrue(self.ledger.active(123))
        self.bot.action('confirm_refund', 2)
        refund = [data for method, data in self.api.calls if method == 'refundStarPayment'][0]
        self.assertEqual(refund['user_id'], 123)
        self.assertEqual(refund['telegram_payment_charge_id'], 'test-charge')
        self.assertTrue(self.has_subscribe())

    def test_failed_refund_keeps_entitlement(self):
        self.success(self.payment())
        original = self.api.call
        def fail(method, **data):
            if method == 'refundStarPayment':
                raise HTTPError('https://example.invalid', 400, 'failed', {}, None)
            return original(method, **data)
        with patch.object(self.api, 'call', side_effect=fail):
            self.bot.action('confirm_refund', 1)
        self.assertTrue(self.ledger.active(123))

    def test_refund_event_is_idempotent(self):
        payment = self.payment()
        self.success(payment)
        for _ in range(2):
            self.bot.handle({'update_id': 21, 'message': {'from': {'id': 123},
                'chat': {'id': 123, 'type': 'private'}, 'refunded_payment': payment}})
        self.assertTrue(self.has_subscribe())

    def test_support_request_is_saved(self):
        self.bot.text('/paysupport Please check my payment')
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM payment_support').fetchone()[0], 1)


class PublicBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.temp.name) / 'main.sqlite3')
        self.api = API()
        self.router = PublicBot(self.db, self.api, 123, timezone.utc,
                                users_path=Path(self.temp.name) / 'users')
        self.update = 0

    def tearDown(self):
        for bot in self.router.users.values():
            bot.db.db.close()
        self.db.db.close()
        self.temp.cleanup()

    def message(self, user, text):
        self.update += 1
        self.router.handle({'update_id': self.update, 'message': {'from': {'id': user},
                            'chat': {'id': user, 'type': 'private'}, 'text': text}})

    def click(self, user, action):
        self.update += 1
        self.router.handle({'update_id': self.update, 'callback_query': {'id': str(self.update),
                            'from': {'id': user}, 'message': {'chat': {'id': user, 'type': 'private'}}, 'data': action}})

    def test_users_can_start_without_accessing_original_owner_data(self):
        self.db.add(1000, 1, date(2026, 9, 10), 1)
        self.message(456, '/start')
        self.assertEqual(self.api.calls[-1][1]['chat_id'], 456)
        self.assertEqual(self.router.users[456].db.recent(), [])
        self.assertEqual(len(self.db.recent()), 1)

    def test_expense_entry_states_and_deletions_are_isolated(self):
        for user in (456, 789):
            self.click(user, 'add')
        self.message(456, '12.50')
        self.message(789, '20')
        self.click(456, 'cat:1')
        self.click(789, 'cat:1')
        self.click(456, 'confirm:1')
        self.assertEqual(self.router.users[456].db.recent(), [])
        self.assertEqual(self.router.users[789].db.recent()[0][3], 2000)

    def test_custom_categories_are_isolated(self):
        self.click(456, 'new_category')
        self.message(456, 'Pets')
        self.message(789, '/start')
        self.assertEqual(len(self.router.users[456].db.categories()), 11)
        self.assertEqual(len(self.router.users[789].db.categories()), 10)

    def test_free_user_cannot_spend_codex_allowance(self):
        self.click(456, 'run_ai')
        self.assertIn('Pro includes', self.api.calls[-1][1]['text'])
        self.assertFalse(self.router.users[456].busy)
        self.click(456, 'weekly_on')
        self.assertIsNone(self.router.users[456].db.get('weekly'))

    def test_subscription_is_isolated_between_users(self):
        self.message(456, '/start')
        self.message(789, '/start')
        ledger = self.router.users[456].subscriptions
        payload = ledger.new_order(456)
        payment = {'invoice_payload': payload, 'currency': 'XTR', 'total_amount': 1,
                   'telegram_payment_charge_id': 'test-charge'}
        self.router.handle({'update_id': 10, 'message': {'from': {'id': 456},
            'chat': {'id': 456, 'type': 'private'}, 'successful_payment': payment}})
        self.assertTrue(ledger.active(456))
        self.assertFalse(self.router.users[789].subscriptions.active(789))
        self.assertFalse(self.router.primary.subscriptions.active(123))

    def test_group_updates_do_not_create_accounts(self):
        self.router.handle({'update_id': 1, 'message': {'from': {'id': 456},
            'chat': {'id': -123, 'type': 'group'}, 'text': '/start'}})
        self.assertFalse(self.router.users)

    def test_only_operator_can_read_support_requests(self):
        self.message(456, '/paysupport Test support question')
        self.message(789, '/support_requests')
        self.assertNotIn('Test support question', self.api.calls[-1][1]['text'])
        self.message(123, '/support_requests')
        self.assertIn('Test support question', self.api.calls[-1][1]['text'])


if __name__ == '__main__':
    unittest.main()
