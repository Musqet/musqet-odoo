# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.addons.pos_musqet.models.pos_payment_method import (
    MUSQET_LANGUAGE_MAX_LENGTH,
    MUSQET_REFERENCE_MAX_LENGTH,
)
from odoo.exceptions import AccessDenied, AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from .common import create_musqet_method, load_migration, record_refund


@tagged('post_install', '-at_install')
class TestMusqetBaseUrl(TransactionCase):
    """Lock the SSRF behaviour of the musqet_base_url constraint against regression.

    Exercised directly on the constraint via new() so the test doesn't depend on the
    other required fields of pos.payment.method.
    """

    def _check(self, url):
        method = self.env['pos.payment.method'].new({'musqet_base_url': url})
        method._check_musqet_base_url()

    def test_accepts_musqet_https_hosts(self):
        for url in (
            'https://api.musqet.tech/api/v1',
            'https://musqet.tech/api',
            'https://staging.api.musqet.tech/v1',
        ):
            # Must not raise.
            self._check(url)

    def test_empty_url_is_allowed(self):
        # Non-Musqet methods carry no base URL; the constraint must skip them.
        self._check('')
        self._check(False)

    def test_base_url_error_messages(self):
        error = self.env['pos.payment.method']._musqet_base_url_error
        self.assertIsNone(error('https://api.musqet.tech/api/v1'))
        self.assertIsNone(error(False))
        self.assertIn('https://', error('http://api.musqet.tech'))
        self.assertIn('IP address', error('https://169.254.169.254/'))
        # urllib3 keeps an IPv6 literal's brackets; the host comparison must allow for that.
        self.assertIn('IP address', error('https://[::1]/x'))
        self.assertIn('internal address', error('https://localhost/'))
        self.assertIn(pos_payment_method.MUSQET_ALLOWED_HOST, error('https://musqet.tech.evil.example/'))
        # urlparse raises on this; the check must still return a message, not raise.
        self.assertIn('not valid', error('https://[::1'))
        self.assertIn('not valid', error('https://api.musqet.tech:99999/'))

    def test_rejects_urls_the_http_client_would_send_elsewhere(self):
        # urlparse reads the host of these as api.musqet.tech; the HTTP client would not.
        error = self.env['pos.payment.method']._musqet_base_url_error
        for url in (
            'https://evil.example\\@api.musqet.tech/api/v1',
            'https://evil.example\\api.musqet.tech/api/v1',
        ):
            self.assertIn('not valid', error(url), msg=url)
        # Past the character filter, so only the host cross-check can catch it: urllib3
        # sends to the punycode host, which is not the host urlparse checked.
        self.assertIn('not valid', error('https://ápi.musqet.tech/api/v1'))

    def test_rejects_credentials_and_unsafe_characters(self):
        # Both parsers agree on these hosts; they are refused by policy.
        error = self.env['pos.payment.method']._musqet_base_url_error
        for url in (
            'https://user:pass@api.musqet.tech/api/v1',
            'https://evil.example@api.musqet.tech/api/v1',
            'https://api.musqet.tech /api/v1',
            'https://api.musqet.tech\t/api/v1',
            'https://api.musqet.tech/api/v1\n',
        ):
            self.assertIn('not valid', error(url), msg=url)

    def test_request_rechecks_a_base_url_written_behind_the_orm(self):
        # A direct database write skips the save-time constraint; the bearer token must
        # still never go to an off-domain host.
        method = create_musqet_method(self, 'MSQ-URL-001')
        self.env.flush_all()
        for url, expected in (
            ('https://attacker.example/api', pos_payment_method.MUSQET_ALLOWED_HOST),
            ('https://attacker.example\\@api.musqet.tech/api', 'not valid'),
        ):
            self.env.cr.execute(
                'UPDATE pos_payment_method SET musqet_base_url = %s WHERE id = %s', (url, method.id))
            method.invalidate_recordset()
            with patch.object(pos_payment_method.requests, 'request') as mock_request:
                result = method.musqet_get_sale('sale-123')
            mock_request.assert_not_called()
            self.assertIn(expected, result['error']['message'], msg=url)

    def test_rejects_non_https(self):
        with self.assertRaises(ValidationError):
            self._check('http://api.musqet.tech/v1')

    def test_rejects_ip_literal_hosts(self):
        for url in (
            'https://169.254.169.254/latest/meta-data',  # cloud metadata endpoint
            'https://127.0.0.1/x',                        # loopback
            'https://10.0.0.5/x',                         # private
            'https://[::1]/x',                            # IPv6 loopback
        ):
            with self.assertRaises(ValidationError, msg=url):
                self._check(url)

    def test_rejects_internal_hostnames(self):
        for url in (
            'https://localhost/x',
            'https://foo.internal/x',
            'https://box.lan/x',
            'https://host.localdomain/x',
        ):
            with self.assertRaises(ValidationError, msg=url):
                self._check(url)

    def test_rejects_off_domain_and_suffix_confusion(self):
        for url in (
            'https://evil.com/api',
            'https://api.musqet.tech.evil.com/x',  # suffix confusion
            'https://notmusqet.tech/x',            # not a subdomain of musqet.tech
        ):
            with self.assertRaises(ValidationError, msg=url):
                self._check(url)


