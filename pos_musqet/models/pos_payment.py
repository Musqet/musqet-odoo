# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class PosPayment(models.Model):
    _inherit = 'pos.payment'

    # The rail the Musqet terminal actually settled on, read from the top-level ``rail``
    # field of the sale result ("card" | "bitcoin"), never inferred from metadata. Stored
    # on the payment so it round-trips on sync and is reloaded when a past order is
    # reopened — issue #7 reads it to gate refunds (card vs Lightning reverse differently).
    #
    # pos.payment does not override _load_pos_data_fields, so the POS data loader reads all
    # stored fields (read([]) == all fields) — this field therefore loads into the frontend
    # and serializes back on sync with no extra wiring, matching pos_razorpay/pos_pine_labs.
    musqet_rail = fields.Char(string="Musqet Rail", copy=False)
    # On a refund payment line, the Musqet saleId of the original card sale this refund
    # reverses (set in the POS frontend on a refund order). The Musqet API keeps no
    # sale↔refund linkage of its own (Musqet/musqet#2094), so this is where the link lives:
    # the create-refund backend guard reads the original payment by id, and cumulative-refund
    # accounting (#9) will reconcile refunds to their original sale through this field.
    musqet_refund_of = fields.Char(string="Musqet Refund Of", copy=False)
