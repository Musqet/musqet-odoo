# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""The settle check: each synced Musqet line checked against the server's records."""
from unittest.mock import patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.tests import tagged

from .common import MusqetPosRecordsCase, make_pos_user, musqet_response, record_sale


@tagged('post_install', '-at_install')
class TestMusqetSettleCheck(MusqetPosRecordsCase):
    """pos.payment.musqet_settle_check against the server's sale and refund records."""

    def test_the_line_of_the_order_sent_keeps_verified(self):
        # A copy of a real sale's saleId on another order: the line of the order Odoo sent the
        # sale for stays Verified and refundable; the copy is Not verified and refused.
        record_sale(self.env, self.method, 'sale-sent', reference=self.order.uuid)
        genuine = self._payment(amount=10.0, transaction_id='sale-sent')
        copy = self._payment(pos_order_id=self._order().id, amount=10.0, transaction_id='sale-sent')
        self.assertEqual((genuine.musqet_settle_check, copy.musqet_settle_check),
                         ('verified', 'unverified'))
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-COPY'}, copy.id)
        self.assertIn('has not confirmed', result['error']['message'])
        mock_request.assert_not_called()
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-sent')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-SENT'}, genuine.id)
        self.assertNotIn('error', result)
        # Two lines on the order sent for: Odoo can't tell them apart, so neither.
        twin = self._payment(amount=10.0, transaction_id='sale-sent')
        self.assertEqual({genuine.musqet_settle_check, twin.musqet_settle_check}, {'unverified'})

    def test_changing_the_order_reference_checks_its_lines_again(self):
        # Either form of the reference the sale was sent with.
        for field in ('uuid', 'pos_reference'):
            with self.subTest(field=field):
                order = self._order()
                order.pos_reference = 'Order 00001-001-%s' % field
                self._record_sale('sale-renamed-%s' % field, reference=order[field])
                line = self._payment(pos_order_id=order.id, amount=10.0,
                                     transaction_id='sale-renamed-%s' % field)
                self.assertEqual(line.musqet_settle_check, 'verified')
                order[field] = 'renamed-%s' % field
                self.assertEqual(line.musqet_settle_check, 'unverified')

    def test_a_sole_line_on_another_order_is_not_verified(self):
        # The sale was sent for another order (which never synced): a line on this one, even
        # alone and for the right amount, isn't the line the sale vouches for.
        record_sale(self.env, self.method, 'sale-elsewhere', reference='Order 99999-999-9999')
        line = self._payment(amount=10.0, transaction_id='sale-elsewhere')
        self.assertEqual(line.musqet_settle_check, 'unverified')

    def test_a_line_check_claims_the_sale_record(self):
        # Two lines copying one saleId, synced at once, must meet on the record's row.
        self._original('sale-lock')
        sale = self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-lock')])
        before = self._row_version(sale)
        with self.env.cr.savepoint():
            self._payment(pos_order_id=self._order().id, amount=10.0, transaction_id='sale-lock')
            self.assertNotEqual(self._row_version(sale), before)

    def test_a_sale_line_musqet_confirmed_is_verified(self):
        self._record_sale('sale-ok', amount_minor=1000)
        payment = self._payment(amount=10.0, transaction_id='sale-ok')
        self.assertEqual(payment.musqet_settle_check, 'verified')

    def test_a_sale_line_without_a_matching_settle_is_not_verified(self):
        self._record_sale('sale-pending', state='pending')
        self._record_sale('sale-999', amount_minor=999)
        self._record_sale('sale-eur', currency='XTS')
        record_sale(self.env, self.second, 'sale-second')
        for transaction_id in (False, 'sale-unknown', 'sale-pending', 'sale-999', 'sale-eur',
                               'sale-second'):
            with self.subTest(transaction_id=transaction_id):
                payment = self._payment(amount=10.0, transaction_id=transaction_id)
                self.assertEqual(payment.musqet_settle_check, 'unverified')

    def test_a_line_of_another_method_is_not_checked(self):
        payment = self._payment(payment_method_id=self.plain.id, amount=10.0,
                                musqet_settle_check='verified')
        self.assertFalse(payment.musqet_settle_check)

    def test_a_line_is_verified_once_musqet_reports_it_complete(self):
        # A sale and a refund (refund-1, pending, from setUp) both settle from a status read.
        self._record_sale('sale-late', state='pending')
        sale_line = self._payment(amount=10.0, transaction_id='sale-late')
        refund_line = self._payment(transaction_id='refund-1')
        self.assertEqual(sale_line.musqet_settle_check, 'unverified')
        self.assertEqual(refund_line.musqet_settle_check, 'unverified')
        for sale_id, amount in (('sale-late', 1000), ('refund-1', 500)):
            fake = musqet_response(sale_id, 'COMPLETE', amountInCents=amount, rail='card',
                                   currency=self.method.company_id.currency_id.name)
            with patch.object(pos_payment_method.requests, 'request', return_value=fake):
                self.method.musqet_get_sale(sale_id)
        self.assertEqual(sale_line.musqet_settle_check, 'verified')
        self.assertEqual(refund_line.musqet_settle_check, 'verified')

    def test_a_refund_line_is_checked_against_its_refund_record(self):
        self._record_refund('refund-ok', amount_minor=500, state='done')
        self._record_refund('refund-600', amount_minor=600, state='done')
        self.assertEqual(self._payment(transaction_id='refund-ok').musqet_settle_check, 'verified')
        self.assertEqual(
            self._payment(transaction_id='refund-600').musqet_settle_check, 'unverified')

    def test_a_written_settle_check_is_put_right(self):
        payment = self._payment(amount=10.0, transaction_id='sale-unknown',
                                musqet_settle_check='verified')
        self.assertEqual(payment.musqet_settle_check, 'unverified')
        # As over RPC: a real user, not the superuser.
        manager = make_pos_user(self.env, 'musqet_manager_flag', group='point_of_sale.group_pos_manager')
        payment.with_user(manager).write({'musqet_settle_check': 'verified'})
        self.assertEqual(payment.musqet_settle_check, 'unverified')
        # A server path writing POS values under sudo doesn't keep it either.
        payment.sudo().write({'musqet_settle_check': 'verified'})
        self.assertEqual(payment.musqet_settle_check, 'unverified')

    def test_a_copied_sale_id_leaves_every_line_unverified(self):
        # A record that names no order (the upgrade made it from lines sharing the saleId)
        # vouches for one paid line only: a second line claiming it makes both Not verified
        # until it is gone.
        record_sale(self.env, self.method, 'sale-copied')
        genuine = self._payment(amount=10.0, transaction_id='sale-copied')
        self.assertEqual(genuine.musqet_settle_check, 'verified')
        copy = self._payment(pos_order_id=self._order().id, amount=10.0, transaction_id='sale-copied')
        self.assertEqual((genuine.musqet_settle_check, copy.musqet_settle_check),
                         ('unverified', 'unverified'))
        copy.transaction_id = 'sale-other'
        self.assertEqual(genuine.musqet_settle_check, 'verified')
        copy.transaction_id = 'sale-copied'
        self.assertEqual(genuine.musqet_settle_check, 'unverified')
        copy.unlink()
        self.assertEqual(genuine.musqet_settle_check, 'verified')

    def test_lines_of_an_unpaid_order_are_not_checked(self):
        draft = self._order(state='draft')
        with self.assertNoLogs('odoo.addons.pos_musqet.models.pos_payment', 'WARNING'):
            payment = self._payment(pos_order_id=draft.id, amount=10.0, transaction_id='sale-wip')
        self.assertFalse(payment.musqet_settle_check)
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment', 'WARNING'):
            draft.state = 'paid'
        self.assertEqual(payment.musqet_settle_check, 'unverified')
        cancelled = self._payment(pos_order_id=self._order(state='cancel').id, amount=10.0,
                                  transaction_id='sale-cancelled')
        self.assertFalse(cancelled.musqet_settle_check)
        # Moved to a paid order: checked; back to an unpaid one: cleared.
        cancelled.pos_order_id = self.order
        self.assertEqual(cancelled.musqet_settle_check, 'unverified')
        cancelled.pos_order_id = self._order(state='draft')
        self.assertFalse(cancelled.musqet_settle_check)

    def test_a_draft_line_does_not_claim_the_sale(self):
        genuine = self._original('sale-draft-copy')
        self._payment(pos_order_id=self._order(state='draft').id, amount=10.0,
                      transaction_id='sale-draft-copy')
        self.assertEqual(genuine.musqet_settle_check, 'verified')

    def test_editing_a_verified_line_checks_it_again(self):
        self._record_sale('sale-edit', amount_minor=1000)
        payment = self._payment(amount=10.0, transaction_id='sale-edit')
        payment.amount = 20.0
        self.assertEqual(payment.musqet_settle_check, 'unverified')

    def test_logs_a_line_that_is_not_verified(self):
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment', 'WARNING') as logs:
            payment = self._payment(amount=10.0, transaction_id='sale-forged')
        self.assertEqual(len(logs.output), 1)
        self.assertIn('sale-forged', logs.output[0])
        self.assertIn(str(payment.id), logs.output[0])

    # -- Sale records ----------------------------------------------------------
