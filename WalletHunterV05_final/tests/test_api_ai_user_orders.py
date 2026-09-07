"""HTTP boundaries for user-operated orders: fakes only, no exchange network."""
import unittest
from unittest.mock import Mock, patch

import test_api_safety as fixtures
from core.ai_review import account_guard


class ApiAiUserOrdersTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ApiSafetyTests('test_dashboard_isolates_user_account_events_and_settings')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.server, self.store, self.request = self.fixture.server, self.fixture.store, self.fixture.request
        self.service = Mock()
        self.service.summary.return_value = {'pending': [], 'history': [], 'status': 'WAITING'}
        self.service.prepare.return_value = self.service.summary.return_value
        self.service.decide.return_value = {'status': 'DECLINED'}
        self.fixture.stack.enter_context(patch.object(self.server, 'ai_user_orders', self.service))
        self.public = self.fixture.stack.enter_context(patch.object(self.server, 'public_account_for', return_value=object()))

    def test_auth_required_and_extra_fields_not_accepted(self):
        for url, body in (('/api/ai/orders/prepare', {}), ('/api/ai/orders/example/decision', {'confirm': True})):
            self.assertEqual(self.request('POST', url, body=body, data='').status_code, 401)
            self.assertEqual(self.request('POST', url, body={**body, 'user_id': 2}).status_code, 422)
        for value in ('true', 'false', 1, 0, None):
            self.assertEqual(self.request('POST', '/api/ai/orders/example/decision', body={'confirm': value}).status_code, 422)
        self.service.decide.assert_not_called()
        self.service.prepare.assert_not_called()

    def test_client_cannot_override_frozen_order(self):
        for field in ('size', 'leverage', 'amount', 'direction', 'account', 'limit_price'):
            response = self.request('POST', '/api/ai/orders/example/decision', body={'confirm': True, field: 999})
            self.assertEqual(response.status_code, 422)
        self.service.decide.assert_not_called()

    def test_prepare_is_public_only_and_isolated(self):
        before = self.store.load()
        response = self.request('POST', '/api/ai/orders/prepare', body={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.prepare.call_args.args[0], 1)
        self.assertEqual(self.store.load(), before)
        self.server.account_client_for.assert_not_called()

    def test_decline_has_no_public_or_signing_client(self):
        response = self.request('POST', '/api/ai/orders/example/decision', body={'confirm': False})
        self.assertEqual(response.status_code, 200, response.text)
        args = self.service.decide.call_args.args
        self.assertEqual(args[:3], (1, 'example', False))
        self.assertIsNone(args[4])
        self.public.assert_not_called()
        self.server.account_client_for.assert_not_called()

    def test_only_service_can_invoke_signing_after_its_checks(self):
        response = self.request('POST', '/api/ai/orders/example/decision', body={'confirm': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.server.account_client_for.assert_not_called()
        self.assertTrue(callable(self.service.decide.call_args.args[5]))

    def test_busy_account_blocks_before_service(self):
        with account_guard(self.fixture.tmp, fixtures.ACCOUNT_A):
            response = self.request('POST', '/api/ai/orders/example/decision', body={'confirm': True})
        self.assertEqual(response.status_code, 409)
        self.service.decide.assert_not_called()

    def test_errors_are_sanitized_without_retry(self):
        self.service.decide.side_effect = RuntimeError('must not expose credentials or HTTP payload')
        response = self.request('POST', '/api/ai/orders/example/decision', body={'confirm': True})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('credentials', response.text)
        self.assertEqual(self.service.decide.call_count, 1)
        self.server.account_client_for.assert_not_called()

    def test_uid_is_taken_only_from_verified_session(self):
        response = self.request('POST', '/api/ai/orders/example/decision', uid=2, body={'confirm': False})
        self.assertEqual(response.status_code, 200)
        args = self.service.decide.call_args.args
        self.assertEqual(args[0], 2)
        self.assertEqual(args[3]['account']['address'], fixtures.ACCOUNT_B)

