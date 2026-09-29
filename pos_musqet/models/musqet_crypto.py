# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Symmetric encryption for pos_musqet secrets at rest.

The key comes from the environment (MUSQET_FIELD_ENCRYPTION_KEY), NEVER the database, so a
Postgres-only leak cannot decrypt the stored secrets. It does NOT defend against host
compromise: Odoo decrypts at runtime, so the key is present in this process. No Odoo imports
here, so a migration can import this module directly.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

_logger = logging.getLogger(__name__)

ENV_KEY = 'MUSQET_FIELD_ENCRYPTION_KEY'


class MusqetEncryptionKeyMissing(Exception):
    """Raised when a secret must be encrypted but MUSQET_FIELD_ENCRYPTION_KEY is unset."""


def _fernet():
    key = os.environ.get(ENV_KEY)
    if not key:
        raise MusqetEncryptionKeyMissing(
            '%s is not set; cannot encrypt or decrypt Musqet secrets.' % ENV_KEY
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext):
    """Return a Fernet ciphertext (text) for a non-empty plaintext, else False."""
    if not plaintext:
        return False
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext):
    """Return the plaintext for a Fernet ciphertext, or False if it cannot be decrypted.

    A decrypt failure (wrong key, corruption, or a value that predates encryption) is logged
    and yields False rather than raising, so a mis-provisioned box degrades instead of crashing
    on every read. Encryption is the loud path; decryption is the soft one.
    """
    if not ciphertext:
        return False
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except MusqetEncryptionKeyMissing:
        _logger.error('%s is not set; cannot decrypt a stored Musqet secret.', ENV_KEY)
        return False
    except InvalidToken:
        _logger.error('A stored Musqet secret failed to decrypt (wrong key or corrupted value).')
        return False
