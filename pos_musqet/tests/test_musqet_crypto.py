# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from odoo.addons.pos_musqet.models import musqet_crypto, pos_payment_method
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools.sql import column_exists

from .common import (
    TEST_FERNET_KEY,
    create_musqet_method,
    load_pre_migration,
    reset_decrypt_failure_log,
    use_test_encryption_key,
)


@tagged('post_install', '-at_install')
class TestMusqetSecretEncryption(TransactionCase):
    """The API key and webhook secret are stored as Fernet ciphertext, never plaintext.

    Guards the defence against a leaked read-only copy of the database: the stored column
    holds ciphertext bound to its record and field, the plaintext field round-trips through
    the key in the environment, the plaintext has no column, and a missing or broken key
    degrades instead of crashing.
    """

    def setUp(self):
        super().setUp()
        reset_decrypt_failure_log(self)
        self.method = create_musqet_method(self, 'MSQ-ENC-001')

    def _stored_row(self):
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT musqet_api_key_encrypted, musqet_webhook_secret_encrypted '
            'FROM pos_payment_method WHERE id = %s', (self.method.id,))
        return self.env.cr.fetchone()

    def test_api_key_stored_as_ciphertext_bound_to_its_record(self):
        stored_key, _ = self._stored_row()
        self.assertTrue(stored_key)
        self.assertNotIn('secret-token', stored_key)
        self.assertEqual(
            Fernet(TEST_FERNET_KEY).decrypt(stored_key.encode()).decode(),
            'pos.payment.method,%s,musqet_api_key\nsecret-token' % self.method.id)

    def test_plaintext_fields_have_no_column(self):
        # Every secret field (one with a ciphertext column) is non-stored and has no column,
        # and the 19.0.1.1.0 migration covers exactly these fields.
        fields = self.method._fields
        secrets = [name for name in fields
                   if musqet_crypto.encrypted_field_name(name) in fields]
        self.assertEqual(sorted(secrets), sorted(load_pre_migration().COLUMNS))
        for name in secrets:
            self.assertFalse(fields[name].store, name)
            self.assertFalse(column_exists(self.env.cr, self.method._table, name), name)

    def test_api_key_round_trips_and_reaches_the_header(self):
        self.method.invalidate_recordset()
        self.assertEqual(self.method.sudo().musqet_api_key, 'secret-token')
        response = MagicMock(status_code=200)
        response.json.return_value = {}
        with patch.object(pos_payment_method.requests, 'request', return_value=response) as mock_request:
            self.method.musqet_get_sale('sale-123')
        self.assertEqual(
            mock_request.call_args.kwargs['headers']['Authorization'], 'Bearer secret-token')

    def test_webhook_secret_round_trips(self):
        self.method.musqet_webhook_secret = 'whsec-xyz'
        _, stored_secret = self._stored_row()
        self.assertTrue(stored_secret)
        self.assertNotIn('whsec-xyz', stored_secret)
        self.method.invalidate_recordset()
        self.assertEqual(self.method.musqet_webhook_secret, 'whsec-xyz')

    def test_writing_one_secret_leaves_the_other_intact(self):
        stored_key, _ = self._stored_row()
        self.method.musqet_webhook_secret = 'whsec-xyz'
        self.assertEqual(self._stored_row()[0], stored_key)

    def test_empty_secret_is_not_encrypted(self):
        self.method.musqet_api_key = False
        self.assertFalse(self._stored_row()[0])

    def test_key_with_surrounding_whitespace_still_works(self):
        # An env file or a pasted value often carries a trailing newline.
        use_test_encryption_key(self, '  %s\n' % TEST_FERNET_KEY)
        self.method.invalidate_recordset()
        self.assertEqual(self.method.musqet_api_key, 'secret-token')

    def test_write_without_key_raises_a_user_error(self):
        use_test_encryption_key(self, None)
        with self.assertRaisesRegex(UserError, musqet_crypto.ENV_KEY):
            self.method.musqet_api_key = 'x'

    def test_write_with_malformed_key_raises_a_user_error(self):
        use_test_encryption_key(self, 'not-a-key')
        with self.assertRaisesRegex(UserError, musqet_crypto.ENV_KEY):
            self.method.musqet_webhook_secret = 'x'

    def test_read_with_wrong_key_degrades_to_empty(self):
        # A mis-provisioned box must not crash on every read: decrypt logs and yields False.
        self.method.invalidate_recordset()
        use_test_encryption_key(self, Fernet.generate_key().decode())
        with self.assertLogs(musqet_crypto.__name__, level='ERROR') as logs:
            self.assertFalse(self.method.musqet_api_key)
        # The log names the secret, so an admin knows which one to re-enter.
        self.assertIn('pos.payment.method,%s,musqet_api_key' % self.method.id, logs.output[0])

    def test_read_without_key_degrades_to_empty(self):
        self.method.invalidate_recordset()
        use_test_encryption_key(self, None)
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_api_key)

    def test_decrypt_failure_is_logged_once(self):
        use_test_encryption_key(self, 'not-a-key')
        self.method.invalidate_recordset()
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_api_key)
        self.method.invalidate_recordset()
        with self.assertNoLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_api_key)

    def test_ciphertext_without_a_label_does_not_decrypt(self):
        # A valid token under the right key, but not bound to any record or field.
        unbound = Fernet(TEST_FERNET_KEY).encrypt(b'secret-token').decode()
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE pos_payment_method SET musqet_api_key_encrypted = %s WHERE id = %s',
            (unbound, self.method.id))
        self.method.invalidate_recordset()
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_api_key)

    def test_ciphertext_moved_to_another_record_does_not_decrypt(self):
        other = create_musqet_method(self, 'MSQ-ENC-002')
        stored_key, _ = self._stored_row()
        self.env.cr.execute(
            'UPDATE pos_payment_method SET musqet_api_key_encrypted = %s WHERE id = %s',
            (stored_key, other.id))
        other.invalidate_recordset()
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(other.musqet_api_key)

    def test_ciphertext_moved_to_another_field_does_not_decrypt(self):
        stored_key, _ = self._stored_row()
        self.env.cr.execute(
            'UPDATE pos_payment_method SET musqet_webhook_secret_encrypted = %s WHERE id = %s',
            (stored_key, self.method.id))
        self.method.invalidate_recordset()
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_webhook_secret)

    def test_request_with_unreadable_key_says_why_and_sends_nothing(self):
        use_test_encryption_key(self, Fernet.generate_key().decode())
        self.method.invalidate_recordset()
        with patch.object(pos_payment_method.requests, 'request') as mock_request, \
                self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            result = self.method.musqet_get_sale('sale-123')
        mock_request.assert_not_called()
        # The cashier sees no host detail; the server log carries the cause.
        self.assertIn('cannot be read', result['error']['message'])
        self.assertNotIn(musqet_crypto.ENV_KEY, result['error']['message'])

    def test_onchange_copy_reads_the_secret_without_an_alarm(self):
        # A form onchange computes on an in-memory copy (NewId) of the saved record; the
        # binding must use the saved id, or every edit logs a false tamper error.
        self.method.invalidate_recordset()
        draft = self.method.new({'name': 'Renamed'}, origin=self.method)
        with self.assertNoLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertEqual(draft.musqet_api_key, 'secret-token')

    def test_unsaved_record_has_no_secret(self):
        draft = self.env['pos.payment.method'].new({'musqet_api_key_encrypted': 'not-a-token'})
        with self.assertNoLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(draft.musqet_api_key)

    def test_unsaved_record_cannot_take_a_secret(self):
        draft = self.env['pos.payment.method'].new({'musqet_api_key': 'secret-token'})
        with self.assertRaisesRegex(UserError, 'Save the payment method'):
            draft._inverse_musqet_api_key()

    def test_decrypt_failure_is_logged_again_after_a_recovery(self):
        # 1. The key breaks: logged.
        use_test_encryption_key(self, 'not-a-key')
        self.method.invalidate_recordset()
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_api_key)
        # 2. The key is fixed: the secret reads again.
        use_test_encryption_key(self)
        self.method.invalidate_recordset()
        self.assertEqual(self.method.musqet_api_key, 'secret-token')
        # 3. The key breaks again: logged again.
        use_test_encryption_key(self, 'not-a-key')
        self.method.invalidate_recordset()
        with self.assertLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method.musqet_api_key)

    def test_request_without_any_api_key_sends_nothing(self):
        self.method.musqet_api_key = False
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_get_sale('sale-123')
        mock_request.assert_not_called()
        self.assertEqual(result['error']['message'], 'No Musqet API key is configured.')

    def test_malformed_webhook_header_does_not_read_the_secret(self):
        self.method.musqet_webhook_secret = 'whsec-xyz'
        use_test_encryption_key(self, 'not-a-key')
        self.method.invalidate_recordset()
        with self.assertNoLogs(musqet_crypto.__name__, level='ERROR'):
            self.assertFalse(self.method._musqet_verify_webhook('garbage', b'{}'))
            self.assertFalse(self.method._musqet_verify_webhook('t=1,v1=abc', b'{}'))


