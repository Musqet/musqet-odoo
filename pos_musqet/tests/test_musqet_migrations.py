# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""The upgrade backfills of sale and refund records."""
from odoo.tests import tagged

from .common import MusqetPosRecordsCase, load_migration


@tagged('post_install', '-at_install')
class TestMusqetUpgradeBackfills(MusqetPosRecordsCase):
    """The 19.0.1.2.0 and 19.0.1.3.0 backfills, and the clearing of guessed references."""

    def test_sale_backfill_names_no_order_for_a_shared_sale_id(self):
        # Two lines already shared a saleId before the upgrade: the record can't say which
        # one the sale was sent for, so neither is Verified.
        older = self._payment(pos_order_id=self._order().id, amount=10.0, transaction_id='sale-shared')
        newer = self._payment(amount=10.0, transaction_id='sale-shared')
        self.env['pos.musqet.sale']._musqet_record_synced_sales()
        sale = self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-shared')])
        self.assertFalse(sale.reference)
        (older | newer)._musqet_check_settles()
        self.assertEqual({older.musqet_settle_check, newer.musqet_settle_check}, {'unverified'})

    def test_upgrade_clears_the_reference_of_a_shared_refund(self):
        # The 19.0.1.2.0 backfill named one order for a refund saleId several linked lines
        # carried, of any order state; 19.0.1.3.0 clears it, and then none of them is Verified.
        shared = self._record_refund('refund-shared', state='done')
        lines = self._payment(transaction_id='refund-shared') | self._payment(
            pos_order_id=self._order().id, transaction_id='refund-shared')
        # Carried by one line: kept.
        single = self._record_refund('refund-single', state='done')
        self._payment(transaction_id='refund-single')
        # The genuine line's order is unpaid and a copy is on a paid one; the old backfill
        # guessed either order. Cleared both ways, so the copy is never Verified.
        guesses = {}
        for guess in ('unpaid', 'paid'):
            unpaid, paid = self._order(state='draft'), self._order()
            guessed_order = unpaid if guess == 'unpaid' else paid
            refund = self._record_refund(
                'refund-%s' % guess, state='done', reference=guessed_order.uuid)
            self._payment(pos_order_id=unpaid.id, transaction_id=refund.sale_id)
            copy = self._payment(pos_order_id=paid.id, transaction_id=refund.sale_id)
            guesses[guess] = (refund, copy)
        # Sent for a refund order that never synced, with copies on two paid orders: not a
        # backfill guess (no sharer's order), and the duplicate check knows that refund only
        # by this reference, so it is kept.
        sent = self._record_refund('refund-sent', state='done', reference='R-UNSYNCED')
        for _copy in range(2):
            self._payment(pos_order_id=self._order().id, transaction_id='refund-sent')
        load_migration('19.0.1.3.0', 'post-migration').migrate(self.env.cr, '19.0.1.2.0')
        self.assertFalse(shared.reference)
        self.assertEqual(single.reference, self.order.uuid)
        self.assertEqual(sent.reference, 'R-UNSYNCED')
        lines._musqet_check_settles()
        self.assertEqual(set(lines.mapped('musqet_settle_check')), {'unverified'})
        for guess, (refund, copy) in guesses.items():
            with self.subTest(guess=guess):
                self.assertFalse(refund.reference)
                copy._musqet_check_settles()
                self.assertEqual(copy.musqet_settle_check, 'unverified')

    def test_a_backfilled_refund_with_its_genuine_line_unpaid_verifies_no_copy(self):
        # Upgrading from 19.0.1.1.0: the genuine refund line is on an unpaid order and a copy
        # of its saleId on a paid one. The backfill can't say which was sent (no reference),
        # so the copy, though the only paid claimant, is not Verified.
        unpaid = self._order(state='draft')
        self._payment(pos_order_id=unpaid.id, transaction_id='refund-bf', musqet_refund_of='sale-orig-1')
        copy = self._payment(pos_order_id=self._order().id, transaction_id='refund-bf',
                             musqet_refund_of='sale-orig-1')
        self.env['pos.musqet.refund']._musqet_record_synced_refunds()
        refund = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-bf')])
        self.assertFalse(refund.reference)
        copy._musqet_check_settles()
        self.assertEqual(copy.musqet_settle_check, 'unverified')

    def test_sale_backfill_skips_an_amount_too_large_to_store(self):
        self._payment(amount=30000000.0, transaction_id='sale-huge')
        logger = 'odoo.addons.pos_musqet.models.pos_musqet_record'
        with self.assertLogs(logger, 'WARNING') as logs:
            self.env['pos.musqet.sale']._musqet_record_synced_sales()
        self.assertIn('sale-huge', logs.output[0])
        self.assertFalse(self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-huge')]))

    def test_backfill_does_nothing_on_a_fresh_install(self):
        self._payment(transaction_id='refund-legacy', musqet_refund_of='sale-orig-1')
        load_migration('19.0.1.2.0', 'post-migration').migrate(self.env.cr, None)
        self.assertFalse(self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-legacy')]))

    def test_backfill_records_linked_refunds_once(self):
        legacy = self._payment(transaction_id='refund-legacy', musqet_refund_of='sale-orig-1', amount=-3.0)
        self._payment(transaction_id='refund-1')  # already recorded in setUp
        self._payment(transaction_id='refund-unlinked')  # no musqet_refund_of: not a refund
        Refund = self.env['pos.musqet.refund']
        load_migration('19.0.1.2.0', 'post-migration').migrate(self.env.cr, '19.0.1.1.0')
        refunds = Refund.search([])
        self.assertEqual(sorted(refunds.mapped('sale_id')), ['refund-1', 'refund-legacy'])
        Refund._musqet_record_synced_refunds()  # running again adds nothing
        self.assertEqual(Refund.search([]), refunds)
        backfilled = refunds.filtered(lambda refund: refund.sale_id == 'refund-legacy')
        self.assertEqual(backfilled.original_sale_id, 'sale-orig-1')
        self.assertEqual(backfilled.amount_minor, 300)
        self.assertEqual(backfilled.state, 'done')
        self.assertEqual(backfilled.payment_method_id, self.method)
        self.assertEqual(backfilled.reference, legacy.pos_order_id.pos_reference or legacy.pos_order_id.uuid)
        self.assertTrue(backfilled.reference)

    # -- Settle check on synced payments (musqet_settle_check) --------------------

    def test_sale_backfill_records_synced_sales_once(self):
        legacy = self._payment(amount=12.5, transaction_id='sale-legacy', musqet_rail='card')
        self._payment(amount=3.0, transaction_id='sale-legacy-btc', musqet_rail='bitcoin')
        self._payment(amount=3.0, transaction_id='sale-legacy-odd', musqet_rail='paypal')
        self._payment(amount=-3.0, transaction_id='refund-legacy')  # a refund line
        self._payment(amount=3.0, transaction_id='refund-linked', musqet_refund_of='sale-x')
        self._payment(amount=3.0)  # no saleId
        self._payment(payment_method_id=self.plain.id, amount=3.0, transaction_id='cash-1')
        for state in ('draft', 'cancel'):  # never paid
            self._payment(pos_order_id=self._order(state=state).id, amount=3.0,
                          transaction_id='sale-%s' % state)
        self._original('sale-recorded')  # recorded already
        Sale = self.env['pos.musqet.sale']
        load_migration('19.0.1.3.0', 'post-migration').migrate(self.env.cr, '19.0.1.2.0')
        sales = Sale.search([])
        self.assertEqual(
            sorted(sales.mapped('sale_id')),
            ['sale-legacy', 'sale-legacy-btc', 'sale-legacy-odd', 'sale-recorded'])
        Sale._musqet_record_synced_sales()  # running again adds nothing
        self.assertEqual(Sale.search([]), sales)
        rails = {sale.sale_id: sale.rail for sale in sales}
        self.assertEqual(rails['sale-legacy-btc'], 'bitcoin')
        self.assertFalse(rails['sale-legacy-odd'])
        backfilled = sales.filtered(lambda sale: sale.sale_id == 'sale-legacy')
        self.assertEqual(backfilled.amount_minor, 1250)
        self.assertEqual(backfilled.currency, legacy.currency_id.name)
        self.assertEqual(backfilled.rail, 'card')
        self.assertEqual(backfilled.state, 'done')
        self.assertEqual(backfilled.payment_method_id, self.method)
        self.assertEqual(backfilled.company_id, self.method.company_id)
        self.assertEqual(backfilled.reference, legacy.pos_order_id.pos_reference or legacy.pos_order_id.uuid)
        self.assertTrue(backfilled.reference)

    def test_sale_backfill_does_nothing_on_a_fresh_install(self):
        self._payment(amount=10.0, transaction_id='sale-legacy', musqet_rail='card')
        load_migration('19.0.1.3.0', 'post-migration').migrate(self.env.cr, None)
        self.assertFalse(self.env['pos.musqet.sale'].search([]))
