# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo import fields, models

# Musqet sale statuses (a refund uses the same ones) and the refund state each maps to.
MUSQET_SUCCESS_STATUS = 'COMPLETE'
MUSQET_FAILURE_STATUSES = ('ERROR', 'CANCELLED', 'CANCELLED_REMOTE', 'TIMED_OUT')


class PosMusqetRefund(models.Model):
    """A refund Musqet accepted, recorded by the server when it sends the refund.

    The Musqet API keeps no link from a refund to the sale it reverses and enforces no
    cumulative limit (Musqet/musqet#2094), so the over-refund cap and the duplicate check
    in ``pos.payment.method.musqet_create_refund`` read these records. They are written
    only by the server (sudo), never from the browser, and exist as soon as Musqet accepts
    the refund, before the refund order reaches Odoo.
    """
    _name = 'pos.musqet.refund'
    _description = 'Musqet Refund'
    _order = 'id desc'

    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    payment_method_id = fields.Many2one(
        'pos.payment.method', required=True, readonly=True, ondelete='restrict')
    original_sale_id = fields.Char(
        string="Original Musqet Sale", required=True, readonly=True, index=True,
        help="The Musqet saleId of the card sale this refund reverses.")
    sale_id = fields.Char(
        string="Musqet Refund Sale", required=True, readonly=True,
        help="The Musqet saleId of the refund itself.")
    reference = fields.Char(readonly=True, help="The refund order reference sent to Musqet.")
    amount_minor = fields.Integer(
        string="Amount (minor units)", required=True, readonly=True,
        help="The refunded amount in minor units of the company currency (cents).")
    state = fields.Selection(
        [('pending', "Pending"), ('done', "Done"), ('failed', "Failed")],
        required=True, default='pending', readonly=True,
        help="Pending and Done count against the original sale; Failed does not.")

    _unique_sale = models.Constraint(
        'UNIQUE (payment_method_id, sale_id)', 'This Musqet refund is already recorded.')

    def _musqet_apply_status(self, status):
        """Move the refunds to the state a Musqet status means; other statuses leave them."""
        if status == MUSQET_SUCCESS_STATUS:
            self.write({'state': 'done'})
        elif status in MUSQET_FAILURE_STATUSES:
            self.write({'state': 'failed'})
