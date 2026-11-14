# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Who may drive the Musqet terminal through the server proxy."""
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.exceptions import AccessDenied, MissingError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .common import (
    clear_company,
    create_musqet_method,
    list_method_on_config,
    make_pos_user,
    open_plain_method,
    open_session,
    write_between_sessions,
)


def proxy_calls(method, names=None):
    """The proxy entry points a cashier can reach over RPC, on ``method`` (only ``names``, if
    given), each with a request it would otherwise send."""
    calls = {
        'musqet_create_sale': lambda: method.musqet_create_sale(
            {'amountInCents': 500, 'reference': 'Order-S'}),
        'musqet_create_refund': lambda: method.musqet_create_refund(
            {'amountInCents': 500, 'reference': 'Refund-S'}, 1),
        'musqet_get_sale': lambda: method.musqet_get_sale('sale-1'),
        'musqet_cancel_sale': lambda: method.musqet_cancel_sale('sale-1'),
        'musqet_get_latest_status': lambda: method.musqet_get_latest_status(),
    }
    return {name: call for name, call in calls.items() if names is None or name in names}


class ProxyCallsMixin:
    """Run checks on each proxy entry point, in its own subTest, with the Musqet API stubbed."""

    def for_each_proxy_call(self, method, check, names=None):
        """Call ``check(call, mock_request)`` for each of proxy_calls(method, names)."""
        for name, call in proxy_calls(method, names).items():
            with self.subTest(method=name), \
                 patch.object(pos_payment_method.requests, 'request') as mock_request:
                check(call, mock_request)

    def assertProxyDenied(self, method, names=None):
        """Each entry point raises AccessDenied on ``method`` before any request goes out."""
        def check(call, mock_request):
            with self.assertRaises(AccessDenied):
                call()
            mock_request.assert_not_called()
        self.for_each_proxy_call(method, check, names)


@tagged('post_install', '-at_install')
class TestMusqetProxyAccess(ProxyCallsMixin, TransactionCase):
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
        # out for it. On an open session, so the method check is what refuses it.
        plain = open_plain_method(self.env)
        self.assertProxyDenied(
            plain.with_user(self.cashier), names=('musqet_get_sale', 'musqet_cancel_sale'))


@tagged('post_install', '-at_install')
class TestMusqetProxyScope(ProxyCallsMixin, TransactionCase):
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

    def assertDenied(self, method):
        """Every entry point refuses ``method`` to the cashier: no request goes out, and no
        buffer is read."""
        if method.exists():
            method.musqet_latest_response = self.BUFFERED
        self.assertProxyDenied(method.with_user(self.cashier))
        if method.exists():
            self.assertEqual(method.musqet_latest_response, self.BUFFERED)

    def _missing_method(self):
        """A payment method id no record has."""
        Method = self.env['pos.payment.method'].with_context(active_test=False)
        return Method.browse(Method.search([], order='id desc', limit=1).id + 1000)

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

    def test_a_till_being_closed_polls_and_cancels_but_starts_nothing(self):
        # A sale still in progress as the session closes must still be read or cancelled.
        self.session.state = 'closing_control'
        method = self.method.with_user(self.cashier)

        def refused(call, mock_request):
            self.assertIn('No POS session using this Musqet terminal is open',
                          call()['error']['message'])
            mock_request.assert_not_called()

        def allowed(call, mock_request):
            mock_request.return_value.status_code = 200
            mock_request.return_value.json.return_value = {'saleId': 'sale-1', 'status': 'PENDING'}
            call()
            mock_request.assert_called_once()

        self.for_each_proxy_call(
            method, refused, names=('musqet_create_sale', 'musqet_create_refund'))
        self.for_each_proxy_call(method, allowed, names=('musqet_get_sale', 'musqet_cancel_sale'))
        self.method.musqet_latest_response = self.BUFFERED
        self.assertTrue(method.musqet_get_latest_status())

    def test_allows_a_method_whose_session_is_still_opening(self):
        # The POS opens on the till before the opening cash count is confirmed.
        self.session.state = 'opening_control'
        self.method.musqet_latest_response = self.BUFFERED
        self.assertTrue(self.method.with_user(self.cashier).musqet_get_latest_status())

    def test_denies_another_companys_method_on_its_own_session(self):
        other = self._other_company_method('MSQ-SCOPE-B02')
        self._open_session_in(self.other_company, other)
        self.assertDenied(other)

    def test_denies_a_missing_method_as_it_denies_another_companys(self):
        # The same AccessDenied, so a refusal says nothing about which ids exist.
        missing = self._missing_method()
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

    def test_another_open_till_on_the_method_still_starts_sales(self):
        # Checked per method: the server can't tell which session the caller is on.
        self.session.state = 'closing_control'
        open_session(self.env, self.method)

        def sent(call, mock_request):
            mock_request.return_value.status_code = 200
            mock_request.return_value.json.return_value = {'saleId': 'sale-2', 'status': 'PENDING'}
            self.assertNotIn('error', call())
            mock_request.assert_called_once()

        self.for_each_proxy_call(
            self.method.with_user(self.cashier), sent, names=('musqet_create_sale',))

    def test_another_companys_till_listing_the_method_does_not_count(self):
        # Listed there by a direct database write: Odoo and _check_musqet_company refuse it.
        self.session.state = 'closed'
        other_session = self._open_session_in(
            self.other_company, self._other_company_method('MSQ-SCOPE-B04'))
        list_method_on_config(self.method, other_session.config_id)
        self.assertDenied(self.method)

    def test_an_internal_call_on_a_missing_method_fails_at_the_check(self):
        missing = self._missing_method()
        with self.assertRaises(MissingError):
            missing.sudo()._musqet_check_access()


