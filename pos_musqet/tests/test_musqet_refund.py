# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Refund authorisation, against a stubbed original payment."""
import json
from unittest.mock import MagicMock, patch

import requests

from odoo.addons.pos_musqet.models import pos_musqet_refund, pos_payment_method
from odoo.addons.pos_musqet.models.pos_payment_method import MUSQET_REFERENCE_MAX_LENGTH
from odoo.exceptions import AccessDenied, AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from .common import (
    PROXY_LOGGER,
    RECORD_LOGGER,
    UNANSWERED,
    create_musqet_method,
    dirty_field_names,
    make_pos_user,
    musqet_response,
    record_refund,
    record_sale,
)


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
        # Spec'd on pos.payment, so a method the refund calls that pos.payment lacks fails
        # here rather than answering with a truthy mock.
        original = MagicMock(spec=type(self.env['pos.payment']))
        original.exists.return_value = original
        original.company_id = company if company is not None else self.method.company_id
        original.payment_method_id.use_payment_terminal = terminal
        original.payment_method_id.id = self.method.id
        # A paid sale line its sale vouches for. The check itself (_musqet_check_settles) is
        # a stub here; TestMusqetRefundWithPosRecords runs it on real rows.
        original.musqet_settle_check = 'verified'
        original._musqet_is_sale_line.return_value = True
        original._musqet_order_is_paid.return_value = True
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
        refund._musqet_order_references.return_value = {
            ref for ref in (refund.pos_order_id.pos_reference, refund.pos_order_id.uuid) if ref}
        return refund

    def _call_refund(self, payload, original, original_payment_id=123, prior_refunds=None,
                     sale_id='refund-1', post_status=200, get_status='PENDING', get_http=200,
                     post_error=None):
        """Run musqet_create_refund as the cashier with the Musqet API stubbed.

        The create (POST) returns ``sale_id`` with HTTP ``post_status``, or raises
        ``post_error``; a status read (GET) of an earlier recorded refund returns
        ``get_status`` with HTTP ``get_http``.
        """
        # No default currency: an omitted one means the company currency, so most tests take
        # that path. Tests that send a currency do so deliberately.
        payload = {'reference': 'REFUND-ORDER-1', **payload}

        def fake_request(method, url, **kwargs):
            response = MagicMock()
            if method == 'GET':
                response.status_code = get_http
                # The full partner shape of the card sale or refund read, at its recorded
                # amount: settling a pending original from this read must leave it refundable.
                read_id = url.rsplit('/', 1)[1]
                record = (self.env['pos.musqet.refund'].search([('sale_id', '=', read_id)])
                          or self.env['pos.musqet.sale'].search([('sale_id', '=', read_id)]))
                response.json.return_value = {
                    'saleId': read_id, 'status': get_status, 'rail': 'card',
                    'amountInCents': record[:1].amount_minor or 1000, 'currency': self.currency}
            else:
                # Anything still unwritten now could fail after the refund is sent.
                self.dirty_at_post = dirty_field_names(self.env)
                if post_error:
                    raise post_error
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
        with self.assertLogs(RECORD_LOGGER, 'ERROR') as logs, mute_logger('odoo.sql_db'):
            result, _ = self._call_refund(
                {'amountInCents': 500, 'reference': 'REFUND-LOST'}, self._stub_original(amount=10.0))
        # The POS still gets the saleId, so it polls the refund and syncs its payment.
        self.assertEqual(result, {'saleId': 'refund-1', 'status': 'PENDING'})
        # The failed insert didn't abort the request's transaction: it can still be used.
        self.assertEqual(self.env['pos.musqet.refund'].search_count([('sale_id', '=', 'refund-1')]), 1)
        self.assertIn('refund-1', logs.output[0])
        self.assertIn('REFUND-LOST', logs.output[0])
        self.assertIn('500', logs.output[0])

    def _lose_refund(self, original, amount, reference):
        """Send a refund whose reply times out."""
        with mute_logger(PROXY_LOGGER, RECORD_LOGGER):
            return self._call_refund(
                {'amountInCents': amount, 'reference': reference}, original,
                post_error=requests.exceptions.ReadTimeout('timed out'))[0]

    def test_records_a_refund_whose_reply_was_lost(self):
        result = self._lose_refund(self._stub_original(amount=10.0), 600, 'REFUND-A')
        self.assertIn(UNANSWERED, result['error']['message'])
        refund = self._recorded_refunds()
        self.assertEqual(len(refund), 1)
        self.assertFalse(refund.sale_id)
        self.assertEqual(refund.state, 'pending')
        self.assertEqual(refund.reference, 'REFUND-A')
        self.assertEqual(refund.amount_minor, 600)

    def test_an_unanswered_refund_counts_and_refuses_its_refund_order(self):
        original = self._stub_original(amount=10.0)
        self._lose_refund(original, 600, 'REFUND-A')
        # Its refund order is refused whatever the amount, and told why.
        result, mock_request = self._call_refund(
            {'amountInCents': 100, 'reference': 'REFUND-A'}, original, sale_id='refund-a2')
        self.assertIn(UNANSWERED, result['error']['message'])
        self.assertFalse(self._posts(mock_request))
        # It counts against the sale: another refund order may take only what is left.
        result, mock_request = self._call_refund(
            {'amountInCents': 600, 'reference': 'REFUND-B'}, original, sale_id='refund-b')
        self.assertIn('exceeds', result['error']['message'])
        result, mock_request = self._call_refund(
            {'amountInCents': 400, 'reference': 'REFUND-B'}, original, sale_id='refund-b')
        self.assertNotIn('error', result)
        # It has no saleId to read from Musqet.
        self.assertFalse(self._gets(mock_request))

    def test_an_unanswered_refund_of_another_sale_refuses_its_refund_order(self):
        # A refund order refunding two sales: the lost refund of one holds up the other's.
        lost = record_refund(self.env, self.method, False, original_sale_id='sale-orig-other',
                             reference='REFUND-A')
        original = self._stub_original(amount=10.0)
        result, mock_request = self._call_refund(
            {'amountInCents': 100, 'reference': 'REFUND-A'}, original)
        self.assertIn(UNANSWERED, result['error']['message'])
        self.assertFalse(self._posts(mock_request))
        # Not once a manager has marked it failed.
        lost.state = 'failed'
        result, mock_request = self._call_refund(
            {'amountInCents': 100, 'reference': 'REFUND-A'}, original)
        self.assertNotIn('error', result)

    def test_a_manager_clears_an_unanswered_refund(self):
        original = self._stub_original(amount=10.0)
        self._lose_refund(original, 600, 'REFUND-A')
        manager = make_pos_user(self.env, 'musqet_manager_lost_refund',
                                group='point_of_sale.group_pos_manager')
        self._recorded_refunds().with_user(manager).action_musqet_mark_failed()
        result, mock_request = self._call_refund(
            {'amountInCents': 1000, 'reference': 'REFUND-A'}, original, sale_id='refund-a2')
        self.assertNotIn('error', result)
        self.assertEqual(len(self._posts(mock_request)), 1)

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
                fake = musqet_response('refund-1', status, amountInCents=500)
                with patch.object(pos_payment_method.requests, 'request', return_value=fake):
                    self.method.with_user(self.cashier).musqet_get_sale('refund-1')
                self.assertEqual(self._recorded_refunds().state, state)

    def test_poll_error_leaves_a_recorded_refund_pending(self):
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        fake = musqet_response('refund-1', 'CANCELLED', status_code=502)
        with patch.object(pos_payment_method.requests, 'request', return_value=fake):
            self.method.with_user(self.cashier).musqet_get_sale('refund-1')
        self.assertEqual(self._recorded_refunds().state, 'pending')

    def test_an_invalid_request_calls_nothing(self):
        # The payload is checked before anything is read from Musqet for the original.
        record_refund(self.env, self.method, 'refund-pending')
        original = self._stub_original(amount=10.0, state='pending')
        for payload in ({'amountInCents': 0}, {'amountInCents': 500, 'reference': ''}):
            with self.subTest(payload=payload):
                result, mock_request = self._call_refund(payload, original)
                self.assertIn('error', result)
                mock_request.assert_not_called()

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

    def test_a_refund_settles_at_the_amount_musqet_reports(self):
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        refund = self._recorded_refunds()
        for bad in (None, True, '500'):
            with self.subTest(amount=bad), self.assertLogs(RECORD_LOGGER, 'WARNING'):
                refund._musqet_apply_response({'status': 'COMPLETE', 'amountInCents': bad})
                self.assertEqual(refund.state, 'pending')
        refund._musqet_apply_response({'status': 'COMPLETE', 'amountInCents': 450})
        self.assertEqual((refund.state, refund.amount_minor), ('done', 450))

    def test_a_settled_refund_keeps_its_state(self):
        # Once done, a later failure status (or any status) can't take it out of the cap.
        self._call_refund({'amountInCents': 500}, self._stub_original(amount=10.0))
        refund = self._recorded_refunds()
        refund._musqet_apply_response({'status': 'COMPLETE', 'amountInCents': 500})
        refund._musqet_apply_response({'status': 'ERROR'})
        refund._musqet_apply_response({'status': 'COMPLETE', 'amountInCents': 1})
        self.assertEqual(refund.state, 'done')

    def test_rereads_at_most_the_limit_of_pending_refunds(self):
        for index in range(pos_musqet_refund.MUSQET_REFUND_REREAD_LIMIT + 2):
            record_refund(self.env, self.method, 'refund-p%d' % index)
        _, mock_request = self._call_refund({'amountInCents': 1}, self._stub_original(amount=10.0))
        reads = [call.args[1] for call in self._gets(mock_request)]
        # The oldest ones, in order.
        self.assertEqual(reads, [
            'https://api.musqet.tech/api/v1/terminal/sales/refund-p%d' % index
            for index in range(pos_musqet_refund.MUSQET_REFUND_REREAD_LIMIT)])

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
        # A line not Verified (say it copies a real sale's saleId), a refund line, or a line
        # of an order never paid: the sale record is fine, the line is not.
        cases = (('flag', 'unverified'), ('flag', False), ('sale line', False), ('paid', False))
        for case, value in cases:
            with self.subTest(case=case, value=value):
                original = self._stub_original(amount=10.0)
                if case == 'flag':
                    original.musqet_settle_check = value
                elif case == 'sale line':
                    original._musqet_is_sale_line.return_value = value
                else:
                    original._musqet_order_is_paid.return_value = value
                result, mock_request = self._call_refund({'amountInCents': 500}, original)
                self.assertIn('has not confirmed', result['error']['message'])
                mock_request.assert_not_called()

    def test_rejects_refund_of_a_sale_with_no_known_rail(self):
        # Read again first (a later reply may report the rail); still none, so refused.
        result, mock_request = self._call_refund(
            {'amountInCents': 500}, self._stub_original(rail=False), get_status='PROCESSING')
        self.assertIn('Only card payments', result['error']['message'])
        self.assertEqual(len(self._gets(mock_request)), 1)
        self.assertFalse(self._posts(mock_request))

    def test_reads_the_rail_of_a_done_sale_settled_without_one(self):
        result, mock_request = self._call_refund(
            {'amountInCents': 500}, self._stub_original(rail=False), get_status='COMPLETE')
        self.assertNotIn('error', result)
        self.assertEqual(
            self.env['pos.musqet.sale'].search([('sale_id', '=', 'sale-orig-1')]).rail, 'card')

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
