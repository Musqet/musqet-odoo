# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging
from collections import Counter, defaultdict

from odoo import api, fields, models

from .musqet_common import minor_units

_logger = logging.getLogger(__name__)

# A payment write that touches one of these sets musqet_refund_of again from the refund record.
MUSQET_REFUND_LINK_FIELDS = {'transaction_id', 'payment_method_id', 'musqet_refund_of'}
# A payment write that touches one of these re-runs the settle check. musqet_settle_check is
# one of them, so a value written over RPC is put right at once.
MUSQET_SETTLE_CHECK_FIELDS = {'transaction_id', 'payment_method_id', 'amount', 'musqet_settle_check'}
# Orders whose Musqet lines are not checked: not paid (yet), so nothing to vouch for.
MUSQET_UNCHECKED_ORDER_STATES = ('draft', 'cancel')


class PosPayment(models.Model):
    _inherit = 'pos.payment'

    # The rail the Musqet terminal settled on, as the POS reports it from the top-level
    # ``rail`` field of the sale result ("card" | "bitcoin"), never inferred from metadata.
    # Stored on the payment so it round-trips on sync and is reloaded when a past order is
    # reopened, where the POS uses it to pick the refund path. The server's refund check
    # reads the rail from its own sale record (pos.musqet.sale) instead; only the upgrade to
    # 19.0.1.3.0 read this field, to record the sales synced before it.
    #
    # pos.payment does not override _load_pos_data_fields, so the POS data loader reads all
    # stored fields (read([]) == all fields) — this field therefore loads into the frontend
    # and serializes back on sync with no extra wiring, matching pos_razorpay/pos_pine_labs.
    musqet_rail = fields.Char(string="Musqet Rail", copy=False)
    # On a refund payment line, the Musqet saleId of the original card sale this refund
    # reverses. The Musqet API keeps no sale↔refund linkage of its own (Musqet/musqet#2094).
    # The POS sets it when a refund settles, and the server sets it from its own refund
    # record (pos.musqet.refund) whenever the line's saleId has one, which also covers a
    # refund settled after a page refresh, when the POS no longer knows the original. The
    # over-refund cap reads the refund records; this field is the link on the payment.
    musqet_refund_of = fields.Char(string="Musqet Refund Of", copy=False)
    # Whether Musqet confirmed this Musqet payment line. The POS marks an order paid on the
    # browser's word, so the server checks each synced line against its own records: a sale
    # line against the done pos.musqet.sale with its saleId (amount and currency Musqet
    # captured), a refund line against the done pos.musqet.refund (amount), and only one paid
    # line may claim each. Empty on a line of another method, on a line of an order not yet
    # paid, and on lines synced before 19.0.1.3.0, which were never checked.
    musqet_settle_check = fields.Selection(
        [('verified', "Verified"), ('unverified', "Not verified")],
        string="Musqet Check", copy=False, readonly=True,
        help="Verified: Musqet reported this payment complete for this amount. Not verified: "
             "Odoo has no such report, so check the payment in the Musqet app.")

    @api.model_create_multi
    def create(self, vals_list):
        payments = super().create(vals_list)
        payments._musqet_link_recorded_refunds()
        payments._musqet_check_settles()
        return payments

    def write(self, vals):
        # The check's own write (sudo, the flag alone) needs no second check. A caller over
        # RPC can't write as superuser, so its value is always checked.
        check = MUSQET_SETTLE_CHECK_FIELDS & vals.keys() and not (
            self.env.su and vals.keys() == {'musqet_settle_check'})
        # The lines that shared a saleId with these before the write, which may now be the
        # only line of their sale.
        previous = self._musqet_lines_sharing_sales() if check else self.browse()
        result = super().write(vals)
        if MUSQET_REFUND_LINK_FIELDS & vals.keys():
            self._musqet_link_recorded_refunds()
        if check:
            (self | previous)._musqet_check_settles()
        return result

    def unlink(self):
        others = self._musqet_lines_sharing_sales() - self
        result = super().unlink()
        others.exists()._musqet_check_settles()
        return result

    def _musqet_lines_sharing_sales(self):
        """Every payment with the (method, saleId) of one of these Musqet lines, these included.

        May also hold lines of another listed method with a listed saleId; the check works
        per (method, saleId), so they are only checked again.
        """
        musqet = self.filtered(
            lambda payment: payment.transaction_id
            and payment.payment_method_id.use_payment_terminal == 'musqet')
        if not musqet:
            return self.browse()
        return self.sudo().search([
            ('transaction_id', 'in', musqet.mapped('transaction_id')),
            ('payment_method_id', 'in', musqet.payment_method_id.ids),
        ])

    @api.model
    def _musqet_check_settles_of(self, records):
        """Re-check the payments carrying the saleIds of sale or refund ``records``."""
        if records:
            self.sudo().search([
                ('transaction_id', 'in', records.mapped('sale_id')),
                ('payment_method_id', 'in', records.payment_method_id.ids),
            ])._musqet_check_settles()

    def _musqet_check_settles(self):
        """Set musqet_settle_check on these payments, and on every line sharing their saleIds.

        A line is checked against the server's done sale or refund record of its
        (method, saleId), and against the other lines claiming it: one Musqet sale or refund
        vouches for one paid line, so a saleId copied onto a second line leaves both Not
        verified. Logs each Musqet line that becomes Not verified, so it can be checked by
        hand.
        """
        payments = self.sudo() | self._musqet_lines_sharing_sales()
        musqet = payments.filtered(lambda payment: payment.payment_method_id.use_payment_terminal == 'musqet')
        sale_ids = [sale_id for sale_id in musqet.mapped('transaction_id') if sale_id]
        sales, refunds = {}, {}
        if sale_ids:
            domain = [
                ('sale_id', 'in', sale_ids),
                ('payment_method_id', 'in', musqet.payment_method_id.ids),
                ('state', '=', 'done'),
            ]
            for model, by_key in (('pos.musqet.sale', sales), ('pos.musqet.refund', refunds)):
                for record in self.env[model].sudo().search(domain):
                    by_key[(record.payment_method_id.id, record.sale_id)] = record
        claims = Counter(
            payment._musqet_claim_key() for payment in musqet
            if payment.transaction_id
            and payment.pos_order_id.state not in MUSQET_UNCHECKED_ORDER_STATES)
        to_write = defaultdict(lambda: self.browse())
        for payment in payments:
            value = payment._musqet_settle_value(sales, refunds, claims)
            if payment.musqet_settle_check != value:
                to_write[value] |= payment
        for value, changed in to_write.items():
            if value == 'unverified':
                for payment in changed:
                    _logger.warning(
                        "Musqet payment %s (order %s, sale %s, amount %s) has no matching sale or "
                        "refund reported complete by Musqet, or shares it with another line",
                        payment.id, payment.pos_order_id.name, payment.transaction_id, payment.amount)
            changed.sudo().write({'musqet_settle_check': value})

    def _musqet_claim_key(self):
        """The sale or refund this line claims: (method, saleId, whether it is a sale)."""
        return (self.payment_method_id.id, self.transaction_id, self.amount > 0)

    def _musqet_settle_value(self, sales, refunds, claims):
        """This payment's musqet_settle_check, given the done records by (method, saleId) and
        how many paid lines claim each sale or refund."""
        self.ensure_one()
        if (self.payment_method_id.use_payment_terminal != 'musqet'
                or self.pos_order_id.state in MUSQET_UNCHECKED_ORDER_STATES):
            return False
        if claims[self._musqet_claim_key()] > 1:
            return 'unverified'
        key = (self.payment_method_id.id, self.transaction_id)
        amount = minor_units(self.amount, self.currency_id)
        if amount > 0:
            sale = sales.get(key)
            verified = sale and sale.amount_minor == amount and sale.currency == self.currency_id.name
        else:
            refund = refunds.get(key)
            verified = refund and refund.amount_minor == -amount
        return 'verified' if verified else 'unverified'

    def _musqet_link_recorded_refunds(self):
        """Set musqet_refund_of from the server's refund record, over what the POS sent."""
        payments = self.filtered(
            lambda payment: payment.transaction_id
            and payment.payment_method_id.use_payment_terminal == 'musqet')
        if not payments:
            return
        refunds = self.env['pos.musqet.refund'].sudo().search([
            ('sale_id', 'in', payments.mapped('transaction_id')),
            ('payment_method_id', 'in', payments.payment_method_id.ids),
        ])
        original_by_refund = {
            (refund.payment_method_id.id, refund.sale_id): refund.original_sale_id
            for refund in refunds
        }
        to_link = defaultdict(lambda: self.browse())
        for payment in payments:
            original_sale_id = original_by_refund.get(
                (payment.payment_method_id.id, payment.transaction_id))
            if original_sale_id and payment.musqet_refund_of != original_sale_id:
                to_link[original_sale_id] |= payment
        for original_sale_id, linked in to_link.items():
            # This write re-enters write(), which finds every link already right and writes
            # nothing more. (No context flag: an RPC caller sets the context, and could set
            # it to keep a forged link.)
            linked.sudo().write({'musqet_refund_of': original_sale_id})


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def write(self, vals):
        result = super().write(vals)
        # A Musqet line is checked once its order is paid (and cleared if it goes back to
        # draft or is cancelled).
        if 'state' in vals:
            self.payment_ids._musqet_check_settles()
        return result
