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
            'groups_id': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
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
            'groups_id': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
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
            self.method.with_user(self.cashier).musqet_create_sale({'serial': 'MSQ-WH-002'})
        self.assertFalse(self.method.musqet_latest_response)


@tagged('post_install', '-at_install')
class TestMusqetCreateRefund(TransactionCase):
    """musqet_create_refund is the server-side authority for a refund.

    The Musqet API enforces neither rail correctness nor over-refund (Musqet/musqet#2094), so
    this method must: re-read the (untrusted) original payment by id, refuse anything that
    isn't a card-settled Musqet sale of this company whose captured amount covers the request,
    and FORCE the refund-defining fields (type, mode, serial) so a tampered payload can't widen
    the action. The original pos.payment is stubbed (a real one needs a full POS session, left
    to the live smoke run) so these tests exercise the authorization logic directly.
    """

    def setUp(self):
        super().setUp()
        self.cashier = self.env['res.users'].create({
            'name': 'Musqet Cashier',
            'login': 'musqet_cashier_refund',
            'groups_id': [(6, 0, [self.env.ref('point_of_sale.group_pos_user').id])],
        })
        self.method = self.env['pos.payment.method'].create({
            'name': 'Musqet Terminal',
            'use_payment_terminal': 'musqet',
            'musqet_base_url': 'https://api.musqet.tech/api/v1',
            'musqet_api_key': 'secret-token',
            'musqet_terminal_serial': 'MSQ-RF-001',
        })

    def _stub_original(self, rail='card', amount=10.0, terminal='musqet', company=None):
        original = MagicMock()
        original.exists.return_value = original
        original.company_id = company if company is not None else self.method.company_id
        original.payment_method_id.use_payment_terminal = terminal
        original.musqet_rail = rail
        original.amount = amount
        original.currency_id.decimal_places = 2
        original.currency_id.name = 'USD'
        return original

    def _call_refund(self, payload, original, original_payment_id=123):
        # Default the request currency to the stub's so the currency-equality guard passes
        # unless a test deliberately overrides it.
        payload = {'currency': 'USD', **payload}
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'saleId': 'refund-1', 'status': 'PENDING'}
        with patch.object(type(self.env['pos.payment']), 'browse', return_value=original), \
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
            'currency': 'USD',   # must match the original; the mismatch case is tested separately
            'extra': 'smuggled',
        }
        result, mock_request = self._call_refund(payload, original)
        self.assertNotIn('error', result)
        sent = mock_request.mock_calls[0].kwargs['json']
        self.assertEqual(sent['type'], 'refund')         # forced — not the payload's "sale"
        self.assertEqual(sent['mode'], 'card')           # forced — not the payload's "any"
        self.assertEqual(sent['serial'], 'MSQ-RF-001')   # this method's serial, not the payload's
        self.assertEqual(sent['currency'], 'USD')        # the original's currency, from the record
        self.assertNotIn('extra', sent)

    def test_rejects_currency_mismatch(self):
        # The over-refund cap compares minor units, so a request in a different currency than
        # the original could compare mismatched scales — reject before forwarding.
        original = self._stub_original(rail='card', amount=10.0)   # currency USD
        result, mock_request = self._call_refund({'amountInCents': 500, 'currency': 'EUR'}, original)
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
