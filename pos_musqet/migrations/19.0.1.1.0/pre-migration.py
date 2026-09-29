# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Encrypt existing plaintext secrets and remove the plaintext columns.

Runs BEFORE the ORM schema sync. At this point the old plaintext columns (`musqet_api_key`,
`musqet_webhook_secret`) still exist and the new `*_encrypted` columns do not. We create the
new columns, encrypt each plaintext value into them, then drop the plaintext columns: Odoo
does not drop a column when a field merely becomes non-stored, so without the drop the
plaintext would stay in the table. Requires MUSQET_FIELD_ENCRYPTION_KEY to be set during the
upgrade when any secret is stored, or it raises and aborts the upgrade (deliberate: better to
stop than to drop plaintext without encrypting it).
"""
import logging

from odoo.addons.pos_musqet.models.musqet_crypto import encrypt_secret

_logger = logging.getLogger(__name__)

# (old plaintext column, new encrypted column)
COLUMNS = [
    ('musqet_api_key', 'musqet_api_key_encrypted'),
    ('musqet_webhook_secret', 'musqet_webhook_secret_encrypted'),
]


def _column_exists(cr, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'pos_payment_method' AND column_name = %s",
        (column,),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return  # fresh install, nothing to migrate
    # Column names are fixed literals from COLUMNS (identifiers can't be bound); values are
    # always bound with %s.
    for old_col, new_col in COLUMNS:
        if not _column_exists(cr, old_col):
            continue
        cr.execute('ALTER TABLE pos_payment_method ADD COLUMN IF NOT EXISTS %s varchar' % new_col)
        cr.execute(
            "SELECT id, %s FROM pos_payment_method WHERE %s IS NOT NULL AND %s <> ''"
            % (old_col, old_col, old_col)
        )
        rows = cr.fetchall()
        for rec_id, plaintext in rows:
            cr.execute(
                'UPDATE pos_payment_method SET %s = %%s WHERE id = %%s' % new_col,
                (encrypt_secret(plaintext), rec_id),
            )
        cr.execute('ALTER TABLE pos_payment_method DROP COLUMN %s' % old_col)
        _logger.info(
            'pos_musqet: encrypted %d existing value(s) from %s into %s and dropped %s',
            len(rows), old_col, new_col, old_col,
        )
