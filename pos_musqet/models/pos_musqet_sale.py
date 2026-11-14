# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.tools import SQL

from .musqet_common import (
    MUSQET_RAIL_SELECTION,
    MUSQET_SUCCESS_STATUS,
    MUSQET_TERMINAL,
    MUSQET_UNPAID_ORDER_STATES,
    minor_units,
    parse_iso_currency,
    parse_minor_amount,
    parse_rail,
    reply_is_not_found,
    sale_key_domain,
)

_logger = logging.getLogger(__name__)

# At most this many pending sales of an order are read from Musqet before another sale of it
# is started (pos.musqet.sale._musqet_sale_to_resume). An order normally has at most one: the
# terminal takes one command at a time.
MUSQET_SALE_REREAD_LIMIT = 5
# A pending sale older than this that Musqet answers not found for no longer blocks a new sale
# of its order: Musqet times a sale out after about five minutes, so it can't still be paid.
MUSQET_SALE_STALE_AFTER = timedelta(minutes=15)
# The first key of the advisory lock on starting a sale of an order (_musqet_try_lock_order):
# ASCII "MS", the namespace of the two-key lock. The second key is a 32-bit hash of the
# company and order reference, so two orders may now and then share a lock; the cost is a
# rare, false "already being started" refusal.
MUSQET_SALE_START_LOCK = 0x4D53
# At most this many saleIds of the order's lines are taken from the POS: an order has a
# handful of payment lines.
MUSQET_LINE_SALE_IDS_LIMIT = 100
# A saleId from the POS longer than this is ignored (Musqet's are far shorter).
MUSQET_SALE_ID_MAX_LENGTH = 255


