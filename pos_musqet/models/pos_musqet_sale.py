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
    sale_key_domain,
)

_logger = logging.getLogger(__name__)

# At most this many pending sales of an order are read from Musqet before another sale of it
# is started (pos.musqet.sale._musqet_sale_to_resume). An order normally has at most one: the
# terminal takes one command at a time.
MUSQET_SALE_REREAD_LIMIT = 5
# A pending sale older than this that Musqet still can't settle no longer blocks a new sale
# of its order: Musqet times a sale out after about five minutes.
MUSQET_SALE_STALE_AFTER = timedelta(minutes=15)
# The first key of the advisory lock on starting a sale of an order (_musqet_try_lock_order);
# the second is a hash of the company and order reference.
MUSQET_SALE_START_LOCK = 0x4D53
# At most this many saleIds of the order's lines are taken from the POS, each at most this
# long: an order has a handful of payment lines.
MUSQET_SETTLED_SALE_IDS_LIMIT = 100
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
    def _musqet_sale_to_resume(self, method, *, reference, amount, currency, settled_sale_ids):
        """The sale of order ``reference`` a new sale request on ``method`` should resume.

        Returns ``(sale, message)``: refuse the request with ``message`` if there is one,
        else resume ``sale`` if it is set, else send a new sale.

        The POS gives up on a sale it has polled for too long, or lost contact over, and a
        page refresh forgets the sale it was waiting on, yet the customer can still pay it
        on the terminal. A retry must not send a second sale for the same payment. So the
        order's open sales (_musqet_open_sales_of) are read from Musqet first if still
        pending, then:

        1. one still pending on another method, or for another amount, refuses the request:
           it may still be paid, so it has to end first (cancel it, or let it time out);
        2. one on ``method`` for this amount and ``currency`` is resumed, a pending one before
           a done one: the POS polls it, and settles the line with it once Musqet reports it
           complete;
        3. otherwise a new sale is sent, as when a done sale was for another amount.

        Requests to start a sale of one order are decided one at a time: a second one while
        the first is still being decided (and sent) is refused. The lock lasts until the
        first request's transaction ends, so its sale is recorded by then. Not covered: a
        second request that read the database just before the first one committed and takes
        the lock just after (a window of milliseconds) still misses the first sale's record;
        the terminal's own 409 CONFLICT stops it on the same terminal.
        """
        if not self._musqet_try_lock_order(method._musqet_company(), reference):
            return self.browse(), _(
                "A Musqet payment for this order is already being started. Wait for it, then "
                "try again.")
        open_sales = self._musqet_open_sales_of(method._musqet_company(), reference, settled_sale_ids)
        for sale in open_sales.filtered(lambda sale: sale.state == 'pending')[:MUSQET_SALE_REREAD_LIMIT]:
            sale.payment_method_id.musqet_get_sale(sale.sale_id)
        # Still pending after the read: Musqet can't be reached, or hasn't settled it. Only a
        # recent one may still be paid; Musqet times a sale out after about five minutes, so
        # an older one that still reads as pending is one Musqet can't be asked about (gone,
        # or its method misconfigured), and must not block the order for good.
        cutoff = fields.Datetime.now() - MUSQET_SALE_STALE_AFTER
        open_sales = open_sales.filtered(
            lambda sale: sale.state == 'done' or (sale.state == 'pending' and sale.create_date > cutoff))

        def is_repeat(sale):
            return (sale.payment_method_id == method and sale.amount_minor == amount
                    and sale.currency == currency)

        pending = open_sales.filtered(lambda sale: sale.state == 'pending')
        if pending.filtered(lambda sale: not is_repeat(sale)):
            return self.browse(), _(
                "A Musqet payment for this order is still in progress. Finish or cancel it on "
                "the terminal, then try again.")
        return (pending.filtered(is_repeat) or open_sales.filtered(is_repeat))[:1], None

    @api.model
    def _musqet_try_lock_order(self, company, reference):
        """Take the lock on starting a sale of order ``reference`` in ``company``, if it is free.

        A transaction-level advisory lock, so it is released when the transaction ends. Not
        waited for: a waiting transaction would keep its old snapshot (REPEATABLE READ), and
        miss the sale the first one records.
        """
        self.env.cr.execute(SQL(
            'SELECT pg_try_advisory_xact_lock(%s, hashtext(%s))',
            MUSQET_SALE_START_LOCK, '%s:%s' % (company.id, reference)))
        return self.env.cr.fetchone()[0]

    @api.model
    def _musqet_open_sales_of(self, company, reference, settled_sale_ids):
        """The sales of order ``reference`` in ``company`` that a new sale may repeat.

        Those Musqet hasn't failed, on a method that is still a Musqet one (a record of any
        other can't be read), and not already on a payment line: neither one of
        ``settled_sale_ids`` (the saleIds on the order's lines, which the POS sends: it comes
        from the browser, so it can only leave sales out, never refuse more) nor a synced
        payment with its (method, saleId).
        """
        if not isinstance(settled_sale_ids, (list, tuple)):
            settled_sale_ids = []
        settled = [
            sale_id for sale_id in settled_sale_ids[:MUSQET_SETTLED_SALE_IDS_LIMIT]
            if isinstance(sale_id, str) and len(sale_id) <= MUSQET_SALE_ID_MAX_LENGTH
        ]
        sales = self.sudo().search([
            ('company_id', '=', company.id),
            ('reference', '=', reference),
            ('state', '!=', 'failed'),
            ('sale_id', 'not in', settled),
            ('payment_method_id.use_payment_terminal', '=', MUSQET_TERMINAL),
        ], order='id')
        on_lines = {
            (payment.payment_method_id.id, payment.transaction_id)
            for payment in self.env['pos.payment'].sudo().search(sale_key_domain(
                'transaction_id', sales.mapped('sale_id'), sales.payment_method_id))
        } if sales else set()
        return sales.filtered(
            lambda sale: (sale.payment_method_id.id, sale.sale_id) not in on_lines)

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
