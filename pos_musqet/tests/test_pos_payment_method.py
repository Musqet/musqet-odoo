# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.addons.pos_musqet.models.musqet_common import MUSQET_FAILURE_STATUSES, minor_units
from odoo.addons.pos_musqet.models.pos_payment_method import (
    MUSQET_LANGUAGE_MAX_LENGTH,
    MUSQET_REFERENCE_MAX_LENGTH,
)
from odoo.exceptions import AccessDenied, AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase
from odoo.tools import SQL, mute_logger

from .common import (
    create_musqet_method,
    dirty_field_names,
    load_migration,
    make_pos_user,
    musqet_response,
    record_refund,
    record_sale,
)


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
        self.cashier = make_pos_user(self.env, 'musqet_cashier')
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
        self.cashier = make_pos_user(self.env, 'musqet_cashier_status')
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
        self.cashier = make_pos_user(self.env, 'musqet_cashier_sale')
        self.method = create_musqet_method(self, 'MSQ-SALE-001')
        self.currency = self.method.company_id.currency_id.name

    def _call_sale(self, payload):
        # A new saleId per call (sale-1, sale-2, ...), as Musqet gives: each sale is recorded.
        self.sales_sent = getattr(self, 'sales_sent', 0) + 1
        fake = musqet_response('sale-%d' % self.sales_sent)
        with patch.object(pos_payment_method.requests, 'request', return_value=fake) as mock_request:
            result = self.method.with_user(self.cashier).musqet_create_sale(payload)
        return result, mock_request

    def test_nothing_is_left_unwritten_when_the_sale_is_sent(self):
        # Clearing the webhook slot is written before the POST: written at commit, it could
        # fail after the sale started, and Odoo's retry would start it again.
        self.method.musqet_latest_response = json.dumps({'saleId': 'old', 'status': 'COMPLETE'})
        dirty_at_post = []

        def fake_request(method, url, **kwargs):
            dirty_at_post.extend(dirty_field_names(self.env))
            return musqet_response('sale-1')

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
class TestMusqetSaleRecords(TransactionCase):
    """The server's record of each sale (pos.musqet.sale), settled only by Musqet's replies."""

    def setUp(self):
        super().setUp()
        self.cashier = make_pos_user(self.env, 'musqet_cashier_records')
        self.method = create_musqet_method(self, 'MSQ-REC-001')
        self.currency = self.method.company_id.currency_id.name
        # A complete card sale of the requested amount, as Musqet reports it.
        self.COMPLETE = {'status': 'COMPLETE', 'amountInCents': 1250, 'currency': self.currency,
                         'rail': 'card'}

    def _start_sale(self, status_code=200, status='PENDING'):
        fake = musqet_response('sale-1', status, status_code=status_code)
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            return self.method.with_user(self.cashier).musqet_create_sale(
                {'amountInCents': 1250, 'reference': 'Order-1'})

    def _poll(self, body, status_code=200, method=None):
        # ``body`` may name another saleId, as a reply about another sale would, or none.
        fake = MagicMock(status_code=status_code)
        fake.json.return_value = {'saleId': 'sale-1', **body}
        if fake.json.return_value['saleId'] is None:
            del fake.json.return_value['saleId']
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            (method or self.method).with_user(self.cashier).musqet_get_sale('sale-1')

    def _sale(self):
        return self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-1')])

    def test_records_an_accepted_sale(self):
        self._start_sale()
        sale = self._sale()
        self.assertEqual(len(sale), 1)
        self.assertEqual(sale.state, 'pending')
        self.assertEqual(sale.amount_minor, 1250)
        self.assertEqual(sale.currency, self.currency)
        self.assertEqual(sale.reference, 'Order-1')
        self.assertFalse(sale.rail)
        self.assertEqual(sale.payment_method_id, self.method)
        self.assertEqual(sale.company_id, self.method.company_id)

    def test_applies_the_status_of_the_create_reply(self):
        self._start_sale(status='ERROR')
        self.assertEqual(self._sale().state, 'failed')

    def test_a_reply_about_another_sale_changes_nothing(self):
        self._start_sale()
        self._poll({**self.COMPLETE, 'saleId': 'sale-2'})
        self.assertEqual(self._sale().state, 'pending')

    def test_settles_from_a_status_read_whose_reply_has_no_sale_id(self):
        # The read is about sale-1 by its URL.
        self._start_sale()
        self._poll({'saleId': None, **self.COMPLETE})
        self.assertEqual(self._sale().state, 'done')

    def test_takes_the_rail_from_a_later_reply_if_the_first_had_none(self):
        self._start_sale()
        self._poll({**self.COMPLETE, 'rail': None})
        self.assertEqual((self._sale().state, self._sale().rail), ('done', False))
        self._poll(self.COMPLETE)
        self.assertEqual(self._sale().rail, 'card')

    def test_upper_cases_the_reported_currency(self):
        self._start_sale()
        self._poll({**self.COMPLETE, 'currency': 'xts'})
        self.assertEqual(self._sale().currency, 'XTS')

    def test_records_nothing_when_musqet_refuses_the_sale(self):
        self.assertIn('error', self._start_sale(status_code=409))
        self.assertFalse(self._sale())

    def test_settles_a_sale_from_what_musqet_reports(self):
        # Musqet's amount, currency and rail, over what was requested.
        self._start_sale()
        self._poll({'status': 'PROCESSING'})
        self.assertEqual(self._sale().state, 'pending')
        self._poll({'status': 'COMPLETE', 'amountInCents': 1200, 'currency': 'XTS', 'rail': 'card'})
        sale = self._sale()
        self.assertEqual(sale.state, 'done')
        self.assertEqual(sale.amount_minor, 1200)
        self.assertEqual(sale.currency, 'XTS')
        self.assertEqual(sale.rail, 'card')

    def test_a_sale_that_ends_badly_is_failed(self):
        for status in MUSQET_FAILURE_STATUSES:
            with self.subTest(status=status):
                self._start_sale()
                self._poll({'status': status})
                self.assertEqual(self._sale().state, 'failed')
                self._sale().unlink()

    def test_a_complete_reply_without_a_valid_amount_or_currency_leaves_it_pending(self):
        # Done means Musqet confirmed what it captured.
        self._start_sale()
        logger = 'odoo.addons.pos_musqet.models.pos_musqet_sale'
        for bad in ({'amountInCents': True}, {'amountInCents': None}, {'currency': 'GBPX'},
                    {'currency': None}):
            with self.subTest(bad=bad), self.assertLogs(logger, 'WARNING'):
                self._poll({**self.COMPLETE, **bad})
                self.assertEqual(self._sale().state, 'pending')

    def test_ignores_an_unknown_rail(self):
        self._start_sale()
        self._poll({**self.COMPLETE, 'rail': 'paypal'})
        self.assertEqual((self._sale().state, self._sale().rail), ('done', False))

    def test_a_settled_sale_keeps_its_state(self):
        self._start_sale()
        self._poll(self.COMPLETE)
        self._poll({'status': 'ERROR'})
        self._poll({**self.COMPLETE, 'amountInCents': 99999, 'rail': 'bitcoin'})
        sale = self._sale()
        self.assertEqual((sale.state, sale.amount_minor, sale.rail), ('done', 1250, 'card'))

    def test_a_failed_read_leaves_the_sale_pending(self):
        self._start_sale()
        self._poll(self.COMPLETE, status_code=502)
        self.assertEqual(self._sale().state, 'pending')

    def test_another_method_cannot_settle_the_sale(self):
        self._start_sale()
        self._poll(self.COMPLETE, method=create_musqet_method(self, 'MSQ-REC-002'))
        self.assertEqual(self._sale().state, 'pending')

    def test_logs_a_sale_musqet_accepted_but_odoo_could_not_record(self):
        record_sale(self.env, self.method, 'sale-1')
        logger = 'odoo.addons.pos_musqet.models.pos_payment_method'
        with self.assertLogs(logger, 'ERROR') as logs, mute_logger('odoo.sql_db'):
            result = self._start_sale()
        # The POS still gets the saleId, so it polls the sale.
        self.assertEqual(result, {'saleId': 'sale-1', 'status': 'PENDING'})
        self.assertEqual(len(self._sale()), 1)
        self.assertIn('sale-1', logs.output[0])
        self.assertIn('Order-1', logs.output[0])
        self.assertIn('1250', logs.output[0])

    def test_cashier_cannot_read_or_write_sale_records(self):
        self._start_sale()
        Sale = self.env['pos.musqet.sale'].with_user(self.cashier)
        with self.assertRaises(AccessError):
            Sale.search([])
        with self.assertRaises(AccessError):
            Sale.create({
                'company_id': self.method.company_id.id, 'payment_method_id': self.method.id,
                'sale_id': 'forged', 'amount_minor': 1000000, 'currency': self.currency,
                'rail': 'card', 'state': 'done',
            })


