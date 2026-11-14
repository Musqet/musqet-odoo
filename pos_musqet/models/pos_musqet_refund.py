# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging

from odoo import _, api, fields, models

from .musqet_common import MUSQET_TERMINAL, minor_units, parse_minor_amount, sale_key_domain

_logger = logging.getLogger(__name__)

# At most this many pending refunds of a sale are re-read from Musqet per refund request, so
# refunds Musqet never resolves can't make every later refund wait on many slow calls. The
# ones not re-read stay pending and keep counting against the sale.
MUSQET_REFUND_REREAD_LIMIT = 5


class PosMusqetRefund(models.Model):
    """A refund Musqet accepted, recorded by the server when it sends the refund.

    The Musqet API keeps no link from a refund to the sale it reverses and enforces no
    cumulative limit (Musqet/musqet#2094), so the over-refund cap and the duplicate check
    in ``pos.payment.method.musqet_create_refund`` read these records. They are created
    only by the server (sudo), and exist as soon as Musqet accepts the refund, before the
    refund order reaches Odoo (except in the case the "Not covered" paragraph of
    musqet_create_refund names), or as soon as its reply is lost (unanswered, with no
    saleId). A POS manager can mark a pending one failed
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

    def _musqet_unanswered_message(self):
        return _(
            "Odoo did not get a clear answer from Musqet to a refund request for this order, so "
            "it can't tell whether the card was refunded. A manager must check it in the Musqet "
            "app and clear it under Musqet Refunds before this order can send another Musqet "
            "refund.")

    def _musqet_done_vals(self, response):
        """A refund settles at the amount Musqet reports refunded.

        A refund's status reply has the same shape as a sale's, the response shape of
        Musqet's partner API (Musqet/musqet#2094).
        """
        amount = parse_minor_amount(response.get('amountInCents'))
        return None if amount is None else {'amount_minor': amount}

    @api.model
    def _musqet_sale_refunds_domain(self, company, sale_id):
        """The domain of the refund records of sale ``sale_id`` in ``company``."""
        return [('company_id', '=', company.id), ('original_sale_id', '=', sale_id)]

    @api.model
    def _musqet_reread_pending(self, company, sale_id):
        """Ask Musqet about sale ``sale_id``'s oldest pending refunds, so failed ones stop counting.

        Runs before anything locks the sale's rows (see
        pos.payment.method.musqet_create_refund), so the calls to Musqet don't hold up other
        refunds of the sale. A refund that can't be re-read stays pending and keeps counting,
        the safe side for a cap. Only a Musqet method may call the API
        (pos.payment.method._musqet_check_method).
        """
        pending = self.sudo().search(
            self._musqet_sale_refunds_domain(company, sale_id) + [
                ('state', '=', 'pending'),
                # An unanswered refund has no saleId to read.
                ('sale_id', '!=', False),
                # A refund whose method is no longer a Musqet one can't be re-read, so it
                # mustn't use up the limit.
                ('payment_method_id.use_payment_terminal', '=', MUSQET_TERMINAL),
            ],
            order='id', limit=MUSQET_REFUND_REREAD_LIMIT)
        for refund in pending:
            refund.payment_method_id.musqet_get_sale(refund.sale_id)

    @api.model
    def _musqet_refusal_message(self, original, *, amount, reference):
        """Return why a refund of ``amount`` over the cap or repeating one is refused, else None.

        ``original`` is the pos.musqet.sale being refunded. Must run after
        original._musqet_claim_for_refund, so it sees every earlier refund's record.
        """
        all_recorded = self.sudo().search(
            self._musqet_sale_refunds_domain(original.company_id, original.sale_id))
        # All records, failed ones too: a synced payment of a refund Musqet reports as failed
        # must not count through the synced branch either.
        unrecorded_payments = all_recorded._musqet_unrecorded_synced(original)
        # A failed refund moved no money, so it neither counts nor blocks a retry.
        counting = all_recorded.filtered(lambda refund: refund.state != 'failed')
        # A refund of this refund order (of any sale) whose reply was lost may have gone
        # through: refuse before the cap (which counts it), so the cashier is told why.
        if self.sudo().search_count([
            ('company_id', '=', original.company_id.id), ('reference', '=', reference),
        ] + self._musqet_unanswered_domain(), limit=1):
            return self._musqet_unanswered_message()
        # Cumulative cap: the Musqet API enforces no limit across refunds (Musqet/musqet#2094),
        # so cap against what is left after every refund already sent for this sale.
        captured = original.amount_minor
        already_refunded = sum(counting.mapped('amount_minor')) + sum(
            abs(minor_units(payment.amount, payment.currency_id))
            for payment in unrecorded_payments)
        if amount > captured - already_refunded:
            return _("The refund amount exceeds the original payment.")
        # Duplicate check: the API has no idempotency key, so a refund re-sent with the same
        # order reference would refund twice. The key is the order reference, so one refund
        # order can carry one Musqet refund of a given sale.
        state = counting._musqet_reference_states(original).get(reference)
        if state == 'pending':
            # Say so plainly: a refund cancelled moments ago can still read as pending.
            return _("A refund for this order is still in progress at the terminal.")
        if state or any(
            reference in payment._musqet_order_references() for payment in unrecorded_payments
        ):
            return _("This refund has already been processed.")
        return None

    def _musqet_reference_states(self, original):
        """Map each order reference these refunds are known by to their state.

        A synced payment belongs to a refund when it has the refund's (method, saleId), the
        record's own key; a payment of another method that shares the saleId does not.

        A refund is known by the reference it was sent with and by both forms of its synced
        order's reference (a record backfilled at the upgrade holds at most one of them: none
        if several payments shared its saleId). A reference of a pending refund maps to
        'pending' even if a done one shares it.
        """
        state_by_key = {
            (refund.payment_method_id.id, refund.sale_id): refund.state for refund in self}
        states = {}

        def note(reference, state):
            if reference and states.get(reference) != 'pending':
                states[reference] = state

        for refund in self:
            note(refund.reference, refund.state)
        if self:
            for payment in self.env['pos.payment'].sudo().search(sale_key_domain(
                'transaction_id', self.mapped('sale_id'), self.payment_method_id,
            ) + [('company_id', '=', original.company_id.id)]):
                state = state_by_key.get((payment.payment_method_id.id, payment.transaction_id))
                if state:
                    for reference in payment._musqet_order_references():
                        note(reference, state)
        return states

    def _musqet_unrecorded_synced(self, original):
        """Synced Musqet refund payments of the original that have no refund record among these.

        The upgrade to 19.0.1.2.0 records every refund synced before it, so a payment here
        is one whose refund was never recorded: Odoo failed to save the record (logged), or
        Musqet accepted it while its response was lost. Its link is the musqet_refund_of the
        POS wrote, so it counts only if the POS still knew the original when it settled (not
        after a page refresh). A payment whose (method, saleId) has a record is left to the
        record, which Musqet's status keeps current.
        """
        recorded_keys = {(refund.payment_method_id.id, refund.sale_id) for refund in self}
        return [
            payment for payment in self.env['pos.payment'].sudo().search([
                ('musqet_refund_of', '=', original.sale_id),
                ('company_id', '=', original.company_id.id),
                # Only a settled Musqet refund (it has a saleId) can be a refund of a Musqet
                # sale, as in the backfill and the link hook.
                ('payment_method_id.use_payment_terminal', '=', MUSQET_TERMINAL),
                ('transaction_id', '!=', False),
            ])
            if (payment.payment_method_id.id, payment.transaction_id) not in recorded_keys
        ]

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
