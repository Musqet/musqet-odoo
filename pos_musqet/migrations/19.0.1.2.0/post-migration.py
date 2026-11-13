# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Record the refunds made before refunds were recorded on the server.

Runs after the ORM has created pos.musqet.refund. Each refund payment already synced with a
musqet_refund_of link gets a done refund record, so the over-refund cap counts it from the
record rather than from fields on the payment that the POS can rewrite.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing synced yet
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['pos.musqet.refund']._musqet_record_synced_refunds()
