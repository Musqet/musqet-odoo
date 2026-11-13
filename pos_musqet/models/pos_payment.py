# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging
from collections import Counter, defaultdict

from odoo import api, fields, models
from odoo.tools import SQL

from .musqet_common import MUSQET_TERMINAL, MUSQET_UNPAID_ORDER_STATES, minor_units, sale_key_domain

_logger = logging.getLogger(__name__)

# A payment write that touches one of these sets musqet_refund_of again from the refund record.
MUSQET_REFUND_LINK_FIELDS = {'transaction_id', 'payment_method_id', 'musqet_refund_of'}
# A payment write that touches one of these re-runs the settle check. musqet_settle_check is
# one of them, so a value written by anyone but the check itself is put right at once.
MUSQET_SETTLE_CHECK_FIELDS = {
    'transaction_id', 'payment_method_id', 'amount', 'pos_order_id', 'musqet_settle_check'}


class PosPayment(models.Model):
    _inherit = 'pos.payment'

    # The rail the Musqet terminal settled on, as the POS reports it from the top-level
    # ``rail`` field of the sale result ("card" | "bitcoin"), never inferred from metadata.
    # Stored on the payment so it round-trips on sync and is reloaded when a past order is
    # reopened, where the POS uses it to pick the refund path. The server's refund check
    # reads the rail from its own sale record (pos.musqet.sale) instead; only the upgrade to
    # 19.0.1.3.0 read this field, to record the sales synced before it.
    #
    # pos.payment does not override _load_pos_data_fields, so the POS data loader reads all
    # stored fields (read([]) == all fields) — this field therefore loads into the frontend
    # and serializes back on sync with no extra wiring, matching pos_razorpay/pos_pine_labs.
    musqet_rail = fields.Char(string="Musqet Rail", copy=False)
    # On a refund payment line, the Musqet saleId of the original card sale this refund
    # reverses. The Musqet API keeps no sale↔refund linkage of its own (Musqet/musqet#2094).
    # The POS sets it when a refund settles, and the server sets it from its own refund
    # record (pos.musqet.refund) whenever the line's saleId has one, which also covers a
    # refund settled after a page refresh, when the POS no longer knows the original. The
    # over-refund cap reads the refund records; this field is the link on the payment.
    musqet_refund_of = fields.Char(string="Musqet Refund Of", copy=False)
    # Whether Musqet confirmed this Musqet payment line. The POS marks an order paid on the
    # browser's word, so the server checks each synced line against its own records: a sale
    # line against the done pos.musqet.sale with its saleId (amount and currency Musqet
    # captured), a refund line against the done pos.musqet.refund (amount), and only one paid
    # line may claim each. Empty on a line of another method, and on a line of an order not
    # yet paid. A line synced before 19.0.1.3.0 starts empty, and is checked once it, its
    # order's state or a line sharing its saleId changes.
    musqet_settle_check = fields.Selection(
        [('verified', "Verified"), ('unverified', "Not verified")],
        string="Musqet Check", copy=False, readonly=True,
        help="Verified: Musqet reported this payment complete for this amount. Not verified: "
             "Odoo has no such report, so check the payment in the Musqet app.")

    @api.model_create_multi
    def create(self, vals_list):
        payments = super().create(vals_list)
        payments._musqet_link_recorded_refunds()
        payments._musqet_check_settles()
        return payments

    def write(self, vals):
        check = bool(MUSQET_SETTLE_CHECK_FIELDS & vals.keys())
        # The lines that shared a saleId with these before the write, which may now be the
        # only line of their sale. (_musqet_check_settles searches again after the write,
        # for the lines sharing the new saleIds.)
        previous = self._musqet_lines_sharing_sales() if check else self.browse()
        result = super().write(vals)
        if MUSQET_REFUND_LINK_FIELDS & vals.keys():
            self._musqet_link_recorded_refunds()
        if check:
            # The check's own write of the flag comes back here too and checks again. That
            # nested check may write other lines' flags first (when the outer one writes more
            # than one value), but every pass computes the same values, so it ends with all
            # of them right. (No exemption for the check's own write: a server path writing
            # POS values under sudo must not keep a forged flag either.)
            (self | previous)._musqet_check_settles()
        return result

    def unlink(self):
        others = self._musqet_lines_sharing_sales() - self
        result = super().unlink()
        others.exists()._musqet_check_settles()
        return result

    def _musqet_lines(self):
        """These payments that are Musqet lines with a saleId."""
        return self.filtered(
            lambda payment: payment.transaction_id
            and payment.payment_method_id.use_payment_terminal == MUSQET_TERMINAL)

    def _musqet_lines_sharing_sales(self):
        """Every payment with the (method, saleId) of one of these Musqet lines, these included."""
        lines = self._musqet_lines()
        if not lines:
            return self.browse()
        return self.sudo().search(sale_key_domain(
            'transaction_id', lines.mapped('transaction_id'), lines.payment_method_id))

    @api.model
    def _musqet_check_settles_of(self, records):
        """Re-check the payments carrying the saleIds of sale or refund ``records``."""
        if records:
            self.sudo().search(sale_key_domain(
                'transaction_id', records.mapped('sale_id'), records.payment_method_id,
            ))._musqet_check_settles()

    def _musqet_check_settles(self):
        """Set musqet_settle_check on these payments, and on every line sharing their saleIds.

        A line is checked against the server's done sale or refund record of its
        (method, saleId), and against the other paid lines claiming it: one Musqet sale or
        refund vouches for one line. When a saleId is copied onto another line, only the line
        of the order Odoo sent to Musqet (the record's reference) stays Verified; if no single
        line is that one, all of them are Not verified. Logs each Musqet line that becomes
        Not verified, so it can be checked by hand.
        """
        own_lines = self._musqet_lines()
        # Lock first, so a line of the same sale synced at the same time is counted below.
        own_lines.filtered(lambda line: line._musqet_order_is_paid())._musqet_lock_records()
        payments = self.sudo() | self._musqet_lines_sharing_sales()
        lines = payments._musqet_lines()
        sales, refunds = lines._musqet_done_records()
        claimants = defaultdict(list)
        for line in lines:
            if line._musqet_order_is_paid():
                claimants[line._musqet_claim_key()].append(line)
        to_write = defaultdict(lambda: self.sudo().browse())
        for payment in payments:
            value = payment._musqet_settle_value(sales, refunds, claimants)
            if payment.musqet_settle_check != value:
                to_write[value] |= payment
        for value, changed in to_write.items():
            if value == 'unverified':
                for payment in changed:
                    _logger.warning(
                        "Musqet payment %s (order %r, sale %r, amount %s) has no matching sale or "
                        "refund reported complete by Musqet, or shares it with another line",
                        payment.id, payment.pos_order_id.name, payment.transaction_id, payment.amount)
            changed.write({'musqet_settle_check': value})

    def _musqet_lock_records(self):
        """Make checks of these lines' sales and refunds run one after the other.

        Updates the record rows of these lines' saleIds (to the same values), as
        pos.payment.method._musqet_claim_original_sale does for refunds: a second
        transaction checking a line of the same sale waits, and if the first committed, its
        update fails with a serialisation error and Odoo retries it with a snapshot that
        holds the first line. Without this, two lines copying one saleId, synced at once,
        could each count only itself and both end up Verified. A saleId with no record has
        nothing to vouch for its lines, so it needs no lock.
        """
        if not self:
            return
        for table in ('pos_musqet_sale', 'pos_musqet_refund'):
            self.env.cr.execute(SQL(
                'UPDATE %s SET write_date = write_date'
                ' WHERE sale_id IN %s AND payment_method_id IN %s',
                SQL.identifier(table), tuple(self.mapped('transaction_id')),
                tuple(self.payment_method_id.ids)))

    def _musqet_done_records(self):
        """The done sale and refund records of these Musqet lines, each by (method, saleId)."""
        sales, refunds = {}, {}
        if self:
            domain = sale_key_domain('sale_id', self.mapped('transaction_id'), self.payment_method_id)
            for model, by_key in (('pos.musqet.sale', sales), ('pos.musqet.refund', refunds)):
                for record in self.env[model].sudo().search(domain + [('state', '=', 'done')]):
                    by_key[(record.payment_method_id.id, record.sale_id)] = record
        return sales, refunds

    def _musqet_order_is_paid(self):
        """Whether this payment's order is paid: only then is its Musqet line checked, does it
        claim its sale, and may it be refunded through the terminal."""
        return self.pos_order_id.state not in MUSQET_UNPAID_ORDER_STATES

    def _musqet_order_references(self):
        """Both forms of this payment's order reference, leaving out an empty one.

        The POS sends a sale's or refund's reference as order.pos_reference || order.uuid, so
        a reference may be either one.
        """
        order = self.pos_order_id
        return {reference for reference in (order.pos_reference, order.uuid) if reference}

    def _musqet_is_sale_line(self):
        """Whether this line takes money (a sale), rather than gives it back (a refund)."""
        return minor_units(self.amount, self.currency_id) > 0

    def _musqet_claim_key(self):
        """The sale or refund this line claims: (method, saleId, whether it is a sale)."""
        return (self.payment_method_id.id, self.transaction_id, self._musqet_is_sale_line())

    def _musqet_settle_value(self, sales, refunds, claimants):
        """This payment's musqet_settle_check, given the done records by (method, saleId) and
        the paid lines claiming each sale or refund (by _musqet_claim_key)."""
        self.ensure_one()
        if self.payment_method_id.use_payment_terminal != MUSQET_TERMINAL or not self._musqet_order_is_paid():
            return False
        key = (self.payment_method_id.id, self.transaction_id)
        is_sale = self._musqet_is_sale_line()
        record = (sales if is_sale else refunds).get(key)
        if not record:
            return 'unverified'
        rivals = claimants[self._musqet_claim_key()]
        if len(rivals) > 1:
            # Contested: the line of the order the command was sent for wins, if it is alone.
            sent_for = [line for line in rivals if record.reference in line._musqet_order_references()]
            if sent_for != [self]:
                return 'unverified'
        amount = minor_units(self.amount, self.currency_id)
        if is_sale:
            verified = record.amount_minor == amount and record.currency == self.currency_id.name
        else:
            verified = record.amount_minor == -amount
        return 'verified' if verified else 'unverified'

    def _musqet_link_recorded_refunds(self):
        """Set musqet_refund_of from the server's refund record, over what the POS sent."""
        payments = self._musqet_lines()
        if not payments:
            return
        refunds = self.env['pos.musqet.refund'].sudo().search(sale_key_domain(
            'sale_id', payments.mapped('transaction_id'), payments.payment_method_id))
        original_by_refund = {
            (refund.payment_method_id.id, refund.sale_id): refund.original_sale_id
            for refund in refunds
        }
        to_link = defaultdict(lambda: self.browse())
        for payment in payments:
            original_sale_id = original_by_refund.get(
                (payment.payment_method_id.id, payment.transaction_id))
            if original_sale_id and payment.musqet_refund_of != original_sale_id:
                to_link[original_sale_id] |= payment
        for original_sale_id, linked in to_link.items():
            # This write re-enters write(), which finds every link already right and writes
            # nothing more. (No context flag: an RPC caller sets the context, and could set
            # it to keep a forged link.)
            linked.sudo().write({'musqet_refund_of': original_sale_id})