@tagged('post_install', '-at_install')
class TestMusqetWebhookRoute(HttpCase):
    """A signed webhook settles the server's record of the sale, as a status read does."""

    SECRET = 'whsec_route_0123456789'

    def setUp(self):
        super().setUp()
        self.method = create_musqet_method(self, 'MSQ-WHR-001', musqet_webhook_secret=self.SECRET)
        self.sale = record_sale(self.env, self.method, 'sale-wh', state='pending', rail=False)

    def _post(self, body, secret=None):
        raw = json.dumps(body).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new((secret or self.SECRET).encode(), timestamp.encode() + b'.' + raw,
                          hashlib.sha256).hexdigest()
        self.url_open('/musqet/webhook', data=raw, headers={
            'Content-Type': 'application/json',
            'x-webhook-signature': 't=%s,v1=%s' % (timestamp, digest),
        })
        self.sale.invalidate_recordset()

    def _body(self):
        return {'data': {
            'saleId': 'sale-wh', 'status': 'COMPLETE', 'amountInCents': 1000,
            'currency': self.sale.currency, 'rail': 'card', 'serial': 'MSQ-WHR-001',
        }}

    def test_a_signed_webhook_settles_the_sale(self):
        self._post(self._body())
        self.assertEqual((self.sale.state, self.sale.rail), ('done', 'card'))

    def test_a_signed_webhook_settles_a_refund(self):
        refund = record_refund(self.env, self.method, 'refund-wh')
        body = self._body()
        body['data']['saleId'] = 'refund-wh'
        self._post(body)
        refund.invalidate_recordset()
        self.assertEqual(refund.state, 'done')

    def test_a_non_string_sale_id_changes_nothing(self):
        sale = record_sale(self.env, self.method, '123', state='pending')
        body = self._body()
        body['data']['saleId'] = 123
        self._post(body)
        sale.invalidate_recordset()
        self.assertEqual(sale.state, 'pending')

    def test_an_unsigned_webhook_changes_nothing(self):
        self._post(self._body(), secret='whsec_wrong')
        self.assertEqual(self.sale.state, 'pending')


