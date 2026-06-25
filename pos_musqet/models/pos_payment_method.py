# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import hashlib
import hmac
import ipaddress
import json
import logging
import pprint
import time
import urllib.parse

import requests

from odoo import fields, models, api, _
from odoo.exceptions import ValidationError, AccessDenied
from odoo.tools import consteq

_logger = logging.getLogger(__name__)

DEFAULT_MUSQET_BASE_URL = 'https://api.musqet.tech/api/v1'
# Network timeout (seconds) for every server-side call to the Musqet API.
MUSQET_TIMEOUT = 10
# Reject webhook signatures whose timestamp is more than this many seconds from now, to
# blunt replay of a captured-but-stale notification (§4.5). Generous enough to absorb
# clock skew between Musqet and the merchant host.
MUSQET_WEBHOOK_TOLERANCE = 300
# The base URL must point at the Musqet cloud. The field stays configurable (prod vs
# staging) but is locked to this domain so an ERP manager can't repoint it at an
# internal host and exfiltrate the bearer token (SSRF). See _check_musqet_base_url.
MUSQET_ALLOWED_HOST = 'musqet.tech'


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [('musqet', 'Musqet')]

    # Musqet
    # groups= controls ORM/UI visibility, not storage. The key is read server-side via
    # self.sudo().musqet_api_key and is intentionally excluded from _load_pos_data_fields
    # so it is never loaded into POS JS.
    musqet_api_key = fields.Char(
        string="Musqet API Key",
        help="Bearer token used by Odoo to authenticate to the Musqet terminal API. "
             "Stored on the backend only and never exposed to the POS frontend.",
        copy=False,
        groups='base.group_erp_manager',
    )
    # Restricted to ERP managers like the key: it's the destination the bearer token is
    # sent to (an SSRF sink), so configuring it needs the same privilege as the token.
    # Not surfaced to the POS frontend — every Musqet call is backend-proxied (see below),
    # so the browser never needs the base URL.
    musqet_base_url = fields.Char(
        string="Musqet API URL",
        help="Base URL of the Musqet terminal API.",
        default=DEFAULT_MUSQET_BASE_URL,
        groups='base.group_erp_manager',
    )
    musqet_terminal_serial = fields.Char(
        string="Musqet Terminal Serial",
        help="Serial number of the physical Musqet terminal bound to this payment method.",
        copy=False,
    )
    # Webhook (#6) — only used on publicly-reachable deployments. The pilot is behind NAT
    # and settles via polling, so these stay unset there and the push path simply never fires.
    musqet_webhook_secret = fields.Char(
        string="Musqet Webhook Signing Secret",
        help="Shared secret used to verify the HMAC signature of inbound Musqet webhooks. "
             "Only needed for publicly-reachable deployments; the pilot settles via polling.",
        copy=False,
        groups='base.group_erp_manager',
    )
    # Buffer for the latest asynchronous webhook result, read by the POS frontend after a
    # MUSQET_LATEST_RESPONSE notification (mirrors pos_adyen.adyen_latest_response). Restricted
    # like the other Musqet config; whitelisted in _is_write_forbidden so the public webhook
    # controller can write it while a POS session is open.
    musqet_latest_response = fields.Char(copy=False, groups='base.group_erp_manager')
    musqet_webhook_url = fields.Char(
        string="Musqet Webhook URL",
        help="Register this URL with Musqet to receive signed sale notifications. "
             "Reachable only on a public-HTTPS deployment.",
        readonly=True,
        store=False,
        compute='_compute_musqet_webhook_url',
    )

    def _compute_musqet_webhook_url(self):
        # get_base_url() forbids a multi-record recordset, so resolve it per record.
        for payment_method in self:
            payment_method.musqet_webhook_url = '%s/musqet/webhook' % payment_method.get_base_url()

    @api.model
    def _load_pos_data_fields(self, config):
        # Surface only non-sensitive fields to the POS frontend. The API key and base URL
        # are deliberately omitted — the key must never reach the browser, and the base
        # URL isn't needed there since every Musqet call goes through the backend proxy.
        params = super()._load_pos_data_fields(config)
        params += ['musqet_terminal_serial']
        return params

    @api.constrains('musqet_terminal_serial')
    def _check_musqet_terminal_serial(self):
        for payment_method in self:
            if not payment_method.musqet_terminal_serial:
                continue
            # sudo() to search across all companies so the serial is globally unique.
            existing = self.sudo().search([
                ('id', '!=', payment_method.id),
                ('musqet_terminal_serial', '=', payment_method.musqet_terminal_serial),
            ])
            same_company = existing.filtered(lambda m: m.company_id == payment_method.company_id)
            if same_company:
                raise ValidationError(_(
                    'Terminal %(terminal)s is already used on payment method %(payment_method)s.',
                    terminal=payment_method.musqet_terminal_serial,
                    payment_method=same_company[0].display_name))
            if existing:
                # Don't disclose the other company's name/method to a user without access to it.
                raise ValidationError(_(
                    'Terminal %(terminal)s is already in use elsewhere.',
                    terminal=payment_method.musqet_terminal_serial))

    @api.constrains('musqet_base_url')
    def _check_musqet_base_url(self):
        # This URL is the base for server-side calls that carry the bearer token, so a
        # bad value is an SSRF / credential-leak sink. Reject anything that isn't an
        # https:// Musqet domain (no IP literals, no loopback/link-local/internal hosts).
        for payment_method in self:
            url = payment_method.musqet_base_url
            if not url:
                continue
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'https':
                raise ValidationError(_("The Musqet API URL must use https://."))
            host = (parsed.hostname or '').lower()
            if not host:
                raise ValidationError(_("The Musqet API URL must include a host."))
            # Reject IP-literal hosts outright — covers the cloud metadata endpoint
            # (169.254.169.254) and any private/loopback/link-local address.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValidationError(_("The Musqet API URL must be a domain name, not an IP address."))
            # Reject obvious internal hostnames as a defence-in-depth layer.
            if host == 'localhost' or host.endswith(('.local', '.internal', '.lan', '.localdomain')):
                raise ValidationError(_(
                    "The Musqet API URL must point to a public Musqet host, not an internal address."))
            # Lock to the Musqet domain (or a subdomain of it).
            if host != MUSQET_ALLOWED_HOST and not host.endswith('.' + MUSQET_ALLOWED_HOST):
                raise ValidationError(_(
                    "The Musqet API URL must be on the %(domain)s domain.", domain=MUSQET_ALLOWED_HOST))

    # -- Backend proxy --------------------------------------------------------
    # These methods hold the Bearer key and make the Musqet HTTPS calls server-side:
    # this dodges CORS and keeps the key off the browser. The POS frontend reaches them
    # via this.pos.data.call("pos.payment.method", "<method>", [[id], ...args]).

    def _musqet_check_access(self):
        # Mirrors pos_adyen.proxy_adyen_request: only a POS user (or a sudo/internal
        # call) may drive the terminal.
        if not self.env.su and not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessDenied()

    def _musqet_headers(self):
        # The key is read with sudo() because the field is restricted to ERP managers;
        # it lives only in this header and is never logged.
        return {'Authorization': 'Bearer %s' % (self.sudo().musqet_api_key or '')}

    def _musqet_url(self, base_url, path):
        return base_url.rstrip('/') + '/' + path.lstrip('/')

    def _musqet_request(self, method, path, payload=None):
        """Make one server-side call to the Musqet API.

        Returns the parsed JSON body on success, or a clean ``{'error': {...}}`` dict on
        a transport failure or non-2xx response — never raises the raw error, and never
        puts the API key in a log line.
        """
        self.ensure_one()
        self._musqet_check_access()
        # Read with sudo() because the field is restricted to ERP managers (like the key),
        # but _musqet_check_access intentionally lets a plain POS cashier drive the
        # terminal — a cashier isn't an ERP manager, so an un-sudo'd read would fail.
        base_url = self.sudo().musqet_base_url
        if not base_url:
            # Keep the "always return {error}" contract: rstrip on a falsy field would
            # otherwise raise a raw AttributeError before the try block below.
            return {'error': {'message': _("No Musqet API URL is configured.")}}
        url = self._musqet_url(base_url, path)
        _logger.info("Musqet %s %s by user #%d", method, path, self.env.uid)
        if payload is not None:
            _logger.debug("Musqet request payload:\n%s", pprint.pformat(payload))
        try:
            response = requests.request(
                method, url,
                headers=self._musqet_headers(),
                json=payload if payload is not None else None,
                timeout=MUSQET_TIMEOUT,
                # The base URL is statically validated (https, no IP/internal hosts, locked
                # to musqet.tech), but that guard only covers the configured URL. Don't
                # follow redirects, or a 3xx Location could send the request — and return a
                # response body — from an internal/loopback host, defeating the SSRF guard.
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as error:
            # str(error) may include the URL but never the headers, so no key leaks.
            _logger.warning("Musqet %s %s failed: %s", method, path, error)
            return {'error': {
                'message': _("Could not reach the Musqet terminal service."),
                'exception': str(error),
            }}
        if response.status_code // 100 != 2:
            _logger.warning("Musqet %s %s returned HTTP %s", method, path, response.status_code)
            # Prefer the structured JSON error body; fall back to raw text as a detail.
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            return {'error': {
                'status_code': response.status_code,
                'message': _("The Musqet terminal service returned an error."),
                'detail': detail,
            }}
        try:
            return response.json()
        except ValueError:
            _logger.warning("Musqet %s %s returned a non-JSON body", method, path)
            return {'error': {
                'status_code': response.status_code,
                'message': _("Received an invalid response from the Musqet terminal service."),
            }}

    def musqet_create_sale(self, payload):
        """POST /terminal/sales — start a sale on the bound terminal.

        ``payload`` is built by the caller (§4.1: serial, amountInCents, mode, type,
        shouldPrint, language, reference); amountInCents must already be integer minor
        units of the business currency. Returns ``{saleId, status, ...}`` or ``{error}``.
        """
        # Drop any buffered webhook result from a previous sale so a late notification for
        # it can't be mistaken for this one (mirrors pos_adyen). Written with sudo() because
        # the field is ERP-manager-restricted but a plain cashier drives the terminal.
        self.sudo().musqet_latest_response = ''
        return self._musqet_request('POST', '/terminal/sales', payload=payload)

    def musqet_create_refund(self, payload, original_payment_id):
        """POST /terminal/sales with type:"refund" — refund a card-settled Musqet sale.

        The Musqet terminal API is a thin command pipe: it enforces merchant scoping but NOT
        over-refund, rail correctness, or any sale<->refund linkage (Musqet/musqet#2094). So
        THIS method — not the browser — is the authority for a refund.

        ``original_payment_id`` (the Odoo id of the original pos.payment, supplied by the POS)
        is UNTRUSTED, like the rest of the payload. We re-read that payment server-side and
        refuse the refund unless it is a card-settled Musqet payment of this company whose
        captured amount covers the request, then FORCE the refund-defining fields (type, mode,
        serial) and forward only known keys — so a tampered payload (mode:"any", type:"sale",
        a foreign serial/amount, extra keys) cannot widen the action. Returns the create
        response or a clean ``{error}``.

        Cumulative-refund accounting and idempotency/dedup (issue #9): the request is capped
        against the REMAINING refundable amount (captured minus refunds already settled against
        this same original, linked via musqet_refund_of), and a refund whose order reference was
        already processed is refused. Both are best-effort for the trusted-operator pilot — a
        refund's pos.payment isn't in the DB until its refund order syncs, so two refunds fired
        inside that window can't see each other here and instead rely on the per-terminal
        in-flight lock (#8), the API's 409 on a concurrent command, and the frontend
        single-settle guard. Odoo's native refund-quantity capping remains a further backstop.
        """
        self.ensure_one()
        self._musqet_check_access()
        # Re-read the claimed original payment server-side; never trust the browser's word for
        # it. sudo() to read across the cashier's restrictions — the validation below, not the
        # ORM record rules, is what authorises the refund.
        try:
            original = self.env['pos.payment'].sudo().browse(int(original_payment_id)).exists()
        except (TypeError, ValueError):
            original = self.env['pos.payment']
        if not original:
            return {'error': {'message': _("The original payment to refund could not be found.")}}
        # Same company as the method driving the refund (defence-in-depth over the API's own
        # merchant scoping). Fall back to the active company so a no-company method can't refund
        # an arbitrary company's payment.
        if original.company_id != (self.company_id or self.env.company):
            return {'error': {'message': _("This payment cannot be refunded here.")}}
        # Card-settled Musqet sale only: the terminal can't reverse Lightning and the API
        # won't refuse the attempt for us (Musqet/musqet#2094).
        if original.payment_method_id.use_payment_terminal != 'musqet' or original.musqet_rail != 'card':
            return {'error': {'message': _(
                "Only card payments taken on a Musqet terminal can be refunded automatically.")}}
        # The cap below compares minor units, so the requested amount (computed by the POS in
        # the session currency) and the captured amount (the original's currency) must be the
        # same currency, or the exponents could differ and the over-refund guard would compare
        # mismatched scales. Enforce the assumption rather than only commenting it.
        if payload.get('currency') != original.currency_id.name:
            return {'error': {'message': _("The refund currency does not match the original payment.")}}
        # Validate the requested amount against the captured amount, in the same minor units the
        # POS computed it. Reject a non-positive amount or an over-refund.
        try:
            amount = int(payload.get('amountInCents'))
        except (TypeError, ValueError):
            return {'error': {'message': _("The refund amount is invalid.")}}
        # `is not None`, not `or 2`: a zero-decimal currency (JPY) has decimal_places == 0,
        # and `0 or 2` would wrongly use 2 — computing a cap 100x too loose. Mirrors the
        # frontend's Number.isInteger() check, which also preserves 0.
        decimals = original.currency_id.decimal_places
        if decimals is None:
            decimals = 2
        captured = int(round(original.amount * (10 ** decimals)))
        # Cumulative over-refund cap (issue #9). The per-call cap (amount <= captured) isn't
        # enough on its own: the Musqet API keeps no sale<->refund linkage and enforces no
        # cumulative limit (Musqet/musqet#2094), so N separate refunds each <= captured could
        # sum past the original. Subtract what's already been refunded against this same original
        # sale — linked through the musqet_refund_of saleId that _finishSale persists on every
        # settled Musqet refund — and cap against the REMAINING refundable amount instead.
        #
        # Window: a refund's pos.payment isn't in the DB until its refund order syncs, so two
        # refunds fired before the first syncs won't see each other here. That window is closed
        # upstream by the per-terminal in-flight lock (#8), the API's 409 on a second concurrent
        # command, and the frontend single-settle guard — refunds settle one at a time and the
        # order syncs on validation, so by the time a second refund order is rung the first is
        # persisted. Best-effort for the trusted-operator pilot (documented on issue #9).
        prior_refunds = self.env['pos.payment']
        already_refunded = 0
        if original.transaction_id:
            prior_refunds = self.env['pos.payment'].sudo().search([
                ('musqet_refund_of', '=', original.transaction_id),
                ('company_id', '=', original.company_id.id),
            ])
            # Refund payment lines carry a negative amount, so take the magnitude.
            already_refunded = sum(
                abs(int(round(refund.amount * (10 ** decimals)))) for refund in prior_refunds
            )
        remaining = captured - already_refunded
        if amount <= 0 or amount > remaining:
            return {'error': {'message': _("The refund amount exceeds the original payment.")}}
        # Idempotency / dedup (issue #9). The API has no idempotency key, so a re-fired refund
        # carrying the same order reference would create a SECOND refund. If a refund already
        # settled against this original carries this refund order's reference, treat the new call
        # as a duplicate and refuse it. (Same unsynced window as the cap above; the in-flight
        # cases are covered by the frontend single-settle guard and the API 409.) Compare against
        # both the order's pos_reference and uuid to mirror how the reference is built caller-side
        # (order.pos_reference || order.uuid).
        reference = payload.get('reference')
        if reference and any(
            reference in (refund.pos_order_id.pos_reference, refund.pos_order_id.uuid)
            for refund in prior_refunds
        ):
            return {'error': {'message': _("This refund has already been processed.")}}
        # Forward only known fields and FORCE the refund-defining ones server-side. serial and
        # currency are taken from trusted server records, not the payload.
        outgoing = {
            'serial': self.musqet_terminal_serial,
            'type': 'refund',
            'mode': 'card',
            'amountInCents': amount,
            'currency': original.currency_id.name,
            'reference': reference,
            'language': payload.get('language'),
            'shouldPrint': bool(payload.get('shouldPrint', True)),
        }
        # Drop any buffered webhook result from a previous sale (mirrors musqet_create_sale).
        self.sudo().musqet_latest_response = ''
        return self._musqet_request('POST', '/terminal/sales', payload=outgoing)

    def musqet_get_sale(self, sale_id):
        """GET /terminal/sales/:id — poll a sale's status.

        Returns ``{status, updatedAt, metadata, ...}`` or ``{error}``.
        """
        return self._musqet_request(
            'GET', '/terminal/sales/%s' % urllib.parse.quote(str(sale_id), safe=''))

    def musqet_cancel_sale(self, sale_id):
        """POST /terminal/sales/:id/cancel — request cancellation of an in-flight sale.

        Returns the parsed Musqet response or ``{error}``.
        """
        return self._musqet_request(
            'POST', '/terminal/sales/%s/cancel' % urllib.parse.quote(str(sale_id), safe=''))

    # -- Webhook (async push) -------------------------------------------------
    # Used only on publicly-reachable deployments. The public controller verifies the HMAC,
    # buffers the result here and pings the POS session; the frontend pulls it via
    # musqet_get_latest_status. The pilot is behind NAT and relies on polling instead.

    def _is_write_forbidden(self, fields):
        # The base blocks writes to most config fields while a POS session is open. Allow
        # the webhook controller to buffer its result during a session (mirrors pos_adyen).
        return super()._is_write_forbidden(fields - {'musqet_latest_response'})

    def musqet_get_latest_status(self):
        """Return the latest webhook-buffered sale result for the POS frontend.

        Named with the ``musqet_`` prefix to match the proxy convention and the JS call site.
        Same access guard as the proxy: a POS cashier (or an internal/sudo call) may read it.
        Returns the parsed canonical sale shape, or ``False`` if nothing is buffered.
        """
        self.ensure_one()
        self._musqet_check_access()
        latest = self.sudo().musqet_latest_response
        if not latest:
            return False
        # Consume-once: clear the buffer as we hand it over so a duplicate notification — or a
        # captured-but-still-fresh webhook replayed inside the timestamp window — can't drive a
        # second settlement. Assumes one terminal per method (see the webhook controller); if a
        # result is consumed by the wrong session the poll loop remains the backstop.
        self.sudo().musqet_latest_response = ''
        return json.loads(latest)

    def _musqet_verify_webhook(self, signature_header, raw_body):
        """Verify a Musqet webhook signature (§4.5).

        Header form ``x-webhook-signature: t=<ts>,v1=<hmac>`` where
        ``hmac = HMAC-SHA256("<ts>.<rawBody>", signing_secret)`` (hex). Returns True only if
        the configured signing secret reproduces the signature AND the timestamp is fresh.
        Comparison is constant-time (consteq); a missing secret, malformed header or stale
        timestamp is rejected. ``raw_body`` must be the exact bytes received — re-encoding a
        parsed body could change it and break the signature.
        """
        self.ensure_one()
        secret = self.sudo().musqet_webhook_secret
        if not secret or not signature_header:
            return False
        # Parse "t=<ts>,v1=<hmac>" into a dict, tolerant of surrounding spaces.
        parts = {}
        for chunk in signature_header.split(','):
            key, sep, value = chunk.partition('=')
            if sep:
                parts[key.strip()] = value.strip()
        timestamp = parts.get('t')
        signature = parts.get('v1')
        if not timestamp or not signature:
            return False
        # Reject stale/future timestamps to blunt replay (seconds since the epoch).
        try:
            age = abs(time.time() - int(timestamp))
        except (TypeError, ValueError):
            return False
        if age > MUSQET_WEBHOOK_TOLERANCE:
            return False
        # Recompute over the raw bytes: b"<ts>." + body. Built from bytes so the body is
        # never re-encoded (which could change it and fail an otherwise-valid signature).
        if isinstance(raw_body, str):
            raw_body = raw_body.encode()
        signed_payload = timestamp.encode() + b'.' + (raw_body or b'')
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return consteq(expected, signature)
