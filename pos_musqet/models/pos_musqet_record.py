# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class PosMusqetRecord(models.AbstractModel):
    """What the server's records of Musqet sales and refunds share.

    Each record is one Musqet saleId taken on one payment method, created only by the
    server (sudo) and settled only from Musqet's own replies. pos.musqet.sale and
    pos.musqet.refund add what their kind needs.
    """
    _name = 'pos.musqet.record'
    _description = 'Musqet Sale or Refund Record'
    _order = 'id desc'

    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    payment_method_id = fields.Many2one(
        'pos.payment.method', required=True, readonly=True, ondelete='restrict')
    sale_id = fields.Char(required=True, readonly=True, help="The Musqet saleId.")
    reference = fields.Char(readonly=True, help="The order reference sent to Musqet.")
    amount_minor = fields.Integer(string="Amount (minor units)", required=True, readonly=True)
    state = fields.Selection(
        [('pending', "Pending"), ('done', "Done"), ('failed', "Failed")],
        required=True, default='pending', readonly=True)

    @api.model
    def _musqet_record_unrecorded(self, payments, vals_for):
        """Create a done record for each payment whose (method, saleId) has none.

        For the upgrade backfills; safe to run again. ``vals_for(payment)`` gives the
        model's own values. Returns how many were created.
        """
        recorded = {
            (record.payment_method_id.id, record.sale_id)
            for record in self.sudo().search([('sale_id', 'in', payments.mapped('transaction_id'))])
        }
        vals_list = []
        for payment in payments:
            key = (payment.payment_method_id.id, payment.transaction_id)
            if key in recorded:
                continue
            recorded.add(key)
            order = payment.pos_order_id
            vals_list.append({
                'company_id': payment.company_id.id,
                'payment_method_id': payment.payment_method_id.id,
                'sale_id': payment.transaction_id,
                'reference': order.pos_reference or order.uuid,
                'state': 'done',
                **vals_for(payment),
            })
        self.sudo().create(vals_list)
        return len(vals_list)
