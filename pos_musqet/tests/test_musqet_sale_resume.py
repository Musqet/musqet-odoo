# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Resuming a sale already open for the order, and cancelling a sale."""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.addons.pos_musqet.models.pos_musqet_sale import (
    MUSQET_SALE_REREAD_LIMIT,
    MUSQET_SALE_STALE_AFTER,
    MUSQET_SALE_START_LOCK,
    MUSQET_SETTLED_SALE_IDS_LIMIT,
)
from odoo.sql_db import db_connect
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import SQL

from .common import (
    MusqetPosRecordsCase,
    create_musqet_method,
    make_pos_user,
    musqet_response,
    record_sale,
)


def stub_reply(sale_id, reply):
    """A stubbed response about ``sale_id`` from ``reply``: a status, or a dict of a
    ``status``, an optional ``status_code`` and any other fields."""
    if isinstance(reply, str):
        reply = {'status': reply}
    fields = dict(reply)
    return musqet_response(sale_id, fields.pop('status'), fields.pop('status_code', 200), **fields)


class FakeMusqet:
    """Stands in for requests.request: answers a status read of each saleId from
    ``statuses`` (PENDING if not listed), a create with a new saleId, and a cancel with
    ``cancel`` (see stub_reply). Records each call as (HTTP method, path)."""

    def __init__(self, statuses=None, cancel=None):
        self.statuses = statuses or {}
        self.cancel = cancel or {'status': 'CANCELLED_REMOTE'}
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = url.split('/api/v1', 1)[1]
        self.calls.append((method, path))
        if method == 'POST' and path == '/terminal/sales':
            return musqet_response('new-sale')
        sale_id = path.split('/')[3]
        if path.endswith('/cancel'):
            return stub_reply(sale_id, self.cancel)
        return stub_reply(sale_id, self.statuses.get(sale_id, 'PENDING'))

    @property
    def sent(self):
        """Whether a new sale was sent."""
        return ('POST', '/terminal/sales') in self.calls


