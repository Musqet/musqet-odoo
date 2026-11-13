# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Symmetric encryption for pos_musqet secrets at rest.

The key comes from the environment (MUSQET_FIELD_ENCRYPTION_KEY), NEVER the database, so a
read-only copy of the database (a dump or backup) cannot decrypt the stored secrets. It does
NOT defend against host compromise: Odoo decrypts at runtime, so the key is present in this
process. No Odoo imports here, so a migration can import this module directly.

Each ciphertext is bound to the record and field it was written for (see ``secret_label``):
the label is encrypted with the secret and checked on decrypt, so a ciphertext copied into
another row or column does not decrypt there.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

_logger = logging.getLogger(__name__)

ENV_KEY = 'MUSQET_FIELD_ENCRYPTION_KEY'

# (label, reason) pairs already logged by this worker process, so a broken key or value
# logs one error per secret and reason rather than one per read. A successful decrypt
# clears the label's entries, so a key that breaks again later is reported again.
_reported_failures = set()


class MusqetEncryptionKeyMissing(Exception):
    """MUSQET_FIELD_ENCRYPTION_KEY is unset or is not a valid Fernet key.

    Raised by encrypt_secret (saving a secret fails loudly); decrypt_secret catches it and
    degrades to False.
    """


def secret_label(model_name, record_id, field_name):
    """The label a secret is bound to: its model, saved record id and field."""
    return '%s,%s,%s' % (model_name, record_id, field_name)


def encrypted_field_name(field_name):
    """The stored column holding the ciphertext of a secret field."""
    return field_name + '_encrypted'


def _fernet():
    # strip(): an env file or a pasted value often carries a trailing newline or spaces.
    key = (os.environ.get(ENV_KEY) or '').strip()
    if not key:
        raise MusqetEncryptionKeyMissing('%s is not set.' % ENV_KEY)
    try:
        return Fernet(key.encode())
    except ValueError:
        # Never echo the value: it is (a mangled copy of) the key.
        raise MusqetEncryptionKeyMissing(
            '%s is not a valid Fernet key (32 url-safe base64-encoded bytes).' % ENV_KEY
        ) from None


def encrypt_secret(plaintext, label):
    """Return a Fernet ciphertext (text) of a non-empty plaintext bound to label, else False.

    Raises MusqetEncryptionKeyMissing if the key is unset or invalid.
    """
    if not plaintext:
        return False
    return _fernet().encrypt(('%s\n%s' % (label, plaintext)).encode()).decode()


def decrypt_secret(ciphertext, label):
    """Return the plaintext of a ciphertext bound to label, or False if it cannot be read.

    A failure (key unset or invalid, wrong key, corrupted value, or a ciphertext written for
    another record or field) is logged once per label and reason until that label next
    decrypts, and yields False rather
    than raising, so a mis-provisioned box degrades instead of crashing on every read.
    Encryption is the loud path; decryption is the soft one.
    """
    if not ciphertext:
        return False
    try:
        bound = _fernet().decrypt(ciphertext.encode()).decode()
    except MusqetEncryptionKeyMissing as error:
        reason = str(error)
    except InvalidToken:
        reason = 'wrong key or corrupted value'
    else:
        bound_label, separator, plaintext = bound.partition('\n')
        if separator and bound_label == label:
            _forget_failures(label)
            return plaintext
        reason = 'the value was written for another record or field'
    if (label, reason) not in _reported_failures:
        _reported_failures.add((label, reason))
        _logger.error('Cannot decrypt the Musqet secret %s: %s', label, reason)
    return False


def _forget_failures(label):
    for reported in [entry for entry in _reported_failures if entry[0] == label]:
        _reported_failures.discard(reported)
