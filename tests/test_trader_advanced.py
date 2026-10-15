"""Offline protocol, durable confirmation and owner-only bot integration checks."""
import copy
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import test_watch as fixtures
from app.trader_advanced import (
    PREFIX, Confirmations, RemoteError, SignedHTTP, TraderTelegram,
    packed, reference, request_for, review_text, signed_headers,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'trader_advanced'


def fixture(name='view-awaiting-approval'):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def token(markup):
    return markup['inline_keyboard'][0][0]['callback_data'][3:]


class MockHTTP:
    """One ledger authority shared by a simulated browser and Telegram."""
    def __init__(self):
        self.view = fixture()
        self.calls = []
        self.receipts = {}
        self.lost_response = False
        self.rejected = None
        self.events = 0

    def browser_change(self):
        self.view['state_version'] += 1

    def request(self, sender, method, target, body=b''):
        self.calls.append((sender, method, target, body))
        if method == 'GET':
            if target == PREFIX + '/status':
                return {'availability': 'READY', 'authorization': 'VERIFIED', 'prerequisites': []}
            if target.startswith(PREFIX + '/improvements?'):
                return {'items': [copy.deepcopy(self.view)], 'next_cursor': None}
            return copy.deepcopy(self.view)
        request = json.loads(body)
        key = request['idempotency_key']
        if key in self.receipts:
            return self.receipts[key]
        if self.rejected:
            raise RemoteError(*self.rejected)
        if request['state_version'] != self.view['state_version']:
            raise RemoteError(409, 'STATE_CONFLICT')
        self.events += 1
        self.view['state_version'] += 1
        receipt = {'receipt_id': 'receipt:' + str(self.events), 'idempotency_key': key}
        self.receipts[key] = receipt
        if self.lost_response:
            self.lost_response = False
            raise TimeoutError('response lost after server commit')
        return receipt


class ProtocolTests(unittest.TestCase):
    def test_exact_hmac_bytes(self):
        key = b'x' * 32; body = b'{"hello":"test"}'; nonce = 'n' * 43
        target = '/strategy/advanced/improvements?market=US&limit=10'
        headers = signed_headers(key, 100, 'POST', target, body, timestamp=12345, nonce=nonce)
        canonical = '\n'.join(['ione-advanced-auth-v1', 'telegram', '100', '12345', nonce,
                               'POST', target, hashlib.sha256(body).hexdigest()])
        self.assertEqual(headers['X-IONE-Signature'], hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(headers['X-IONE-Sender'], '100')
        self.assertNotIn('Origin', headers)
        self.assertNotIn('Cookie', headers)

    def test_bad_key_and_sender_refused(self):
        for key, sender in [(b'a', 100), (b'a'*32, 'ceo')]:
            with self.assertRaises(ValueError):
                signed_headers(key, sender, 'GET', PREFIX + '/status', b'')

    def test_only_loopback_origin_no_redirects(self):
        for url in ['https://example.com', 'http://localhost:8888', 'http://127.0.0.1:8888/path',
                    'http://user@127.0.0.1:8888', 'http://127.0.0.1:8888?token=x']:
            with self.assertRaises(ValueError):
                SignedHTTP(url, '/unused')

    def test_mock_http_carries_exact_bytes_and_hides_raw_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / 'signing.key'
            key_path.write_bytes(b't'*32); key_path.chmod(0o600)
            captured = []
            def opener(req, timeout):
                captured.append(req)
                return io.BytesIO(b'{"availability":"READY"}')
            client = SignedHTTP('http://127.0.0.1:8888', key_path, opener=opener)
            self.assertEqual(client.request(100, 'GET', PREFIX + '/status')['availability'], 'READY')
            req = captured[0]
            self.assertIsNone(req.data)
            self.assertEqual(len(req.get_header('X-ione-nonce')), 43)
            self.assertNotIn('Cookie', req.headers)
            key_path.chmod(0o644)
            with self.assertRaises(ValueError):
                client.request(100, 'GET', PREFIX + '/status')

    def test_untrusted_error_body_not_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / 'signing.key'
            key_path.write_bytes(b't'*32); key_path.chmod(0o600)
            def opener(req, timeout):
                raise urllib.error.HTTPError(req.full_url, 403, 'secret=private', {},
                                             io.BytesIO(b'{"detail":{"code":"token=secret"}}'))
            client = SignedHTTP('http://127.0.0.1:8888', key_path, opener=opener)
            with self.assertRaises(RemoteError) as caught:
                client.request(100, 'GET', PREFIX + '/status')
            self.assertEqual(caught.exception.code, 'REQUEST_REFUSED')
            self.assertNotIn('secret', str(caught.exception))


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private' / 'confirm.sqlite'
        self.now = 1000
        self.store = Confirmations(self.path, clock=lambda: self.now)
        self.http = MockHTTP()
        self.handler = TraderTelegram(self.http, self.store)

    def offer(self, action='approve_experiment'):
        return self.handler.offer(100, 100, 'command:1', self.http.view, action)

    def test_approve_is_card_only_then_one_time_exact_request(self):
        text, markup = self.offer()
        self.assertIn('최종 확인', text)
        self.assertEqual(self.http.events, 0)
        callback = token(markup)
        first = self.handler.callback(callback, 100, 100)
        self.handler.callback(callback, 100, 100)
        self.assertEqual(self.http.events, 1)
        self.assertEqual(sum(c[1] == 'POST' for c in self.http.calls), 1)
        body = json.loads(self.http.calls[0][3])
        self.assertEqual(body['state_version'], 2)
        self.assertEqual(body['improvement_ref'], reference(fixture()['improvement'], 'improvement_id'))
        self.assertEqual(body['decision'], 'approve_experiment')
        self.assertNotIn('actor', body)
        self.assertIsNone(first[1])

    def test_wrong_sender_and_chat_rejected(self):
        _, markup = self.offer()
        for sender, chat in [(101, 100), (100, 101)]:
            with self.assertRaises(ValueError):
                self.handler.callback(token(markup), sender, chat)
        self.assertEqual(self.http.events, 0)

    def test_expiry_blocks_before_any_http(self):
        _, markup = self.offer(); self.now += 601
        with self.assertRaises(ValueError):
            self.handler.callback(token(markup), 100, 100)
        self.assertEqual(self.http.calls, [])

    def test_restart_preserves_immutable_request(self):
        _, markup = self.offer()
        reopened = TraderTelegram(self.http, Confirmations(self.path, clock=lambda: self.now))
        self.http.view['improvement']['revision'] = 9
        reopened.callback(token(markup), 100, 100)
        self.assertEqual(json.loads(self.http.calls[-1][3])['improvement_ref']['revision'], 1)

    def test_stale_409_from_browser_change_never_refetches_or_approves_latest(self):
        _, markup = self.offer()
        self.http.browser_change()
        reply, _ = self.handler.callback(token(markup), 100, 100)
        self.assertIn('STATE_CONFLICT', reply)
        self.handler.callback(token(markup), 100, 100)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.events, 0)

    def test_lost_response_reuses_exact_body_and_key_after_restart(self):
        _, markup = self.offer(); self.http.lost_response = True
        reply, retry = self.handler.callback(token(markup), 100, 100)
        self.assertIn('같은 요청', reply)
        self.assertEqual(token(retry), token(markup))
        reopened = TraderTelegram(self.http, Confirmations(self.path, clock=lambda: self.now))
        reopened.callback(token(retry), 100, 100)
        self.assertEqual(self.http.events, 1)
        self.assertEqual(self.http.calls[0][3], self.http.calls[1][3])

    def test_server_500_is_unknown_same_request_retry(self):
        _, markup = self.offer(); self.http.rejected = (503, 'STORAGE_UNAVAILABLE')
        _, retry = self.handler.callback(token(markup), 100, 100)
        self.assertEqual(token(retry), token(markup))
        self.http.rejected = None
        self.handler.callback(token(retry), 100, 100)
        self.assertEqual(self.http.events, 1)

    def test_inflight_crash_retries_after_30s_with_same_key(self):
        _, markup = self.offer(); key = token(markup)
        self.store.claim(key, 100, 100)
        with self.assertRaises(ValueError):
            self.handler.callback(key, 100, 100)
        self.now += 31
        self.handler.callback(key, 100, 100)
        self.assertEqual(self.http.events, 1)

    def test_view_then_review_then_explicit_confirmation_no_mutation(self):
        _, actions = self.handler.command('/trader view ' + self.http.view['improvement']['improvement_id'], 100, 100, 'telegram:1')
        text, confirmation = self.handler.callback(token(actions), 100, 100)
        self.assertIn('최종 확인', text)
        self.assertEqual(self.http.events, 0)
        self.handler.callback(token(confirmation), 100, 100)
        self.assertEqual(self.http.events, 1)

    def test_duplicate_command_replays_same_card_without_getting_new_revision(self):
        command = '/trader approve ' + self.http.view['improvement']['improvement_id']
        first = self.handler.command(command, 100, 100, 'telegram:1')
        self.http.browser_change()
        again = self.handler.command(command, 100, 100, 'telegram:1')
        self.assertEqual(first, again)
        self.assertEqual(len(self.http.calls), 1)

    def test_duplicate_view_and_review_callback_return_same_tokens(self):
        command = '/trader view ' + self.http.view['improvement']['improvement_id']
        first = self.handler.command(command, 100, 100, 'telegram:1')
        again = self.handler.command(command, 100, 100, 'telegram:1')
        self.assertEqual(first, again)
        card = self.handler.callback(token(first[1]), 100, 100)
        self.assertEqual(card, self.handler.callback(token(first[1]), 100, 100))

    def test_start_needs_separate_action_and_matching_approval_event(self):
        self.http.view = fixture('view-approved')
        _, markup = self.offer('start_experiment')
        self.assertEqual(self.http.events, 0)
        self.handler.callback(token(markup), 100, 100)
        request = json.loads(self.http.calls[0][3])
        self.assertEqual(request['object_type'], 'ExperimentRequest')
        self.assertEqual(request['approval_event_id'], 'decision:1')
        self.assertEqual(request['input_ref'], self.http.view['plan']['input_ref'])
        self.assertEqual(self.http.calls[0][2], PREFIX + '/experiments')

    def test_missing_approval_ref_blocks_start(self):
        view = fixture('view-approved'); view['decisions'] = []
        with self.assertRaises(ValueError):
            request_for(view, 'start_experiment')

    def test_all_decision_scopes_refs_and_redaction(self):
        view = fixture('view-job-completed')
        for action in ['hold', 'reject', 'adopt']:
            body = request_for(view, action, 'token=secret-test-value')['body']
            self.assertEqual(body['application_scope'], 'TEST_ONLY')
            self.assertEqual(body['result_ref']['content_hash'], view['result']['content_hash'])
            self.assertNotIn('secret-test-value', body['reason'])
        view = fixture()
        for obj in [view['improvement'], view['plan'], view['plan']['input_ref']]:
            obj['source_kind'] = 'historical'; obj['namespace'] = 'real:research'
        self.assertEqual(request_for(view, 'approve_experiment')['body']['application_scope'], 'RESEARCH_ONLY')

    def test_cross_market_or_source_ref_blocks_request(self):
        for field, value in [('market', 'KR'), ('source_kind', 'historical'), ('namespace', 'other:data')]:
            view = fixture(); view['plan'][field] = value
            with self.assertRaises(ValueError):
                request_for(view, 'approve_experiment')

    def test_backend_disallowed_action_never_builds_confirmation(self):
        self.http.view['allowed_actions'] = []
        with self.assertRaises(ValueError):
            self.offer()
        self.assertEqual(self.http.events, 0)

    def test_status_and_list_are_readonly(self):
        for command in ['/trader status', '/trader list US']:
            text, markup = self.handler.command(command, 100, 100, command)
            self.assertTrue(text); self.assertIsNone(markup)
        self.assertTrue(all(call[1] == 'GET' for call in self.http.calls))

    def test_private_state_and_redacted_review(self):
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        view = fixture(); view['improvement']['draft']['hypothesis'] = 'token=private-test-token'
        text = review_text(view)
        self.assertNotIn('private-test-token', text)
        self.assertIn('비용', text)
        self.assertIn('채택 기준', text)
        self.assertLess(len(text), 4096)


class BotTests(fixtures.BotFixture):
    def setUp(self):
        super().setUp()
        self.http = MockHTTP()
        self.bot.trader_advanced = TraderTelegram(self.http, Confirmations(self.settings.state_dir / 'trader.sqlite'))

    def test_owner_only_command_does_not_call_chat_or_other_actions(self):
        update = self.message('/trader status')
        update['message']['from']['id'] = 101
        self.bot._handle_update(update)
        self.assertEqual(self.http.calls, [])
        self.bot._handle_update(self.message('/trader status', update_id=2))
        self.assertEqual(self.http.calls[0][0], 100)
        self.assertEqual(self.tg.sent[-1]['reply_markup'], None)

    def test_duplicate_updates_callbacks_only_one_mutation(self):
        command = '/trader approve ' + self.http.view['improvement']['improvement_id']
        self.bot._handle_update(self.message(command))
        self.bot._handle_update(self.message(command))
        callback_data = self.buttons()[0][1]
        self.press(callback_data, update_id=50)
        self.press(callback_data, update_id=50)
        self.press(callback_data, update_id=51)
        self.assertEqual(self.http.events, 1)

    def test_callback_requires_registered_private_chat_and_user(self):
        self.bot._handle_update(self.message('/trader approve ' + self.http.view['improvement']['improvement_id']))
        callback_data = self.buttons()[0][1]
        for i, kwargs in enumerate([{'user_id':101}, {'chat_id':101}, {'chat_type':'group'}]):
            self.press(callback_data, update_id=50+i, **kwargs)
        self.assertEqual(self.http.events, 0)

    def test_unconfigured_feature_does_not_crash_other_commands(self):
        del self.bot.trader_advanced
        with patch.object(TraderTelegram, 'load', side_effect=ValueError('not configured')):
            self.bot._handle_update(self.message('/trader status'))
        self.assertIn('확인', self.tg.sent[-1]['text'])
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
