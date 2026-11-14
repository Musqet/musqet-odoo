# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""A sale or refund request whose reply never reached Odoo: recorded unanswered, failing closed."""
import ssl
from http.client import RemoteDisconnected
from unittest.mock import MagicMock, patch

import requests
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NewConnectionError,
    ProtocolError,
    ProxyError,
    SSLError,
)

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from .common import (
    PROXY_LOGGER,
    RECORD_LOGGER,
    UNANSWERED,
    MusqetPosRecordsCase,
    create_musqet_method,
    make_pos_user,
    musqet_response,
    open_session,
    record_sale,
)


def wrapped(reason, error=requests.exceptions.ConnectionError):
    """A requests ``error`` as requests raises it: wrapping urllib3's MaxRetryError, whose
    reason is urllib3's own error."""
    return error(MaxRetryError(None, '/terminal/sales', reason))


def reply(status_code=200, body=None, unreadable=False):
    """A stubbed HTTP response: ``body`` as JSON, or a body that isn't JSON (``unreadable``:
    True, or the error parsing it raises)."""
    response = MagicMock(status_code=status_code, text='<html>')
    if unreadable:
        response.json.side_effect = (
            unreadable if isinstance(unreadable, Exception) else ValueError('not JSON'))
    else:
        response.json.return_value = body
    return response


# Requests that may have reached Musqet with the reply lost: (label, raised error or reply).
LOST = [
    ('read timeout', requests.exceptions.ReadTimeout('read timed out')),
    ('dropped connection', requests.exceptions.ConnectionError(
        ProtocolError('Connection aborted.', RemoteDisconnected('closed')))),
    ('body cut short', requests.exceptions.ChunkedEncodingError('incomplete read')),
    ('TLS failure mid-response', wrapped(
        SSLError(ssl.SSLError('bad record mac')), requests.exceptions.SSLError)),
    ('TLS failure reading the body', requests.exceptions.SSLError('read failed')),
    ('proxy connection reset', wrapped(
        ProxyError('Cannot connect to proxy.', ConnectionResetError('reset')),
        requests.exceptions.ProxyError)),
    ('500', reply(500, {'error': {'code': 500}})),
    ('504 without JSON', reply(504, unreadable=True)),
    ('502 nested too deep to parse', reply(502, unreadable=RecursionError('too deep'))),
    ('2xx without JSON', reply(200, unreadable=True)),
    ('408 from a gateway', reply(408, unreadable=True)),
    ('429 from a gateway', reply(429, unreadable=True)),
    ('3xx not followed', reply(302, unreadable=True)),
    ('2xx nested too deep to parse', reply(200, unreadable=RecursionError('too deep'))),
    ('2xx without a saleId', reply(200, {'status': 'PENDING'})),
    ('2xx with an empty saleId', reply(200, {'saleId': '', 'status': 'PENDING'})),
    ('2xx not an object', reply(200, ['PENDING'])),
    ('2xx with an error message', reply(200, {'error': 'upstream timeout'})),
    ('2xx with an error object', reply(200, {'error': {'code': 504}})),
]
# Requests Musqet refused or never got: (label, raised error or reply).
NOT_ACCEPTED = [
    ('connect timeout', requests.exceptions.ConnectTimeout(
        MaxRetryError(None, '/terminal/sales', ConnectTimeoutError('timed out')))),
    ('connection refused', wrapped(NewConnectionError(None, 'refused'))),
    ('certificate not verified', wrapped(
        SSLError(ssl.SSLCertVerificationError(1, 'certificate verify failed')),
        requests.exceptions.SSLError)),
    ('proxy refused', wrapped(
        ProxyError('Unable to connect to proxy', NewConnectionError(None, 'refused')),
        requests.exceptions.ProxyError)),
    ('409 terminal busy', reply(409, {'error': {'code': 409, 'message': 'busy'}})),
    ('400', reply(400, {'error': {'code': 400}})),
    ('401 bad key', reply(401, {'error': {'code': 401}})),
    ('403', reply(403, {'error': {'code': 403}})),
    ('404 wrong URL', reply(404, unreadable=True)),
    ('422', reply(422, {'error': {'code': 422}})),
]