@tagged('post_install', '-at_install')
class TestMusqetCreateRefund(TransactionCase):
    """musqet_create_refund is the server-side authority for a refund.

    The Musqet API enforces neither rail correctness nor over-refund (Musqet/musqet#2094), so
    this method must: re-read the (untrusted) original payment by id, refuse anything that
    isn't a card-settled Musqet sale of this company, in the company currency, whose captured
    amount covers the request, set type, mode and serial on the server, require the company
    currency, and require a valid reference, so a tampered payload can't widen the action. The
    original pos.payment is stubbed (TestMusqetRefundWithPosRecords uses real ones) so these
    tests exercise the authorization logic directly. The server's record of the original sale
    (pos.musqet.sale) is real.
    """

    def setUp(self):
        super().setUp()
        self.cashier = make_pos_user(self.env, 'musqet_cashier_refund')
        self.method = create_musqet_method(self, 'MSQ-RF-001')
        self.currency = self.method.company_id.currency_id.name
        self.other_currency = 'EUR' if self.currency != 'EUR' else 'USD'

    def _stub_original(self, rail='card', amount=10.0, terminal='musqet', company=None,
                       currency=None, state='done'):
        """A stubbed original payment of saleId 'sale-orig-1', and the real record of that sale.

        ``rail``, ``amount``, ``currency`` and ``state`` describe the sale record, which is all
        the refund may trust. The payment's own amount, rail and currency are deliberately
        wrong, so every test also shows they are not read.
        """
        vals = {
            'amount_minor': round(amount * 100),
            'rail': rail,
            'currency': currency or self.currency,
            'state': state,
        }
        sale = self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-1')])
        if sale:
            sale.write(vals)
        else:
            record_sale(self.env, self.method, 'sale-orig-1', **vals)
        original = MagicMock()
        original.exists.return_value = original
        original.company_id = company if company is not None else self.method.company_id
        original.payment_method_id.use_payment_terminal = terminal
        original.payment_method_id.id = self.method.id
        original.musqet_settle_check = 'verified'
        original.pos_order_id.state = 'paid'
        original.musqet_rail = 'card'
        original.amount = 1000000.0
        original.currency_id.decimal_places = 2
        original.currency_id.name = self.other_currency
        # A real string saleId: the sale and refund records are real and keyed on it. Only
        # the synced-payment search is stubbed, in _call_refund (see prior_refunds).
        original.transaction_id = 'sale-orig-1'
        return original

    def _stub_prior_refund(self, amount, reference='OTHER-ORDER', pos_reference=None, uuid=None):
        # A previously-settled refund of the original. Refund payment lines carry a negative
        # amount in a real order, which the cumulative cap takes the magnitude of. By default
        # both order keys carry `reference`; pass pos_reference/uuid explicitly to exercise a
        # single arm of the dedup's `reference in (pos_reference, uuid)` membership check.
        refund = MagicMock()
        refund.amount = amount
        refund.currency_id.decimal_places = 2
        # Taken on this method, so a record of the same saleId here is its record.
        refund.payment_method_id.id = self.method.id
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
                # The full partner shape of a 10.00 card sale (or refund): settling a pending
                # original from this read must leave it refundable.
                response.json.return_value = {
                    'saleId': url.rsplit('/', 1)[1], 'status': get_status, 'rail': 'card',
                    'amountInCents': 1000, 'currency': self.currency}
            else:
                # Anything still unwritten now could fail after the refund is sent.
                self.dirty_at_post = dirty_field_names(self.env)
                response.status_code = post_status
                response.json.return_value = {'saleId': sale_id, 'status': 'PENDING'}
            return response

        # Stub the search for synced refund payments of the original (the musqet_refund_of
        # domain) so that branch of the cap runs without a populated POS database; default to
        # none. Every other payment search finds nothing.
        Payment = self.env['pos.payment']

        def fake_search(domain, *args, **kwargs):
            if any(term[0] == 'musqet_refund_of' for term in domain if isinstance(term, tuple)):
                return prior_refunds or []
            return Payment.browse()

        with patch.object(type(Payment), 'browse', return_value=original), \
             patch.object(type(Payment), 'search', side_effect=fake_search), \
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
        original = self._stub_original(amount=10.0, currency=self.other_currency)
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
        for status, state in (('PROCESSING', 'pending'), ('COMPLETE', 'done')):
            with self.subTest(status=status):
                fake = musqet_response('refund-1', status)
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

    def test_refuses_a_sale_musqet_has_not_confirmed(self):
        # No record, a failed one, or one Musqet still reports in progress: nothing to refund.
        for state, get_status in ((None, None), ('failed', None), ('pending', 'PROCESSING')):
            with self.subTest(state=state):
                original = self._stub_original(amount=10.0, state=state or 'done')
                if state is None:
                    self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-1')]).unlink()
                result, mock_request = self._call_refund(
                    {'amountInCents': 500}, original, get_status=get_status)
                self.assertIn('has not confirmed', result['error']['message'])
                self.assertFalse(self._posts(mock_request))

    def test_refuses_a_line_its_sale_does_not_vouch_for(self):
        # A line flagged Not verified (say it copies a real sale's saleId), a refund line, or
        # a line of an order never paid: the sale record is fine, the line is not.
        cases = (('musqet_settle_check', 'unverified'), ('amount', -10.0), ('amount', 0.0))
        for field, value in cases + (('state', 'draft'), ('state', 'cancel')):
            with self.subTest(field=field, value=value):
                original = self._stub_original(amount=10.0)
                setattr(original.pos_order_id if field == 'state' else original, field, value)
                result, mock_request = self._call_refund({'amountInCents': 500}, original)
                self.assertIn('has not confirmed', result['error']['message'])
                mock_request.assert_not_called()

    def test_refunds_a_line_synced_before_the_check_existed(self):
        original = self._stub_original(amount=10.0)
        original.musqet_settle_check = False
        result, _ = self._call_refund({'amountInCents': 500}, original)
        self.assertNotIn('error', result)

    def test_reads_a_pending_sale_from_musqet_before_refunding_it(self):
        # The POS closed before the sale's result was read: Musqet's word settles it now.
        original = self._stub_original(amount=10.0, state='pending')
        result, mock_request = self._call_refund(
            {'amountInCents': 500}, original, get_status='COMPLETE')
        self.assertNotIn('error', result)
        self.assertEqual(
            [call.args[1] for call in self._gets(mock_request)],
            ['https://api.musqet.tech/api/v1/terminal/sales/sale-orig-1'])
        self.assertEqual(len(self._posts(mock_request)), 1)

    def test_rejects_refund_of_a_sale_with_no_known_rail(self):
        result, mock_request = self._call_refund(
            {'amountInCents': 500}, self._stub_original(rail=False))
        self.assertIn('Only card payments', result['error']['message'])
        mock_request.assert_not_called()

    def test_ignores_a_record_of_the_sale_on_another_method(self):
        original = self._stub_original(amount=10.0)
        self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-1')]).payment_method_id = \
            create_musqet_method(self, 'MSQ-RF-002')
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('has not confirmed', result['error']['message'])
        mock_request.assert_not_called()

    def test_ignores_a_record_of_the_sale_in_another_company(self):
        original = self._stub_original(amount=10.0)
        other = self.env['res.company'].create({'name': 'Other Co'})
        self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-1')]).company_id = other
        result, mock_request = self._call_refund({'amountInCents': 500}, original)
        self.assertIn('has not confirmed', result['error']['message'])
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
        self.second = create_musqet_method(self, 'MSQ-LINK-002')
        config = self.env['pos.config'].create({
            'name': 'Musqet Link POS',
            'payment_method_ids': [(6, 0, (self.method | self.plain | self.second).ids)]})
        self.session = self.env['pos.session'].create({'config_id': config.id, 'user_id': self.env.uid})
        self.order = self._order()
        record_refund(self.env, self.method, 'refund-1', amount_minor=500)

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

    def _original(self, sale_id, amount_minor=1000):
        """A synced card sale payment of 10.00 and the server's done record of that sale."""
        record_sale(self.env, self.method, sale_id, amount_minor=amount_minor)
        return self._payment(amount=10.0, transaction_id=sale_id, musqet_rail='card')

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

    def test_refund_claims_the_original_sale_record(self):
        # A concurrent refund of the same sale whose snapshot predates this one's record must
        # fail on this row and be retried, so the refund must update the row. Run it in a
        # savepoint: an update there gives the row a new version (xmin) from the savepoint's
        # own transaction id, while a mere read leaves it as created.
        original = self._original('sale-orig-real')
        sale = self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-real')])
        fake = musqet_response('refund-real')
        before = self._row_version(sale)
        with self.env.cr.savepoint(), \
             patch.object(pos_payment_method.requests, 'request', return_value=fake):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-REAL'}, original.id)
        self.assertNotIn('error', result)
        self.assertNotEqual(self._row_version(sale), before)
        refund = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-real')])
        self.assertEqual(refund.original_sale_id, 'sale-orig-real')

    def test_refuses_a_refund_by_the_other_form_of_its_order_reference(self):
        # A backfilled record holds one form of the reference; the refund's synced payment
        # links it to its order, whose other form must be caught too. Real rows, so the
        # payment search's filter is exercised.
        original = self._original('sale-orig-ref')
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
        fake = musqet_response('refund-new')
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': self.order.uuid}, original.id)
        self.assertNotIn('error', result)

    def test_a_payment_of_another_method_sharing_a_refund_sale_id_is_not_that_refund(self):
        # A payment line on another order, of another method, that happens to carry a
        # recorded refund's saleId must not make that order's refund a duplicate.
        original = self._original('sale-orig-m')
        record_refund(self.env, self.method, 'refund-m', original_sale_id='sale-orig-m', state='done')
        other_order = self._order()
        self._payment(pos_order_id=other_order.id, payment_method_id=self.plain.id,
                      transaction_id='refund-m')
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-new')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 200, 'reference': other_order.uuid}, original.id)
        self.assertNotIn('error', result)

    def test_a_synced_refund_of_another_method_counts_beside_a_record_sharing_its_sale_id(self):
        # The record (this method, saleId X) is a different refund from a synced payment of
        # another Musqet method that also carries saleId X: both count.
        original = self._original('sale-orig-x')
        record_refund(self.env, self.method, 'refund-x', original_sale_id='sale-orig-x',
                      amount_minor=600, state='done')
        self._payment(payment_method_id=self.second.id, transaction_id='refund-x',
                      musqet_refund_of='sale-orig-x', amount=-6.0)
        with patch.object(pos_payment_method.requests, 'request') as mock_request:
            result = self.method.musqet_create_refund(
                {'amountInCents': 1, 'reference': 'R-X'}, original.id)
        self.assertIn('exceeds', result['error']['message'])
        mock_request.assert_not_called()

    def test_a_non_musqet_payment_claiming_to_refund_the_sale_does_not_count(self):
        original = self._original('sale-orig-c')
        self._payment(payment_method_id=self.plain.id, transaction_id='cash-1',
                      musqet_refund_of='sale-orig-c', amount=-10.0)
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-c')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 1000, 'reference': 'R-C'}, original.id)
        self.assertNotIn('error', result)

    def test_a_musqet_payment_without_a_sale_id_does_not_count(self):
        # A settled Musqet refund always has a saleId, so a line without one isn't a refund.
        original = self._original('sale-orig-n')
        self._payment(musqet_refund_of='sale-orig-n', amount=-10.0)
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-n')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 1000, 'reference': 'R-N'}, original.id)
        self.assertNotIn('error', result)

    def test_neither_line_of_a_copied_sale_can_be_refunded(self):
        # Two paid lines claiming one sale (a duplicated sync, or a copy): both are Not
        # verified, so a refund naming either is refused until the extra line is gone.
        genuine = self._original('sale-orig-dup')
        twin = self._payment(pos_order_id=self._order().id, amount=10.0,
                             transaction_id='sale-orig-dup')
        for line in (genuine, twin):
            with self.subTest(line=line.id), \
                 patch.object(pos_payment_method.requests, 'request') as mock_request:
                result = self.method.musqet_create_refund(
                    {'amountInCents': 500, 'reference': 'R-DUP'}, line.id)
                self.assertIn('has not confirmed', result['error']['message'])
                mock_request.assert_not_called()
        twin.unlink()
        with patch.object(pos_payment_method.requests, 'request',
                          return_value=musqet_response('refund-dup')):
            result = self.method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'R-DUP'}, genuine.id)
        self.assertNotIn('error', result)

    def test_backfill_does_nothing_on_a_fresh_install(self):
        self._payment(transaction_id='refund-legacy', musqet_refund_of='sale-orig-1')
        load_migration('19.0.1.2.0', 'post-migration').migrate(self.env.cr, None)
        self.assertFalse(self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-legacy')]))

    def test_manager_sees_only_their_companies_refunds(self):
        other = self.env['res.company'].create({'name': 'Other Co'})
        plain_other = self.env['pos.payment.method'].create({'name': 'Other Method', 'company_id': other.id})
        record_refund(self.env, plain_other, 'refund-other', original_sale_id='sale-other')
        manager = make_pos_user(self.env, 'musqet_manager', group='point_of_sale.group_pos_manager', company_id=self.method.company_id.id,
            company_ids=[(6, 0, self.method.company_id.ids)])
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

    # -- Settle check on synced payments (musqet_settle_check) --------------------

    def test_a_sale_line_musqet_confirmed_is_verified(self):
        record_sale(self.env, self.method, 'sale-ok', amount_minor=1000)
        payment = self._payment(amount=10.0, transaction_id='sale-ok')
        self.assertEqual(payment.musqet_settle_check, 'verified')

    def test_a_sale_line_without_a_matching_settle_is_not_verified(self):
        record_sale(self.env, self.method, 'sale-pending', state='pending')
        record_sale(self.env, self.method, 'sale-999', amount_minor=999)
        record_sale(self.env, self.method, 'sale-eur', currency='XTS')
        record_sale(self.env, self.second, 'sale-second')
        for transaction_id in (False, 'sale-unknown', 'sale-pending', 'sale-999', 'sale-eur',
                               'sale-second'):
            with self.subTest(transaction_id=transaction_id):
                payment = self._payment(amount=10.0, transaction_id=transaction_id)
                self.assertEqual(payment.musqet_settle_check, 'unverified')

    def test_a_line_of_another_method_is_not_checked(self):
        payment = self._payment(payment_method_id=self.plain.id, amount=10.0,
                                musqet_settle_check='verified')
        self.assertFalse(payment.musqet_settle_check)

    def test_a_line_is_verified_once_musqet_reports_it_complete(self):
        # A sale and a refund (refund-1, pending, from setUp) both settle from a status read.
        record_sale(self.env, self.method, 'sale-late', state='pending')
        sale_line = self._payment(amount=10.0, transaction_id='sale-late')
        refund_line = self._payment(transaction_id='refund-1')
        self.assertEqual(sale_line.musqet_settle_check, 'unverified')
        self.assertEqual(refund_line.musqet_settle_check, 'unverified')
        for sale_id in ('sale-late', 'refund-1'):
            fake = musqet_response(sale_id, 'COMPLETE', amountInCents=1000, rail='card',
                                   currency=self.method.company_id.currency_id.name)
            with patch.object(pos_payment_method.requests, 'request', return_value=fake):
                self.method.musqet_get_sale(sale_id)
        self.assertEqual(sale_line.musqet_settle_check, 'verified')
        self.assertEqual(refund_line.musqet_settle_check, 'verified')

    def test_a_refund_line_is_checked_against_its_refund_record(self):
        record_refund(self.env, self.method, 'refund-ok', amount_minor=500, state='done')
        record_refund(self.env, self.method, 'refund-600', amount_minor=600, state='done')
        self.assertEqual(self._payment(transaction_id='refund-ok').musqet_settle_check, 'verified')
        self.assertEqual(
            self._payment(transaction_id='refund-600').musqet_settle_check, 'unverified')

    def test_a_written_settle_check_is_put_right(self):
        payment = self._payment(amount=10.0, transaction_id='sale-unknown',
                                musqet_settle_check='verified')
        self.assertEqual(payment.musqet_settle_check, 'unverified')
        # As over RPC: a real user, not the superuser.
        manager = make_pos_user(self.env, 'musqet_manager_flag', group='point_of_sale.group_pos_manager')
        payment.with_user(manager).write({'musqet_settle_check': 'verified'})
        self.assertEqual(payment.musqet_settle_check, 'unverified')
        # A server path writing POS values under sudo doesn't keep it either.
        payment.sudo().write({'musqet_settle_check': 'verified'})
        self.assertEqual(payment.musqet_settle_check, 'unverified')

    def test_a_copied_sale_id_leaves_every_line_unverified(self):
        # One Musqet sale vouches for one paid line: a second line claiming it, on another
        # order, makes both Not verified until it is gone.
        genuine = self._original('sale-copied')
        self.assertEqual(genuine.musqet_settle_check, 'verified')
        copy = self._payment(pos_order_id=self._order().id, amount=10.0, transaction_id='sale-copied')
        self.assertEqual((genuine.musqet_settle_check, copy.musqet_settle_check),
                         ('unverified', 'unverified'))
        copy.transaction_id = 'sale-other'
        self.assertEqual(genuine.musqet_settle_check, 'verified')
        copy.transaction_id = 'sale-copied'
        self.assertEqual(genuine.musqet_settle_check, 'unverified')
        copy.unlink()
        self.assertEqual(genuine.musqet_settle_check, 'verified')

    def test_lines_of_an_unpaid_order_are_not_checked(self):
        draft = self._order(state='draft')
        with self.assertNoLogs('odoo.addons.pos_musqet.models.pos_payment', 'WARNING'):
            payment = self._payment(pos_order_id=draft.id, amount=10.0, transaction_id='sale-wip')
        self.assertFalse(payment.musqet_settle_check)
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment', 'WARNING'):
            draft.state = 'paid'
        self.assertEqual(payment.musqet_settle_check, 'unverified')
        cancelled = self._payment(pos_order_id=self._order(state='cancel').id, amount=10.0,
                                  transaction_id='sale-cancelled')
        self.assertFalse(cancelled.musqet_settle_check)
        # Moved to a paid order: checked; back to an unpaid one: cleared.
        cancelled.pos_order_id = self.order
        self.assertEqual(cancelled.musqet_settle_check, 'unverified')
        cancelled.pos_order_id = self._order(state='draft')
        self.assertFalse(cancelled.musqet_settle_check)

    def test_a_draft_line_does_not_claim_the_sale(self):
        genuine = self._original('sale-draft-copy')
        self._payment(pos_order_id=self._order(state='draft').id, amount=10.0,
                      transaction_id='sale-draft-copy')
        self.assertEqual(genuine.musqet_settle_check, 'verified')

    def test_editing_a_verified_line_checks_it_again(self):
        record_sale(self.env, self.method, 'sale-edit', amount_minor=1000)
        payment = self._payment(amount=10.0, transaction_id='sale-edit')
        payment.amount = 20.0
        self.assertEqual(payment.musqet_settle_check, 'unverified')

    def test_logs_a_line_that_is_not_verified(self):
        with self.assertLogs('odoo.addons.pos_musqet.models.pos_payment', 'WARNING') as logs:
            payment = self._payment(amount=10.0, transaction_id='sale-forged')
        self.assertEqual(len(logs.output), 1)
        self.assertIn('sale-forged', logs.output[0])
        self.assertIn(str(payment.id), logs.output[0])

    # -- Sale records ----------------------------------------------------------

    def test_sale_backfill_records_synced_sales_once(self):
        legacy = self._payment(amount=12.5, transaction_id='sale-legacy', musqet_rail='card')
        self._payment(amount=3.0, transaction_id='sale-legacy-btc', musqet_rail='bitcoin')
        self._payment(amount=3.0, transaction_id='sale-legacy-odd', musqet_rail='paypal')
        self._payment(amount=-3.0, transaction_id='refund-legacy')  # a refund line
        self._payment(amount=3.0, transaction_id='refund-linked', musqet_refund_of='sale-x')
        self._payment(amount=3.0)  # no saleId
        self._payment(payment_method_id=self.plain.id, amount=3.0, transaction_id='cash-1')
        for state in ('draft', 'cancel'):  # never paid
            self._payment(pos_order_id=self._order(state=state).id, amount=3.0,
                          transaction_id='sale-%s' % state)
        self._original('sale-recorded')  # recorded already
        Sale = self.env['pos.musqet.sale']
        load_migration('19.0.1.3.0', 'post-migration').migrate(self.env.cr, '19.0.1.2.0')
        sales = Sale.search([])
        self.assertEqual(
            sorted(sales.mapped('sale_id')),
            ['sale-legacy', 'sale-legacy-btc', 'sale-legacy-odd', 'sale-recorded'])
        Sale._musqet_record_synced_sales()  # running again adds nothing
        self.assertEqual(Sale.search([]), sales)
        rails = {sale.sale_id: sale.rail for sale in sales}
        self.assertEqual(rails['sale-legacy-btc'], 'bitcoin')
        self.assertFalse(rails['sale-legacy-odd'])
        backfilled = sales.filtered(lambda sale: sale.sale_id == 'sale-legacy')
        self.assertEqual(backfilled.amount_minor, 1250)
        self.assertEqual(backfilled.currency, legacy.currency_id.name)
        self.assertEqual(backfilled.rail, 'card')
        self.assertEqual(backfilled.state, 'done')
        self.assertEqual(backfilled.payment_method_id, self.method)
        self.assertEqual(backfilled.company_id, self.method.company_id)
        self.assertEqual(backfilled.reference, legacy.pos_order_id.pos_reference or legacy.pos_order_id.uuid)
        self.assertTrue(backfilled.reference)

    def test_sale_backfill_does_nothing_on_a_fresh_install(self):
        self._payment(amount=10.0, transaction_id='sale-legacy', musqet_rail='card')
        load_migration('19.0.1.3.0', 'post-migration').migrate(self.env.cr, None)
        self.assertFalse(self.env['pos.musqet.sale'].search([]))

    def test_manager_sees_only_their_companies_sales(self):
        other = self.env['res.company'].create({'name': 'Other Co'})
        plain_other = self.env['pos.payment.method'].create({'name': 'Other Method', 'company_id': other.id})
        record_sale(self.env, plain_other, 'sale-other')
        record_sale(self.env, self.method, 'sale-mine')
        manager = make_pos_user(
            self.env, 'musqet_manager_sales', group='point_of_sale.group_pos_manager',
            company_id=self.method.company_id.id, company_ids=[(6, 0, self.method.company_id.ids)])
        seen = self.env['pos.musqet.sale'].with_user(manager).search([])
        self.assertEqual(seen.mapped('sale_id'), ['sale-mine'])

    def test_manager_can_mark_a_stuck_pending_refund_failed(self):
        manager = make_pos_user(self.env, 'musqet_manager_fail', group='point_of_sale.group_pos_manager')
        stuck = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        done = record_refund(self.env, self.method, 'refund-done', state='done')
        (stuck | done).with_user(manager).action_musqet_mark_failed()
        self.assertEqual(stuck.state, 'failed')
        self.assertEqual(done.state, 'done')

    def test_cashier_cannot_mark_a_refund_failed_with_its_state_cached(self):
        cashier = make_pos_user(self.env, 'musqet_cashier_cached')
        stuck = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        self.assertEqual(stuck.state, 'pending')  # now in the cache
        with self.assertRaises(AccessError):
            stuck.with_user(cashier).action_musqet_mark_failed()
        self.assertEqual(stuck.state, 'pending')

    def test_cashier_cannot_mark_a_refund_failed(self):
        cashier = make_pos_user(self.env, 'musqet_cashier_fail')
        stuck = self.env['pos.musqet.refund'].search([('sale_id', '=', 'refund-1')])
        # As over RPC: nothing cached, so the explicit check is what's tested.
        stuck.invalidate_recordset()
        with self.assertRaises(AccessError):
            stuck.with_user(cashier).action_musqet_mark_failed()
        self.assertEqual(stuck.state, 'pending')


@tagged('post_install', '-at_install')
class TestMusqetMinorUnits(TransactionCase):
    """minor_units converts an amount to whole minor units of its currency."""

    def test_converts_by_the_currency_decimal_places(self):
        for decimals, amount, expected in ((2, 12.34, 1234), (0, 1500.0, 1500), (3, 1.5, 1500),
                                          (None, 12.34, 1234)):
            with self.subTest(decimals=decimals):
                currency = MagicMock(decimal_places=decimals)
                self.assertEqual(minor_units(amount, currency), expected)
