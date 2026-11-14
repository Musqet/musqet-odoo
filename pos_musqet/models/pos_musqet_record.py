# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging
from collections import Counter

from odoo import _, api, fields, models
from odoo.tools import SQL

from .musqet_common import (
    MUSQET_FAILURE_STATUSES,
    MUSQET_MAX_MINOR_AMOUNT,
    MUSQET_SUCCESS_STATUS,
    reply_outcome_is_unknown,
    sale_key_domain,
)

_logger = logging.getLogger(__name__)


class PosMusqetRecord(models.AbstractModel):
    """What the server's records of Musqet sales and refunds share.

    Each record is one Musqet saleId taken on one payment method, created only by the
    server (sudo) and settled only from Musqet's own replies. pos.musqet.sale and
    pos.musqet.refund add what their kind needs.

    A record with no saleId is unanswered: a request whose reply never reached Odoo, so
    Musqet may have accepted it (_musqet_record_sent). Nothing can read it from Musqet (the
    API has no lookup by reference), so it stays pending, blocking its order's next request
    and counting as a pending one would, until a POS manager who has checked it in the
    Musqet app marks it failed (action_musqet_mark_failed).
    """
    _name = 'pos.musqet.record'
    _description = 'Musqet Sale or Refund Record'
    _order = 'id desc'
    # What a record is, for log lines: 'sale' or 'refund'.
    _musqet_kind = 'record'

    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    payment_method_id = fields.Many2one(
        'pos.payment.method', required=True, readonly=True, ondelete='restrict')
    sale_id = fields.Char(
        readonly=True, index=True,
        help="The Musqet saleId. Empty if Musqet's reply to the request never reached Odoo.")
    reference = fields.Char(readonly=True, help="The order reference sent to Musqet.")
    amount_minor = fields.Integer(string="Amount (minor units)", required=True, readonly=True)
    state = fields.Selection(
        [('pending', "Pending"), ('done', "Done"), ('failed', "Failed")],
        required=True, default='pending', readonly=True)

    def action_musqet_mark_failed(self):
        """Mark these records failed, for a POS manager who has checked them in the Musqet app.

        A pending record keeps counting (and an unanswered one keeps blocking its order) until
        Musqet reports how it ended. If that can never happen (Musqet doesn't know the saleId,
        the method has lost its Musqet configuration, or the reply to the request was lost),
        this is the way out. Only the records _musqet_may_mark_failed allows move.

        It can't be undone: a failed record is never re-read. (A failed refund's synced
        payment doesn't count either, so its amount leaves the over-refund cap for good.)
        """
        # Explicit, not left to the read of `state`. Read access is POS managers' only
        # (ir.model.access.csv), within their companies (pos_musqet_security.xml).
        self.check_access('read')
        self.filtered(lambda record: record._musqet_may_mark_failed()).sudo().write({'state': 'failed'})

    def _musqet_is_unanswered(self):
        """Whether this record is unanswered and still pending: the reply to its request never
        reached Odoo (it has no saleId), and no manager has marked it failed."""
        self.ensure_one()
        return self.state == 'pending' and not self.sale_id

    @api.model
    def _musqet_unanswered_domain(self):
        """The domain of the records _musqet_is_unanswered selects. The managers' "Reply lost"
        filters (views/pos_musqet_sale_views.xml, views/pos_musqet_refund_views.xml) copy it."""
        return [('state', '=', 'pending'), ('sale_id', '=', False)]

    @api.model
    def _musqet_has_unanswered(self, company, reference):
        """Whether order ``reference`` of ``company`` has an unanswered record of this kind, on
        any payment method (it is never read, so a method no longer Musqet's still counts)."""
        return bool(self.sudo().search_count([
            ('company_id', '=', company.id), ('reference', '=', reference),
        ] + self._musqet_unanswered_domain(), limit=1))

    def _musqet_may_mark_failed(self):
        """Whether a POS manager may mark this record failed: it is pending."""
        self.ensure_one()
        return self.state == 'pending'

    def _musqet_unanswered_message(self):
        """The message refusing a request of an order with an unanswered record of this kind."""
        raise NotImplementedError

    def _musqet_apply_response(self, response):
        """Settle the pending records as Musqet's ``response`` (its own word about them) says.

        A failure status fails them. COMPLETE makes them done with the values
        _musqet_done_vals takes from the response, Musqet's word over what was requested; a
        COMPLETE without them (say, no valid amount) leaves them pending, and is logged. Any
        other status leaves them as they are. Only a pending record moves: a settled one
        keeps its state whatever a later read says, and one already in the right state isn't
        rewritten on every poll.
        """
        pending = self.filtered(lambda record: record.state == 'pending')
        if not pending or not isinstance(response, dict):
            return
        status = response.get('status')
        if status in MUSQET_FAILURE_STATUSES:
            pending.write({'state': 'failed'})
            return
        if status != MUSQET_SUCCESS_STATUS:
            return
        vals = self._musqet_done_vals(response)
        if vals is None:
            _logger.warning(
                "Musqet reported %s(s) %r complete without the values Odoo needs to settle it",
                self._musqet_kind, pending.mapped('sale_id'))
            return
        pending.write({**vals, 'state': 'done'})
        self.env['pos.payment']._musqet_check_settles_of(pending)

    def _musqet_needs_status_read(self):
        """Whether a status read of this record could still settle something: it is pending."""
        self.ensure_one()
        return self.state == 'pending'

    def _musqet_done_vals(self, response):
        """The values a COMPLETE ``response`` settles a record with, or None if it lacks them."""
        raise NotImplementedError

    @api.model
    def _musqet_record_sent(self, response, vals, details):
        """Record a sale or refund request sent to Musqet, as its create ``response`` says.

        Returns None if Musqet's answer is known, else the message for the POS. A response
        with a saleId is recorded under it (_musqet_record_accepted); one that leaves unknown
        whether Musqet accepted the request (reply_outcome_is_unknown) is recorded unanswered
        (_musqet_record_unanswered). Musqet's refusal records nothing. ``vals`` are the
        record's values but the saleId; ``details`` describe the request for the logs.
        """
        sale_id = response.get('saleId') if isinstance(response, dict) else None
        if sale_id:
            self._musqet_record_accepted(response, str(sale_id), vals, details)
            return None
        if reply_outcome_is_unknown(response):
            return self._musqet_record_unanswered(vals, details)
        return None

    @api.model
    def _musqet_record_accepted(self, response, sale_id, vals, details):
        """Record the request Musqet accepted as ``sale_id``, settled as ``response`` says.

        Runs in a savepoint, and never raises: Musqet already has the command, so the POS must
        still get the saleId to poll it, and failing the request would leave the terminal armed
        with a command Odoo knows nothing about. A failure is logged with ``details``, enough
        to reconcile it by hand.
        """
        try:
            with self.env.cr.savepoint():
                self.sudo().create({'sale_id': sale_id, **vals})._musqet_apply_response(response)
        except Exception:
            _logger.exception(
                "Musqet accepted %s %r (%s) but Odoo could not record it",
                self._musqet_kind, sale_id, details)

    @api.model
    def _musqet_record_unanswered(self, vals, details):
        """Record a request Musqet may have accepted, its reply lost; return the POS message.

        Pending with no saleId until a manager marks it failed, and the message says the
        order is blocked until then. If it can't be saved (in a savepoint, logged), nothing
        blocks a retry, and the message says to check the Musqet app first.
        """
        try:
            with self.env.cr.savepoint():
                self.sudo().create(vals)
        except Exception:
            _logger.exception(
                "Musqet's reply to a %s request (%s) was lost or unreadable, and Odoo could not "
                "record it: Musqet may have accepted it", self._musqet_kind, details)
            return _(
                "Odoo did not get a clear answer from Musqet to this request, and could not "
                "record it, so it can't tell whether it went through. Check the Musqet app "
                "before trying again.")
        _logger.warning(
            "Musqet's reply to a %s request (%s) was lost or unreadable: recorded it unanswered, "
            "blocking the order until a manager checks it in the Musqet app",
            self._musqet_kind, details)
        return self._musqet_unanswered_message()

    @api.model
    def _musqet_lock(self, condition):
        """Update this model's rows matching ``condition`` (an SQL condition) to the same values.

        A no-op write that takes the rows' locks: a concurrent transaction updating them waits,
        and if this one commits first it fails with a serialisation error, so Odoo retries it
        with a snapshot that sees this one's work. A plain lock would leave it its old snapshot.
        """
        self.env.cr.execute(SQL(
            'UPDATE %s SET write_date = write_date WHERE %s', SQL.identifier(self._table), condition))

    @api.model
    def _musqet_record_unrecorded(self, payments, vals_for):
        """Create a done record for each payment whose (method, saleId) has none.

        For the upgrade backfills; safe to run again. ``vals_for(payment)`` gives the
        model's own values. Returns how many were created.

        The reference is the payment's order's, unless several payments share the
        (method, saleId): which of them the command was sent for is unknown, so the record
        names none, and the settle check can't pick one of them as genuine. An amount too
        large to store is skipped and logged rather than failing the upgrade.
        """
        shared = Counter((payment.payment_method_id.id, payment.transaction_id) for payment in payments)
        recorded = {
            (record.payment_method_id.id, record.sale_id)
            for record in self.sudo().search(sale_key_domain(
                'sale_id', payments.mapped('transaction_id'), payments.payment_method_id))
        }
        vals_list = []
        for payment in payments:
            key = (payment.payment_method_id.id, payment.transaction_id)
            if key in recorded:
                continue
            recorded.add(key)
            vals = vals_for(payment)
            if abs(vals['amount_minor']) > MUSQET_MAX_MINOR_AMOUNT:
                _logger.warning(
                    "pos_musqet: not recording Musqet %s %r of payment %s: amount %s is too large",
                    self._musqet_kind, payment.transaction_id, payment.id, payment.amount)
                continue
            order = payment.pos_order_id
            vals_list.append({
                'company_id': payment.company_id.id,
                'payment_method_id': payment.payment_method_id.id,
                'sale_id': payment.transaction_id,
                'reference': (order.pos_reference or order.uuid) if shared[key] == 1 else False,
                'state': 'done',
                **vals,
            })
        self.sudo().create(vals_list)
        return len(vals_list)