@tagged('post_install', '-at_install')
class TestMusqetProxyAccess(TransactionCase):
    """The proxy must work for a plain POS cashier, not just ERP managers.

    musqet_base_url and musqet_api_key are restricted to base.group_erp_manager, but
    _musqet_check_access lets any point_of_sale.group_pos_user drive the terminal. Both
    restricted fields must therefore be read via sudo() inside the proxy. This test drives
    the proxy as a cashier (with requests mocked) so a missing sudo() surfaces as a failure
    instead of slipping through — the constraint tests above run as admin and can't catch it.
    """

    def setUp(self):
        super().setUp()
        self.cashier = self.env['res.users'].create({
            'name': 'Musqet Cashier',
            'login': 'musqet_cashier',
            'group_ids': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
        })
        self.method = create_musqet_method(self, 'MSQ-TEST-001')

    def _fake_response(self, status_code=200, json_body=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_body if json_body is not None else {}
        return response

    def test_cashier_can_drive_proxy_through_restricted_fields(self):
        fake = self._fake_response(json_body={'status': 'PENDING'})
        with patch.object(pos_payment_method.requests, 'request', return_value=fake) as mock_request:
            result = self.method.with_user(self.cashier).musqet_get_sale('sale-123')

        # No AccessError on the group-restricted base_url/key reads, and the parsed body is returned.
        self.assertEqual(result, {'status': 'PENDING'})
        # The restricted base URL was read (via sudo) and reached the request intact...
        _, called_args, called_kwargs = mock_request.mock_calls[0]
        self.assertEqual(called_args[0], 'GET')
        self.assertEqual(called_args[1], 'https://api.musqet.tech/api/v1/terminal/sales/sale-123')
        # ...the restricted key reached the Authorization header...
        self.assertEqual(called_kwargs['headers']['Authorization'], 'Bearer secret-token')
        # ...and redirects are not followed (SSRF guard).
        self.assertFalse(called_kwargs['allow_redirects'])

    def test_get_and_cancel_deny_a_non_musqet_payment_method(self):
        # _musqet_check_access also refuses a method that isn't a Musqet one: no request may go
        # out for it.
        plain = self.env['pos.payment.method'].create({'name': 'Plain Method'}).with_user(self.cashier)
        calls = {
            'musqet_get_sale': lambda: plain.musqet_get_sale('sale-1'),
            'musqet_cancel_sale': lambda: plain.musqet_cancel_sale('sale-1'),
        }
        for name, call in calls.items():
            with self.subTest(method=name), \
                 patch.object(pos_payment_method.requests, 'request') as mock_request:
                with self.assertRaises(AccessDenied):
                    call()
                mock_request.assert_not_called()


@tagged('post_install', '-at_install')
class TestMusqetWebhookVerification(TransactionCase):
    """The webhook HMAC is the only authentication on a public, unauthenticated route.

    Lock down §4.5: a request is accepted only with the configured signing secret, a fresh
    timestamp, and a signature over the exact raw bytes. Exercised directly on
    _musqet_verify_webhook so the test doesn't need the HTTP stack.
    """

    SECRET = 'whsec_test_0123456789'

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-WH-001', musqet_webhook_secret=self.SECRET)

    def _sign(self, raw_body, timestamp, secret=None):
        if isinstance(raw_body, str):
            raw_body = raw_body.encode()
        signed_payload = ('%s.' % timestamp).encode() + raw_body
        digest = hmac.new((secret or self.SECRET).encode(), signed_payload, hashlib.sha256).hexdigest()
        return 't=%s,v1=%s' % (timestamp, digest)

    def test_accepts_a_valid_fresh_signature(self):
        body = b'{"saleId":"s-1","status":"COMPLETE","serial":"MSQ-WH-001"}'
        header = self._sign(body, int(time.time()))
        self.assertTrue(self.method._musqet_verify_webhook(header, body))

    def test_signature_is_bound_to_the_exact_bytes(self):
        # A signature for one body must not verify a different body (tamper detection).
        body = b'{"saleId":"s-1","status":"COMPLETE","serial":"MSQ-WH-001"}'
        header = self._sign(body, int(time.time()))
        tampered = b'{"saleId":"s-1","status":"COMPLETE","serial":"MSQ-WH-999"}'
        self.assertFalse(self.method._musqet_verify_webhook(header, tampered))

    def test_rejects_wrong_secret(self):
        body = b'{"status":"COMPLETE"}'
        header = self._sign(body, int(time.time()), secret='whsec_attacker')
        self.assertFalse(self.method._musqet_verify_webhook(header, body))

    def test_rejects_stale_timestamp(self):
        body = b'{"status":"COMPLETE"}'
        stale = int(time.time()) - pos_payment_method.MUSQET_WEBHOOK_TOLERANCE - 60
        self.assertFalse(self.method._musqet_verify_webhook(self._sign(body, stale), body))

    def test_rejects_future_timestamp(self):
        body = b'{"status":"COMPLETE"}'
        future = int(time.time()) + pos_payment_method.MUSQET_WEBHOOK_TOLERANCE + 60
        self.assertFalse(self.method._musqet_verify_webhook(self._sign(body, future), body))

    def test_rejects_malformed_or_missing_header(self):
        body = b'{"status":"COMPLETE"}'
        for header in ('', 'garbage', 't=123', 'v1=abc', None):
            self.assertFalse(self.method._musqet_verify_webhook(header, body), msg=header)

    def test_rejects_when_no_secret_configured(self):
        self.method.musqet_webhook_secret = False
        body = b'{"status":"COMPLETE"}'
        header = self._sign(body, int(time.time()))
        self.assertFalse(self.method._musqet_verify_webhook(header, body))


@tagged('post_install', '-at_install')
class TestMusqetLatestStatus(TransactionCase):
    """musqet_get_latest_status is the cashier-facing, consume-once read of the webhook buffer."""

    def setUp(self):
        super().setUp()
        self.cashier = self.env['res.users'].create({
            'name': 'Musqet Cashier',
            'login': 'musqet_cashier_status',
            'group_ids': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
        })
        self.method = create_musqet_method(self, 'MSQ-WH-002')

    def test_denies_a_non_musqet_payment_method_and_keeps_its_buffer(self):
        # The method check guards the buffer itself: a refused read must not consume it.
        plain = self.env['pos.payment.method'].create({'name': 'Plain Method'})
        plain.musqet_latest_response = '{"status": "COMPLETE"}'
        with self.assertRaises(AccessDenied):
            plain.with_user(self.cashier).musqet_get_latest_status()
        self.assertEqual(plain.musqet_latest_response, '{"status": "COMPLETE"}')

    def test_returns_false_when_nothing_buffered(self):
        self.assertFalse(self.method.with_user(self.cashier).musqet_get_latest_status())

    def test_returns_parsed_buffer_for_a_cashier(self):
        # The buffer is ERP-manager-restricted, but a cashier must be able to read it via the
        # method's sudo() — same context bug class as the proxy fields.
        sale = {'saleId': 's-9', 'status': 'COMPLETE', 'rail': 'bitcoin'}
        self.method.musqet_latest_response = json.dumps(sale)
        self.assertEqual(self.method.with_user(self.cashier).musqet_get_latest_status(), sale)

    def test_consumes_the_buffer_once(self):
        # Consume-once idempotency: the first read returns the result and clears the buffer,
        # so a duplicate notification (or a replayed-but-fresh webhook) reads nothing.
        sale = {'saleId': 's-9', 'status': 'COMPLETE'}
        self.method.musqet_latest_response = json.dumps(sale)
        method_as_cashier = self.method.with_user(self.cashier)
        self.assertEqual(method_as_cashier.musqet_get_latest_status(), sale)
        self.assertFalse(self.method.musqet_latest_response)
        self.assertFalse(method_as_cashier.musqet_get_latest_status())

    def test_denies_a_non_pos_user(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessDenied):
            self.method.with_user(public).musqet_get_latest_status()

    def test_create_sale_clears_a_stale_buffer(self):
        # A new sale must drop any buffered result from a previous one so a late notification
        # for it can't be mistaken for the new sale.
        self.method.musqet_latest_response = json.dumps({'saleId': 'old', 'status': 'COMPLETE'})
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'saleId': 'new', 'status': 'PENDING'}
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            self.method.with_user(self.cashier).musqet_create_sale({'amountInCents': 500})
        self.assertFalse(self.method.musqet_latest_response)