@tagged('post_install', '-at_install')
class TestMusqetSaleResume(TransactionCase):
    """musqet_create_sale resumes a sale of the order still open at Musqet instead of
    sending a second one, so a retry can't charge the customer twice."""

    def setUp(self):
        super().setUp()
        self.cashier = make_pos_user(self.env, 'musqet_cashier_resume')
        self.method = create_musqet_method(self, 'MSQ-RES-001')
        self.other_method = create_musqet_method(self, 'MSQ-RES-002')

    def _sale(self, sale_id, state='pending', method=None, **values):
        return record_sale(
            self.env, method or self.method, sale_id, state=state,
            **{'reference': 'Order-R', 'amount_minor': 1000, 'rail': False, **values})

    def _create(self, fake, amount=1000, settled_sale_ids=None, reference='Order-R'):
        with patch.object(pos_payment_method.requests, 'request', side_effect=fake):
            return self.method.with_user(self.cashier).musqet_create_sale(
                {'amountInCents': amount, 'reference': reference}, settled_sale_ids)

    def assertResumed(self, result, fake, sale_id):
        self.assertEqual(result, {'saleId': sale_id, 'reference': 'Order-R', 'resumed': True})
        self.assertFalse(fake.sent)

    def test_resumes_a_pending_sale_of_the_order(self):
        self._sale('sale-p')
        fake = FakeMusqet()
        self.assertResumed(self._create(fake), fake, 'sale-p')
        # Read from Musqet first, in case it ended.
        self.assertIn(('GET', '/terminal/sales/sale-p'), fake.calls)

    def test_resumes_a_done_sale_not_on_the_order(self):
        # The POS gave up, or was refreshed, and the customer paid anyway.
        self._sale('sale-d', state='done', rail='card')
        fake = FakeMusqet()
        self.assertResumed(self._create(fake), fake, 'sale-d')
        # A done sale is not read again.
        self.assertEqual(fake.calls, [])

    def test_resumes_a_sale_that_completes_on_the_read(self):
        sale = self._sale('sale-p')
        fake = FakeMusqet(statuses={'sale-p': {
            'status': 'COMPLETE', 'amountInCents': 1000, 'rail': 'card',
            'currency': self.method.company_id.currency_id.name}})
        self.assertResumed(self._create(fake), fake, 'sale-p')
        self.assertEqual(sale.state, 'done')

    def test_sends_a_new_sale_when_the_open_one_is_on_the_order(self):
        # A split payment: the order's first line already carries the done sale.
        self._sale('sale-d', state='done', rail='card')
        fake = FakeMusqet()
        result = self._create(fake, settled_sale_ids=['sale-d'])
        self.assertEqual(result['saleId'], 'new-sale')
        self.assertTrue(fake.sent)

    def test_sends_a_new_sale_when_the_pending_one_has_failed(self):
        sale = self._sale('sale-p')
        fake = FakeMusqet(statuses={'sale-p': 'CANCELLED_REMOTE'})
        result = self._create(fake)
        self.assertEqual(result['saleId'], 'new-sale')
        self.assertEqual(sale.state, 'failed')

    def test_sends_a_new_sale_when_the_done_one_was_for_another_amount(self):
        self._sale('sale-d', state='done', rail='card', amount_minor=700)
        fake = FakeMusqet()
        self.assertEqual(self._create(fake)['saleId'], 'new-sale')

    def test_sends_a_new_sale_when_the_done_one_was_in_another_currency(self):
        self._sale('sale-d', state='done', rail='card', currency='XAU')
        fake = FakeMusqet()
        self.assertEqual(self._create(fake)['saleId'], 'new-sale')

    def test_refuses_while_a_sale_for_another_amount_is_in_progress(self):
        self._sale('sale-p', amount_minor=700)
        fake = FakeMusqet()
        result = self._create(fake)
        self.assertIn('still in progress', result['error']['message'])
        self.assertFalse(fake.sent)

    def test_refuses_while_a_sale_on_another_terminal_is_in_progress(self):
        self._sale('sale-p', method=self.other_method)
        fake = FakeMusqet()
        result = self._create(fake)
        self.assertIn('still in progress', result['error']['message'])
        self.assertFalse(fake.sent)

    def test_refuses_while_a_pending_sale_cannot_be_read(self):
        # Fails closed: it may still be paid.
        self._sale('sale-p', amount_minor=700)
        fake = FakeMusqet(statuses={'sale-p': {'status': 'PENDING', 'status_code': 503}})
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment_method', 'WARNING'):
            result = self._create(fake)
        self.assertIn('still in progress', result['error']['message'])
        self.assertFalse(fake.sent)

    def test_resumes_a_pending_sale_before_a_done_one(self):
        # The pending one is armed on the terminal now; the done one stays to be resumed later.
        self._sale('sale-d', state='done', rail='card')
        self._sale('sale-p')
        fake = FakeMusqet()
        self.assertResumed(self._create(fake), fake, 'sale-p')

    def test_ignores_sales_of_other_orders_and_companies(self):
        self._sale('sale-o', reference='Order-Other')
        other_company = self.env['res.company'].create({'name': 'Other Musqet Co'})
        self._sale('sale-c', company_id=other_company.id)
        fake = FakeMusqet()
        self.assertEqual(self._create(fake)['saleId'], 'new-sale')

    def test_ignores_a_sale_of_a_method_no_longer_musqet(self):
        # It can't be read, so it would block the order for good.
        self._sale('sale-p', method=self.other_method, amount_minor=700)
        self.other_method.use_payment_terminal = False
        fake = FakeMusqet()
        self.assertEqual(self._create(fake)['saleId'], 'new-sale')
        self.assertEqual(fake.calls, [('POST', '/terminal/sales')])

    def test_settled_sale_ids_of_the_wrong_type_are_ignored(self):
        self._sale('sale-d', state='done', rail='card')
        for settled in ('sale-d', [['sale-d']], [1], {'sale-d': 1}):
            with self.subTest(settled=settled):
                fake = FakeMusqet()
                self.assertResumed(self._create(fake, settled_sale_ids=settled), fake, 'sale-d')

    def test_takes_at_most_the_limit_of_settled_sale_ids(self):
        self._sale('sale-d', state='done', rail='card')
        settled = ['other-%d' % index for index in range(MUSQET_SETTLED_SALE_IDS_LIMIT)]
        fake = FakeMusqet()
        self.assertResumed(self._create(fake, settled_sale_ids=settled + ['sale-d']), fake, 'sale-d')

    def test_ignores_an_overlong_settled_sale_id(self):
        long_id = 'x' * 256
        self._sale(long_id, state='done', rail='card')
        fake = FakeMusqet()
        self.assertResumed(self._create(fake, settled_sale_ids=[long_id]), fake, long_id)

    def test_a_stale_pending_sale_no_longer_blocks_the_order(self):
        # Musqet can't be asked about it (here: it answers 404), and it is long past the
        # terminal's expiry, so it can't still be paid.
        sale = self._sale('sale-p', amount_minor=700)
        self.env.cr.execute(SQL(
            'UPDATE pos_musqet_sale SET create_date = %s WHERE id = %s',
            fields.Datetime.now() - MUSQET_SALE_STALE_AFTER - timedelta(minutes=1), sale.id))
        sale.invalidate_recordset(['create_date'])
        fake = FakeMusqet(statuses={'sale-p': {'status': 'PENDING', 'status_code': 404}})
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment_method', 'WARNING'):
            result = self._create(fake)
        self.assertEqual(result['saleId'], 'new-sale')
        self.assertEqual(sale.state, 'pending')

    def test_refuses_while_another_request_is_starting_a_sale_of_the_order(self):
        # Another transaction holds the order's lock: it is deciding, or sending, its sale.
        other = db_connect(self.env.cr.dbname).cursor()
        self.addCleanup(other.close)
        other.execute(SQL(
            'SELECT pg_advisory_xact_lock(%s, hashtext(%s))',
            MUSQET_SALE_START_LOCK, '%s:%s' % (self.method.company_id.id, 'Order-R')))
        fake = FakeMusqet()
        result = self._create(fake)
        self.assertIn('already being started', result['error']['message'])
        self.assertFalse(fake.sent)
        other.rollback()
        # Once it is done, the order is free again.
        self.assertEqual(self._create(fake)['saleId'], 'new-sale')

    def test_the_lock_is_per_order(self):
        other = db_connect(self.env.cr.dbname).cursor()
        self.addCleanup(other.close)
        other.execute(SQL(
            'SELECT pg_advisory_xact_lock(%s, hashtext(%s))',
            MUSQET_SALE_START_LOCK, '%s:%s' % (self.method.company_id.id, 'Order-Other')))
        fake = FakeMusqet()
        self.assertEqual(self._create(fake)['saleId'], 'new-sale')

    def test_reads_at_most_the_limit_of_pending_sales(self):
        for index in range(MUSQET_SALE_REREAD_LIMIT + 2):
            self._sale('sale-%d' % index, amount_minor=700)
        fake = FakeMusqet()
        self._create(fake)
        self.assertEqual(len([call for call in fake.calls if call[0] == 'GET']),
                         MUSQET_SALE_REREAD_LIMIT)


