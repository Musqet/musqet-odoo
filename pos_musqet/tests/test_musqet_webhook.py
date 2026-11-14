# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""The Musqet webhook: signature check, the buffer the POS reads, and the route."""
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.controllers.main import MUSQET_WEBHOOK_MAX_BYTES
from odoo.addons.pos_musqet.models.pos_payment_method import MUSQET_WEBHOOK_MAX_TIMESTAMP_DIGITS
from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessDenied
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase
from odoo.tools import mute_logger

from .common import (
    create_musqet_method,
    list_method_on_config,
    make_pos_user,
    musqet_response,
    open_plain_method,
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

    def test_rejects_a_header_naming_t_or_v1_twice(self):
        # One unambiguous value of each: a repeat isn't resolved by taking either one.
        body = b'{"status":"COMPLETE"}'
        header = self._sign(body, int(time.time()))
        t_part, v1_part = header.split(',')
        # Other keys are ignored, repeated or not.
        self.assertTrue(self.method._musqet_verify_webhook(header + ',v0=a,v0=b', body))
        for repeated in ('%s,%s' % (header, t_part), '%s,%s' % (header, v1_part),
                         '%s,%s' % (t_part, header), 't=1,' + header):
            with self.subTest(header=repeated):
                self.assertFalse(self.method._musqet_verify_webhook(repeated, body))

    def test_rejects_a_timestamp_that_is_not_plain_digits(self):
        # int() reads all of these, but each is signed as written: only plain digits pass.
        body = b'{"status":"COMPLETE"}'
        now = int(time.time())
        arabic = str(now).translate(str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩'))
        for timestamp in ('+%d' % now, '%s_%s' % (str(now)[:5], str(now)[5:]), arabic,
                          '%d.0' % now, '-%d' % now):
            with self.subTest(timestamp=timestamp):
                header = self._sign(body, timestamp)
                self.assertFalse(self.method._musqet_verify_webhook(header, body))

    def test_rejects_a_timestamp_over_the_digit_limit(self):
        # Rejected, not raised: int() refuses over 4,300 digits, and a few hundred overflow
        # the float age check.
        body = b'{"status":"COMPLETE"}'
        longest = str(int(time.time())).zfill(MUSQET_WEBHOOK_MAX_TIMESTAMP_DIGITS)
        self.assertTrue(self.method._musqet_verify_webhook(self._sign(body, longest), body))
        for timestamp in ('0' + longest, '9' * 400, '9' * 5000):
            with self.subTest(digits=len(timestamp)):
                header = self._sign(body, timestamp)
                self.assertFalse(self.method._musqet_verify_webhook(header, body))

    def test_rejects_a_v1_that_is_not_ascii_or_not_a_digest(self):
        # Rejected, not raised: the constant-time compare refuses non-ASCII strings.
        body = b'{"status":"COMPLETE"}'
        t_part = self._sign(body, int(time.time())).split(',')[0]
        for v1 in ('é', 'é' * 64, 'a' * 63, 'a' * 65):
            with self.subTest(v1=v1):
                header = '%s,v1=%s' % (t_part, v1)
                self.assertFalse(self.method._musqet_verify_webhook(header, body))

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
        plain = open_plain_method(self.env)
        plain.musqet_latest_response = '{"status": "COMPLETE"}'
        with self.assertRaises(AccessDenied):
            plain.with_user(self.cashier).musqet_get_latest_status('s-9')
        self.assertEqual(plain.musqet_latest_response, '{"status": "COMPLETE"}')

    def test_returns_false_when_nothing_buffered(self):
        self.assertFalse(self.method.with_user(self.cashier).musqet_get_latest_status('s-9'))

    def test_returns_parsed_buffer_for_a_cashier(self):
        # The buffer is ERP-manager-restricted, but a cashier must be able to read it via the
        # method's sudo() — same context bug class as the proxy fields.
        sale = {'saleId': 's-9', 'status': 'COMPLETE', 'rail': 'bitcoin'}
        self.method.musqet_latest_response = json.dumps(sale)
        self.assertEqual(self.method.with_user(self.cashier).musqet_get_latest_status('s-9'), sale)

    def test_consumes_the_buffer_once(self):
        # Consume-once idempotency: the first read returns the result and clears the buffer,
        # so a duplicate notification (or a replayed-but-fresh webhook) reads nothing.
        sale = {'saleId': 's-9', 'status': 'COMPLETE'}
        self.method.musqet_latest_response = json.dumps(sale)
        method_as_cashier = self.method.with_user(self.cashier)
        self.assertEqual(method_as_cashier.musqet_get_latest_status('s-9'), sale)
        self.assertFalse(self.method.musqet_latest_response)
        self.assertFalse(method_as_cashier.musqet_get_latest_status('s-9'))

    def test_only_the_till_waiting_on_the_sale_takes_it(self):
        # Every till using the method is pinged: one waiting on another sale (or on none
        # yet) gets nothing, and the result stays for the till that is waiting on it.
        sale = {'saleId': 's-9', 'status': 'COMPLETE'}
        self.method.musqet_latest_response = json.dumps(sale)
        method_as_cashier = self.method.with_user(self.cashier)
        for other in ('s-8', '', None, False, ['s-9'], {'saleId': 's-9'}):
            with self.subTest(sale_id=other):
                self.assertFalse(method_as_cashier.musqet_get_latest_status(other))
                self.assertEqual(json.loads(self.method.musqet_latest_response), sale)
        # A till still on the frontend from before 19.0.1.7.0 names no sale.
        self.assertFalse(method_as_cashier.musqet_get_latest_status())
        self.assertEqual(json.loads(self.method.musqet_latest_response), sale)
        self.assertEqual(method_as_cashier.musqet_get_latest_status('s-9'), sale)

    def test_denies_a_non_pos_user(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessDenied):
            self.method.with_user(public).musqet_get_latest_status('s-9')

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
    SALE_ID = 'sale-wh'

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-WHR-001', musqet_webhook_secret=self.SECRET)
        self.sale = record_sale(self.env, self.method, self.SALE_ID, state='pending', rail=False)

    def _post(self, body, secret=None, read=None):
        """Post ``body`` (bytes as they are, anything else as JSON) signed; a status read it
        triggers gets ``read`` (by default, the sale complete at 9.00 on card). Returns the
        response and the stubbed API calls."""
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new((secret or self.SECRET).encode(), timestamp.encode() + b'.' + raw,
                          hashlib.sha256).hexdigest()
        data = body.get('data') if isinstance(body, dict) else None
        read = read or musqet_response(
            data['saleId'] if data else self.SALE_ID, 'COMPLETE', amountInCents=900,
            currency=self.sale.currency, rail='card')
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
            'saleId': self.SALE_ID, 'status': 'COMPLETE', 'amountInCents': 99999,
            'currency': self.sale.currency, 'rail': 'card', 'serial': 'MSQ-WHR-001',
        }}

    def assertAccepted(self, response):
        # Musqet gets the same plain answer whatever happened, never a JSON-RPC envelope.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'[accepted]')
        self.assertEqual(response.headers['Content-Type'], 'text/plain; charset=utf-8')

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

    def test_pings_no_till_of_another_company_listing_the_method(self):
        # Listed there by a direct database write: those tills don't use the method.
        other_company = self.env['res.company'].create({'name': 'Other Webhook Co'})
        env_other = self.env(context={'allowed_company_ids': other_company.ids})
        other_session = open_session(
            env_other, create_musqet_method(self, 'MSQ-WHR-B01', company_id=other_company.id))
        list_method_on_config(self.method, other_session.config_id)
        mine = open_session(self.env, self.method)
        with patch.object(type(self.env['pos.config']), '_notify', autospec=True) as notify:
            self._post(self._body())
        self.assertEqual({call.args[0].id for call in notify.mock_calls}, {mine.config_id.id})

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
        # Answered as normal; nothing to buffer, the poll covers the till.
        self.assertAccepted(response)
        self.assertIsNone(self._buffered())
        self.assertEqual(self.sale.state, 'pending')

    def test_every_answer_is_a_plain_accepted(self):
        # Signed or not, whatever the body: the answer says nothing about the request.
        jsonrpc = {'jsonrpc': '2.0', 'method': 'call', 'params': self._body(), 'id': 1}
        cases = {
            'settled': (self._body(), None),
            'unsigned': (self._body(), 'whsec_wrong'),
            'not json': (b'{"data": ', None),
            'not an object': (b'[1, 2]', None),
            'json-rpc call': (jsonrpc, None),
        }
        for name, (body, secret) in cases.items():
            with self.subTest(name), mute_logger('odoo.addons.pos_musqet.controllers.main'):
                response, _ = self._post(body, secret=secret)
                self.assertAccepted(response)
        # The signed case settled the sale; the rest only had to be answered.
        self.assertEqual(self.sale.state, 'done')

    def test_a_failing_signature_check_still_answers(self):
        # A registered serial gets the same answer as an unknown one, whatever the header.
        Method = type(self.env['pos.payment.method'])
        with patch.object(Method, '_musqet_verify_webhook', side_effect=OverflowError), \
             mute_logger('odoo.addons.pos_musqet.controllers.main'):
            response, mock_request = self._post(self._body())
        self.assertAccepted(response)
        mock_request.assert_not_called()
        self.assertEqual(self.sale.state, 'pending')

    def test_a_failed_push_still_answers(self):
        # The record is settled; only the push to the till is lost, and its poll covers it.
        Method = type(self.env['pos.payment.method'])
        with patch.object(Method, '_musqet_sessions', side_effect=Exception('boom')), \
             mute_logger('odoo.addons.pos_musqet.controllers.main'):
            response, _ = self._post(self._body())
        self.assertAccepted(response)
        self.assertEqual(self.sale.state, 'done')
        # The buffer write is undone with the ping: no result is left waiting unannounced.
        self.assertIsNone(self._buffered())

    def test_any_failure_still_answers(self):
        # One guard behind the whole handler: even the serial lookup failing is answered.
        Method = type(self.env['pos.payment.method'])
        with patch.object(Method, 'search', side_effect=ValueError('boom')), \
             mute_logger('odoo.addons.pos_musqet.controllers.main'):
            response, mock_request = self._post(self._body())
        self.assertAccepted(response)
        mock_request.assert_not_called()

    def test_a_form_body_is_dropped(self):
        # Odoo reads a form body as fields, leaving nothing to verify.
        for mimetype in ('application/x-www-form-urlencoded', 'multipart/form-data; boundary=x'):
            with self.subTest(mimetype), \
                 self.assertLogs('odoo.addons.pos_musqet.controllers.main', 'WARNING') as logs:
                response = self.url_open('/musqet/webhook', data=b'serial=MSQ-WHR-001',
                                         headers={'Content-Type': mimetype})
            self.assertAccepted(response)
            self.assertIn('sent as a form', logs.output[0])

    def test_a_serial_that_is_not_a_string_reads_nothing(self):
        # Signed, but the serial must name one terminal: a list, say, isn't looked up.
        for serial in (['MSQ-WHR-001'], {'serial': 'MSQ-WHR-001'}, 1, 'MSQ-WHR-001\x00'):
            with self.subTest(serial=serial), self.assertLogs(
                    'odoo.addons.pos_musqet.controllers.main', 'WARNING') as logs:
                body = self._body()
                body['data']['serial'] = serial
                response, mock_request = self._post(body)
                self.assertAccepted(response)
                mock_request.assert_not_called()
            # Refused by the check, not caught as a failure further on.
            self.assertIn('serial missing or not a plain string', logs.output[0])
        self.assertEqual(self.sale.state, 'pending')

    def test_an_oversized_body_is_refused_unread(self):
        body = self._body()
        body['data']['padding'] = 'x' * MUSQET_WEBHOOK_MAX_BYTES
        with mute_logger('odoo.http'):
            response, mock_request = self._post(body)
        self.assertEqual(response.status_code, 413)
        mock_request.assert_not_called()
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
