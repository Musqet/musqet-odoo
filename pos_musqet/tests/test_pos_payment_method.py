# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessDenied, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


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
        self.method = self.env['pos.payment.method'].create({
            'name': 'Musqet Terminal',
            'use_payment_terminal': 'musqet',
            'musqet_base_url': 'https://api.musqet.tech/api/v1',
            'musqet_api_key': 'secret-token',
            'musqet_terminal_serial': 'MSQ-TEST-001',
        })

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
        self.method = self.env['pos.payment.method'].create({
            'name': 'Musqet Terminal',
            'use_payment_terminal': 'musqet',
            'musqet_base_url': 'https://api.musqet.tech/api/v1',
            'musqet_api_key': 'secret-token',
            'musqet_terminal_serial': 'MSQ-WH-001',
            'musqet_webhook_secret': self.SECRET,
        })

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
        self.method = self.env['pos.payment.method'].create({
            'name': 'Musqet Terminal',
            'use_payment_terminal': 'musqet',
            'musqet_base_url': 'https://api.musqet.tech/api/v1',
            'musqet_api_key': 'secret-token',
            'musqet_terminal_serial': 'MSQ-WH-002',
        })

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
        self.method = self.env['pos.payment.method'].create({
            'name': 'Musqet Terminal',
            'use_payment_terminal': 'musqet',
            'musqet_base_url': 'https://api.musqet.tech/api/v1',
            'musqet_api_key': 'secret-token',
            'musqet_terminal_serial': 'MSQ-SALE-001',
        })
        self.currency = self.method.company_id.currency_id.name

    def _call_sale(self, payload):
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'saleId': 'sale-1', 'status': 'PENDING'}
        with patch.object(pos_payment_method.requests, 'request', return_value=fake) as mock_request:
            result = self.method.with_user(self.cashier).musqet_create_sale(payload)
        return result, mock_request

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
        result, mock_request = self._call_sale({'amountInCents': 500, 'reference': 'r' * 255})
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['reference'], 'r' * 255)

    def test_language_length_limits(self):
        for language, kept in (('', False), ('x' * 16, True), ('x' * 17, False)):
            with self.subTest(language=language):
                result, mock_request = self._call_sale({'amountInCents': 500, 'language': language})
                self.assertNotIn('error', result)
                self.assertEqual('language' in mock_request.mock_calls[0].kwargs['json'], kept)

    def test_rejects_a_currency_other_than_the_company_currency(self):
        other = 'EUR' if self.currency != 'EUR' else 'USD'
        for currency in (other, 'NOT-A-CODE', ['USD']):
            with self.subTest(currency=currency):
                result, mock_request = self._call_sale({'amountInCents': 500, 'currency': currency})
                self.assertIn('error', result)
                mock_request.assert_not_called()

    def test_rejects_an_invalid_reference(self):
        for reference in ({'nested': 'object'}, '', 'x' * 256, 42):
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
    isn't a card-settled Musqet sale of this company whose captured amount covers the request,
    set type, mode, serial and currency on the server, and require a valid reference, so a
    tampered payload can't widen the action. The original pos.payment is stubbed (a real one
    needs a full POS session, left to the live smoke run) so these tests exercise the
    authorization logic directly.
    """

    def setUp(self):
        super().setUp()
        self.cashier = self.env['res.users'].create({
            'name': 'Musqet Cashier',
            'login': 'musqet_cashier_refund',
            'group_ids': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
        })
        self.method = self.env['pos.payment.method'].create({
            'name': 'Musqet Terminal',
            'use_payment_terminal': 'musqet',
            'musqet_base_url': 'https://api.musqet.tech/api/v1',
            'musqet_api_key': 'secret-token',
            'musqet_terminal_serial': 'MSQ-RF-001',
        })
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
        # A real string saleId so the cumulative-refund search runs against a concrete value
        # (the search itself is stubbed in _call_refund — see prior_refunds).
        original.transaction_id = 'sale-orig-1'
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

    def _call_refund(self, payload, original, original_payment_id=123, prior_refunds=None):
        # Default the request currency to the stub's so the currency-equality guard passes
        # unless a test deliberately overrides it.
        payload = {'currency': self.currency, 'reference': 'REFUND-ORDER-1', **payload}
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'saleId': 'refund-1', 'status': 'PENDING'}
        # Stub the prior-refunds search so the cumulative cap / dedup logic is exercised without
        # a populated POS database; default to none.
        with patch.object(type(self.env['pos.payment']), 'browse', return_value=original), \
             patch.object(type(self.env['pos.payment']), 'search', return_value=prior_refunds or []), \
             patch.object(pos_payment_method.requests, 'request', return_value=fake) as mock_request:
            result = self.method.with_user(self.cashier).musqet_create_refund(
                payload, original_payment_id)
        return result, mock_request

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
            'currency': self.currency,   # must match the original; mismatch is tested separately
            'extra': 'smuggled',
        }
        result, mock_request = self._call_refund(payload, original)
        self.assertNotIn('error', result)
        sent = mock_request.mock_calls[0].kwargs['json']
        self.assertEqual(sent['type'], 'refund')         # forced — not the payload's "sale"
        self.assertEqual(sent['mode'], 'card')           # forced — not the payload's "any"
        self.assertEqual(sent['serial'], 'MSQ-RF-001')   # this method's serial, not the payload's
        self.assertEqual(sent['currency'], self.currency)  # the original's currency, from the record
        self.assertNotIn('extra', sent)

    def test_rejects_currency_mismatch(self):
        # The over-refund cap compares minor units, so a request in a different currency than
        # the original could compare mismatched scales — reject before forwarding.
        original = self._stub_original(rail='card', amount=10.0)   # company currency
        result, mock_request = self._call_refund(
            {'amountInCents': 500, 'currency': self.other_currency}, original)
        self.assertIn('error', result)
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
        # Exactly the remaining 600 is allowed through.
        result, mock_request = self._call_refund(
            {'amountInCents': 600}, original, prior_refunds=prior)
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['amountInCents'], 600)
        # One cent past the remaining is rejected before any terminal command.
        result, mock_request = self._call_refund(
            {'amountInCents': 601}, original, prior_refunds=prior)
        self.assertIn('error', result)
        mock_request.assert_not_called()

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
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            mock_request.return_value.status_code = 200
            mock_request.return_value.json.return_value = {'saleId': 'refund-1'}
            with patch.object(type(self.env['pos.payment']), 'browse',
                              return_value=self._stub_original(amount=10.0)), \
                 patch.object(type(self.env['pos.payment']), 'search', return_value=[]):
                result = self.method.with_user(self.cashier).musqet_create_refund(
                    {'amountInCents': 500, 'reference': 'R-1'}, 123)
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['currency'], self.currency)

    def test_rejects_an_original_not_in_the_company_currency(self):
        original = self._stub_original(amount=10.0)
        original.currency_id.name = self.other_currency
        result, mock_request = self._call_refund(
            {'amountInCents': 500, 'currency': self.other_currency}, original)
        self.assertIn('error', result)
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
            {'amountInCents': 500, 'reference': 'r' * 255}, self._stub_original())
        self.assertNotIn('error', result)
        self.assertEqual(mock_request.mock_calls[0].kwargs['json']['reference'], 'r' * 255)

    def test_rejects_a_missing_or_invalid_reference(self):
        original = self._stub_original(amount=10.0)
        for reference in (None, '', {'nested': 'object'}, 'x' * 256):
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
