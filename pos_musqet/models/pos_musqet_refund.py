# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models

from .musqet_common import MUSQET_TERMINAL, minor_units, parse_minor_amount, sale_key_domain

_logger = logging.getLogger(__name__)


class PosMusqetRefund(models.Model):
    """A refund Musqet accepted, recorded by the server when it sends the refund.

    The Musqet API keeps no link from a refund to the sale it reverses and enforces no
    cumulative limit (Musqet/musqet#2094), so the over-refund cap and the duplicate check
    in ``pos.payment.method.musqet_create_refund`` read these records. They are created
    only by the server (sudo), and exist as soon as Musqet accepts the refund, before the
    refund order reaches Odoo (except in the cases the "Not covered" paragraph of
    musqet_create_refund names). A POS manager can mark a pending one failed
    (action_musqet_mark_failed); nothing else changes them from the browser. The upgrade
    to 19.0.1.2.0 also created done records for the refunds synced before it
    (_musqet_record_synced_refunds).
    """
    _name = 'pos.musqet.refund'
    _inherit = ['pos.musqet.record']
    _description = 'Musqet Refund'
    _musqet_kind = 'refund'

    original_sale_id = fields.Char(
        string="Original Musqet Sale", required=True, readonly=True, index=True,
        help="The Musqet saleId of the card sale this refund reverses.")
    sale_id = fields.Char(string="Musqet Refund Sale", help="The Musqet saleId of the refund itself.")
    reference = fields.Char(help="The refund order reference sent to Musqet.")
    amount_minor = fields.Integer(
        help="The amount in minor units of the company currency (cents): the amount requested "
             "while pending, the amount Musqet reports refunded once done.")
    state = fields.Selection(help="Pending and Done count against the original sale; Failed does not.")

    _unique_sale = models.Constraint(
        'UNIQUE (payment_method_id, sale_id)', 'This Musqet refund is already recorded.')

    def action_musqet_mark_failed(self):
        """Mark pending refunds failed, for a POS manager who has checked them in Musqet.

        A pending refund keeps counting against its sale until Musqet reports how it ended.
        If that read can never succeed (Musqet doesn't know the saleId, or the method has
        lost its Musqet configuration), this is the way out. Only pending refunds move.

        It can't be undone: a failed refund is never re-read, and its synced payment doesn't
        count either, so the amount leaves the cap for good.
        """
        # Explicit, not left to the read of `state`. Read access is POS managers' only
        # (ir.model.access.csv), within their companies (pos_musqet_security.xml).
        self.check_access('read')
        self.filtered(lambda refund: refund.state == 'pending').sudo().write({'state': 'failed'})

    def _musqet_done_vals(self, response):
        """A refund settles at the amount Musqet reports refunded (the sale partner shape)."""
        amount = parse_minor_amount(response.get('amountInCents'))
        return None if amount is None else {'amount_minor': amount}

    @api.model
    def _musqet_forget_shared_references(self):
        """Clear the reference of each refund record that several linked refund lines carry.

        For the upgrade to 19.0.1.3.0 (see its post-migration). The 19.0.1.2.0 backfill took
        the reference of an arbitrary one of the linked payments, so for a saleId several of
        them shared, it named one of their orders arbitrarily, and the settle check would take
        that order's line as the genuine one (even a copy on a paid order, with the genuine
        line on an unpaid one). A record is cleared when at least two linked refund lines, of
        any order state, carry it and its reference is one of their orders. With no
        reference, none of those lines is Verified (the settle check then needs a line no
        other line carries), so clearing never makes a line Verified. A reference that names
        none of their orders is no guess of that backfill, and is kept: the duplicate check
        may know that refund only by it. The duplicate check still knows a cleared refund by
        its synced payments' order references.
        """
        refunds = self.sudo().search([('reference', '!=', False), ('state', '=', 'done')])
        payments = self.env['pos.payment'].sudo().search(sale_key_domain(
            'transaction_id', refunds.mapped('sale_id'), refunds.payment_method_id,
        ) + [('musqet_refund_of', '!=', False)])
        # Linked refund lines of every order state, by their claim: the lines the old backfill
        # chose among.
        carriers = payments._musqet_claimants(paid_only=False)
        Payment = self.env['pos.payment']

        def has_guessed_reference(refund):
            lines = carriers[Payment._musqet_claim_key_for(
                refund.payment_method_id.id, refund.sale_id, is_sale=False)]
            return len(lines) > 1 and any(
                refund.reference in line._musqet_order_references() for line in lines)

        shared = refunds.filtered(has_guessed_reference)
        shared.write({'reference': False})
        _logger.info("pos_musqet: cleared the order reference of %d shared refund(s)", len(shared))

    @api.model
    def _musqet_record_synced_refunds(self):
        """Record each synced, linked Musqet refund payment that has no record yet, as done.

        For the upgrade to 19.0.1.2.0 (see its post-migration); safe to run again. The
        payment's own amount and link are the best evidence there is for a refund made
        before refunds were recorded on the server. Unlike the sale backfill, a refund line of
        an order never paid is recorded too: the terminal refunded the card all the same, so
        it counts against the sale.
        """
        payments = self.env['pos.payment'].sudo().search([
            ('musqet_refund_of', '!=', False),
            ('transaction_id', '!=', False),
            ('payment_method_id.use_payment_terminal', '=', MUSQET_TERMINAL),
        ])
        count = self._musqet_record_unrecorded(payments, lambda payment: {
            'original_sale_id': payment.musqet_refund_of,
            'amount_minor': abs(minor_units(payment.amount, payment.currency_id)),
        })
        _logger.info("pos_musqet: recorded %d refund(s) synced before this version", count)
