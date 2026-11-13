# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from collections import defaultdict

from odoo import api, fields, models


class PosPayment(models.Model):
    _inherit = 'pos.payment'

    # The rail the Musqet terminal actually settled on, read from the top-level ``rail``
    # field of the sale result ("card" | "bitcoin"), never inferred from metadata. Stored
    # on the payment so it round-trips on sync and is reloaded when a past order is
    # reopened — issue #7 reads it to gate refunds (card vs Lightning reverse differently).
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

    @api.model_create_multi
    def create(self, vals_list):
        payments = super().create(vals_list)
        payments._musqet_link_recorded_refunds()
        return payments

    def write(self, vals):
        result = super().write(vals)
        if {'transaction_id', 'payment_method_id', 'musqet_refund_of'} & vals.keys():
            self._musqet_link_recorded_refunds()
        return result

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
            original = original_by_refund.get((payment.payment_method_id.id, payment.transaction_id))
            if original and payment.musqet_refund_of != original:
                to_link[original] |= payment
        for original, linked in to_link.items():
            # This write re-enters write(), which finds every link already right and writes
            # nothing more. (No context flag: an RPC caller sets the context, and could set
            # it to keep a forged link.)
            linked.sudo().write({'musqet_refund_of': original})