class PosMusqetSale(models.Model):
    """A sale Odoo started on a Musqet terminal, settled from what Musqet reports.

    The POS marks an order paid on the browser's word, so the server keeps its own record of
    each sale: created when Musqet accepts the sale (musqet_create_sale), and settled from
    Musqet's own replies to a status read (musqet_get_sale), polled by the POS or triggered
    by a signed webhook. A done record holds the amount Musqet captured, the currency and the
    rail it reports. The over-refund cap and the rail check read it instead of the synced
    payment line, and each synced Musqet payment line is checked against it
    (pos.payment.musqet_settle_check).

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
    not_found_at = fields.Datetime(
        string="Not Found at Musqet", readonly=True,
        help="When Musqet answered that it has no such sale, long after the sale was started. "
             "It no longer blocks a new sale of its order, and is not read again. Check it in "
             "the Musqet app: it may have been paid under a previous API key.")

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

    def _musqet_needs_status_read(self):
        """Also a done sale with no rail yet: a later reply fills it in, and until then the
        sale can't be refunded (only a card sale can)."""
        self.ensure_one()
        return super()._musqet_needs_status_read() or (self.state == 'done' and not self.rail)

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
    def _musqet_resolve_sale(self, payment):
        """The done sale record of ``payment``'s (method, saleId) in its company, or nothing.

        A record still waiting on Musqet (_musqet_needs_status_read: its result never read
        back, say the POS closed mid-sale, or settled without a rail) is read from Musqet
        first, with the method that made the sale. One that can't be read stays as it is and
        can't be refunded.
        """
        sale = self.sudo().search([
            ('company_id', '=', payment.company_id.id),
            ('payment_method_id', '=', payment.payment_method_id.id),
            ('sale_id', '=', payment.transaction_id),
        ])
        if sale and sale._musqet_needs_status_read():
            sale.payment_method_id.musqet_get_sale(sale.sale_id)
        return sale.filtered(lambda record: record.state == 'done')

    @api.model
    def _musqet_sale_to_resume(self, method, *, reference, amount, currency, line_sale_ids):
        """The sale of order ``reference`` a new sale request on ``method`` should resume.

        Returns ``(sale, message)``: refuse the request with ``message`` if there is one,
        else resume ``sale`` if it is set, else send a new sale.

        The POS gives up on a sale it has polled for too long, or lost contact over, and a
        page refresh forgets the sale it was waiting on, yet the customer can still pay it
        on the terminal. A retry must not send a second sale for the same payment. So the
        order's open sales (_musqet_open_sales_of) are read from Musqet first if still
        pending, newest first, then:

        1. one still pending that this request may not resume refuses the request: it may
           still be paid, so it has to end first (cancel it, or let it time out);
        2. otherwise the newest one on ``method`` for this amount and ``currency`` is resumed,
           a pending one before a done one: the POS polls it, and settles the line with it
           once Musqet reports it complete;
        3. otherwise a new sale is sent, as when a done sale was for another amount. (Such a
           sale, on no payment line, is money Musqet took that the order doesn't show; it is
           not refused, as nothing in the POS could clear it. Its record is under Musqet
           Sales, and a line copying it is Not verified.)

        A sale may be resumed only if its method, amount and currency are the request's, it
        is not on one of the order's lines (``line_sale_ids``, the saleIds on them, which the
        POS sends), and this request's read of it, if any, didn't get a 404 of any kind
        (Musqet doesn't know it, or the method can't reach Musqet: either way polling it
        would fail). A done sale on a line is left out; a pending one
        on a line still refuses, as does any pending sale that can't be resumed.
        ``line_sale_ids`` comes from the browser, so it can only refuse more, or send a new
        sale where one was done, never hide a pending sale.

        A pending sale whose read got Musqet's own not-found reply (reply_is_not_found) and
        that is older than MUSQET_SALE_STALE_AFTER can't still be paid on the terminal: it
        is stamped not_found_at, logged once for reconciliation, and left out of this request
        and every later one (never read again). Its state stays pending, as it may have been
        paid (a sale of the business the API key belonged to before it was changed reads as
        not found too). A recent one still refuses. A read that fails
        any other way (Musqet unreachable, or the method's configuration broken, say a base
        URL whose every route answers 404 without Musqet's error body: an administrator must
        fix the method), or a sale past the read limit, still counts as pending.

        Not checked here: that ``reference`` is an order of the caller's session (a done sale
        of another order resumed onto a line leaves that line Not verified, as the sale's
        reference names its own order), or that the caller uses the methods whose sales are
        read; scoping a cashier to their own POS is a separate check.

        Requests to start a sale of one order are decided one at a time: a second one while
        the first is still being decided (and sent) is refused. The lock lasts until the
        first request's transaction ends, so its sale is recorded by then. Not covered: a
        second request that read the database just before the first one committed and takes
        the lock just after (a window of milliseconds) still misses the first sale's record;
        the terminal's own 409 CONFLICT stops it on the same terminal.
        """
        company = method._musqet_company()
        if not self._musqet_try_lock_order(company, reference):
            return self.browse(), _(
                "A Musqet payment for this order is already being started. Wait for it, then "
                "try again.")
        open_sales = self._musqet_open_sales_of(company, reference)
        unreadable = self._musqet_read_pending(open_sales)
        on_lines = self._musqet_line_sale_ids(line_sale_ids)
        # After the reads: a sale the POS has on a line may only now read as done, and one
        # Musqet no longer knows may only now be stamped.
        open_sales = open_sales.filtered(
            lambda sale: sale.state != 'failed' and not sale.not_found_at
            and not (sale.state == 'done' and sale.sale_id in on_lines))

        def resumable(sale):
            return (sale.payment_method_id == method and sale.amount_minor == amount
                    and sale.currency == currency and sale.sale_id not in on_lines
                    and sale not in unreadable)

        pending = open_sales.filtered(lambda sale: sale.state == 'pending')
        resumable_pending = pending.filtered(resumable)
        if pending - resumable_pending:
            return self.browse(), _(
                "A Musqet payment for this order is still in progress. Finish or cancel it on "
                "the terminal, then try again. If this keeps happening, ask a manager to check "
                "the Musqet payment methods.")
        candidates = resumable_pending or open_sales.filtered(resumable)
        return candidates.sorted('id', reverse=True)[:1], None

    @api.model
    def _musqet_read_pending(self, sales):
        """Read the newest pending ones of ``sales`` from Musqet; return those that got a 404.

        Reads at most MUSQET_SALE_REREAD_LIMIT. One older than MUSQET_SALE_STALE_AFTER that
        got Musqet's own not-found reply is stamped not_found_at and logged, once (see
        _musqet_sale_to_resume).
        """
        to_read = sales.filtered(lambda sale: sale.state == 'pending').sorted('id', reverse=True)
        cutoff = fields.Datetime.now() - MUSQET_SALE_STALE_AFTER
        unreadable = self.sudo().browse()
        for sale in to_read[:MUSQET_SALE_REREAD_LIMIT]:
            reply = sale.payment_method_id.musqet_get_sale(sale.sale_id)
            error = reply.get('error') if isinstance(reply, dict) else None
            if isinstance(error, dict) and error.get('status_code') == 404:
                unreadable |= sale
            if sale.create_date < cutoff and reply_is_not_found(reply):
                sale.not_found_at = fields.Datetime.now()
                _logger.warning(
                    "Musqet no longer knows sale %r (method %s, order %r), pending since %s: "
                    "no longer blocking the order. Check it in the Musqet app.",
                    sale.sale_id, sale.payment_method_id.id, sale.reference, sale.create_date)
        return unreadable

    @api.model
    def _musqet_order_lock_key(self, company, reference):
        """The second key of the advisory lock on starting a sale of order ``reference``."""
        return '%s:%s' % (company.id, reference)

    @api.model
    def _musqet_try_lock_order(self, company, reference):
        """Take the lock on starting a sale of order ``reference`` in ``company``, if it is free.

        A transaction-level advisory lock, so it is released when the transaction ends. Not
        waited for: a waiting transaction would keep its old snapshot (REPEATABLE READ), and
        miss the sale the first one records.
        """
        self.env.cr.execute(SQL(
            'SELECT pg_try_advisory_xact_lock(%s, hashtext(%s))',
            MUSQET_SALE_START_LOCK, self._musqet_order_lock_key(company, reference)))
        return self.env.cr.fetchone()[0]

    @api.model
    def _musqet_line_sale_ids(self, line_sale_ids):
        """The usable saleIds of ``line_sale_ids`` (from the POS), as a set."""
        if not isinstance(line_sale_ids, (list, tuple)):
            return set()
        return {
            sale_id for sale_id in line_sale_ids[:MUSQET_LINE_SALE_IDS_LIMIT]
            if isinstance(sale_id, str) and len(sale_id) <= MUSQET_SALE_ID_MAX_LENGTH
        }

    @api.model
    def _musqet_open_sales_of(self, company, reference):
        """The sales of order ``reference`` in ``company`` that a new sale may repeat.

        Those Musqet hasn't failed or said it no longer knows (not_found_at), on a method
        that is still a Musqet one (a record of any other can't be read), and not on a synced
        payment with their (method, saleId).
        """
        sales = self.sudo().search([
            ('company_id', '=', company.id),
            ('reference', '=', reference),
            ('state', '!=', 'failed'),
            ('payment_method_id.use_payment_terminal', '=', MUSQET_TERMINAL),
            ('not_found_at', '=', False),
        ], order='id')
        on_lines = {
            (payment.payment_method_id.id, payment.transaction_id)
            for payment in self.env['pos.payment'].sudo().search(sale_key_domain(
                'transaction_id', sales.mapped('sale_id'), sales.payment_method_id))
        } if sales else set()
        return sales.filtered(lambda sale: (sale.payment_method_id.id, sale.sale_id) not in on_lines)

    def _musqet_claim_for_refund(self):
        """Make refunds of this sale decide one after the other, each seeing the last.

        Every refund decision updates the sale records with this sale's saleId in its
        company (to the same values), the same key the cap counts refunds by, so two
        refunds of one sale still meet even through two methods' records of it. A second
        decision on the same sale waits for the first transaction to end. If the first one
        committed, the update fails with a serialisation error: Odoo runs a request in
        REPEATABLE READ, so its snapshot may predate the first refund's record. Odoo then
        retries the whole request with a fresh snapshot that includes the record. A lock alone
        would not do: the waiting transaction would keep its old snapshot. The retry is safe
        only while nothing has been sent, which is why pos.payment.method.musqet_create_refund
        flushes every pending write before it sends the refund.

        The row lock lasts until commit, so it spans the call to Musqet (up to
        MUSQET_TIMEOUT, in models/pos_payment_method.py). Only the server writes sale
        records, so nothing else waits on it but another refund of the sale, a status read of
        it, or a check of its payment lines (pos.payment._musqet_lock_records). The refund's
        own check of the original line (pos.payment.method._musqet_settled_original) takes the
        same lock a little earlier, after its calls to Musqet, and also holds the line's row if
        it changed its flag, so a session close touching the line waits too.
        """
        self.ensure_one()
        self._musqet_lock(SQL('company_id = %s AND sale_id = %s', self.company_id.id, self.sale_id))

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
