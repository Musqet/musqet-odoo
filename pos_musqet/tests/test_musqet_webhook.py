# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""The Musqet webhook: signature check, the buffer the POS reads, and the route."""
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessDenied
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase
from odoo.tools import mute_logger

from .common import (
    create_musqet_method,
    make_pos_user,
    musqet_response,
    open_session,
    record_refund,
    record_sale,
)


@tagged('post_install', '-at_install')
class TestMusqetWebhookVerification(TransactionCase):
    """The webhook HMAC is the only authentication on a public, unauthenticated route.

    Lock down §4.5: a request is accepted only with the configured signing secret, a fresh
    timestamp, and a signature over the exact raw bytes. Exercised directly on
    _musqet_verify_webhook so the test doesn't need the HTTP stack.
    """

    SECRET = 'whsec_test_0123456789'

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-WH-001', musqet_webhook_secret=self.SECRET)

    def _sign(self, raw_body, timestamp, secret=None):
        if isinstance(raw_body, str):
            raw_body = raw_body.encode()
        signed_payload = ('%s.' % timestamp).encode() + raw_body
        digest = hmac.new((secret or self.SECRET).encode(), signed_payload, hashlib.sha256).hexdigest()
        return 't=%s,v1=%s' % (timestamp, digest)

    def test_accepts_a_valid_fresh_signature(self):
        body = b'{"saleId":"s-1","status":"COMPLETE","serial":"MSQ-WH-001"}'
        header = self._sign(body, int(time.time()))
        self.assertTrue(self.method._musqet_verify_webhook(header, body))

    def test_signature_is_bound_to_the_exact_bytes(self):
        # A signature for one body must not verify a different body (tamper detection).
        body = b'{"saleId":"s-1","status":"COMPLETE","serial":"MSQ-WH-001"}'
        header = self._sign(body, int(time.time()))
        tampered = b'{"saleId":"s-1","status":"COMPLETE","serial":"MSQ-WH-999"}'
        self.assertFalse(self.method._musqet_verify_webhook(header, tampered))

    def test_rejects_wrong_secret(self):
        body = b'{"status":"COMPLETE"}'
        header = self._sign(body, int(time.time()), secret='whsec_attacker')
        self.assertFalse(self.method._musqet_verify_webhook(header, body))

    def test_rejects_stale_timestamp(self):
        body = b'{"status":"COMPLETE"}'
        stale = int(time.time()) - pos_payment_method.MUSQET_WEBHOOK_TOLERANCE - 60
        self.assertFalse(self.method._musqet_verify_webhook(self._sign(body, stale), body))

    def test_rejects_future_timestamp(self):
        body = b'{"status":"COMPLETE"}'
        future = int(time.time()) + pos_payment_method.MUSQET_WEBHOOK_TOLERANCE + 60
        self.assertFalse(self.method._musqet_verify_webhook(self._sign(body, future), body))

    def test_rejects_malformed_or_missing_header(self):
        body = b'{"status":"COMPLETE"}'
        for header in ('', 'garbage', 't=123', 'v1=abc', None):
            self.assertFalse(self.method._musqet_verify_webhook(header, body), msg=header)

    def test_rejects_when_no_secret_configured(self):
        self.method.musqet_webhook_secret = False
        body = b'{"status":"COMPLETE"}'
        header = self._sign(body, int(time.time()))
        self.assertFalse(self.method._musqet_verify_webhook(header, body))