@tagged('post_install', '-at_install')
class TestMusqetCreateSale(TransactionCase):
    """musqet_create_sale rebuilds the request server-side from an untrusted payload.

    Any POS user can call it over RPC, and the API accepts ``type`` on the same endpoint,
    so the payload must never be able to turn a sale into a refund, name a serial other
    than the method's own, pick the currency, or smuggle extra keys through. (Which method
    record the caller runs it on is a separate access question.)
    """

    def setUp(self):
        super().setUp()
        self.cashier = self.env['res.users'].create({
            'name': 'Musqet Cashier',
            'login': 'musqet_cashier_sale',
            'group_ids': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
        })
        self.method = create_musqet_method(self, 'MSQ-SALE-001')
        self.currency = self.method.company_id.currency_id.name

    def _call_sale(self, payload):
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'saleId': 'sale-1', 'status': 'PENDING'}
        with patch.object(pos_payment_method.requests, 'request', return_value=fake) as mock_request:
            result = self.method.with_user(self.cashier).musqet_create_sale(payload)
        return result, mock_request

    def test_nothing_is_left_unwritten_when_the_sale_is_sent(self):
        # Clearing the webhook slot is written before the POST: written at commit, it could
        # fail after the sale started, and Odoo's retry would start it again.
        self.method.musqet_latest_response = json.dumps({'saleId': 'old', 'status': 'COMPLETE'})
        dirty_at_post = []

        def fake_request(method, url, **kwargs):
            dirty_at_post.extend(
                field.name for field, ids in self.env.transaction.field_dirty.items() if ids)
            response = MagicMock(status_code=200)
            response.json.return_value = {'saleId': 'sale-1', 'status': 'PENDING'}
            return response

        with patch.object(pos_payment_method.requests, 'request', side_effect=fake_request):
            result = self.method.with_user(self.cashier).musqet_create_sale({'amountInCents': 500})
        self.assertNotIn('error', result)
        self.assertEqual(dirty_at_post, [])

    def test_sends_a_well_formed_sale(self):
        payload = {
            'amountInCents': 1250,
            'mode': 'any',
            'shouldPrint': True,
            'language': 'en',
            'reference': 'Order 00001-001-0001',
            'currency': self.currency,
        }
        result, mock_request = self._call_sale(payload)
        self.assertEqual(result, {'saleId': 'sale-1', 'status': 'PENDING'})
        _, called_args, called_kwargs = mock_request.mock_calls[0]
        self.assertEqual(called_args[:2], ('POST', 'https://api.musqet.tech/api/v1/terminal/sales'))
        self.assertEqual(called_kwargs['json'], {
            'serial': 'MSQ-SALE-001',
            'type': 'sale',
            'mode': 'any',
            'amountInCents': 1250,
            'shouldPrint': True,
            'language': 'en',
            'reference': 'Order 00001-001-0001',
            'currency': self.currency,
        })

    def test_forces_type_sale_and_own_serial_and_drops_unknown_keys(self):
        payload = {
            'amountInCents': 99999999,
            'type': 'refund',
            'serial': 'MSQ-OTHER-999',
            'extra': 'smuggled',
        }
        result, mock_request = self._call_sale(payload)
        self.assertNotIn('error', result)
        sent = mock_request.mock_calls[0].kwargs['json']
        self.assertEqual(sent['type'], 'sale')            # never the payload's "refund"
        self.assertEqual(sent['serial'], 'MSQ-SALE-001')  # this method's serial
        self.assertNotIn('extra', sent)
        self.assertEqual(sent['mode'], 'any')             # default when the payload omits it
        self.assertEqual(sent['currency'], self.currency)  # the company's, when omitted

    def test_rejects_invalid_amounts(self):
        for amount in (None, 0, -100, 12.5, '500', True, [500]):
            with self.subTest(amount=amount):
                result, mock_request = self._call_sale({'amountInCents': amount})
                self.assertIn('error', result)
                mock_request.assert_not_called()

    def test_rejects_unknown_mode(self):
        result, mock_request = self._call_sale({'amountInCents': 500, 'mode': 'refund'})
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_a_non_dict_payload(self):
        result, mock_request = self._call_sale(['amountInCents', 500])
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_drops_or_defaults_cosmetic_fields_of_the_wrong_type(self):
        for should_print in ('yes', 'false', 0, None):
            with self.subTest(shouldPrint=should_print):
                result, mock_request = self._call_sale(
                    {'amountInCents': 500, 'language': ['en'], 'shouldPrint': should_print})
                self.assertNotIn('error', result)
                sent = mock_request.mock_calls[0].kwargs['json']
                self.assertNotIn('language', sent)
                self.assertIs(sent['shouldPrint'], True)   # the default, never a coerced value

    def test_keeps_should_print_false(self):
        result, mock_request = self._call_sale({'amountInCents': 500, 'shouldPrint': False})
        self.assertNotIn('error', result)
        self.assertIs(mock_request.mock_calls[0].kwargs['json']['shouldPrint'], False)

    def test_accepts_every_valid_mode(self):
        for mode in ('any', 'card', 'bitcoin'):
            with self.subTest(mode=mode):
                result, mock_request = self._call_sale({'amountInCents': 500, 'mode': mode})
                self.assertNotIn('error', result)
                self.assertEqual(mock_request.mock_calls[0].kwargs['json']['mode'], mode)

    def test_accepts_a_reference_at_the_length_limit(self):
        result, mock_request = self._call_sale({'amountInCents': 500, 'reference': 'r' * MUSQET_REFERENCE_MAX_LENGTH})
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['reference'], 'r' * MUSQET_REFERENCE_MAX_LENGTH)

    def test_language_length_limits(self):
        for language, kept in (
            ('', False),
            ('x' * MUSQET_LANGUAGE_MAX_LENGTH, True),
            ('x' * (MUSQET_LANGUAGE_MAX_LENGTH + 1), False),
        ):
            with self.subTest(language=language):
                result, mock_request = self._call_sale({'amountInCents': 500, 'language': language})
                self.assertNotIn('error', result)
                self.assertEqual('language' in mock_request.mock_calls[0].kwargs['json'], kept)

    def test_rejects_a_currency_other_than_the_company_currency(self):
        other = 'EUR' if self.currency != 'EUR' else 'USD'
        for currency in (other, 'NOT-A-CODE', ['USD']):
            with self.subTest(currency=currency):
                result, mock_request = self._call_sale({'amountInCents': 500, 'currency': currency})
                self.assertIn('company currency', result['error']['message'])
                mock_request.assert_not_called()

    def test_rejects_an_invalid_reference(self):
        for reference in ({'nested': 'object'}, '', 'x' * (MUSQET_REFERENCE_MAX_LENGTH + 1), 42):
            with self.subTest(reference=reference):
                result, mock_request = self._call_sale({'amountInCents': 500, 'reference': reference})
                self.assertIn('error', result)
                mock_request.assert_not_called()

    def test_denies_a_non_musqet_payment_method(self):
        cash = self.env['pos.payment.method'].create({'name': 'Plain Method'})
        with self.assertRaises(AccessDenied):
            cash.with_user(self.cashier).musqet_create_sale({'amountInCents': 500})

    def test_rejects_when_no_serial_is_configured(self):
        self.method.musqet_terminal_serial = False
        result, mock_request = self._call_sale({'amountInCents': 500})
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_does_not_clear_the_buffer_on_a_rejected_request(self):
        self.method.musqet_latest_response = json.dumps({'saleId': 'live', 'status': 'PENDING'})
        self._call_sale({'amountInCents': 0})
        self.assertTrue(self.method.musqet_latest_response)

    def test_denies_a_non_pos_user(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessDenied):
            self.method.with_user(public).musqet_create_sale({'amountInCents': 500})


