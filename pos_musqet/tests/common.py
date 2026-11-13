# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Helpers shared by the pos_musqet test suites."""
import importlib.util
import os
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from odoo.addons.pos_musqet.models import musqet_crypto
from odoo.tools.misc import file_path

# The secrets are encrypted at rest with a key from the environment, so every suite that
# stores a secret patches one in for its duration (see use_test_encryption_key).
TEST_FERNET_KEY = Fernet.generate_key().decode()


def use_test_encryption_key(test, key=TEST_FERNET_KEY):
    """Set the encryption key for the rest of the test; None unsets it.

    May be called again to switch keys; each call is undone at cleanup.
    """
    patcher = patch.dict(os.environ)
    patcher.start()
    test.addCleanup(patcher.stop)
    if key is None:
        os.environ.pop(musqet_crypto.ENV_KEY, None)
    else:
        os.environ[musqet_crypto.ENV_KEY] = key


def create_musqet_method(test, serial, **overrides):
    """Create a configured Musqet payment method, with the test encryption key set."""
    use_test_encryption_key(test)
    return test.env['pos.payment.method'].create({
        'name': 'Musqet Terminal %s' % serial,
        'use_payment_terminal': 'musqet',
        'musqet_base_url': 'https://api.musqet.tech/api/v1',
        'musqet_api_key': 'secret-token',
        'musqet_terminal_serial': serial,
        **overrides,
    })


def musqet_response(sale_id, status='PENDING', status_code=200):
    """A stubbed Musqet API response carrying a sale's id and status."""
    response = MagicMock(status_code=status_code)
    response.json.return_value = {'saleId': sale_id, 'status': status}
    return response


def dirty_field_names(env):
    """The fields with writes still waiting to reach the database."""
    return {field.name for field, ids in env.transaction.field_dirty.items() if ids}


def record_refund(env, method, sale_id, original_sale_id='sale-orig-1', amount_minor=1, **overrides):
    """Create a pos.musqet.refund of ``original_sale_id`` on ``method`` (in its company)."""
    return env['pos.musqet.refund'].create({
        'company_id': method.company_id.id,
        'payment_method_id': method.id,
        'original_sale_id': original_sale_id,
        'sale_id': sale_id,
        'amount_minor': amount_minor,
        **overrides,
    })


def load_migration(version, name):
    """Load a migration script, e.g. load_migration('19.0.1.1.0', 'pre-migration')."""
    # The directory name has dots and the file a hyphen, so it can't be imported by name.
    path = file_path('pos_musqet/migrations/%s/%s.py' % (version, name))
    module_name = 'pos_musqet_%s_%s' % (name.replace('-', '_'), version.replace('.', '_'))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reset_decrypt_failure_log(test):
    """Start and end the test with no decrypt failures recorded as already logged."""
    musqet_crypto.reset_reported_failures()
    test.addCleanup(musqet_crypto.reset_reported_failures)
