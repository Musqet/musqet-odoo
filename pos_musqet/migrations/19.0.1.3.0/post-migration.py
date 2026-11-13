# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Record the sales made before sales were recorded on the server.

Runs after the ORM has created pos.musqet.sale. Each Musqet sale payment already synced gets a
done sale record from its own amount, currency and rail, so it can still be refunded: a refund
reads the sale record, not the payment. The payments themselves are left unchecked
(musqet_settle_check stays empty): nothing but the payment vouches for them.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing synced yet
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['pos.musqet.sale']._musqet_record_synced_sales()