def answer(outcome):
    """A requests.request stand-in whose create raises or returns ``outcome``, and whose
    status reads answer PENDING."""
    def request(method, url, **kwargs):
        if method == 'POST':
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return musqet_response(url.rsplit('/', 1)[1])
    return MagicMock(side_effect=request)


@tagged('post_install', '-at_install')
class TestMusqetLostSaleReply(TransactionCase):
    """musqet_create_sale records a sale whose reply was lost, and refuses its order until a
    manager clears it."""

    def setUp(self):
        super().setUp()
        self.cashier = make_pos_user(self.env, 'musqet_cashier_lost')
        self.manager = make_pos_user(
            self.env, 'musqet_manager_lost', group='point_of_sale.group_pos_manager')
        self.method = create_musqet_method(self, 'MSQ-LOST-001')
        self.other_method = create_musqet_method(self, 'MSQ-LOST-002')
        open_session(self.env, self.method, self.other_method)

    def _create(self, outcome, reference='Order-L', amount=1000, method=None):
        """Start a sale of order ``reference``, its create answered by ``outcome``; return the
        reply and the request stub."""
        stub = answer(outcome)
        with patch.object(pos_payment_method.requests, 'request', stub):
            result = (method or self.method).with_user(self.cashier).musqet_create_sale(
                {'amountInCents': amount, 'reference': reference})
        return result, stub

    def _sales(self, reference='Order-L'):
        return self.env['pos.musqet.sale'].search([('reference', '=', reference)])

    def _lose(self, reference='Order-L'):
        """Start a sale of order ``reference`` whose reply times out."""
        with self.assertLogs(PROXY_LOGGER, 'WARNING'), self.assertLogs(RECORD_LOGGER, 'WARNING'):
            return self._create(requests.exceptions.ReadTimeout('timed out'), reference)[0]

    def test_records_a_sale_whose_reply_was_lost(self):
        for index, (label, outcome) in enumerate(LOST):
            reference = 'Order-L%d' % index
            with self.subTest(label), self.assertLogs(RECORD_LOGGER, 'WARNING') as logs, \
                 mute_logger(PROXY_LOGGER):
                result, _stub = self._create(outcome, reference)
                self.assertIn(UNANSWERED, result['error']['message'])
                sale = self._sales(reference)
                self.assertEqual(len(sale), 1)
                self.assertFalse(sale.sale_id)
                self.assertEqual(sale.state, 'pending')
                self.assertEqual(sale.amount_minor, 1000)
                self.assertEqual(sale.payment_method_id, self.method)
                self.assertEqual(sale.company_id, self.method.company_id)
                # The log is the trail for reconciling it.
                self.assertIn(reference, logs.output[-1])

    def test_a_null_error_in_an_accepted_reply_is_no_error(self):
        accepted = reply(200, {'saleId': 'sale-ok', 'status': 'PENDING', 'error': None})
        result, _stub = self._create(accepted)
        self.assertEqual(result['saleId'], 'sale-ok')
        self.assertEqual(self._sales().sale_id, 'sale-ok')

    def test_a_null_error_in_a_status_read_is_no_error(self):
        sale = record_sale(self.env, self.method, 'sale-r', state='pending', rail=False)

        def read(sale_id):
            stub = MagicMock(return_value=reply(
                200, {'saleId': sale_id, 'status': 'CANCELLED', 'error': None}))
            with patch.object(pos_payment_method.requests, 'request', stub):
                return self.method.with_user(self.cashier).musqet_get_sale('sale-r')

        # A reply about another sale still reaches neither the record nor the till.
        with mute_logger(PROXY_LOGGER):
            self.assertIn('different sale', read('sale-other')['error']['message'])
        self.assertEqual(sale.state, 'pending')
        self.assertEqual(read('sale-r'), {'saleId': 'sale-r', 'status': 'CANCELLED'})
        self.assertEqual(sale.state, 'failed')

    def test_only_a_refusal_in_musqets_shape_reaches_the_till(self):
        # A gateway's page (an unknown outcome) stays out of the reply, which reaches the
        # cashier's browser; a refusal in Musqet's error shape keeps only an integer code
        # (reply_is_not_found reads it) and a capped message.
        record_sale(self.env, self.method, 'sale-r', state='pending', rail=False)
        cases = (
            (reply(502, {'error': {'code': 502}}), False),
            (reply(404, {'error': {'code': 404}}), True),
            # A refusal status, but not Musqet's body (a wrong base URL, a WAF's page).
            (reply(404, unreadable=True), False),
            (reply(403, {'message': 'blocked'}), False),
        )
        for response, has_detail in cases:
            with self.subTest(status_code=response.status_code), mute_logger(PROXY_LOGGER):
                stub = MagicMock(return_value=response)
                with patch.object(pos_payment_method.requests, 'request', stub):
                    result = self.method.with_user(self.cashier).musqet_get_sale('sale-r')
                self.assertEqual('detail' in result['error'], has_detail)
        stub = MagicMock(return_value=reply(404, {
            'error': {'code': 404, 'message': 'Terminal sale not found.', 'trace': ['x']},
            'requestId': 'r-1'}))
        with patch.object(pos_payment_method.requests, 'request', stub), mute_logger(PROXY_LOGGER):
            result = self.method.with_user(self.cashier).musqet_get_sale('sale-r')
        self.assertEqual(result['error']['detail'],
                         {'error': {'code': 404, 'message': 'Terminal sale not found.'}})
        # A boolean is no code, and a long message is capped.
        stub = MagicMock(return_value=reply(404, {'error': {'code': True, 'message': 'x' * 5000}}))
        with patch.object(pos_payment_method.requests, 'request', stub), mute_logger(PROXY_LOGGER):
            result = self.method.with_user(self.cashier).musqet_get_sale('sale-r')
        self.assertEqual(result['error']['detail'], {'error': {
            'message': 'x' * pos_payment_method.MUSQET_REFUSAL_MESSAGE_MAX_LENGTH}})

    def test_records_nothing_when_musqet_refused_or_never_got_the_request(self):
        for index, (label, outcome) in enumerate(NOT_ACCEPTED):
            reference = 'Order-N%d' % index
            with self.subTest(label), mute_logger(PROXY_LOGGER):
                result, _stub = self._create(outcome, reference)
                self.assertNotIn(UNANSWERED, result['error']['message'])
                self.assertFalse(self._sales(reference))

    def test_says_so_when_an_unanswered_sale_cannot_be_recorded(self):
        Sale = type(self.env['pos.musqet.sale'])
        with patch.object(Sale, 'create', side_effect=ValueError('cannot save')), \
             mute_logger(PROXY_LOGGER), self.assertLogs(RECORD_LOGGER, 'ERROR') as logs:
            result, _stub = self._create(requests.exceptions.ReadTimeout('timed out'))
        # Nothing blocks a retry, so the cashier isn't told a manager must clear it.
        self.assertIn('could not record it', result['error']['message'])
        self.assertNotIn('A manager must', result['error']['message'])
        self.assertFalse(self._sales())
        self.assertIn('Order-L', logs.output[0])

    def test_refuses_the_order_until_the_sale_is_cleared(self):
        self._lose()
        # Whatever the amount or method, and before reading anything from Musqet.
        for amount, method in ((1000, self.method), (500, self.method), (1000, self.other_method)):
            with self.subTest(amount=amount, method=method.name):
                result, stub = self._create(musqet_response('new-sale'), amount=amount, method=method)
                self.assertIn(UNANSWERED, result['error']['message'])
                stub.assert_not_called()
        # Another order is not held up.
        result, _stub = self._create(musqet_response('sale-other'), reference='Order-M')
        self.assertEqual(result['saleId'], 'sale-other')
        # Once a manager has checked it in the Musqet app and marked it failed, the order may
        # take a new sale.
        self._sales().with_user(self.manager).action_musqet_mark_failed()
        self.assertEqual(self._sales().state, 'failed')
        result, stub = self._create(musqet_response('sale-retry'))
        self.assertEqual(result['saleId'], 'sale-retry')
        self.assertEqual(len(stub.mock_calls), 1)

    def test_still_refuses_once_its_method_is_no_longer_musqet(self):
        # Unlike a sale with a saleId, an unanswered one is never read, so its method's
        # configuration doesn't matter.
        self._lose()
        self.method.write({'payment_provider': False})
        result, stub = self._create(musqet_response('new-sale'), method=self.other_method)
        self.assertIn(UNANSWERED, result['error']['message'])
        stub.assert_not_called()

    def test_refuses_before_reading_a_pending_sale_of_the_order(self):
        # A pending sale with a saleId would be read, and resumed; the unanswered one wins.
        self._lose()
        record_sale(self.env, self.method, 'sale-p', state='pending', rail=False, reference='Order-L')
        result, stub = self._create(musqet_response('new-sale'))
        self.assertIn(UNANSWERED, result['error']['message'])
        stub.assert_not_called()

    def test_only_an_unanswered_pending_sale_may_be_marked_failed(self):
        self._lose()
        lost = self._sales()
        pending = record_sale(self.env, self.method, 'sale-p', state='pending', rail=False)
        done = record_sale(self.env, self.method, 'sale-d')
        (lost | pending | done).with_user(self.manager).action_musqet_mark_failed()
        self.assertEqual(lost.state, 'failed')
        # A sale with a saleId is settled only by Musqet's replies: marking it failed could
        # hide a sale the customer paid.
        self.assertEqual(pending.state, 'pending')
        self.assertEqual(done.state, 'done')

    def test_logs_who_marked_a_sale_failed(self):
        self._lose()
        with self.assertLogs(RECORD_LOGGER, 'INFO') as logs:
            self._sales().with_user(self.manager).action_musqet_mark_failed()
        self.assertIn('Order-L', logs.output[0])
        self.assertIn('user #%d' % self.manager.id, logs.output[0])

    def test_a_cashier_cannot_mark_a_sale_failed(self):
        self._lose()
        with self.assertRaises(AccessError):
            self._sales().with_user(self.cashier).action_musqet_mark_failed()
        self.assertEqual(self._sales().state, 'pending')

    def test_a_lost_status_read_records_nothing(self):
        # A status read whose reply is lost changes nothing: reads are safe to repeat, and an
        # unanswered record comes only from a create.
        record_sale(self.env, self.method, 'sale-p', state='pending', rail=False, reference='Order-L')
        stub = MagicMock(side_effect=requests.exceptions.ReadTimeout('timed out'))
        with patch.object(pos_payment_method.requests, 'request', stub), \
             self.assertLogs(PROXY_LOGGER, 'WARNING'):
            result = self.method.with_user(self.cashier).musqet_get_sale('sale-p')
        self.assertTrue(result['error']['outcome_unknown'])
        # Only the error's type reaches the browser; its text (the URL) stays in the log.
        self.assertEqual(result['error']['exception'], 'ReadTimeout')
        self.assertEqual(self._sales().mapped('sale_id'), ['sale-p'])


@tagged('post_install', '-at_install')
class TestMusqetLostRecordsAmongPayments(MusqetPosRecordsCase):
    """An unanswered record's empty saleId doesn't match the payments that have none."""

    def test_a_payment_with_no_saleid_does_not_hide_an_unanswered_sale(self):
        # A synced Musqet line with no saleId (a line the POS never got one for).
        self._payment(amount=10.0, transaction_id=False)
        lost = record_sale(self.env, self.method, False, state='pending', reference='Order-L')
        open_sales = self.env['pos.musqet.sale']._musqet_open_sales_of(self.method.company_id, 'Order-L')
        self.assertEqual(open_sales, lost)

    def test_an_unanswered_refund_names_no_payment_with_no_saleid(self):
        original = self._original('sale-orig-1')
        self._payment(amount=-1.0, transaction_id=False)
        refunds = self._record_refund(False, reference='REFUND-LOST')
        states = refunds._musqet_reference_states(self.env['pos.musqet.sale'].search(
            [('sale_id', '=', original.transaction_id)]))
        # Only the reference it was sent with: not the order of the line with no saleId.
        self.assertEqual(states, {'REFUND-LOST': 'pending'})
