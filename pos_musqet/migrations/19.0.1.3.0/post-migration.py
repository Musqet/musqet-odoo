# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Record the sales made before sales were recorded on the server.

Runs after the ORM has created pos.musqet.sale. Each Musqet sale payment of a paid order
already synced gets a done sale record from its own amount, currency and rail, so it can still
be refunded: a refund reads the sale record, not the payment. The upgrade checks no payment:
musqet_settle_check starts empty, until the line, its order's state or a line sharing its
saleId changes (see pos.payment.musqet_settle_check).

It also clears the order references the 19.0.1.2.0 upgrade guessed for refunds several paid
lines share (see pos.musqet.refund._musqet_forget_shared_references).
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing synced yet
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['pos.musqet.sale']._musqet_record_synced_sales()
    env['pos.musqet.refund']._musqet_forget_shared_references()
