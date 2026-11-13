# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Helpers shared by the Musqet sale and refund records and the payment models."""

# The Musqet sale statuses (a refund uses the same ones) that end a sale. Same lists as
# SUCCESS_STATUS and FAILURE_STATUSES in static/src/app/payment_musqet.js.
MUSQET_SUCCESS_STATUS = 'COMPLETE'
MUSQET_FAILURE_STATUSES = ('ERROR', 'CANCELLED', 'CANCELLED_REMOTE', 'TIMED_OUT')

# The rails a settled Musqet sale reports in its top-level ``rail`` field.
MUSQET_RAIL_SELECTION = [('card', "Card"), ('bitcoin', "Bitcoin")]
MUSQET_RAILS = tuple(rail for rail, _label in MUSQET_RAIL_SELECTION)


def minor_units(amount, currency):
    """``amount`` in whole minor units of ``currency`` (cents for GBP, yen for JPY)."""
    # `is not None`, not `or 2`: a zero-decimal currency (JPY) has decimal_places == 0, and
    # `0 or 2` would wrongly use 2, computing a cap 100x too loose. Mirrors the frontend's
    # Number.isInteger() check, which also preserves 0.
    decimals = currency.decimal_places
    if decimals is None:
        decimals = 2
    return int(round(amount * (10 ** decimals)))


def record_unrecorded_payments(model, payments, vals_for):
    """Create a done ``model`` record for each payment whose (method, saleId) has none.

    For the upgrade backfills of pos.musqet.sale and pos.musqet.refund; safe to run again.
    ``vals_for(payment)`` gives the model's own values. Returns how many were created.
    """
    model = model.sudo()
    recorded = {
        (record.payment_method_id.id, record.sale_id)
        for record in model.search([('sale_id', 'in', payments.mapped('transaction_id'))])
    }
    vals_list = []
    for payment in payments:
        key = (payment.payment_method_id.id, payment.transaction_id)
        if key in recorded:
            continue
        recorded.add(key)
        order = payment.pos_order_id
        vals_list.append({
            'company_id': payment.company_id.id,
            'payment_method_id': payment.payment_method_id.id,
            'sale_id': payment.transaction_id,
            'reference': order.pos_reference or order.uuid,
            'state': 'done',
            **vals_for(payment),
        })
    model.create(vals_list)
    return len(vals_list)
