# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Constants and helpers shared by the Musqet models."""

# The use_payment_terminal value of a Musqet payment method.
MUSQET_TERMINAL = 'musqet'

# The Musqet sale statuses (a refund uses the same ones) that end a sale. Same lists as
# SUCCESS_STATUS and FAILURE_STATUSES in static/src/app/payment_musqet.js.
MUSQET_SUCCESS_STATUS = 'COMPLETE'
MUSQET_FAILURE_STATUSES = ('ERROR', 'CANCELLED', 'CANCELLED_REMOTE', 'TIMED_OUT')

# The rails a settled Musqet sale reports in its top-level ``rail`` field.
MUSQET_RAIL_SELECTION = [('card', "Card"), ('bitcoin', "Bitcoin")]
MUSQET_RAILS = tuple(rail for rail, _label in MUSQET_RAIL_SELECTION)

# The largest amount an Integer field (a Postgres int4) can store.
MUSQET_MAX_MINOR_AMOUNT = 2 ** 31 - 1

# The states of an order not paid (yet): its Musqet lines vouch for nothing, so they are not
# checked, can't be refunded through the terminal, and the upgrade records no sale from them.
MUSQET_UNPAID_ORDER_STATES = ('draft', 'cancel')


def minor_units(amount, currency):
    """``amount`` in whole minor units of ``currency`` (cents for GBP, yen for JPY)."""
    # `is not None`, not `or 2`: a zero-decimal currency (JPY) has decimal_places == 0, and
    # `0 or 2` would wrongly use 2, computing a cap 100x too loose. Mirrors the frontend's
    # Number.isInteger() check, which also preserves 0.
    decimals = currency.decimal_places
    if decimals is None:
        decimals = 2
    return int(round(amount * (10 ** decimals)))


def parse_minor_amount(value):
    """``value`` as a positive whole number of minor units, or None if it is anything else.

    Strict on purpose: no coercion of strings or floats, and bool is refused even though it
    is an int subclass (True would otherwise pass as 1). Capped at what the records can store.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MUSQET_MAX_MINOR_AMOUNT:
        return None
    return value


def parse_rail(value):
    """``value`` as a known Musqet rail, or False if it isn't one.

    False, not None like the other parsers: the result is written straight into a Selection
    field, whose empty value is False.
    """
    return value if value in MUSQET_RAILS else False


def parse_iso_currency(value):
    """``value`` as an upper-case ISO 4217 code, or None if it doesn't look like one."""
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isalpha():
        return value.upper()
    return None


def reply_is_for(sale_id, reply):
    """Whether ``reply`` is Musqet's reply about ``sale_id``: a dict with no error that names
    no other saleId (a status read is about its URL's sale, so one that names none is taken
    as about it)."""
    if not isinstance(reply, dict) or 'error' in reply:
        return False
    echoed = reply.get('saleId')
    return echoed is None or echoed == str(sale_id)


def reply_is_404(reply):
    """Whether ``reply`` (a proxy result) is an HTTP 404 from the API, Musqet's own or not."""
    error = reply.get('error') if isinstance(reply, dict) else None
    return isinstance(error, dict) and error.get('status_code') == 404


def reply_is_not_found(reply):
    """Whether ``reply`` (a proxy result) is Musqet's own 404 NOT_FOUND: it has no such sale.

    Musqet's API answers it with the JSON body ``{"error": {"code": 404, ...}}``, which
    _musqet_request puts in the error's ``detail``. A bare 404 (a wrong base URL, a proxy in
    the way) says nothing about the sale, and is not taken as one.
    """
    if not reply_is_404(reply):
        return False
    detail = reply['error'].get('detail')
    body = detail.get('error') if isinstance(detail, dict) else None
    return isinstance(body, dict) and body.get('code') == 404


def reply_outcome_is_unknown(reply):
    """Whether ``reply`` (a proxy result for a sale or refund request) leaves unknown whether
    Musqet accepted the request.

    True for a reply that names no saleId and is not an ``{error}`` saying Musqet refused the
    request or never got it: the request may have reached Musqet while the reply was lost (a
    read timeout, a dropped connection, a 5xx, a body Odoo can't read). _musqet_request marks
    those errors ``outcome_unknown``.
    """
    if not isinstance(reply, dict):
        return True
    error = reply.get('error')
    if error is not None:
        return isinstance(error, dict) and error.get('outcome_unknown') is True
    return not reply.get('saleId')


def sale_key_domain(sale_field, sale_ids, methods):
    """The domain of the rows with one of ``sale_ids`` in ``sale_field``, on one of ``methods``.

    A Musqet sale or refund is keyed on (method, saleId): ``sale_field`` is ``sale_id`` on a
    sale or refund record and ``transaction_id`` on a payment. This domain may also match a
    listed saleId on another listed method, so callers still compare the key. An empty saleId
    (a record of a request whose reply was lost) is left out: it names no sale, and in a
    domain it would match every row with none.
    """
    return [(sale_field, 'in', [sale_id for sale_id in sale_ids if sale_id]),
            ('payment_method_id', 'in', methods.ids)]
