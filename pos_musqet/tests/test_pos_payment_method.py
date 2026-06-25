# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import ValidationError
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
