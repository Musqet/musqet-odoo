# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Encrypt existing plaintext secrets and remove the plaintext columns.

Runs BEFORE the ORM schema sync. At this point the old plaintext columns (`musqet_api_key`,
`musqet_webhook_secret`) still exist and the new `*_encrypted` columns do not. Two passes:

1. For every column, create the encrypted column, encrypt each plaintext value into it, and
   check every ciphertext decrypts back to its plaintext.
2. Only once every value in every column has passed, drop the plaintext columns. Odoo does
   not drop a column when a field merely becomes non-stored, so without the drop the
   plaintext would stay in the table.

Requires MUSQET_FIELD_ENCRYPTION_KEY to be set (and valid) during the upgrade when any secret
is stored, or it raises in pass 1 and aborts the upgrade before any column is dropped
(deliberate: better to stop than to drop plaintext without a readable ciphertext).
"""
import logging

from odoo.addons.pos_musqet.models.musqet_crypto import (
    decrypt_secret,
    encrypt_secret,
    encrypted_field_name,
    secret_label,
)
from odoo.tools import SQL
from odoo.tools.sql import column_exists

_logger = logging.getLogger(__name__)

MODEL = 'pos.payment.method'
TABLE = 'pos_payment_method'
# Plaintext columns (also the field names).
COLUMNS = ('musqet_api_key', 'musqet_webhook_secret')


class MusqetMigrationError(Exception):
    """A secret did not survive encryption; the upgrade stops before any column is dropped."""


def _encrypt_column(cr, column):
    """Encrypt and verify every value of a plaintext column; return how many there were."""
    encrypted_column = encrypted_field_name(column)
    cr.execute(SQL(
        'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s varchar',
        SQL.identifier(TABLE), SQL.identifier(encrypted_column)))
    cr.execute(SQL(
        "SELECT id, %(col)s FROM %(table)s WHERE %(col)s IS NOT NULL AND %(col)s <> ''",
        col=SQL.identifier(column), table=SQL.identifier(TABLE)))
    rows = cr.fetchall()
    for rec_id, plaintext in rows:
        label = secret_label(MODEL, rec_id, column)
        ciphertext = encrypt_secret(plaintext, label)
        if decrypt_secret(ciphertext, label) != plaintext:
            # Never include the value: it is a live credential.
            raise MusqetMigrationError(
                'pos_musqet: %s on record %s did not decrypt back after encryption; '
                'no plaintext column was dropped.' % (column, rec_id))
        cr.execute(SQL(
            'UPDATE %s SET %s = %s WHERE id = %s',
            SQL.identifier(TABLE), SQL.identifier(encrypted_column), ciphertext, rec_id))
    return len(rows)


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing to migrate
    columns = [column for column in COLUMNS if column_exists(cr, TABLE, column)]
    counts = {column: _encrypt_column(cr, column) for column in columns}
    for column in columns:
        cr.execute(SQL(
            'ALTER TABLE %s DROP COLUMN %s', SQL.identifier(TABLE), SQL.identifier(column)))
        _logger.info(
            'pos_musqet: encrypted %d existing value(s) from %s into %s and dropped %s',
            counts[column], column, encrypted_field_name(column), column,
        )