@tagged('post_install', '-at_install')
class TestMusqetCreateRefund(TransactionCase):
    """musqet_create_refund is the server-side authority for a refund.

    The Musqet API enforces neither rail correctness nor over-refund (Musqet/musqet#2094), so
    this method must: re-read the (untrusted) original payment by id, refuse anything that
    isn't a card-settled Musqet sale of this company, in the company currency, whose captured
    amount covers the request, set type, mode and serial on the server, require the company
    currency, and require a valid reference, so a tampered payload can't widen the action. The
    original pos.payment is stubbed (a real one needs a full POS session, left to the live
    smoke run) so these tests exercise the authorization logic directly.
    """

    def setUp(self):
        super().setUp()
        self.cashier = self.env['res.users'].create({
            'name': 'Musqet Cashier',
            'login': 'musqet_cashier_refund',
            'group_ids': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
        })
        self.method = create_musqet_method(self, 'MSQ-RF-001')
        self.currency = self.method.company_id.currency_id.name
        self.other_currency = 'EUR' if self.currency != 'EUR' else 'USD'

    def _stub_original(self, rail='card', amount=10.0, terminal='musqet', company=None):
        original = MagicMock()
        original.exists.return_value = original
        original.company_id = company if company is not None else self.method.company_id
        original.payment_method_id.use_payment_terminal = terminal
        original.musqet_rail = rail
        original.amount = amount
        original.currency_id.decimal_places = 2
        original.currency_id.name = self.currency
        # A real string saleId: the refund records (pos.musqet.refund) are real and keyed on it.
        # Only the synced-payment search is stubbed, in _call_refund (see prior_refunds).
        original.transaction_id = 'sale-orig-1'
        # No pos.payment row carries this saleId, so the claim (_musqet_claim_original_sale)
        # updates nothing here; TestMusqetRefundWithPosRecords covers it on real rows.
        return original

    def _stub_prior_refund(self, amount, reference='OTHER-ORDER', pos_reference=None, uuid=None):
        # A previously-settled refund of the original. Refund payment lines carry a negative
        # amount in a real order, which the cumulative cap takes the magnitude of. By default
        # both order keys carry `reference`; pass pos_reference/uuid explicitly to exercise a
        # single arm of the dedup's `reference in (pos_reference, uuid)` membership check.
        refund = MagicMock()
        refund.amount = amount
        refund.pos_order_id.pos_reference = reference if pos_reference is None else pos_reference
        refund.pos_order_id.uuid = reference if uuid is None else uuid
        return refund

    def _call_refund(self, payload, original, original_payment_id=123, prior_refunds=None,
                     sale_id='refund-1', post_status=200, get_status='PENDING', get_http=200):
        """Run musqet_create_refund as the cashier with the Musqet API stubbed.

        The create (POST) returns ``sale_id`` with HTTP ``post_status``; a status read (GET)
        of an earlier recorded refund returns ``get_status`` with HTTP ``get_http``.
        """
        # No default currency: an omitted one means the company currency, so most tests take
        # that path. Tests that send a currency do so deliberately.
        payload = {'reference': 'REFUND-ORDER-1', **payload}

        def fake_request(method, url, **kwargs):
            response = MagicMock()
            if method == 'GET':
                response.status_code = get_http
                response.json.return_value = {'saleId': url.rsplit('/', 1)[1], 'status': get_status}
            else:
                # Anything still unwritten now could fail after the refund is sent.
                self.dirty_at_post = {
                    field.name for field in self.env.transaction.field_dirty
                    if self.env.transaction.field_dirty[field]}
                response.status_code = post_status
                response.json.return_value = {'saleId': sale_id, 'status': 'PENDING'}
            return response

        # Stub the synced-refunds search so the legacy branch of the cap is exercised without
        # a populated POS database; default to none.
        with patch.object(type(self.env['pos.payment']), 'browse', return_value=original), \
             patch.object(type(self.env['pos.payment']), 'search', return_value=prior_refunds or []), \
             patch.object(pos_payment_method.requests, 'request', side_effect=fake_request) as mock_request:
            result = self.method.with_user(self.cashier).musqet_create_refund(
                payload, original_payment_id)
        return result, mock_request

    def _calls(self, mock_request, verb):
        """The stubbed API calls made with HTTP ``verb``."""
        return [call for call in mock_request.mock_calls if call.args and call.args[0] == verb]

    def _posts(self, mock_request):
        """The create calls (POST) among the stubbed API calls."""
        return self._calls(mock_request, 'POST')

    def _gets(self, mock_request):
        """The status reads (GET) among the stubbed API calls."""
        return self._calls(mock_request, 'GET')

    def _recorded_refunds(self):
        """The refund records of the stubbed original sale."""
        return self.env['pos.musqet.refund'].search([('original_sale_id', '=', 'sale-orig-1')])

    def test_refunds_a_card_sale_within_the_captured_amount(self):
        original = self._stub_original(rail='card', amount=10.0)
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertNotIn('error', result)
        _, called_args, called_kwargs = mock_request.mock_calls[0]
        self.assertEqual(called_args[0], 'POST')
        self.assertEqual(called_args[1], 'https://api.musqet.tech/api/v1/terminal/sales')
        self.assertEqual(called_kwargs['json']['type'], 'refund')
        self.assertEqual(called_kwargs['json']['amountInCents'], 500)

    def test_forces_refund_fields_and_drops_unknown_keys(self):
        # A tampered payload must not be able to flip the action to a sale, switch the rail to
        # "any", spoof another terminal's serial, or smuggle extra keys through the proxy.
        original = self._stub_original(rail='card', amount=10.0)
        payload = {
            'amountInCents': 1000,
            'type': 'sale',
            'mode': 'any',
            'serial': 'MSQ-EVIL-999',
            'currency': self.currency,   # must be the company currency; mismatch tested separately
            'extra': 'smuggled',
        }
        result, mock_request = self._call_refund(payload, original)
        self.assertNotIn('error', result)
        sent = mock_request.mock_calls[0].kwargs['json']
        self.assertEqual(sent['type'], 'refund')         # forced — not the payload's "sale"
        self.assertEqual(sent['mode'], 'card')           # forced — not the payload's "any"
        self.assertEqual(sent['serial'], 'MSQ-RF-001')   # this method's serial, not the payload's
        self.assertEqual(sent['currency'], self.currency)  # the company currency
        self.assertNotIn('extra', sent)

    def test_rejects_currency_mismatch(self):
        # A refund requested in any currency but the company's is refused before forwarding.
        for currency in (self.other_currency, 'NOT-A-CODE', ['USD']):
            with self.subTest(currency=currency):
                result, mock_request = self._call_refund(
                    {'amountInCents': 500, 'currency': currency}, self._stub_original())
                self.assertIn('company currency', result['error']['message'])
                mock_request.assert_not_called()

    def test_rejects_refund_of_a_lightning_sale(self):
        original = self._stub_original(rail='bitcoin', amount=10.0)
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_refund_of_a_non_musqet_payment(self):
        original = self._stub_original(rail='card', amount=10.0, terminal='stripe')
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_over_refund(self):
        original = self._stub_original(rail='card', amount=10.0)   # captured = 1000 cents
        result, mock_request = self._call_refund({'amountInCents': 1500}, original)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_non_positive_amount(self):
        original = self._stub_original(rail='card', amount=10.0)
        for amount in (0, -100, None, 'x'):
            result, mock_request = self._call_refund({'amountInCents': amount}, original)
            self.assertIn('error', result, msg=amount)
            mock_request.assert_not_called()

    def test_caps_against_remaining_after_prior_refunds(self):
        # captured = 1000 cents; a prior 400-cent refund leaves 600 refundable.
        original = self._stub_original(rail='card', amount=10.0)
        prior = [self._stub_prior_refund(-4.0)]
        # One cent past the remaining is rejected before any terminal command.
        result, mock_request = self._call_refund(
            {'amountInCents': 601}, original, prior_refunds=prior)
        self.assertIn('error', result)
        mock_request.assert_not_called()
        # Exactly the remaining 600 is allowed through. (Second, as an accepted refund is
        # recorded and would count against the 601.)
        result, mock_request = self._call_refund(
            {'amountInCents': 600}, original, prior_refunds=prior)
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['amountInCents'], 600)

    def test_rejects_when_prior_refunds_exhaust_the_amount(self):
        # captured = 1000 cents; prior refunds already total 1000, so nothing remains.
        original = self._stub_original(rail='card', amount=10.0)
        prior = [self._stub_prior_refund(-6.0), self._stub_prior_refund(-4.0)]
        result, mock_request = self._call_refund(
            {'amountInCents': 1}, original, prior_refunds=prior)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_duplicate_refund_reference(self):
        # A refund of this original already carries this refund order's reference -> duplicate.
        original = self._stub_original(rail='card', amount=10.0)
        prior = [self._stub_prior_refund(-3.0, reference='REFUND-ORDER-7')]
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'REFUND-ORDER-7'}, original, prior_refunds=prior)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_dedup_matches_on_the_order_uuid_arm(self):
        # The reference is built caller-side as order.pos_reference || order.uuid, so a prior
        # refund whose pos_reference is empty but whose uuid carries the reference must still be
        # caught. Exercises the uuid arm of the `reference in (pos_reference, uuid)` check alone.
        original = self._stub_original(rail='card', amount=10.0)
        prior = [self._stub_prior_refund(-3.0, pos_reference=False, uuid='REFUND-UUID-7')]
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'REFUND-UUID-7'}, original, prior_refunds=prior)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_allows_a_distinct_reference_within_the_remaining_amount(self):
        # Same original, a different (genuinely new) refund order reference, still within the
        # remaining amount -> allowed through. Guards against the dedup over-matching.
        original = self._stub_original(rail='card', amount=10.0)
        prior = [self._stub_prior_refund(-3.0, reference='REFUND-ORDER-7')]
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'REFUND-ORDER-8'}, original, prior_refunds=prior)
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['amountInCents'], 200)

    def test_rejects_a_non_dict_payload(self):
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.with_user(self.cashier).musqet_create_refund(['amountInCents', 500], 123)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_loosely_typed_amounts(self):
        original = self._stub_original(amount=10.0)
        for amount in (True, 12.9, '500', None):
            with self.subTest(amount=amount):
                result, mock_request = self._call_refund({'amountInCents': amount}, original)
                self.assertIn('error', result)
                mock_request.assert_not_called()

    def test_omitted_currency_means_the_company_currency(self):
        # Same rule as a sale: no currency in the payload means the company currency.
        result, mock_request = self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['currency'], self.currency)

    def test_rejects_an_original_not_in_the_company_currency(self):
        # The over-refund cap compares minor units, so an original in another currency could
        # compare mismatched scales: refused even when the request itself is fine.
        original = self._stub_original(amount=10.0)
        original.currency_id.name = self.other_currency
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('original payment', result['error']['message'])
        mock_request.assert_not_called()

    def test_rejects_when_no_serial_is_configured(self):
        self.method.musqet_terminal_serial = False
        self.method.musqet_latest_response = json.dumps({'saleId': 'live', 'status': 'PENDING'})
        result, mock_request = self._call_refund({'amountInCents': 500}, self._stub_original())
        self.assertIn('error', result)
        mock_request.assert_not_called()
        self.assertTrue(self.method.musqet_latest_response)

    def test_denies_a_non_musqet_payment_method(self):
        plain = self.env['pos.payment.method'].create({'name': 'Plain Method'})
        with self.assertRaises(AccessDenied):
            plain.with_user(self.cashier).musqet_create_refund(
                {'amountInCents': 500, 'currency': self.currency, 'reference': 'R-1'}, 123)

    def test_accepts_a_reference_at_the_length_limit(self):
        result, mock_request = self._call_refund(
            {'amountInCents': 500, 'reference': 'r' * MUSQET_REFERENCE_MAX_LENGTH}, self._stub_original())
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['reference'], 'r' * MUSQET_REFERENCE_MAX_LENGTH)

    def test_rejects_a_missing_or_invalid_reference(self):
        original = self._stub_original(amount=10.0)
        for reference in (None, '', {'nested': 'object'}, 'x' * (MUSQET_REFERENCE_MAX_LENGTH + 1)):
            with self.subTest(reference=reference):
                result, mock_request = self._call_refund(
                    {'amountInCents': 500, 'reference': reference}, original)
                self.assertIn('error', result)
                mock_request.assert_not_called()

    def test_drops_cosmetic_fields_of_the_wrong_type_and_keeps_server_currency(self):
        original = self._stub_original(amount=10.0)
        result, mock_request = self._call_refund(
            {'amountInCents': 500, 'language': ['en'], 'shouldPrint': 'no'}, original)
        self.assertNotIn('error', result)
        sent = mock_request.mock_calls[0].kwargs['json']
        self.assertNotIn('language', sent)
        self.assertIs(sent['shouldPrint'], True)
        self.assertEqual(sent['currency'], self.currency)
        self.assertEqual(sent['reference'], 'REFUND-ORDER-1')

    def test_rejects_cross_company_original(self):
        other_company = self.env['res.company'].create({'name': 'Other Co'})
        original = self._stub_original(rail='card', amount=10.0, company=other_company)
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_rejects_unknown_original_payment(self):
        empty = self.env['pos.payment']
        result, mock_request = self._call_refund({'amountInCents': 500}, empty)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_denies_a_non_pos_user(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessDenied):
            self.method.with_user(public).musqet_create_refund({'amountInCents': 500}, 123)

    # -- Server-side refund records (pos.musqet.refund) ------------------------

    def test_records_an_accepted_refund(self):
        result, _ = self._call_refund(
            {'amountInCents': 500, 'reference': 'REFUND-ORDER-9'}, self._stub_original(amount=10.0))
        self.assertNotIn('error', result)
        refund = self._recorded_refunds()
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund.sale_id, 'refund-1')
        self.assertEqual(refund.reference, 'REFUND-ORDER-9')
        self.assertEqual(refund.amount_minor, 500)
        self.assertEqual(refund.state, 'pending')
        self.assertEqual(refund.payment_method_id, self.method)
        self.assertEqual(refund.company_id, self.method.company_id)

    def test_records_nothing_when_musqet_refuses_the_refund(self):
        result, _ = self._call_refund(
            {'amountInCents': 500}, self._stub_original(amount=10.0), post_status=409)
        self.assertIn('error', result)
        self.assertFalse(self._recorded_refunds())

    def test_counts_a_recorded_refund_before_its_order_syncs(self):
        # Two refunds of 600 against a 1000 sale, with no refund order synced in between: the
        # first is recorded when Musqet accepts it, so the second is over the cap.
        original = self._stub_original(amount=10.0)
        result, _ = self._call_refund(
            {'amountInCents': 600, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        self.assertNotIn('error', result)
        result, mock_request = self._call_refund(
            {'amountInCents': 600, 'reference': 'REFUND-B'}, original, sale_id='refund-b')
        self.assertIn('exceeds', result['error']['message'])
        self.assertFalse(self._posts(mock_request))
        # The remaining 400 still goes through.
        result, mock_request = self._call_refund(
            {'amountInCents': 400, 'reference': 'REFUND-B'}, original, sale_id='refund-b')
        self.assertNotIn('error', result)
        self.assertEqual(len(self._posts(mock_request)), 1)

    def test_refuses_a_recorded_reference_before_its_order_syncs(self):
        # Still pending at Musqet: refused, and the cashier is told it's in progress.
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 200, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'REFUND-A'}, original, sale_id='refund-a2')
        self.assertIn('still in progress', result['error']['message'])
        self.assertFalse(self._posts(mock_request))
        # Once Musqet reports it done, the same reference is a duplicate.
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'REFUND-A'}, original, sale_id='refund-a2',
            get_status='COMPLETE')
        self.assertIn('already been processed', result['error']['message'])
        self.assertFalse(self._posts(mock_request))

    def test_a_pending_refund_wins_over_a_done_one_with_the_same_reference(self):
        # The cashier is told a refund is still running rather than that it's finished.
        record_refund(self.env, self.method, 'refund-a', reference='REFUND-A', state='done')
        record_refund(self.env, self.method, 'refund-b', reference='REFUND-A')
        result, mock_request = self._call_refund(
            {'amountInCents': 1, 'reference': 'REFUND-A'}, self._stub_original(amount=10.0))
        self.assertIn('still in progress', result['error']['message'])
        self.assertFalse(self._posts(mock_request))

    def test_matches_a_recorded_refund_by_either_order_reference(self):
        # A record holds one form of the reference (a backfilled one, the order's
        # pos_reference); its synced payment's order supplies the other (the uuid).
        original = self._stub_original(amount=10.0)
        record_refund(self.env, self.method, 'refund-a', amount_minor=200,
                      reference='Order 00001-001-0001', state='done')
        synced = self._stub_prior_refund(-2.0, pos_reference='Order 00001-001-0001', uuid='uuid-a')
        synced.transaction_id = 'refund-a'
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'uuid-a'}, original, prior_refunds=[synced])
        self.assertIn('already been processed', result['error']['message'])
        self.assertFalse(self._posts(mock_request))

    def test_nothing_is_left_unwritten_when_the_refund_is_sent(self):
        # A write still buffered at the POST could fail at commit, after Musqet has the
        # refund, and Odoo's retry would send it again. The re-read below buffers a state
        # change, and the method buffers clearing its webhook slot.
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 200, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        self.dirty_at_post = None
        result, mock_request = self._call_refund(
            {'amountInCents': 200, 'reference': 'REFUND-B'}, original, sale_id='refund-b',
            get_status='COMPLETE')
        self.assertNotIn('error', result)
        self.assertEqual(self.dirty_at_post, set())

    def test_logs_a_refund_musqet_accepted_but_odoo_could_not_record(self):
        # The same saleId already recorded on this method: the record can't be saved, so the
        # log is the trail for reconciling a refund Musqet has made.
        record_refund(self.env, self.method, 'refund-1', original_sale_id='sale-orig-other')
        logger = 'odoo.addons.pos_musqet.models.pos_payment_method'
        with self.assertLogs(logger, 'ERROR') as logs, mute_logger('odoo.sql_db'):
            result, _ = self._call_refund(
                {'amountInCents': 500, 'reference': 'REFUND-LOST'}, self._stub_original(amount=10.0))
        # The POS still gets the saleId, so it polls the refund and syncs its payment.
        self.assertEqual(result, {'saleId': 'refund-1', 'status': 'PENDING'})
        # The failed insert didn't abort the request's transaction: it can still be used.
        self.assertEqual(self.env['pos.musqet.refund'].search_count([('sale_id', '=', 'refund-1')]), 1)
        self.assertIn('refund-1', logs.output[0])
        self.assertIn('REFUND-LOST', logs.output[0])
        self.assertIn('500', logs.output[0])

    def test_rereads_a_pending_refund_and_drops_a_failed_one(self):
        # The first refund failed at the terminal: once Musqet says so it neither counts nor
        # blocks a retry with the same reference.
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 1000, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        result, mock_request = self._call_refund(
            {'amountInCents': 1000, 'reference': 'REFUND-A'}, original, sale_id='refund-a2',
            get_status='CANCELLED')
        self.assertNotIn('error', result)
        self.assertEqual(
            [call.args[1] for call in self._gets(mock_request)],
            ['https://api.musqet.tech/api/v1/terminal/sales/refund-a'])
        states = {refund.sale_id: refund.state for refund in self._recorded_refunds()}
        self.assertEqual(states, {'refund-a': 'failed', 'refund-a2': 'pending'})

    def test_a_completed_refund_keeps_counting(self):
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 1000, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        result, mock_request = self._call_refund(
            {'amountInCents': 1, 'reference': 'REFUND-B'}, original, sale_id='refund-b',
            get_status='COMPLETE')
        self.assertIn('exceeds', result['error']['message'])
        self.assertFalse(self._posts(mock_request))
        self.assertEqual(self._recorded_refunds().state, 'done')

    def test_poll_updates_a_recorded_refund(self):
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        fake = MagicMock(status_code=200)
        for status, state in (('PROCESSING', 'pending'), ('COMPLETE', 'done')):
            with self.subTest(status=status):
                fake.json.return_value = {'saleId': 'refund-1', 'status': status}
                with patch.object(pos_payment_method.requests, 'request', return_value=fake):
                    self.method.with_user(self.cashier).musqet_get_sale('refund-1')
                self.assertEqual(self._recorded_refunds().state, state)

    def test_poll_error_leaves_a_recorded_refund_pending(self):
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        fake = MagicMock(status_code=502)
        fake.json.return_value = {'status': 'CANCELLED'}
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            self.method.with_user(self.cashier).musqet_get_sale('refund-1')
        self.assertEqual(self._recorded_refunds().state, 'pending')

    def test_a_synced_payment_with_a_record_is_counted_once(self):
        # The refund's own order synced (musqet_refund_of set) while its record exists: count
        # the record only, not both.
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 600, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        synced = self._stub_prior_refund(-6.0, reference='REFUND-A')
        synced.transaction_id = 'refund-a'
        result, _ = self._call_refund(
            {'amountInCents': 400, 'reference': 'REFUND-B'}, original, sale_id='refund-b',
            prior_refunds=[synced])
        self.assertNotIn('error', result)

    def test_a_synced_payment_of_a_failed_record_does_not_count(self):
        # Musqet's status beats the browser's: a refund Musqet reports as failed doesn't count
        # even if a payment for it was synced.
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 1000, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        synced = self._stub_prior_refund(-10.0, reference='REFUND-A')
        synced.transaction_id = 'refund-a'
        result, _ = self._call_refund(
            {'amountInCents': 1000, 'reference': 'REFUND-B'}, original, sale_id='refund-b',
            prior_refunds=[synced], get_status='ERROR')
        self.assertNotIn('error', result)

    def test_a_pending_refund_musqet_cannot_confirm_keeps_counting(self):
        original = self._stub_original(amount=10.0)
        self._call_refund({'amountInCents': 1000, 'reference': 'REFUND-A'}, original, sale_id='refund-a')
        result, mock_request = self._call_refund(
            {'amountInCents': 1, 'reference': 'REFUND-B'}, original, sale_id='refund-b',
            get_http=502, get_status='CANCELLED')
        self.assertIn('exceeds', result['error']['message'])
        self.assertFalse(self._posts(mock_request))
        self.assertEqual(self._recorded_refunds().state, 'pending')

    def test_a_refund_on_a_method_no_longer_musqet_counts_without_a_reread(self):
        plain = self.env['pos.payment.method'].create({'name': 'Former Musqet Method'})
        record_refund(self.env, plain, 'refund-old', amount_minor=1000)
        result, mock_request = self._call_refund(
            {'amountInCents': 1}, self._stub_original(amount=10.0), get_status='CANCELLED')
        self.assertIn('exceeds', result['error']['message'])
        mock_request.assert_not_called()

    def test_a_settled_refund_keeps_its_state(self):
        # Once done, a later failure status (or any status) can't take it out of the cap.
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        refund = self._recorded_refunds()
        refund._musqet_apply_status('COMPLETE')
        refund._musqet_apply_status('ERROR')
        self.assertEqual(refund.state, 'done')

    def test_rereads_at_most_the_limit_of_pending_refunds(self):
        for index in range(pos_payment_method.MUSQET_REFUND_REREAD_LIMIT + 2):
            record_refund(self.env, self.method, 'refund-p%d' % index)
        _, mock_request = self._call_refund({'amountInCents': 1}, self._stub_original(amount=10.0))
        reads = [call.args[1] for call in self._gets(mock_request)]
        # The oldest ones, in order.
        self.assertEqual(reads, [
            'https://api.musqet.tech/api/v1/terminal/sales/refund-p%d' % index
            for index in range(pos_payment_method.MUSQET_REFUND_REREAD_LIMIT)])

    def test_rejects_an_original_without_a_sale_id(self):
        original = self._stub_original(amount=10.0)
        original.transaction_id = False
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('error', result)
        mock_request.assert_not_called()

    def test_cashier_cannot_read_or_write_refund_records(self):
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        Refund = self.env['pos.musqet.refund'].with_user(self.cashier)
        with self.assertRaises(AccessError):
            Refund.search([])
        with self.assertRaises(AccessError):
            Refund.create({
                'company_id': self.method.company_id.id, 'payment_method_id': self.method.id,
                'original_sale_id': 'sale-orig-1', 'sale_id': 'forged', 'amount_minor': -1000,
            })


