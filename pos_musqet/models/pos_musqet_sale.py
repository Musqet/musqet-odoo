# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models

from .musqet_common import (
    MUSQET_FAILURE_STATUSES,
    MUSQET_RAIL_SELECTION,
    MUSQET_RAILS,
    MUSQET_SUCCESS_STATUS,
    minor_units,
    record_unrecorded_payments,
)

_logger = logging.getLogger(__name__)


class PosMusqetSale(models.Model):
    """A sale Odoo started on a Musqet terminal, settled from what Musqet reports.

    The POS marks an order paid on the browser's word, so the server keeps its own record of
    each sale: created when Musqet accepts the sale (musqet_create_sale), and settled from
    Musqet's own replies, the status read (musqet_get_sale) or a signed webhook. A done
    record holds the amount Musqet captured, the currency and the rail it reports. The
    over-refund cap and the rail check read it instead of the synced payment line, and each
    synced Musqet payment line is checked against it (pos.payment.musqet_settle_check).

    Created only by the server (sudo); nothing changes them from the browser. The upgrade
    to 19.0.1.3.0 also created done records for the sales synced before it
    (_musqet_record_synced_sales).
    """
    _name = 'pos.musqet.sale'
    _description = 'Musqet Sale'
    _order = 'id desc'

    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    payment_method_id = fields.Many2one(
        'pos.payment.method', required=True, readonly=True, ondelete='restrict')
    sale_id = fields.Char(
        string="Musqet Sale", required=True, readonly=True, index=True,
        help="The Musqet saleId of the sale.")
    reference = fields.Char(readonly=True, help="The order reference sent to Musqet.")
    amount_minor = fields.Integer(
        string="Amount (minor units)", required=True, readonly=True,
        help="The amount in minor units of the currency (cents): the amount requested while "
             "pending, the amount Musqet captured once done.")
    currency = fields.Char(required=True, readonly=True, help="The ISO code of the sale currency.")
    rail = fields.Selection(
        MUSQET_RAIL_SELECTION, readonly=True,
        help="The rail Musqet reports the sale settled on. Only a card sale can be refunded.")
    state = fields.Selection(
        [('pending', "Pending"), ('done', "Done"), ('failed', "Failed")],
        required=True, default='pending', readonly=True,
        help="Done once Musqet reports the sale complete. Only a done card sale can be refunded.")

    _unique_sale = models.Constraint(
        'UNIQUE (payment_method_id, sale_id)', 'This Musqet sale is already recorded.')

    def _musqet_apply_response(self, response):
        """Settle the pending sales as a Musqet sale response says; other statuses leave them.

        On COMPLETE the record takes the amount, currency and rail from the response, Musqet's
        own word, over what was requested. Only a pending sale moves, as for refunds, except
        that a done sale whose rail Musqet hadn't reported yet takes it from a later reply.
        """
        if not isinstance(response, dict):
            return
        status = response.get('status')
        rail = response.get('rail') if response.get('rail') in MUSQET_RAILS else False
        if status == MUSQET_SUCCESS_STATUS and rail:
            self.filtered(lambda sale: sale.state == 'done' and not sale.rail).write({'rail': rail})
        pending = self.filtered(lambda sale: sale.state == 'pending')
        if not pending:
            return
        if status in MUSQET_FAILURE_STATUSES:
            pending.write({'state': 'failed'})
            return
        if status != MUSQET_SUCCESS_STATUS:
            return
        vals = {'state': 'done', 'rail': rail}
        amount = response.get('amountInCents')
        if isinstance(amount, int) and not isinstance(amount, bool):
            vals['amount_minor'] = amount
        currency = response.get('currency')
        # An ISO 4217 code, compared with res.currency names: upper-cased, and kept only if it
        # looks like one.
        if isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha():
            vals['currency'] = currency.upper()
        pending.write(vals)
        self.env['pos.payment']._musqet_check_settles_of(pending)

    @api.model
    def _musqet_record_synced_sales(self):
        """Record each synced Musqet sale payment that has no record yet, as done.

        For the upgrade to 19.0.1.3.0 (see its post-migration); safe to run again. The
        payment's own amount and rail are the best evidence there is for a sale made before
        sales were recorded on the server. A payment that is a refund (negative, or linked to
        a sale it refunds) is left out.
        """
        payments = self.env['pos.payment'].sudo().search([
            ('amount', '>', 0),
            ('musqet_refund_of', '=', False),
            ('transaction_id', '!=', False),
            ('payment_method_id.use_payment_terminal', '=', 'musqet'),
        ], order='id')
        count = record_unrecorded_payments(self, payments, lambda payment: {
            'amount_minor': minor_units(payment.amount, payment.currency_id),
            'currency': payment.currency_id.name,
            'rail': payment.musqet_rail if payment.musqet_rail in MUSQET_RAILS else False,
        })
        _logger.info("pos_musqet: recorded %d sale(s) synced before this version", count)
