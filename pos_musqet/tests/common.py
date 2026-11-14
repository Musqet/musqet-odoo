# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Helpers shared by the pos_musqet test suites."""
import importlib.util
import os
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from odoo.addons.pos_musqet.models import musqet_crypto
from odoo.tests.common import TransactionCase
from odoo.tools import SQL
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


# Loggers: the proxy warns of a failed Musqet call, the records of a request whose reply was
# lost.
PROXY_LOGGER = 'odoo.addons.pos_musqet.models.pos_payment_method'
RECORD_LOGGER = 'odoo.addons.pos_musqet.models.pos_musqet_record'
# The words of the message refusing an order with an unanswered sale or refund.
UNANSWERED = 'did not get a clear answer from Musqet'


def make_pos_user(env, login, group='point_of_sale.group_pos_user', **values):
    """Create a user in a POS group (a cashier by default) and nothing else."""
    return env['res.users'].create({
        'name': login, 'login': login,
        'group_ids': [(6, 0, [env.ref(group).id])],
        **values,
    })


def open_session(env, *methods):
    """Open a POS session on a new config taking ``methods``: a cashier may drive only a
    terminal on an open session (pos.payment.method._musqet_check_access)."""
    config = env['pos.config'].create({
        'name': 'Musqet POS',
        'payment_method_ids': [(6, 0, [method.id for method in methods])]})
    return env['pos.session'].create({'config_id': config.id, 'user_id': env.uid})


def open_plain_method(env):
    """A payment method that isn't a Musqet one, on an open POS session: so the method check
    (pos.payment.method._musqet_check_method), not the session check, is what refuses it."""
    plain = env['pos.payment.method'].create({'name': 'Plain Method'})
    open_session(env, plain)
    return plain


def clear_company(method):
    """Leave ``method`` with no company, as one saved before 19.0.1.6.0 may be. Raw SQL: the
    ORM refuses it (pos.payment.method._check_musqet_company)."""
    method.env.cr.execute(SQL(
        'UPDATE pos_payment_method SET company_id = NULL WHERE id = %s', method.id))
    method.invalidate_recordset(['company_id'])


def write_between_sessions(records, vals):
    """Write ``vals`` on payment methods as if no POS session using them were open: Odoo
    refuses most edits to a method while one is, and a test's cashier needs one open."""
    with patch.object(type(records), '_is_write_forbidden', return_value=False):
        records.write(vals)


def musqet_response(sale_id, status='PENDING', status_code=200, **fields):
    """A stubbed Musqet API response carrying a sale's id, status and any other ``fields``."""
    response = MagicMock(status_code=status_code)
    response.json.return_value = {'saleId': sale_id, 'status': status, **fields}
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


def record_sale(env, method, sale_id, amount_minor=1000, rail='card', state='done', **overrides):
    """Create a pos.musqet.sale on ``method`` (in its company and currency), done by default."""
    return env['pos.musqet.sale'].create({
        'company_id': method.company_id.id,
        'payment_method_id': method.id,
        'sale_id': sale_id,
        'amount_minor': amount_minor,
        'currency': method.company_id.currency_id.name,
        'rail': rail,
        'state': state,
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


class MusqetPosRecordsCase(TransactionCase):
    """A paid order on two Musqet methods and a plain one, with a factory for real POS
    payments (_payment); setUp records refund 'refund-1' (500 minor units) of that order."""

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-LINK-001')
        self.plain = self.env['pos.payment.method'].create({'name': 'Plain Method'})
        self.second = create_musqet_method(self, 'MSQ-LINK-002')
        self.session = open_session(self.env, self.method, self.plain, self.second)
        self.order = self._order()
        self._record_refund('refund-1', amount_minor=500)

    def _order(self, state='paid'):
        # Paid by default: the settle check skips the lines of an order not yet paid.
        return self.env['pos.order'].create({
            'session_id': self.session.id, 'amount_total': 0, 'amount_tax': 0,
            'amount_paid': 0, 'amount_return': 0, 'state': state,
        })

    def _row_version(self, record):
        self.env.cr.execute(
            SQL('SELECT xmin::text FROM %s WHERE id = %s', SQL.identifier(record._table), record.id))
        return self.env.cr.fetchone()[0]

    def _record_sale(self, sale_id, **values):
        """A sale record as Odoo makes it: sent for self.order (unless ``reference`` says)."""
        return record_sale(self.env, self.method, sale_id, **{'reference': self.order.uuid, **values})

    def _record_refund(self, sale_id, **values):
        """A refund record as Odoo makes it: sent for self.order (unless ``reference`` says)."""
        return record_refund(self.env, self.method, sale_id, **{'reference': self.order.uuid, **values})

    def _original(self, sale_id, amount_minor=1000):
        """A synced card sale payment of 10.00 and the server's done record of that sale."""
        self._record_sale(sale_id, amount_minor=amount_minor)
        return self._payment(amount=10.0, transaction_id=sale_id, musqet_rail='card')

    def _payment(self, **vals):
        return self.env['pos.payment'].create({
            'pos_order_id': self.order.id, 'payment_method_id': self.method.id,
            'amount': -5.0, **vals})
