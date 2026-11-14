# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Refunds against real POS payments: the link, the claim, the gate and access."""
from unittest.mock import patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import (
    MusqetPosRecordsCase,
    create_plain_method,
    make_pos_user,
    musqet_response,
    record_refund,
    record_sale,
)


@tagged('post_install', '-at_install')
class TestMusqetRefundWithPosRecords(MusqetPosRecordsCase):
    """Refund records against real POS payments: the link, the claim, the gate and access.

    A synced refund payment takes its musqet_refund_of from the server's refund record: the
    POS can only set the link while it still holds the refund's original in memory, so a
    refund settled after a page refresh used to sync with no link at all.
    """

    def test_create_links_a_recorded_refund(self):
        # No link from the POS (the page was refreshed), and a wrong one, both end up right.
        for sent in (False, 'sale-forged'):
            with self.subTest(sent=sent):
                payment = self._payment(transaction_id='refund-1', musqet_refund_of=sent)
                self.assertEqual(payment.musqet_refund_of, 'sale-orig-1')

    def test_write_links_a_recorded_refund(self):
        payment = self._payment(transaction_id='other')
        payment.transaction_id = 'refund-1'
        self.assertEqual(payment.musqet_refund_of, 'sale-orig-1')
        payment.musqet_refund_of = 'sale-forged'
        self.assertEqual(payment.musqet_refund_of, 'sale-orig-1')

    def test_leaves_a_payment_without_a_record_alone(self):
        payment = self._payment(transaction_id='refund-unknown', musqet_refund_of='sale-x')
        self.assertEqual(payment.musqet_refund_of, 'sale-x')

    def test_link_ignores_a_non_musqet_method(self):
        record_refund(self.env, self.plain, 'refund-plain', amount_minor=500)
        payment = self._payment(payment_method_id=self.plain.id, transaction_id='refund-plain')
        self.assertFalse(payment.musqet_refund_of)

    def test_refund_claims_every_record_of_the_original_sale(self):
        # A concurrent refund of the same sale whose snapshot predates this one's record must
        # fail on these rows and be retried, so the refund must update them: every sale
        # record with the saleId in the company, the key the cap counts refunds by, even one
        # on another method. Run it in a savepoint: an update there gives a row a new
        # version (xmin) from the savepoint's own transaction id, while a read leaves it.
        original = self._original('sale-orig-real')
        other = record_sale(self.env, self.second, 'sale-orig-real')
        sale = self.env['pos.musqet.sale'].search(
            [('sale_id', '=', 'sale-orig-real'), ('payment_method_id', '=', self.method.id)])
        before = {record: self._row_version(record) for record in (sale, other)}
        with self.env.cr.savepoint(), \
             patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-real')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-REAL'}, original.id)
        self.assertNotIn('error', result)
        for record, version in before.items():
            self.assertNotEqual(self._row_version(record), version)
        refund = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-real')])
        self.assertEqual(refund.original_sale_id, 'sale-orig-real')

    def test_reads_pending_refunds_before_locking_the_sale(self):
        # The slow calls to Musqet must not run while this request holds the sale's row.
        original = self._original('sale-orig-rr')
        record_refund(self.env, self.method, 'refund-rr', original_sale_id='sale-orig-rr',
                      amount_minor=100)
        sale = self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-rr')])
        versions_at_read = []

        def fake_request(method, url, **kwargs):
            if method == 'GET':
                versions_at_read.append(self._row_version(sale))
                return musqet_response('refund-rr', 'PROCESSING')
            return musqet_response('refund-new')

        before = self._row_version(sale)
        with self.env.cr.savepoint(), \
             patch.object(pos_payment_method.requests, 'request', side_effect=fake_request):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-RR'}, original.id)
            self.assertNotIn('error', result)
            self.assertEqual(versions_at_read, [before])
            self.assertNotEqual(self._row_version(sale), before)

    def test_refunds_a_sale_whose_result_was_never_read(self):
        # The POS closed before reading the sale's result: its record is pending and its line
        # Not verified. The refund reads the sale from Musqet, which settles both.
        self._record_sale('sale-unread', state='pending')
        line = self._payment(amount=10.0, transaction_id='sale-unread')
        self.assertEqual(line.musqet_settle_check, 'unverified')
        currency = self.method.company_id.currency_id.name
        replies = [
            musqet_response('sale-unread', 'COMPLETE', amountInCents=1000, currency=currency,
                            rail='card'),
            musqet_response('refund-unread'),
        ]
        with patch.object(pos_payment_method.requests, 'request', side_effect=replies):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-UNREAD'}, line.id)
        self.assertNotIn('error', result)
        self.assertEqual(line.musqet_settle_check, 'verified')

    def test_refunds_a_line_synced_before_the_check_existed(self):
        line = self._original('sale-old')
        # Blank, as the upgrade leaves it: written behind the ORM, after its own writes.
        line.flush_recordset()
        self.env.cr.execute('UPDATE pos_payment SET musqet_settle_check = NULL WHERE id = %s', [line.id])
        line.invalidate_recordset()
        self.assertFalse(line.musqet_settle_check)
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-old')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-OLD'}, line.id)
        self.assertNotIn('error', result)
        self.assertEqual(line.musqet_settle_check, 'verified')

    def test_refuses_a_refund_line_named_as_the_original(self):
        self._original('sale-orig-neg')
        refund_line = self._payment(amount=-5.0, transaction_id='sale-orig-neg')
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-NEG'}, refund_line.id)
        self.assertIn('has not confirmed', result['error']['message'])
        mock_request.assert_not_called()

    def test_refuses_a_refund_by_the_other_form_of_its_order_reference(self):
        # A backfilled record holds one form of the reference; the refund's synced payment
        # links it to its order, whose other form must be caught too. Real rows, so the
        # payment search's filter is exercised.
        original = self._original('sale-orig-ref')
        refund_order = self._order()
        self._payment(pos_order_id=refund_order.id, transaction_id='refund-ref',
                      musqet_refund_of='sale-orig-ref')
        record_refund(self.env, self.method, 'refund-ref', original_sale_id='sale-orig-ref',
                      amount_minor=200, reference='Order 00001-001-0001', state='done')
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': refund_order.uuid}, original.id)
        self.assertIn('already been processed', result['error']['message'])
        mock_request.assert_not_called()
        # Another order's reference is not a duplicate, though its payments are in the table.
        fake = musqet_response('refund-new')
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': self.order.uuid}, original.id)
        self.assertNotIn('error', result)

    def test_a_payment_of_another_method_sharing_a_refund_sale_id_is_not_that_refund(self):
        # A payment line on another order, of another method, that happens to carry a
        # recorded refund's saleId must not make that order's refund a duplicate.
        original = self._original('sale-orig-m')
        record_refund(self.env, self.method, 'refund-m', original_sale_id='sale-orig-m', state='done')
        other_order = self._order()
        self._payment(pos_order_id=other_order.id, payment_method_id=self.plain.id,
                      transaction_id='refund-m')
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-new')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': other_order.uuid}, original.id)
        self.assertNotIn('error', result)

    def test_a_synced_refund_of_another_method_counts_beside_a_record_sharing_its_sale_id(self):
        # The record (this method, saleId X) is a different refund from a synced payment of
        # another Musqet method that also carries saleId X: both count.
        original = self._original('sale-orig-x')
        record_refund(self.env, self.method, 'refund-x', original_sale_id='sale-orig-x',
                      amount_minor=600, state='done')
        self._payment(payment_method_id=self.second.id, transaction_id='refund-x',
                      musqet_refund_of='sale-orig-x', amount=-6.0)
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_create_refund(
                {'amountInCents': 1, 'reference': 'R-X'}, original.id)
        self.assertIn('exceeds', result['error']['message'])
        mock_request.assert_not_called()

    def test_a_non_musqet_payment_claiming_to_refund_the_sale_does_not_count(self):
        original = self._original('sale-orig-c')
        self._payment(payment_method_id=self.plain.id, transaction_id='cash-1',
                      musqet_refund_of='sale-orig-c', amount=-10.0)
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-c')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 1000, 'reference': 'R-C'}, original.id)
        self.assertNotIn('error', result)

    def test_a_musqet_payment_without_a_sale_id_does_not_count(self):
        # A settled Musqet refund always has a saleId, so a line without one isn't a refund.
        original = self._original('sale-orig-n')
        self._payment(musqet_refund_of='sale-orig-n', amount=-10.0)
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-n')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 1000, 'reference': 'R-N'}, original.id)
        self.assertNotIn('error', result)

    def test_neither_line_of_a_copied_sale_can_be_refunded(self):
        # Two paid lines claiming a sale whose record names no order (the upgrade made it from
        # lines sharing the saleId): both are Not verified, so a refund naming either is
        # refused until the extra line is gone.
        record_sale(self.env, self.method, 'sale-orig-dup')
        genuine = self._payment(amount=10.0, transaction_id='sale-orig-dup')
        twin = self._payment(pos_order_id=self._order().id, amount=10.0,
                             transaction_id='sale-orig-dup')
        for line in (genuine, twin):
            with self.subTest(line=line.id), \
                 patch.object(pos_payment_method.requests, 'request') as mock_request:
                result = self.method.musqet_create_refund(
                    {'amountInCents': 500, 'reference': 'R-DUP'}, line.id)
                self.assertIn('has not confirmed', result['error']['message'])
                mock_request.assert_not_called()
        twin.unlink()
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-dup')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-DUP'}, genuine.id)
        self.assertNotIn('error', result)

    def test_manager_sees_only_their_companies_refunds(self):
        other = self.env['res.company'].create({'name': 'Other Co'})
        plain_other = create_plain_method(self.env, 'Other Method', company_id=other.id)
        record_refund(self.env, plain_other, 'refund-other', original_sale_id='sale-other')
        manager = make_pos_user(self.env, 'musqet_manager', group='point_of_sale.group_pos_manager', company_id=self.method.company_id.id,
            company_ids=[(6, 0, self.method.company_id.ids)])
        seen = self.env['pos.musqet.refund'].with_user(manager).search([])
        self.assertEqual(seen.mapped('sale_id'), ['refund-1'])

    def test_manager_sees_only_their_companies_sales(self):
        other = self.env['res.company'].create({'name': 'Other Co'})
        plain_other = create_plain_method(self.env, 'Other Method', company_id=other.id)
        record_sale(self.env, plain_other, 'sale-other')
        record_sale(self.env, self.method, 'sale-mine')
        manager = make_pos_user(
            self.env, 'musqet_manager_sales', group='point_of_sale.group_pos_manager',
            company_id=self.method.company_id.id, company_ids=[(6, 0, self.method.company_id.ids)])
        seen = self.env['pos.musqet.sale'].with_user(manager).search([])
        self.assertEqual(seen.mapped('sale_id'), ['sale-mine'])

    def test_manager_only_reads_sale_and_refund_records(self):
        # The company restriction in ir.access covers every operation, but grants none: a
        # manager's own access stays read-only, so records change only through the server.
        manager = make_pos_user(self.env, 'musqet_manager_ro', group='point_of_sale.group_pos_manager')
        sale = record_sale(self.env, self.method, 'sale-ro')
        refund = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        for record, vals in ((sale, {'sale_id': 'sale-forged', 'company_id': sale.company_id.id,
                                     'payment_method_id': self.method.id}),
                             (refund, {'sale_id': 'refund-forged', 'original_sale_id': 'sale-ro',
                                       'company_id': refund.company_id.id,
                                       'payment_method_id': self.method.id})):
            Model = record.with_user(manager)
            with self.subTest(model=record._name):
                self.assertTrue(Model.search([('id', '=', record.id)]))
                with self.assertRaises(AccessError):
                    Model.write({'state': 'done'})
                with self.assertRaises(AccessError):
                    Model.create(vals)
                with self.assertRaises(AccessError):
                    Model.unlink()

    def test_manager_can_mark_a_stuck_pending_refund_failed(self):
        manager = make_pos_user(self.env, 'musqet_manager_fail', group='point_of_sale.group_pos_manager')
        stuck = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        done = record_refund(self.env, self.method, 'refund-done', state='done')
        (stuck | done).with_user(manager).action_musqet_mark_failed()
        self.assertEqual(stuck.state, 'failed')
        self.assertEqual(done.state, 'done')

    def test_cashier_cannot_mark_a_refund_failed_with_its_state_cached(self):
        cashier = make_pos_user(self.env, 'musqet_cashier_cached')
        stuck = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        self.assertEqual(stuck.state, 'pending')  # now in the cache
        with self.assertRaises(AccessError):
            stuck.with_user(cashier).action_musqet_mark_failed()
        self.assertEqual(stuck.state, 'pending')

    def test_cashier_cannot_mark_a_refund_failed(self):
        cashier = make_pos_user(self.env, 'musqet_cashier_fail')
        stuck = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        # As over RPC: nothing cached, so the explicit check is what's tested.
        stuck.invalidate_recordset()
        with self.assertRaises(AccessError):
            stuck.with_user(cashier).action_musqet_mark_failed()
        self.assertEqual(stuck.state, 'pending')