@tagged('post_install', '-at_install')
class TestMusqetLatestStatus(TransactionCase):
    """musqet_get_latest_status is the cashier-facing, consume-once read of the webhook buffer."""

    def setUp(self):
        super().setUp()
        self.cashier = make_pos_user(self.env, 'musqet_cashier_status')
        self.method = create_musqet_method(self, 'MSQ-WH-002')
        open_session(self.env, self.method)

    def test_denies_a_non_musqet_payment_method_and_keeps_its_buffer(self):
        # The method check guards the buffer itself: a refused read must not consume it.
        plain = self.env['pos.payment.method'].create({'name': 'Plain Method'})
        # On an open session, so the method check is what refuses it.
        open_session(self.env, plain)
        plain.musqet_latest_response = '{"status": "COMPLETE"}'
        with self.assertRaises(AccessDenied):
            plain.with_user(self.cashier).musqet_get_latest_status()
        self.assertEqual(plain.musqet_latest_response, '{"status": "COMPLETE"}')

    def test_returns_false_when_nothing_buffered(self):
        self.assertFalse(self.method.with_user(self.cashier).musqet_get_latest_status())

    def test_returns_parsed_buffer_for_a_cashier(self):
        # The buffer is ERP-manager-restricted, but a cashier must be able to read it via the
        # method's sudo() — same context bug class as the proxy fields.
        sale = {'saleId': 's-9', 'status': 'COMPLETE', 'rail': 'bitcoin'}
        self.method.musqet_latest_response = json.dumps(sale)
        self.assertEqual(self.method.with_user(self.cashier).musqet_get_latest_status(), sale)

    def test_consumes_the_buffer_once(self):
        # Consume-once idempotency: the first read returns the result and clears the buffer,
        # so a duplicate notification (or a replayed-but-fresh webhook) reads nothing.
        sale = {'saleId': 's-9', 'status': 'COMPLETE'}
        self.method.musqet_latest_response = json.dumps(sale)
        method_as_cashier = self.method.with_user(self.cashier)
        self.assertEqual(method_as_cashier.musqet_get_latest_status(), sale)
        self.assertFalse(self.method.musqet_latest_response)
        self.assertFalse(method_as_cashier.musqet_get_latest_status())

    def test_denies_a_non_pos_user(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessDenied):
            self.method.with_user(public).musqet_get_latest_status()

    def test_create_sale_clears_a_stale_buffer(self):
        # A new sale must drop any buffered result from a previous one so a late notification
        # for it can't be mistaken for the new sale.
        self.method.musqet_latest_response = json.dumps({'saleId': 'old', 'status': 'COMPLETE'})
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'saleId': 'new', 'status': 'PENDING'}
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            self.method.with_user(self.cashier).musqet_create_sale(
                {'amountInCents': 500, 'reference': 'Order-T'})
        self.assertFalse(self.method.musqet_latest_response)