@tagged('post_install', '-at_install')
class TestMusqetMethodCompany(ProxyCallsMixin, TransactionCase):
    """A Musqet method acts for one company, so it must name one."""

    def test_a_musqet_method_needs_a_company(self):
        with self.assertRaises(ValidationError):
            create_musqet_method(self, 'MSQ-NOCO-001', company_id=False)
        method = create_musqet_method(self, 'MSQ-NOCO-002')
        with self.assertRaises(ValidationError):
            method.company_id = False

    def test_a_musqet_method_cannot_take_a_company_another_companys_pos_lists_it_on(self):
        # Shared before the rule: the manager must take it off the other company's POS first.
        other_company = self.env['res.company'].create({'name': 'Other Rule Co'})
        method = create_musqet_method(self, 'MSQ-NOCO-005')
        other_session = open_session(
            self.env(context={'allowed_company_ids': other_company.ids}),
            create_musqet_method(self, 'MSQ-NOCO-006', company_id=other_company.id))
        list_method_on_config(method, other_session.config_id)
        clear_company(method)
        with self.assertRaisesRegex(ValidationError, 'another company'):
            write_between_sessions(method, {'company_id': self.env.company.id})

    def test_an_archived_pos_of_another_company_counts_too(self):
        # It may be restored, and the upgrade counts it.
        other_company = self.env['res.company'].create({'name': 'Other Archive Co'})
        env_other = self.env(context={'allowed_company_ids': other_company.ids})
        archived = env_other['pos.config'].create(
            {'name': 'Archived POS', 'payment_method_ids': [(6, 0, [])], 'active': False})
        method = create_musqet_method(self, 'MSQ-NOCO-007')
        list_method_on_config(method, archived)
        clear_company(method)
        with self.assertRaisesRegex(ValidationError, 'another company'):
            method.company_id = self.env.company

    def test_other_methods_may_have_none(self):
        plain = self.env['pos.payment.method'].create({'name': 'Plain Method', 'company_id': False})
        self.assertFalse(plain.company_id)

    def test_a_musqet_method_saved_without_one_starts_nothing(self):
        # Saved before the rule, or by a direct database write: even an internal call can't
        # start a sale or refund for a company the method doesn't name.
        method = create_musqet_method(self, 'MSQ-NOCO-003')
        clear_company(method)

        def check(call, mock_request):
            self.assertIn('no company', call()['error']['message'])
            mock_request.assert_not_called()

        self.for_each_proxy_call(
            method, check, names=('musqet_create_sale', 'musqet_create_refund'))
        domain = [('payment_method_id', '=', method.id)]
        self.assertFalse(self.env['pos.musqet.sale'].search(domain))
        self.assertFalse(self.env['pos.musqet.refund'].search(domain))

    def test_a_cashier_gets_the_same_refusal_for_it_as_for_any_method_not_theirs(self):
        # A method with no company is no cashier's: the same AccessDenied, not the message
        # above, so a refusal still says nothing about which ids exist.
        method = create_musqet_method(self, 'MSQ-NOCO-004')
        open_session(self.env, method)
        clear_company(method)
        self.assertProxyDenied(method.with_user(make_pos_user(self.env, 'musqet_cashier_noco')))
