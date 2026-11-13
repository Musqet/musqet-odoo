# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Encrypt existing plaintext secrets and remove the plaintext columns.

Runs BEFORE the ORM schema sync. At this point the old plaintext columns (`musqet_api_key`,
`musqet_webhook_secret`) still exist and the new `*_encrypted` columns do not. We create the
new columns, encrypt each plaintext value into them, check every ciphertext decrypts back to
its plaintext, then drop the plaintext columns: Odoo does not drop a column when a field
merely becomes non-stored, so without the drop the plaintext would stay in the table.

Requires MUSQET_FIELD_ENCRYPTION_KEY to be set (and valid) during the upgrade when any secret
is stored, or it raises and aborts the upgrade, which rolls back every change (deliberate:
better to stop than to drop plaintext without a readable ciphertext).
"""
import logging

from odoo.addons.pos_musqet.models.musqet_crypto import (
    decrypt_secret,
    encrypt_secret,
    secret_label,
)
from odoo.tools import SQL
from odoo.tools.sql import column_exists

_logger = logging.getLogger(__name__)

TABLE = 'pos_payment_method'
# Plaintext columns (also the field names); each is encrypted into '<column>_encrypted'.
COLUMNS = ('musqet_api_key', 'musqet_webhook_secret')


class MusqetMigrationError(Exception):
    """A secret did not survive encryption; the upgrade stops before anything is dropped."""


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing to migrate
    for column in COLUMNS:
        if not column_exists(cr, TABLE, column):
            continue
        encrypted_column = column + '_encrypted'
        cr.execute(SQL(
            'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s varchar',
            SQL.identifier(TABLE), SQL.identifier(encrypted_column)))
        cr.execute(SQL(
            "SELECT id, %(col)s FROM %(table)s WHERE %(col)s IS NOT NULL AND %(col)s <> ''",
            col=SQL.identifier(column), table=SQL.identifier(TABLE)))
        rows = cr.fetchall()
        for rec_id, plaintext in rows:
            label = secret_label(rec_id, column)
            ciphertext = encrypt_secret(plaintext, label)
            if decrypt_secret(ciphertext, label) != plaintext:
                # Never include the value: it is a live credential.
                raise MusqetMigrationError(
                    'pos_musqet: %s on record %s did not decrypt back after encryption; '
                    'nothing was dropped.' % (column, rec_id))
            cr.execute(SQL(
                'UPDATE %s SET %s = %s WHERE id = %s',
                SQL.identifier(TABLE), SQL.identifier(encrypted_column), ciphertext, rec_id))
        cr.execute(SQL(
            'ALTER TABLE %s DROP COLUMN %s', SQL.identifier(TABLE), SQL.identifier(column)))
        _logger.info(
            'pos_musqet: encrypted %d existing value(s) from %s into %s and dropped %s',
            len(rows), column, encrypted_column, column,
        )
