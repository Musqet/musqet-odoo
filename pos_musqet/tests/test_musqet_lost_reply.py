# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""A sale or refund request whose reply never reached Odoo: recorded unanswered, failing closed."""
from http.client import RemoteDisconnected
from unittest.mock import MagicMock, patch

import requests
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NewConnectionError,
    ProtocolError,
)

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from .common import (
    MusqetPosRecordsCase,
    create_musqet_method,
    make_pos_user,
    musqet_response,
    record_sale,
)

RECORD_LOGGER = 'odoo.addons.pos_musqet.models.pos_musqet_record'
PROXY_LOGGER = 'odoo.addons.pos_musqet.models.pos_payment_method'
# The words of the message refusing an order with an unanswered sale or refund.
UNANSWERED = 'did not get a clear answer from Musqet'


def wrapped(reason):
    """A requests ConnectionError as requests raises it: wrapping urllib3's MaxRetryError."""
    return requests.exceptions.ConnectionError(MaxRetryError(None, '/terminal/sales', reason))


def reply(status_code=200, body=None, unreadable=False):
    """A stubbed HTTP response: ``body`` as JSON, or a body that isn't JSON."""
    response = MagicMock(status_code=status_code, text='<html>')
    if unreadable:
        response.json.side_effect = ValueError('not JSON')
    else:
        response.json.return_value = body
    return response


# Requests that may have reached Musqet with the reply lost: (label, raised error or reply).
LOST = [
    ('read timeout', requests.exceptions.ReadTimeout('read timed out')),
    ('dropped connection', requests.exceptions.ConnectionError(
        ProtocolError('Connection aborted.', RemoteDisconnected('closed')))),
    ('body cut short', requests.exceptions.ChunkedEncodingError('incomplete read')),
    ('500', reply(500, {'error': {'code': 500}})),
    ('504 without JSON', reply(504, unreadable=True)),
    ('2xx without JSON', reply(200, unreadable=True)),
    ('2xx without a saleId', reply(200, {'status': 'PENDING'})),
    ('2xx with an empty saleId', reply(200, {'saleId': '', 'status': 'PENDING'})),
    ('2xx not an object', reply(200, ['PENDING'])),
]
# Requests Musqet refused or never got: (label, raised error or reply).
NOT_ACCEPTED = [
    ('connect timeout', requests.exceptions.ConnectTimeout(
        MaxRetryError(None, '/terminal/sales', ConnectTimeoutError('timed out')))),
    ('connection refused', wrapped(NewConnectionError(None, 'refused'))),
    ('TLS failure', requests.exceptions.SSLError('handshake failed')),
    ('proxy failure', requests.exceptions.ProxyError('proxy refused')),
    ('409 terminal busy', reply(409, {'error': {'code': 409, 'message': 'busy'}})),
    ('400', reply(400, {'error': {'code': 400}})),
    ('3xx not followed', reply(302, unreadable=True)),
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

    def test_records_nothing_when_musqet_refused_or_never_got_the_request(self):
        for index, (label, outcome) in enumerate(NOT_ACCEPTED):
            reference = 'Order-N%d' % index
            with self.subTest(label), mute_logger(PROXY_LOGGER):
                result, _stub = self._create(outcome, reference)
                self.assertNotIn(UNANSWERED, result['error']['message'])
                self.assertFalse(self._sales(reference))

    def test_refuses_the_order_until_the_sale_is_cleared(self):
        self._lose()
        # Whatever the amount or method, and before reading anything from Musqet.
        for amount, method in ((1000, self.method), (500, self.method), (1000, self.other_method)):
            with self.subTest(amount=amount, method=method.name):
                result, stub = self._create(musqet_response('new-sale'), amount=amount, method=method)
                self.assertIn(UNANSWERED, result['error']['message'])
                stub.assert_not_called()
        # Another order is not held up.
        result, stub = self._create(musqet_response('sale-other'), reference='Order-M')
        self.assertEqual(result['saleId'], 'sale-other')
        # Once a manager has checked it in the Musqet app and marked it failed, the order may
        # take a new sale.
        self._sales().with_user(self.manager).action_musqet_mark_failed()
        self.assertEqual(self._sales().state, 'failed')
        result, stub = self._create(musqet_response('sale-retry'))
        self.assertEqual(result['saleId'], 'sale-retry')
        self.assertEqual(len(stub.mock_calls), 1)

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

    def test_a_cashier_cannot_mark_a_sale_failed(self):
        self._lose()
        with self.assertRaises(AccessError):
            self._sales().with_user(self.cashier).action_musqet_mark_failed()
        self.assertEqual(self._sales().state, 'pending')

    def test_a_read_failure_carries_no_unknown_outcome_to_a_status_read(self):
        # A status read whose reply is lost changes nothing: reads are safe to repeat, and an
        # unanswered record comes only from a create.
        record_sale(self.env, self.method, 'sale-p', state='pending', rail=False, reference='Order-L')
        stub = MagicMock(side_effect=requests.exceptions.ReadTimeout('timed out'))
        with patch.object(pos_payment_method.requests, 'request', stub), \
             self.assertLogs(PROXY_LOGGER, 'WARNING'):
            result = self.method.with_user(self.cashier).musqet_get_sale('sale-p')
        self.assertTrue(result['error']['outcome_unknown'])
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
