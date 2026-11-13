# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Who may drive the Musqet terminal through the server proxy."""
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessDenied
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .common import create_musqet_method, make_pos_user


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
