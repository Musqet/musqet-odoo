# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging
from collections import Counter

from odoo import api, fields, models
from odoo.tools import SQL

from .musqet_common import (
    MUSQET_FAILURE_STATUSES,
    MUSQET_MAX_MINOR_AMOUNT,
    MUSQET_SUCCESS_STATUS,
    sale_key_domain,
)

_logger = logging.getLogger(__name__)


class PosMusqetRecord(models.AbstractModel):
    """What the server's records of Musqet sales and refunds share.

    Each record is one Musqet saleId taken on one payment method, created only by the
    server (sudo) and settled only from Musqet's own replies. pos.musqet.sale and
    pos.musqet.refund add what their kind needs.
    """
    _name = 'pos.musqet.record'
    _description = 'Musqet Sale or Refund Record'
    _order = 'id desc'
    # What a record is, for log lines: 'sale' or 'refund'.
    _musqet_kind = 'record'

    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    payment_method_id = fields.Many2one(
        'pos.payment.method', required=True, readonly=True, ondelete='restrict')
    sale_id = fields.Char(required=True, readonly=True, index=True, help="The Musqet saleId.")
    reference = fields.Char(readonly=True, help="The order reference sent to Musqet.")
    amount_minor = fields.Integer(string="Amount (minor units)", required=True, readonly=True)
    state = fields.Selection(
        [('pending', "Pending"), ('done', "Done"), ('failed', "Failed")],
        required=True, default='pending', readonly=True)

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
    def _musqet_record_accepted(self, response, vals, details):
        """Record a sale or refund Musqet accepted (its create ``response``), with ``vals``.

        An {error} response carries no saleId, so nothing is recorded for it. Runs in a
        savepoint, and never raises: Musqet already has the command, so the POS must still
        get the saleId to poll it, and failing the request would leave the terminal armed
        with a command Odoo knows nothing about. A failure is logged with ``details``, enough
        to reconcile it by hand.
        """
        sale_id = response.get('saleId') if isinstance(response, dict) else None
        if not sale_id:
            return
        try:
            with self.env.cr.savepoint():
                self.sudo().create({'sale_id': str(sale_id), **vals})._musqet_apply_response(response)
        except Exception:
            _logger.exception(
                "Musqet accepted %s %r (%s) but Odoo could not record it",
                self._musqet_kind, sale_id, details)

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
