# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models

from .musqet_common import (
    MUSQET_FAILURE_STATUSES,
    MUSQET_RAIL_SELECTION,
    MUSQET_RAILS,
    MUSQET_SUCCESS_STATUS,
    MUSQET_TERMINAL,
    MUSQET_UNPAID_ORDER_STATES,
    minor_units,
    parse_iso_currency,
    parse_minor_amount,
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
    _inherit = ['pos.musqet.record']
    _description = 'Musqet Sale'

    sale_id = fields.Char(string="Musqet Sale", help="The Musqet saleId of the sale.")
    amount_minor = fields.Integer(
        help="The amount in minor units of the currency (cents): the amount requested while "
             "pending, the amount Musqet captured once done.")
    currency = fields.Char(required=True, readonly=True, help="The ISO code of the sale currency.")
    rail = fields.Selection(
        MUSQET_RAIL_SELECTION, readonly=True,
        help="The rail Musqet reports the sale settled on. Only a card sale can be refunded.")
    state = fields.Selection(
        help="Done once Musqet reports the sale complete, with the amount and currency it "
             "captured. Only a done card sale can be refunded.")

    _unique_sale = models.Constraint(
        'UNIQUE (payment_method_id, sale_id)', 'This Musqet sale is already recorded.')

    def _musqet_apply_response(self, response):
        """Settle the pending sales as a Musqet sale response says; other statuses leave them.

        On COMPLETE the record takes the amount, currency and rail from the response, Musqet's
        own word, over what was requested; a COMPLETE without a valid amount and currency
        leaves it pending. Only a pending sale moves, as for refunds, except that a done sale
        whose rail Musqet hadn't reported yet takes it from a later reply.
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
        amount = parse_minor_amount(response.get('amountInCents'))
        currency = parse_iso_currency(response.get('currency'))
        if amount is None or currency is None:
            # Done means Musqet confirmed what it captured; without that, stay pending.
            _logger.warning(
                "Musqet reported sale(s) %s complete without a valid amount and currency",
                ', '.join(pending.mapped('sale_id')))
            return
        pending.write({'state': 'done', 'rail': rail, 'amount_minor': amount, 'currency': currency})
        self.env['pos.payment']._musqet_check_settles_of(pending)

    @api.model
    def _musqet_record_synced_sales(self):
        """Record each synced Musqet sale payment that has no record yet, as done.

        For the upgrade to 19.0.1.3.0 (see its post-migration); safe to run again. The
        payment's own amount and rail are the best evidence there is for a sale made before
        sales were recorded on the server. A payment that is a refund (negative, or linked to
        a sale it refunds), or on an order never paid, is left out.
        """
        payments = self.env['pos.payment'].sudo().search([
            ('amount', '>', 0),
            ('pos_order_id.state', 'not in', MUSQET_UNPAID_ORDER_STATES),
            ('musqet_refund_of', '=', False),
            ('transaction_id', '!=', False),
            ('payment_method_id.use_payment_terminal', '=', MUSQET_TERMINAL),
        ], order='id')
        count = self._musqet_record_unrecorded(payments, lambda payment: {
            'amount_minor': minor_units(payment.amount, payment.currency_id),
            'currency': payment.currency_id.name,
            'rail': payment.musqet_rail if payment.musqet_rail in MUSQET_RAILS else False,
        })
        _logger.info("pos_musqet: recorded %d sale(s) synced before this version", count)