@tagged('post_install', '-at_install')
class TestMusqetCancelSale(TransactionCase):
    """musqet_cancel_sale fails the server's record of a sale Musqet cancelled."""

    def setUp(self):
        super().setUp()
        self.cashier = make_pos_user(self.env, 'musqet_cashier_cancel')
        self.method = create_musqet_method(self, 'MSQ-CAN-001')
        self.sale = record_sale(self.env, self.method, 'sale-c', state='pending', rail=False)

    def _cancel(self, **cancel):
        fake = FakeMusqet(cancel=cancel)
        with patch.object(pos_payment_method.requests, 'request', side_effect=fake):
            return self.method.with_user(self.cashier).musqet_cancel_sale('sale-c')

    def test_a_cancelled_sale_is_failed(self):
        result = self._cancel(status='CANCELLED_REMOTE')
        self.assertEqual(result['status'], 'CANCELLED_REMOTE')
        self.assertEqual(self.sale.state, 'failed')

    def test_a_refused_cancel_leaves_the_sale_pending(self):
        # 409 CONFLICT: the sale had already left a cancellable state.
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment_method', 'WARNING'):
            result = self._cancel(status='CANCELLED_REMOTE', status_code=409)
        self.assertIn('error', result)
        self.assertEqual(self.sale.state, 'pending')

    def test_a_cancel_reply_about_another_sale_changes_nothing(self):
        # Neither the record nor the till: the POS gets an error, not a cancel to release on.
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment_method', 'WARNING'):
            result = self._cancel(status='CANCELLED_REMOTE', saleId='sale-other')
        self.assertEqual(self.sale.state, 'pending')
        self.assertIn('different sale', result['error']['message'])


@tagged('post_install', '-at_install')
class TestMusqetSaleResumeSynced(MusqetPosRecordsCase):
    """A sale already on a synced payment line is never resumed, whatever the POS sends."""

    def test_a_sale_on_a_synced_line_is_not_resumed(self):
        record_sale(self.env, self.method, 'sale-s', reference='Order-R', rail='card')
        self._payment(amount=10.0, transaction_id='sale-s', musqet_rail='card')
        fake = FakeMusqet()
        with patch.object(pos_payment_method.requests, 'request', side_effect=fake):
            result = self.method.musqet_create_sale({'amountInCents': 1000, 'reference': 'Order-R'})
        self.assertEqual(result['saleId'], 'new-sale')

    def test_a_line_takes_only_the_sale_of_its_own_method(self):
        # The line carries sale-t on self.method; the pending sale-t is on self.second, so it
        # is still open (and still blocks), not on a line.
        record_sale(self.env, self.method, 'sale-s', reference='Order-R', rail='card')
        record_sale(self.env, self.second, 'sale-t', reference='Order-R', state='pending', rail=False)
        self._payment(amount=10.0, transaction_id='sale-t')
        fake = FakeMusqet()
        with patch.object(pos_payment_method.requests, 'request', side_effect=fake):
            result = self.method.musqet_create_sale({'amountInCents': 1000, 'reference': 'Order-R'})
        self.assertIn('still in progress', result['error']['message'])

    def test_a_line_of_another_method_with_the_sale_id_does_not_count(self):
        record_sale(self.env, self.method, 'sale-s', reference='Order-R', rail='card')
        self._payment(amount=10.0, transaction_id='sale-s', payment_method_id=self.second.id)
        fake = FakeMusqet()
        with patch.object(pos_payment_method.requests, 'request', side_effect=fake):
            result = self.method.musqet_create_sale({'amountInCents': 1000, 'reference': 'Order-R'})
        self.assertEqual(result, {'saleId': 'sale-s', 'reference': 'Order-R', 'resumed': True})
