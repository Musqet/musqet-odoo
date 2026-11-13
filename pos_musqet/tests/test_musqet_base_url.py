# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""The Musqet API base URL: validation and the per-request re-check."""
from unittest.mock import patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .common import create_musqet_method


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
