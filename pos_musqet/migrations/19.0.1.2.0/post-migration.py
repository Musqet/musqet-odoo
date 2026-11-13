# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Record the refunds made before refunds were recorded on the server.

Runs after the ORM has created pos.musqet.refund. Each refund payment already synced with a
musqet_refund_of link gets a done refund record, so the over-refund cap counts that refund
from the record rather than from the refund payment's own fields, which the POS can rewrite.
(The cap starts from the server's sale record since 19.0.1.3.0.)
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing synced yet
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['pos.musqet.refund']._musqet_record_synced_refunds()