@tagged('post_install', '-at_install')
class TestMusqetSecretMigration(TransactionCase):
    """The 19.0.1.1.0 pre-migration encrypts plaintext secrets, then drops their columns.

    Dropping the plaintext is the one irreversible step, so it must only happen once every
    value has a ciphertext that decrypts back. Each test re-creates the 19.0.1.0.0 columns
    inside the test transaction, which rolls them back afterwards.
    """

    def setUp(self):
        super().setUp()
        reset_decrypt_failure_log(self)
        self.migration = load_pre_migration()
        self.method = create_musqet_method(self, 'MSQ-MIG-001', musqet_api_key=False)
        self.env.flush_all()
        cr = self.env.cr
        cr.execute('ALTER TABLE pos_payment_method ADD COLUMN musqet_api_key varchar, '
                   'ADD COLUMN musqet_webhook_secret varchar')
        cr.execute("UPDATE pos_payment_method SET musqet_api_key = 'legacy-token', "
                   "musqet_webhook_secret = 'legacy-whsec' WHERE id = %s", (self.method.id,))

    def _migrate_expecting(self, error_class):
        # Not self.assertRaises: Odoo's version rolls back to a savepoint on the error,
        # which would undo a premature DROP COLUMN and hide it from the assertions.
        try:
            self.migration.migrate(self.env.cr, '19.0.1.0.0')
        except error_class:
            return
        self.fail('%s not raised' % error_class.__name__)

    def _plaintext_columns(self):
        return [name for name in self.migration.COLUMNS
                if column_exists(self.env.cr, self.migration.TABLE, name)]

    def test_encrypts_and_drops_the_plaintext_columns(self):
        self.migration.migrate(self.env.cr, '19.0.1.0.0')
        self.assertEqual(self._plaintext_columns(), [])
        self.method.invalidate_recordset()
        self.assertEqual(self.method.musqet_api_key, 'legacy-token')
        self.assertEqual(self.method.musqet_webhook_secret, 'legacy-whsec')

    def test_migration_binds_to_the_model_name(self):
        # Every migrated secret's label names this model; a mismatch would make all of
        # them unreadable after the upgrade.
        self.assertEqual(self.migration.MODEL, self.env['pos.payment.method']._name)

    def test_fresh_install_changes_nothing(self):
        self.migration.migrate(self.env.cr, None)
        self.assertEqual(self._plaintext_columns(), list(self.migration.COLUMNS))

    def test_missing_key_aborts_before_dropping(self):
        use_test_encryption_key(self, None)
        self._migrate_expecting(musqet_crypto.MusqetEncryptionKeyUnavailable)
        self.assertEqual(self._plaintext_columns(), list(self.migration.COLUMNS))

    def test_failed_round_trip_aborts_before_dropping(self):
        with patch.object(self.migration, 'decrypt_secret', return_value=False):
            self._migrate_expecting(self.migration.MusqetMigrationError)
        self.assertEqual(self._plaintext_columns(), list(self.migration.COLUMNS))

    def test_failure_on_a_later_column_keeps_every_plaintext_column(self):
        # The API key passes, then the webhook secret fails: nothing may be dropped yet.
        real_decrypt = self.migration.decrypt_secret

        def decrypt_failing_webhook_secret(ciphertext, label):
            return False if label.endswith('musqet_webhook_secret') else real_decrypt(ciphertext, label)

        with patch.object(self.migration, 'decrypt_secret', side_effect=decrypt_failing_webhook_secret):
            self._migrate_expecting(self.migration.MusqetMigrationError)
        self.assertEqual(self._plaintext_columns(), list(self.migration.COLUMNS))
