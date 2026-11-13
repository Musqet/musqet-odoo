# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# The Musqet sale statuses (a refund uses the same ones) that end a sale; see
# _musqet_apply_status for the refund state each leads to. Same lists as SUCCESS_STATUS and
# FAILURE_STATUSES in static/src/app/payment_musqet.js.
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
        """Settle the pending refunds as a Musqet status says; other statuses leave them.

        Only a pending refund moves: a settled one keeps its state whatever a later read
        says, and a refund already in the right state isn't rewritten on every poll.
        """
        pending = self.filtered(lambda refund: refund.state == 'pending')
        if status == MUSQET_SUCCESS_STATUS:
            pending.write({'state': 'done'})
        elif status in MUSQET_FAILURE_STATUSES:
            pending.write({'state': 'failed'})

    @api.model
    def _musqet_record_synced_refunds(self):
        """Record each synced, linked Musqet refund payment that has no record yet, as done.

        For the upgrade to 19.0.1.2.0 (see its post-migration); safe to run again. The
        payment's own amount and link are the best evidence there is for a refund made
        before refunds were recorded on the server.
        """
        Method = self.env['pos.payment.method']
        payments = self.env['pos.payment'].sudo().search([
            ('musqet_refund_of', '!=', False),
            ('transaction_id', '!=', False),
            ('payment_method_id.use_payment_terminal', '=', 'musqet'),
        ])
        recorded = {
            (refund.payment_method_id.id, refund.sale_id)
            for refund in self.sudo().search([('sale_id', 'in', payments.mapped('transaction_id'))])
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
                'original_sale_id': payment.musqet_refund_of,
                'sale_id': payment.transaction_id,
                'reference': order.pos_reference or order.uuid,
                'amount_minor': abs(Method._musqet_minor_units(payment.amount, payment.currency_id)),
                'state': 'done',
            })
        self.sudo().create(vals_list)
        _logger.info("pos_musqet: recorded %d refund(s) synced before this version", len(vals_list))
