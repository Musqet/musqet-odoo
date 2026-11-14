# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Starting a sale and the server's record of each sale."""
import json
from unittest.mock import MagicMock, patch

from odoo.addons.pos_musqet.models import pos_payment_method
from odoo.addons.pos_musqet.models.musqet_common import MUSQET_FAILURE_STATUSES, minor_units
from odoo.addons.pos_musqet.models.pos_payment_method import (
    MUSQET_LANGUAGE_MAX_LENGTH,
    MUSQET_REFERENCE_MAX_LENGTH,
)
from odoo.exceptions import AccessDenied, AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from .common import (
    PROXY_LOGGER,
    RECORD_LOGGER,
    create_musqet_method,
    dirty_field_names,
    make_pos_user,
    musqet_response,
    open_plain_method,
    open_session,
    record_sale,
)


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
        open_session(self.env, self.method)
        self.currency = self.method.company_id.currency_id.name
        self.sales_sent = 0

    def _call_sale(self, payload):
        # A new saleId per call (sale-1, sale-2, ...), as Musqet gives: each sale is recorded.
        # A new order per call too: a second sale of one order would resume the first.
        self.sales_sent += 1
        if isinstance(payload, dict):
            payload = {'reference': 'Order-T%d' % self.sales_sent, **payload}
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
            result = self.method.with_user(self.cashier).musqet_create_sale(
                {'amountInCents': 500, 'reference': 'Order-T'})
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

    def test_rejects_a_sale_without_a_reference(self):
        result, mock_request = self._call_sale({'amountInCents': 500, 'reference': None})
        self.assertIn('reference', result['error']['message'])
        mock_request.assert_not_called()

    def test_rejects_an_invalid_reference(self):
        for reference in ({'nested': 'object'}, '', 'x' * (MUSQET_REFERENCE_MAX_LENGTH + 1), 42):
            with self.subTest(reference=reference):
                result, mock_request = self._call_sale({'amountInCents': 500, 'reference': reference})
                self.assertIn('error', result)
                mock_request.assert_not_called()

    def test_denies_a_non_musqet_payment_method(self):
        cash = open_plain_method(self.env)
        with self.assertRaises(AccessDenied):
            cash.with_user(self.cashier).musqet_create_sale({'amountInCents': 500})

    def test_rejects_when_no_serial_is_configured(self):
        self.method.write({'musqet_terminal_serial': False})
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
        open_session(self.env, self.method)
        self.currency = self.method.company_id.currency_id.name
        # A complete card sale of the requested amount, as Musqet reports it.
        self.complete = {'status': 'COMPLETE', 'amountInCents': 1250, 'currency': self.currency,
                         'rail': 'card'}

    def _start_sale(self, status_code=200, status='PENDING', reference='Order-1'):
        fake = musqet_response('sale-1', status, status_code=status_code)
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            return self.method.with_user(self.cashier).musqet_create_sale(
                {'amountInCents': 1250, 'reference': reference})

    def _poll(self, body, status_code=200, method=None, omit_sale_id=False):
        # ``body`` may name another saleId, as a reply about another sale would.
        body = {'saleId': 'sale-1', **body}
        fake = musqet_response(body.pop('saleId'), body.pop('status', None), status_code, **body)
        if omit_sale_id:
            del fake.json.return_value['saleId']
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            return (method or self.method).with_user(self.cashier).musqet_get_sale('sale-1')

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
        # Neither the record nor the till: the POS gets an error, not a COMPLETE to settle on.
        self._start_sale()
        with self.assertLogs(PROXY_LOGGER, 'WARNING'):
            result = self._poll({**self.complete, 'saleId': 'sale-2'})
        self.assertEqual(self._sale().state, 'pending')
        self.assertIn('different sale', result['error']['message'])

    def test_settles_from_a_status_read_whose_reply_has_no_sale_id(self):
        # The read is about sale-1 by its URL.
        self._start_sale()
        self._poll(self.complete, omit_sale_id=True)
        self.assertEqual(self._sale().state, 'done')

    def test_takes_the_rail_from_a_later_reply_if_the_first_had_none(self):
        self._start_sale()
        self._poll({**self.complete, 'rail': None})
        self.assertEqual((self._sale().state, self._sale().rail), ('done', False))
        self._poll(self.complete)
        self.assertEqual(self._sale().rail, 'card')

    def test_upper_cases_the_reported_currency(self):
        self._start_sale()
        self._poll({**self.complete, 'currency': 'xts'})
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
        for bad in ({'amountInCents': True}, {'amountInCents': None}, {'amountInCents': 2 ** 31},
                    {'currency': 'GBPX'}, {'currency': None}):
            with self.subTest(bad=bad), self.assertLogs(RECORD_LOGGER, 'WARNING'):
                self._poll({**self.complete, **bad})
                self.assertEqual(self._sale().state, 'pending')

    def test_ignores_an_unknown_rail(self):
        self._start_sale()
        self._poll({**self.complete, 'rail': 'paypal'})
        self.assertEqual((self._sale().state, self._sale().rail), ('done', False))

    def test_a_settled_sale_keeps_its_state(self):
        self._start_sale()
        self._poll(self.complete)
        self._poll({'status': 'ERROR'})
        self._poll({**self.complete, 'amountInCents': 99999, 'rail': 'bitcoin'})
        sale = self._sale()
        self.assertEqual((sale.state, sale.amount_minor, sale.rail), ('done', 1250, 'card'))

    def test_a_failed_read_leaves_the_sale_pending(self):
        self._start_sale()
        self._poll(self.complete, status_code=502)
        self.assertEqual(self._sale().state, 'pending')

    def test_another_method_cannot_settle_the_sale(self):
        self._start_sale()
        other = create_musqet_method(self, 'MSQ-REC-002')
        open_session(self.env, other)
        self._poll(self.complete, method=other)
        self.assertEqual(self._sale().state, 'pending')

    def test_logs_a_sale_musqet_accepted_but_odoo_could_not_record(self):
        record_sale(self.env, self.method, 'sale-1')
        with self.assertLogs(RECORD_LOGGER, 'ERROR') as logs, mute_logger('odoo.sql_db'):
            result = self._start_sale()
        # The POS still gets the saleId, so it polls the sale.
        self.assertEqual(result, {'saleId': 'sale-1', 'status': 'PENDING'})
        self.assertEqual(len(self._sale()), 1)
        self.assertIn('sale-1', logs.output[0])
        self.assertIn('Order-1', logs.output[0])
        self.assertIn('1250', logs.output[0])

    def test_logs_the_reference_escaped(self):
        # A reference from the till can't forge lines in the reconciliation log.
        record_sale(self.env, self.method, 'sale-1')
        with self.assertLogs(RECORD_LOGGER, 'ERROR') as logs, \
             mute_logger('odoo.sql_db'):
            self._start_sale(reference='Order-1\nFORGED LINE')
        self.assertNotIn('\n', logs.records[0].getMessage())
        self.assertIn('Order-1\\nFORGED LINE', logs.records[0].getMessage())

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
class TestMusqetMinorUnits(TransactionCase):
    """minor_units converts an amount to whole minor units of its currency."""

    def test_converts_by_the_currency_decimal_places(self):
        for decimals, amount, expected in ((2, 12.34, 1234), (0, 1500.0, 1500), (3, 1.5, 1500),
                                          (None, 12.34, 1234)):
            with self.subTest(decimals=decimals):
                currency = MagicMock(decimal_places=decimals)
                self.assertEqual(minor_units(amount, currency), expected)