@tagged('post_install', '-at_install')
class TestMusqetWebhookRoute(HttpCase):
    """A signed webhook triggers a status read of a sale a read could still settle; its
    record and the till take that reply, never the body."""

    SECRET = 'whsec_route_0123456789'

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-WHR-001', musqet_webhook_secret=self.SECRET)
        self.sale = record_sale(self.env, self.method, 'sale-wh', state='pending', rail=False)

    def _post(self, body, secret=None, read=None):
        """Post ``body`` signed; a status read it triggers gets ``read`` (by default, the
        sale complete at 9.00 on card). Returns the response and the stubbed API calls."""
        raw = json.dumps(body).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new((secret or self.SECRET).encode(), timestamp.encode() + b'.' + raw,
                          hashlib.sha256).hexdigest()
        read = read or musqet_response(
            body['data']['saleId'], 'COMPLETE', amountInCents=900, currency=self.sale.currency,
            rail='card')
        with patch.object(pos_payment_method.requests, 'request', return_value=read) as mock_request:
            response = self.url_open('/musqet/webhook', data=raw, headers={
                'Content-Type': 'application/json',
                'x-webhook-signature': 't=%s,v1=%s' % (timestamp, digest),
            })
        self.sale.invalidate_recordset()
        return response, mock_request

    def _body(self):
        # What the body claims never settles anything: only the status read does.
        return {'data': {
            'saleId': 'sale-wh', 'status': 'COMPLETE', 'amountInCents': 99999,
            'currency': self.sale.currency, 'rail': 'card', 'serial': 'MSQ-WHR-001',
        }}

    def _buffered(self):
        self.method.invalidate_recordset()
        latest = self.method.musqet_latest_response
        return json.loads(latest) if latest else None

    def test_a_signed_webhook_settles_the_sale_from_a_status_read(self):
        _, mock_request = self._post(self._body())
        self.assertEqual([call.args[:2] for call in mock_request.mock_calls if call.args], [
            ('GET', 'https://api.musqet.tech/api/v1/terminal/sales/sale-wh')])
        self.assertEqual((self.sale.state, self.sale.amount_minor, self.sale.rail),
                         ('done', 900, 'card'))
        # The till gets the reply, not the body.
        self.assertEqual(self._buffered()['amountInCents'], 900)

    def test_pings_every_live_session_of_the_method(self):
        # A till being closed may still have the sale in progress; a closed one is gone.
        sessions = {state: open_session(self.env, self.method) for state in (
            'opening_control', 'opened', 'closing_control', 'closed')}
        for state, session in sessions.items():
            session.state = state
        with patch.object(type(self.env['pos.config']), '_notify', autospec=True) as notify:
            self._post(self._body())
        pinged = {call.args[0].id for call in notify.mock_calls}
        self.assertEqual(pinged, {sessions[state].config_id.id for state in (
            'opening_control', 'opened', 'closing_control')})

    def test_the_body_alone_settles_nothing(self):
        # Musqet still reports the sale in progress: the body's COMPLETE reaches neither the
        # record nor the till.
        self._post(self._body(), read=musqet_response('sale-wh', 'PROCESSING'))
        self.assertEqual(self.sale.state, 'pending')
        self.assertEqual(self._buffered()['status'], 'PROCESSING')

    def test_a_reply_without_a_sale_id_is_buffered_with_it(self):
        # The till matches the buffer to its line by saleId. Left out or null, both taken as
        # this sale (reply_is_for).
        for omit in (True, False):
            with self.subTest(omit=omit):
                self.sale.state = 'pending'
                self.method.musqet_latest_response = ''
                reply = musqet_response('sale-wh', 'COMPLETE', amountInCents=900,
                                        currency=self.sale.currency, rail='card')
                if omit:
                    del reply.json.return_value['saleId']
                else:
                    reply.json.return_value['saleId'] = None
                self._post(self._body(), read=reply)
                self.assertEqual(self._buffered()['saleId'], 'sale-wh')
                self.assertEqual(self.sale.state, 'done')

    def test_no_read_without_a_record_a_read_could_settle(self):
        # Settled already, never recorded, or no saleId: the POS's poll covers the till, and
        # a replay costs no call to Musqet. Nothing is buffered.
        done = record_sale(self.env, self.method, 'sale-done', rail='card')
        # An unanswered sale has no saleId: an empty one in the body must not reach it.
        lost = record_sale(self.env, self.method, False, state='pending', rail=False)
        for sale_id in ('sale-done', 'sale-unknown', ''):
            with self.subTest(sale_id=sale_id):
                body = self._body()
                body['data']['saleId'] = sale_id
                _, mock_request = self._post(body, read=musqet_response(sale_id or 'x', 'COMPLETE'))
                mock_request.assert_not_called()
                self.assertIsNone(self._buffered())
        (done | lost).invalidate_recordset()
        self.assertEqual(done.state, 'done')
        self.assertEqual(lost.state, 'pending')

    def test_a_done_sale_without_a_rail_is_read_again(self):
        sale = record_sale(self.env, self.method, 'sale-norail', rail=False)
        body = self._body()
        body['data']['saleId'] = 'sale-norail'
        _, mock_request = self._post(body)
        self.assertEqual(len(mock_request.mock_calls), 1)
        sale.invalidate_recordset()
        self.assertEqual(sale.rail, 'card')

    def test_a_failed_read_buffers_nothing(self):
        _, mock_request = self._post(
            self._body(), read=musqet_response('sale-wh', 'COMPLETE', status_code=502))
        self.assertEqual(len(mock_request.mock_calls), 1)
        self.assertIsNone(self._buffered())
        self.assertEqual(self.sale.state, 'pending')

    def test_a_signed_webhook_settles_a_refund(self):
        refund = record_refund(self.env, self.method, 'refund-wh')
        body = self._body()
        body['data']['saleId'] = 'refund-wh'
        self._post(body)
        refund.invalidate_recordset()
        self.assertEqual((refund.state, refund.amount_minor), ('done', 900))

    def test_a_non_string_sale_id_reads_nothing(self):
        sale = record_sale(self.env, self.method, '123', state='pending')
        body = self._body()
        body['data']['saleId'] = 123
        _, mock_request = self._post(body, read=musqet_response('123', 'COMPLETE'))
        mock_request.assert_not_called()
        sale.invalidate_recordset()
        self.assertEqual(sale.state, 'pending')

    def test_a_failed_settle_still_answers(self):
        Method = type(self.env['pos.payment.method'])
        with patch.object(Method, 'musqet_get_sale', side_effect=Exception('boom')), \
             mute_logger('odoo.addons.pos_musqet.controllers.main'):
            response, _ = self._post(self._body())
        # Answered as normal (no error); nothing to buffer, the poll covers the till.
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('error', response.json())
        self.assertIsNone(self._buffered())
        self.assertEqual(self.sale.state, 'pending')

    def test_an_unknown_serial_is_logged_escaped(self):
        # The serial comes from an unauthenticated body: it can't forge a log line.
        raw = json.dumps({'data': {'saleId': 'sale-wh', 'serial': 'MSQ-NONE\nFORGED LINE'}})
        with self.assertLogs('odoo.addons.pos_musqet.controllers.main', 'WARNING') as logs:
            self.url_open('/musqet/webhook', data=raw.encode(),
                          headers={'Content-Type': 'application/json'})
        self.assertNotIn('\n', logs.records[0].getMessage())
        self.assertIn('MSQ-NONE\\nFORGED LINE', logs.records[0].getMessage())

    def test_an_unsigned_webhook_reads_nothing(self):
        _, mock_request = self._post(self._body(), secret='whsec_wrong')
        mock_request.assert_not_called()
        self.assertEqual(self.sale.state, 'pending')
