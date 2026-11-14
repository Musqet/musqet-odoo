# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Who may drive the Musqet terminal through the server proxy."""
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessDenied, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import SQL

from .common import create_musqet_method, make_pos_user, open_session


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
        open_session(self.env, self.method)

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
class TestMusqetProxyScope(TransactionCase):
    """A cashier may drive only a Musqet method of their own companies on an open POS session.

    The caller picks the method's id over RPC and the proxy reads its restricted fields with
    sudo(), which skips the multi-company record rule, so _musqet_check_access is the only
    thing between a cashier and another company's terminal, API key and webhook buffer.
    """

    BUFFERED = '{"saleId": "s-1", "status": "COMPLETE"}'

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.other_company = self.env['res.company'].create({'name': 'Other Musqet Co'})
        self.cashier = make_pos_user(
            self.env, 'musqet_cashier_scope', company_id=self.company.id,
            company_ids=[(6, 0, self.company.ids)])
        self.method = create_musqet_method(self, 'MSQ-SCOPE-001')
        self.session = open_session(self.env, self.method)

    def _calls(self, method):
        """Every proxy entry point a cashier can reach over RPC, on ``method``."""
        method = method.with_user(self.cashier)
        return {
            'musqet_create_sale': lambda: method.musqet_create_sale(
                {'amountInCents': 500, 'reference': 'Order-S'}),
            'musqet_create_refund': lambda: method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'Refund-S'}, 1),
            'musqet_get_sale': lambda: method.musqet_get_sale('sale-1'),
            'musqet_cancel_sale': lambda: method.musqet_cancel_sale('sale-1'),
            'musqet_get_latest_status': lambda: method.musqet_get_latest_status(),
        }

    def assertDenied(self, method):
        """Every entry point refuses ``method``: no request goes out, and no buffer is read."""
        if method.exists():
            method.musqet_latest_response = self.BUFFERED
        for name, call in self._calls(method).items():
            with self.subTest(method=name), \
                 patch.object(pos_payment_method.requests, 'request') as mock_request:
                with self.assertRaises(AccessDenied):
                    call()
                mock_request.assert_not_called()
        if method.exists():
            self.assertEqual(method.musqet_latest_response, self.BUFFERED)

    def _other_company_method(self, serial):
        return create_musqet_method(self, serial, company_id=self.other_company.id)

    def _open_session_in(self, company, *methods):
        # In the company's own journals: Odoo keeps a config to one company.
        return open_session(self.env(context={'allowed_company_ids': company.ids}), *methods)

    def test_allows_a_method_of_the_cashiers_company_on_an_open_session(self):
        self.method.musqet_latest_response = self.BUFFERED
        self.assertEqual(self.method.with_user(self.cashier).musqet_get_latest_status(),
                         {'saleId': 's-1', 'status': 'COMPLETE'})

    def test_denies_a_method_on_no_open_session(self):
        self.assertDenied(create_musqet_method(self, 'MSQ-SCOPE-002'))

    def test_denies_a_method_whose_session_is_closed(self):
        self.session.state = 'closed'
        self.assertDenied(self.method)

    def test_denies_another_companys_method_on_its_own_session(self):
        other = self._other_company_method('MSQ-SCOPE-B02')
        self._open_session_in(self.other_company, other)
        self.assertDenied(other)

    def test_denies_a_missing_method_as_it_denies_another_companys(self):
        # The same AccessDenied, so a refusal says nothing about which ids exist.
        missing = self.env['pos.payment.method'].browse(
            self.env['pos.payment.method'].search([], order='id desc', limit=1).id + 1000)
        self.assertDenied(missing)

    def test_a_user_of_both_companies_drives_the_other_only_while_it_is_enabled(self):
        other = self._other_company_method('MSQ-SCOPE-B03')
        self._open_session_in(self.other_company, other)
        other.musqet_latest_response = self.BUFFERED
        both = make_pos_user(
            self.env, 'musqet_cashier_both', company_id=self.company.id,
            company_ids=[(6, 0, (self.company | self.other_company).ids)])
        only_mine = other.with_user(both).with_context(allowed_company_ids=self.company.ids)
        with self.assertRaises(AccessDenied):
            only_mine.musqet_get_latest_status()
        self.assertEqual(other.with_user(both).musqet_get_latest_status(),
                         {'saleId': 's-1', 'status': 'COMPLETE'})


@tagged('post_install', '-at_install')
class TestMusqetMethodCompany(TransactionCase):
    """A Musqet method acts for one company, so it must name one."""

    def test_a_musqet_method_needs_a_company(self):
        with self.assertRaises(ValidationError):
            create_musqet_method(self, 'MSQ-NOCO-001', company_id=False)
        method = create_musqet_method(self, 'MSQ-NOCO-002')
        with self.assertRaises(ValidationError):
            method.company_id = False

    def test_other_methods_may_have_none(self):
        plain = self.env['pos.payment.method'].create({'name': 'Plain Method', 'company_id': False})
        self.assertFalse(plain.company_id)

    def test_a_musqet_method_saved_without_one_starts_nothing(self):
        # Saved before the rule, or by a direct database write: even an internal call can't
        # start a sale or refund for a company the method doesn't name.
        method = create_musqet_method(self, 'MSQ-NOCO-003')
        self.env.cr.execute(SQL(
            'UPDATE pos_payment_method SET company_id = NULL WHERE id = %s', method.id))
        method.invalidate_recordset(['company_id'])
        calls = {
            'musqet_create_sale': lambda: method.musqet_create_sale(
                {'amountInCents': 500, 'reference': 'Order-N'}),
            'musqet_create_refund': lambda: method.musqet_create_refund(
                {'amountInCents': 500, 'reference': 'Refund-N'}, 1),
        }
        for name, call in calls.items():
            with self.subTest(method=name), \
                 patch.object(pos_payment_method.requests, 'request') as mock_request:
                self.assertIn('no company', call()['error']['message'])
                mock_request.assert_not_called()
        self.assertFalse(
            self.env['pos.musqet.sale'].search([('payment_method_id', '=', method.id)]))
