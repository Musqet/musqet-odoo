# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import api, fields, models

from .musqet_common import MUSQET_FAILURE_STATUSES, MUSQET_SUCCESS_STATUS, minor_units

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

    original_sale_id = fields.Char(
        string="Original Musqet Sale", required=True, readonly=True, index=True,
        help="The Musqet saleId of the card sale this refund reverses.")
    sale_id = fields.Char(string="Musqet Refund Sale", help="The Musqet saleId of the refund itself.")
    reference = fields.Char(help="The refund order reference sent to Musqet.")
    amount_minor = fields.Integer(
        help="The refunded amount in minor units of the company currency (cents).")
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

    def _musqet_apply_status(self, status):
        """Settle the pending refunds as a Musqet status says; other statuses leave them.

        Only a pending refund moves: a settled one keeps its state whatever a later read
        says, and a refund already in the right state isn't rewritten on every poll.
        """
        pending = self.filtered(lambda refund: refund.state == 'pending')
        if status == MUSQET_SUCCESS_STATUS:
            pending.write({'state': 'done'})
            self.env['pos.payment']._musqet_check_settles_of(pending)
        elif status in MUSQET_FAILURE_STATUSES:
            pending.write({'state': 'failed'})

    @api.model
    def _musqet_record_synced_refunds(self):
        """Record each synced, linked Musqet refund payment that has no record yet, as done.

        For the upgrade to 19.0.1.2.0 (see its post-migration); safe to run again. The
        payment's own amount and link are the best evidence there is for a refund made
        before refunds were recorded on the server.
        """
        payments = self.env['pos.payment'].sudo().search([
            ('musqet_refund_of', '!=', False),
            ('transaction_id', '!=', False),
            ('payment_method_id.use_payment_terminal', '=', 'musqet'),
        ])
        count = self._musqet_record_unrecorded(payments, lambda payment: {
            'original_sale_id': payment.musqet_refund_of,
            'amount_minor': abs(minor_units(payment.amount, payment.currency_id)),
        })
        _logger.info("pos_musqet: recorded %d refund(s) synced before this version", count)
