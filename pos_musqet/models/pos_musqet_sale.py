# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models

from .musqet_common import (
    MUSQET_RAIL_SELECTION,
    MUSQET_SUCCESS_STATUS,
    MUSQET_TERMINAL,
    MUSQET_UNPAID_ORDER_STATES,
    minor_units,
    parse_iso_currency,
    parse_minor_amount,
    parse_rail,
)

_logger = logging.getLogger(__name__)


class PosMusqetSale(models.Model):
    """A sale Odoo started on a Musqet terminal, settled from what Musqet reports.

    The POS marks an order paid on the browser's word, so the server keeps its own record of
    each sale: created when Musqet accepts the sale (musqet_create_sale), and settled from
    Musqet's own replies to a status read (musqet_get_sale), polled by the POS or triggered
    by a signed webhook. A done record holds the amount Musqet captured, the currency and the
    rail it reports. The over-refund cap and the rail check read it instead of the synced payment line, and each
    synced Musqet payment line is checked against it (pos.payment.musqet_settle_check).

    Created only by the server (sudo); nothing changes them from the browser. The upgrade
    to 19.0.1.3.0 also created done records for the sales synced before it
    (_musqet_record_synced_sales).
    """
    _name = 'pos.musqet.sale'
    _inherit = ['pos.musqet.record']
    _description = 'Musqet Sale'
    _musqet_kind = 'sale'

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
        """Settle as for any record; also, a done sale whose rail Musqet hadn't reported yet
        takes it from a later COMPLETE reply. A rail already set never changes."""
        if isinstance(response, dict) and response.get('status') == MUSQET_SUCCESS_STATUS:
            rail = parse_rail(response.get('rail'))
            if rail:
                self.filtered(lambda sale: sale.state == 'done' and not sale.rail).write({'rail': rail})
        super()._musqet_apply_response(response)

    def _musqet_done_vals(self, response):
        """A sale settles at the amount and currency Musqet reports captured, on its rail.

        Done means Musqet confirmed what it captured, so both are required; the rail is not
        (only a refund needs it, and a later reply may fill it in).
        """
        amount = parse_minor_amount(response.get('amountInCents'))
        currency = parse_iso_currency(response.get('currency'))
        if amount is None or currency is None:
            return None
        return {'amount_minor': amount, 'currency': currency, 'rail': parse_rail(response.get('rail'))}

    @api.model
    def _musqet_record_synced_sales(self):
        """Record each synced Musqet sale payment that has no record yet, as done.

        For the upgrade to 19.0.1.3.0 (see its post-migration); safe to run again. The
        payment's own amount and rail are the best evidence there is for a sale made before
        sales were recorded on the server. A payment that is a refund (negative, or linked to
        a sale it refunds), or on an order never paid, is left out.
        """
        # amount > 0 is the settle check's _musqet_is_sale_line: Odoo stores a payment's
        # amount rounded to its currency, so it is never a fraction of a minor unit.
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
            'rail': parse_rail(payment.musqet_rail),
        })
        _logger.info("pos_musqet: recorded %d sale(s) synced before this version", count)