@tagged('post_install', '-at_install')
class TestMusqetRefundWithPosRecords(TransactionCase):
    """Refund records against real POS payments: the link, the claim and the backfill.

    A synced refund payment takes its musqet_refund_of from the server's refund record: the
    POS can only set the link while it still holds the refund's original in memory, so a
    refund settled after a page refresh used to sync with no link at all.
    """

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-LINK-001')
        self.plain = self.env['pos.payment.method'].create({'name': 'Plain Method'})
        config = self.env['pos.config'].create({
            'name': 'Musqet Link POS',
            'payment_method_ids': [(6, 0, (self.method | self.plain).ids)]})
        self.session = self.env['pos.session'].create({'config_id': config.id, 'user_id': self.env.uid})
        self.order = self._order()
        record_refund(self.env, self.method, 'refund-1', amount_minor=500)

    def _order(self):
        return self.env['pos.order'].create({
            'session_id': self.session.id, 'amount_total': 0, 'amount_tax': 0,
            'amount_paid': 0, 'amount_return': 0,
        })

    def _row_version(self, payment):
        self.env.cr.execute('SELECT xmin::text FROM pos_payment WHERE id = %s', [payment.id])
        return self.env.cr.fetchone()[0]

    def _payment(self, **vals):
        return self.env['pos.payment'].create({
            'pos_order_id': self.order.id, 'payment_method_id': self.method.id,
            'amount': -5.0, **vals})

    def test_create_links_a_recorded_refund(self):
        # No link from the POS (the page was refreshed), and a wrong one, both end up right.
        for sent in (False, 'sale-forged'):
            with self.subTest(sent=sent):
                payment = self._payment(transaction_id='refund-1', musqet_refund_of=sent)
                self.assertEqual(payment.musqet_refund_of, 'sale-orig-1')

    def test_write_links_a_recorded_refund(self):
        payment = self._payment(transaction_id='other')
        payment.transaction_id = 'refund-1'
        self.assertEqual(payment.musqet_refund_of, 'sale-orig-1')
        payment.musqet_refund_of = 'sale-forged'
        self.assertEqual(payment.musqet_refund_of, 'sale-orig-1')

    def test_leaves_a_payment_without_a_record_alone(self):
        payment = self._payment(transaction_id='refund-unknown', musqet_refund_of='sale-x')
        self.assertEqual(payment.musqet_refund_of, 'sale-x')

    def test_link_ignores_a_non_musqet_method(self):
        record_refund(self.env, self.plain, 'refund-plain', amount_minor=500)
        payment = self._payment(payment_method_id=self.plain.id, transaction_id='refund-plain')
        self.assertFalse(payment.musqet_refund_of)

    def test_refund_claims_the_original_payment_row(self):
        # A concurrent refund of the same sale whose snapshot predates this one's record must
        # fail on this row and be retried, so the refund must update the row. Run it in a
        # savepoint: an update there gives the row a new version (xmin) from the savepoint's
        # own transaction id, while a mere read leaves it as created.
        original = self._payment(
            amount=10.0, transaction_id='sale-orig-real', musqet_rail='card')
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'saleId': 'refund-real', 'status': 'PENDING'}
        before = self._row_version(original)
        with self.env.cr.savepoint(), \
             patch.object(pos_payment_method.requests, 'request', return_value=fake):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-REAL'}, original.id)
        self.assertNotIn('error', result)
        self.assertNotEqual(self._row_version(original), before)
        refund = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-real')])
        self.assertEqual(refund.original_sale_id, 'sale-orig-real')

    def test_refuses_a_refund_by_the_other_form_of_its_order_reference(self):
        # A backfilled record holds one form of the reference; the refund's synced payment
        # links it to its order, whose other form must be caught too. Real rows, so the
        # payment search's filter is exercised.
        original = self._payment(amount=10.0, transaction_id='sale-orig-ref', musqet_rail='card')
        refund_order = self._order()
        self._payment(pos_order_id=refund_order.id, transaction_id='refund-ref',
                      musqet_refund_of='sale-orig-ref')
        record_refund(self.env, self.method, 'refund-ref', original_sale_id='sale-orig-ref',
                      amount_minor=200, reference='Order 00001-001-0001', state='done')
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': refund_order.uuid}, original.id)
        self.assertIn('already been processed', result['error']['message'])
        mock_request.assert_not_called()
        # Another order's reference is not a duplicate, though its payments are in the table.
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'saleId': 'refund-new', 'status': 'PENDING'}
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': self.order.uuid}, original.id)
        self.assertNotIn('error', result)

    def test_refund_claims_every_row_carrying_the_original_sale(self):
        # Two rows with the original's saleId (say, a duplicated sync): a refund naming one
        # must claim the other too, or a concurrent refund naming the other wouldn't wait.
        original = self._payment(amount=10.0, transaction_id='sale-orig-dup', musqet_rail='card')
        twin = self._payment(amount=10.0, transaction_id='sale-orig-dup', musqet_rail='card')
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'saleId': 'refund-dup', 'status': 'PENDING'}
        before = self._row_version(twin)
        with self.env.cr.savepoint(), \
             patch.object(pos_payment_method.requests, 'request', return_value=fake):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-DUP'}, original.id)
        self.assertNotIn('error', result)
        self.assertNotEqual(self._row_version(twin), before)

    def test_backfill_does_nothing_on_a_fresh_install(self):
        self._payment(transaction_id='refund-legacy', musqet_refund_of='sale-orig-1')
        load_migration('19.0.1.2.0', 'post-migration').migrate(self.env.cr, None)
        self.assertFalse(self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-legacy')]))

    def test_manager_sees_only_their_companies_refunds(self):
        other = self.env['res.company'].create({'name': 'Other Co'})
        plain_other = self.env['pos.payment.method'].create({'name': 'Other Method', 'company_id': other.id})
        record_refund(self.env, plain_other, 'refund-other', original_sale_id='sale-other')
        manager = self.env['res.users'].create({
            'name': 'Musqet Manager', 'login': 'musqet_manager',
            'company_id': self.method.company_id.id,
            'company_ids': [(6, 0, self.method.company_id.ids)],
            'group_ids': [(6, 0, [self.env.ref('point_of_sale.group_pos_manager').id])],
        })
        seen = self.env['pos.musqet.refund'].with_user(manager).search([])
        self.assertEqual(seen.mapped('sale_id'), ['refund-1'])

    def test_backfill_records_linked_refunds_once(self):
        legacy = self._payment(transaction_id='refund-legacy', musqet_refund_of='sale-orig-1', amount=-3.0)
        self._payment(transaction_id='refund-1')  # already recorded in setUp
        self._payment(transaction_id='refund-unlinked')  # no musqet_refund_of: not a refund
        Refund = self.env['pos.musqet.refund']
        load_migration('19.0.1.2.0', 'post-migration').migrate(self.env.cr, '19.0.1.1.0')
        refunds = Refund.search([])
        self.assertEqual(sorted(refunds.mapped('sale_id')), ['refund-1', 'refund-legacy'])
        Refund._musqet_record_synced_refunds()  # running again adds nothing
        self.assertEqual(Refund.search([]), refunds)
        backfilled = refunds.filtered(lambda refund: refund.sale_id == 'refund-legacy')
        self.assertEqual(backfilled.original_sale_id, 'sale-orig-1')
        self.assertEqual(backfilled.amount_minor, 300)
        self.assertEqual(backfilled.state, 'done')
        self.assertEqual(backfilled.payment_method_id, self.method)
        self.assertEqual(backfilled.reference, legacy.pos_order_id.pos_reference or legacy.pos_order_id.uuid)
        self.assertTrue(backfilled.reference)
