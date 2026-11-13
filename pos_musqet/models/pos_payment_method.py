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
from urllib3.util import parse_url

from odoo import fields, models, api, _
from odoo.exceptions import AccessDenied, UserError, ValidationError
from odoo.tools import SQL, consteq

from .musqet_crypto import (
    ENV_KEY,
    MusqetEncryptionKeyUnavailable,
    decrypt_secret,
    encrypt_secret,
    encrypted_field_name,
    secret_label,
)

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
# Rails a sale may be started on (the API's TerminalCommandMode). "any" lets the terminal
# prompt the customer for card or Bitcoin.
MUSQET_SALE_MODES = ('any', 'card', 'bitcoin')
MUSQET_DEFAULT_SALE_MODE = 'any'
# Upper bounds on the free-text request fields. reference: the API schema's max(255).
# language: a short locale code ("en", "pt"); the API checks it against its own list.
MUSQET_REFERENCE_MAX_LENGTH = 255
MUSQET_LANGUAGE_MAX_LENGTH = 16
# First key of the advisory lock that serialises refunds of one original sale (any fixed
# int4 that no other lock in the database uses; this is "MQRF" in ASCII).
MUSQET_REFUND_LOCK_NAMESPACE = 0x4D515246


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [('musqet', 'Musqet')]

    # Musqet
    # Secrets are encrypted at rest: `*_encrypted` holds a Fernet ciphertext (key in the
    # environment, never the DB), and the plaintext field is non-stored, decrypting on read
    # and encrypting on write, so the read sites and the form view are unchanged. See
    # models/musqet_crypto.py. groups= (on both fields) controls ORM/UI visibility, not
    # storage: a non-manager cannot read either, and the proxy reads the key server-side
    # via sudo(). The key is excluded from _load_pos_data_fields, so it never reaches POS JS.
    musqet_api_key_encrypted = fields.Char(
        string="Musqet API Key (ciphertext)",
        help="Encrypted form of the Musqet API key. Set the key through the API Key field; "
             "never edit this value by hand.",
        copy=False,
        groups='base.group_erp_manager',
    )
    musqet_api_key = fields.Char(
        string="Musqet API Key",
        help="Bearer token used by Odoo to authenticate to the Musqet terminal API. "
             "Stored encrypted on the backend only and never exposed to the POS frontend.",
        copy=False,
        store=False,
        groups='base.group_erp_manager',
        compute='_compute_musqet_api_key',
        inverse='_inverse_musqet_api_key',
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
    # Encrypted at rest, same scheme as musqet_api_key.
    musqet_webhook_secret_encrypted = fields.Char(
        string="Musqet Webhook Signing Secret (ciphertext)",
        help="Encrypted form of the webhook signing secret. Set the secret through the "
             "Webhook Signing Secret field; never edit this value by hand.",
        copy=False,
        groups='base.group_erp_manager',
    )
    musqet_webhook_secret = fields.Char(
        string="Musqet Webhook Signing Secret",
        help="Shared secret used to verify the HMAC signature of inbound Musqet webhooks. "
             "Stored encrypted. Only needed for publicly-reachable deployments; the pilot "
             "settles via polling.",
        copy=False,
        store=False,
        groups='base.group_erp_manager',
        compute='_compute_musqet_webhook_secret',
        inverse='_inverse_musqet_webhook_secret',
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

    # One compute per secret (not a shared one) so writing one secret never re-derives or
    # re-encrypts the other.
    @api.depends('musqet_api_key_encrypted')
    def _compute_musqet_api_key(self):
        self._musqet_decrypt_into('musqet_api_key')

    def _inverse_musqet_api_key(self):
        self._musqet_encrypt_from('musqet_api_key')

    @api.depends('musqet_webhook_secret_encrypted')
    def _compute_musqet_webhook_secret(self):
        self._musqet_decrypt_into('musqet_webhook_secret')

    def _inverse_musqet_webhook_secret(self):
        self._musqet_encrypt_from('musqet_webhook_secret')

    def _musqet_secret_label(self, field_name):
        """The label this record's secret is bound to, or None if it was never saved.

        Uses the saved id (_origin), not self.id: a form onchange computes on an in-memory
        copy whose NewId would not match the label the secret was written under.
        """
        record_id = self._origin.id
        return secret_label(self._name, record_id, field_name) if record_id else None

    def _musqet_decrypt_into(self, field_name):
        for payment_method in self:
            label = payment_method._musqet_secret_label(field_name)
            if not label:
                # A record that was never saved has no stored secret to read.
                payment_method[field_name] = False
                continue
            payment_method[field_name] = decrypt_secret(
                payment_method[encrypted_field_name(field_name)], label)

    def _musqet_encrypt_from(self, field_name):
        for payment_method in self:
            label = payment_method._musqet_secret_label(field_name)
            if not label and payment_method[field_name]:
                # Odoo runs inverses on saved records only; never bind a secret to no id.
                raise UserError(_("Save the payment method before setting its Musqet secrets."))
            try:
                ciphertext = encrypt_secret(payment_method[field_name], label)
            except MusqetEncryptionKeyUnavailable:
                raise UserError(_(
                    "The Musqet secret cannot be saved: the encryption key %(variable)s is "
                    "not set, or is not valid, on the Odoo host. See \"Set the "
                    "secret-encryption key\" in the install guide.", variable=ENV_KEY)) from None
            payment_method[encrypted_field_name(field_name)] = ciphertext

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
        for payment_method in self:
            error = self._musqet_base_url_error(payment_method.musqet_base_url)
            if error:
                raise ValidationError(error)

    @api.model
    def _musqet_base_url_error(self, url):
        """Return why a Musqet API URL is not allowed, or None if it is (or is unset).

        This URL is the base for server-side calls that carry the bearer token, so a bad
        value is an SSRF / credential-leak sink. Reject anything that isn't an https://
        Musqet domain (no IP literals, no loopback/link-local/internal hosts). Checked on
        save and again before every request, since a direct database write skips the ORM
        constraint.
        """
        if not url:
            return None
        parsed = self._musqet_parse_base_url(url)
        if not parsed:
            return _("The Musqet API URL is not valid.")
        if parsed.scheme != 'https':
            return _("The Musqet API URL must use https://.")
        host = (parsed.hostname or '').lower()
        if not host:
            return _("The Musqet API URL must include a host.")
        # Reject IP-literal hosts outright: covers the cloud metadata endpoint
        # (169.254.169.254) and any private/loopback/link-local address.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return _("The Musqet API URL must be a domain name, not an IP address.")
        # Reject obvious internal hostnames as a defence-in-depth layer.
        if host == 'localhost' or host.endswith(('.local', '.internal', '.lan', '.localdomain')):
            return _("The Musqet API URL must point to a public Musqet host, not an internal address.")
        # Lock to the Musqet domain (or a subdomain of it).
        if host != MUSQET_ALLOWED_HOST and not host.endswith('.' + MUSQET_ALLOWED_HOST):
            return _("The Musqet API URL must be on the %(domain)s domain.", domain=MUSQET_ALLOWED_HOST)
        return None

    @api.model
    def _musqet_parse_base_url(self, url):
        """Parse a Musqet API URL, or return None if it can't be read one way only.

        urlparse (used by the checks) and urllib3 (used by requests to send the token) split
        some URLs differently: urllib3 also ends the host at a backslash, so
        "https://evil.example\\@api.musqet.tech" reads as api.musqet.tech to urlparse but is
        sent to evil.example. Refuse the characters behind such splits (and so credentials),
        and require both parsers to read the same host.
        """
        if any(char in '\\@' or char.isspace() or not char.isprintable() for char in url):
            return None
        try:
            parsed = urllib.parse.urlparse(url)
            parsed.port  # noqa: B018 -- read to raise on a non-numeric or out-of-range port
            requests_host = parse_url(url).host or ''
        except ValueError:
            # e.g. an unclosed IPv6 bracket ("https://[::1") or a bad port.
            return None
        # urllib3 keeps an IPv6 literal's brackets; urlparse drops them.
        if requests_host.strip('[]').lower() != (parsed.hostname or '').lower():
            return None
        return parsed

    # -- Backend proxy --------------------------------------------------------
    # These methods hold the Bearer key and make the Musqet HTTPS calls server-side:
    # this dodges CORS and keeps the key off the browser. The POS frontend reaches them
    # via this.pos.data.call("pos.payment.method", "<method>", [[id], ...args]).

    def _musqet_check_access(self):
        # Mirrors pos_adyen.proxy_adyen_request: only a POS user (or a sudo/internal
        # call) may drive the terminal.
        if not self.env.su and not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessDenied()
        self._musqet_check_method()

    def _musqet_check_method(self):
        # Only a Musqet payment method drives a Musqet terminal: refuse to run the proxy on
        # any other method record a caller points it at.
        if any(method.use_payment_terminal != 'musqet' for method in self):
            raise AccessDenied()

    def _musqet_company(self):
        """The company a Musqet request acts for: the method's own, else the active one.

        The fallback means a method with no company can't act for an arbitrary company.
        """
        self.ensure_one()
        return self.company_id or self.env.company

    def _musqet_company_currency(self):
        """The currency code every Musqet sale and refund is made in: the company currency.

        amountInCents is read in this currency, so it must never come from the caller.
        sudo(): a cashier may lack read access on res.currency; this is a fixed lookup.
        """
        return self._musqet_company().sudo().currency_id.name

    def _musqet_requested_currency(self, payload):
        """Return the company currency if the payload's currency is it (or is omitted), else None.

        The caller never picks the currency: amountInCents is read in it, so a different one
        would charge a different value from the one the POS records.
        """
        currency = self._musqet_company_currency()
        return currency if payload.get('currency', currency) == currency else None

    def _musqet_check_terminal_request(self, payload):
        """Shared opening checks for a sale or refund: access, payload shape, serial.

        Raises AccessDenied for a caller or method that may not drive the terminal; returns
        an ``{error}`` dict for a malformed payload or a method with no serial, else None.
        """
        self.ensure_one()
        self._musqet_check_access()
        if not isinstance(payload, dict):
            return {'error': {'message': _("The terminal request is invalid.")}}
        if not self.musqet_terminal_serial:
            return {'error': {'message': _("No Musqet terminal serial is configured.")}}
        return None

    def _musqet_headers(self, api_key):
        # The key lives only in this header and is never logged.
        return {'Authorization': 'Bearer %s' % api_key}

    def _musqet_url(self, base_url, path):
        return base_url.rstrip('/') + '/' + path.lstrip('/')

    @api.model
    def _musqet_error(self, message):
        """Build the ``{error}`` dict for a failure that carries only a message."""
        return {'error': {'message': message}}

    def _musqet_request_target(self):
        """Return ``(base_url, api_key, None)`` for a request, or ``(None, None, {error})``.

        Checks the configuration a request needs, so _musqet_request only sends and reads.
        """
        # Read with sudo() because the fields are restricted to ERP managers, but
        # _musqet_check_access intentionally lets a plain POS cashier drive the terminal:
        # a cashier isn't an ERP manager, so an un-sudo'd read would fail.
        payment_method_sudo = self.sudo()
        base_url = payment_method_sudo.musqet_base_url
        if not base_url:
            return None, None, self._musqet_error(_("No Musqet API URL is configured."))
        # Checked again here, not only on save: a direct database write skips the ORM
        # constraint, and this URL receives the bearer token.
        url_error = self._musqet_base_url_error(base_url)
        if url_error:
            return None, None, self._musqet_error(url_error)
        # Never send an empty bearer token: say why there is no key instead of relaying
        # the API's authentication error.
        api_key = payment_method_sudo.musqet_api_key
        if not api_key:
            if payment_method_sudo.musqet_api_key_encrypted:
                # Shown to the cashier, so no cause or host detail: the server log names
                # the secret and why it failed.
                return None, None, self._musqet_error(_(
                    "The Musqet API key cannot be read. Ask an administrator to check the "
                    "Musqet encryption key and API key."))
            return None, None, self._musqet_error(_("No Musqet API key is configured."))
        return base_url, api_key, None

    def _musqet_request(self, method, path, payload=None):
        """Make one server-side call to the Musqet API.

        Returns the parsed JSON body on success, or a clean ``{'error': {...}}`` dict on
        a configuration problem, a transport failure or a non-2xx response: never raises
        the raw error, and never puts the API key in a log line.
        """
        self.ensure_one()
        self._musqet_check_access()
        base_url, api_key, error = self._musqet_request_target()
        if error:
            return error
        url = self._musqet_url(base_url, path)
        _logger.info("Musqet %s %s by user #%d", method, path, self.env.uid)
        if payload is not None:
            _logger.debug("Musqet request payload:\n%s", pprint.pformat(payload))
        try:
            response = requests.request(
                method, url,
                headers=self._musqet_headers(api_key),
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

        ``payload`` is built by the POS (§4.1: amountInCents, mode, shouldPrint, language,
        reference, currency); amountInCents must already be integer minor units of the
        company currency. Returns ``{saleId, status, ...}`` or ``{error}``.

        The payload is UNTRUSTED: any POS user can call this method over RPC with whatever
        they like. The Musqet API accepts ``type`` on this same endpoint, so forwarding the
        payload as-is would let a caller start a refund here and skip every check in
        musqet_create_refund. Instead, rebuild the request from known keys only: ``type``
        is forced to "sale", ``serial`` comes from this method's own record, ``currency``
        must be the company currency, and the remaining fields are checked before they are
        sent. The caller still chooses which payment method record this runs on; scoping
        that to the cashier's own POS is a separate check.
        """
        error = self._musqet_check_terminal_request(payload)
        if error:
            return error
        amount = self._musqet_parse_amount(payload)
        if amount is None:
            return {'error': {'message': _("The sale amount is invalid.")}}
        mode = payload.get('mode', MUSQET_DEFAULT_SALE_MODE)
        if mode not in MUSQET_SALE_MODES:
            return {'error': {'message': _("The payment mode is invalid.")}}
        currency = self._musqet_requested_currency(payload)
        if not currency:
            return {'error': {'message': _("The sale currency does not match the company currency.")}}
        reference = payload.get('reference')
        if reference is not None and not self._musqet_valid_reference(reference):
            return {'error': {'message': _("The sale reference is invalid.")}}
        # The optional fields go first so the server-set fields after them always win, even
        # if the helper's allow-list ever grows.
        outgoing = {
            **self._musqet_optional_terminal_fields(payload),
            'serial': self.musqet_terminal_serial,
            'type': 'sale',
            'mode': mode,
            'amountInCents': amount,
            'currency': currency,
        }
        if reference is not None:
            outgoing['reference'] = reference
        # Drop any buffered webhook result from a previous sale so a late notification for
        # it can't be mistaken for this one (mirrors pos_adyen). Written with sudo() because
        # the field is ERP-manager-restricted but a plain cashier drives the terminal.
        self.sudo().musqet_latest_response = ''
        return self._musqet_request('POST', '/terminal/sales', payload=outgoing)

    @api.model
    def _musqet_parse_amount(self, payload):
        """Return ``amountInCents`` as a positive int, or None if it is anything else.

        Strict on purpose: no coercion of strings or floats, and bool is refused even though
        it is an int subclass (True would otherwise pass as 1).
        """
        amount = payload.get('amountInCents')
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return None
        return amount

    @api.model
    def _musqet_valid_reference(self, reference):
        """Whether ``reference`` is a non-empty string the API will accept."""
        return isinstance(reference, str) and 0 < len(reference) <= MUSQET_REFERENCE_MAX_LENGTH

    @api.model
    def _musqet_optional_terminal_fields(self, payload):
        """Return the cosmetic optional fields of a sale/refund request, type-checked.

        Covers ``shouldPrint`` and ``language`` only; the fields that change what the request
        does (amount, currency, reference) are validated by the caller. A value of the wrong
        type is dropped (language) or replaced by the default (shouldPrint), so a caller can't
        smuggle nested objects or extra keys to the API through them.
        """
        should_print = payload.get('shouldPrint', True)
        fields_out = {'shouldPrint': should_print if isinstance(should_print, bool) else True}
        language = payload.get('language')
        if isinstance(language, str) and 0 < len(language) <= MUSQET_LANGUAGE_MAX_LENGTH:
            fields_out['language'] = language
        return fields_out

    def _musqet_refundable_original(self, original_payment_id, currency):
        """Re-read the claimed original payment and check it may be refunded here.

        Returns ``(original, None)`` for a card-settled Musqet payment of this company in
        ``currency`` (the company currency), else ``(None, {error})``. The id is UNTRUSTED:
        never take the browser's word for the payment. sudo() reads across the cashier's
        restrictions; these checks, not the ORM record rules, authorise the refund.
        """
        Payment = self.env['pos.payment'].sudo()
        try:
            original = Payment.browse(int(original_payment_id)).exists()
        except (TypeError, ValueError):
            original = Payment
        if not original:
            return None, {'error': {'message': _("The original payment to refund could not be found.")}}
        # Same company as the method driving the refund (defence-in-depth over the API's own
        # merchant scoping).
        if original.company_id != self._musqet_company():
            return None, {'error': {'message': _("This payment cannot be refunded here.")}}
        # Card-settled Musqet sale only: the terminal can't reverse Lightning and the API
        # won't refuse the attempt for us (Musqet/musqet#2094).
        # A settled Musqet payment always carries its saleId: the refund records key on it.
        if (original.payment_method_id.use_payment_terminal != 'musqet'
                or original.musqet_rail != 'card' or not original.transaction_id):
            return None, {'error': {'message': _(
                "Only card payments taken on a Musqet terminal can be refunded automatically.")}}
        # The over-refund cap compares minor units, so the original must be in the refund's
        # currency, or the exponents could differ and the cap would compare mismatched scales.
        if original.currency_id.name != currency:
            return None, {'error': {'message': _("The refund currency does not match the original payment.")}}
        return original, None

    def _musqet_lock_original_sale(self, original):
        """Hold a lock on the original sale's refunds until this transaction ends."""
        self.env.cr.execute(SQL(
            'SELECT pg_advisory_xact_lock(%s, hashtext(%s))',
            MUSQET_REFUND_LOCK_NAMESPACE,
            '%s,%s' % (original.company_id.id, original.transaction_id)))

    def _musqet_original_refunds(self, original):
        """Return every recorded refund of the original sale, pending ones re-read first.

        A pending refund may have settled or failed since it was sent; asking Musqet means
        a failed one stops counting against the sale. If Musqet can't be reached it stays
        pending and keeps counting, the safe side for a cap.
        """
        Refund = self.env['pos.musqet.refund'].sudo()
        domain = [
            ('company_id', '=', original.company_id.id),
            ('original_sale_id', '=', original.transaction_id),
        ]
        for refund in Refund.search(domain + [('state', '=', 'pending')]):
            method = refund.payment_method_id
            # Only a Musqet method may call the API (_musqet_check_method).
            if method.use_payment_terminal == 'musqet':
                method.musqet_get_sale(refund.sale_id)
        return Refund.search(domain)

    def _musqet_unrecorded_synced_refunds(self, original, recorded):
        """Synced refund payments of the original that have no refund record.

        These are refunds made before refunds were recorded on the server; the link to the
        original is the musqet_refund_of the POS wrote on them. A payment whose saleId has a
        record is left to the record, which Musqet's status keeps current.
        """
        recorded_sale_ids = set(recorded.mapped('sale_id'))
        return [
            payment for payment in self.env['pos.payment'].sudo().search([
                ('musqet_refund_of', '=', original.transaction_id),
                ('company_id', '=', original.company_id.id),
            ])
            if payment.transaction_id not in recorded_sale_ids
        ]

    def _musqet_record_refund(self, response, original, amount, reference):
        """Record a refund Musqet accepted, so the next refund of the sale counts it."""
        # An {error} response carries no saleId, so nothing is recorded for it.
        sale_id = response.get('saleId') if isinstance(response, dict) else None
        if not sale_id:
            return
        refund = self.env['pos.musqet.refund'].sudo().create({
            'company_id': original.company_id.id,
            'payment_method_id': self.id,
            'original_sale_id': original.transaction_id,
            'sale_id': str(sale_id),
            'reference': reference,
            'amount_minor': amount,
        })
        refund._musqet_apply_status(response.get('status'))

    def musqet_create_refund(self, payload, original_payment_id):
        """POST /terminal/sales with type:"refund" — refund a card-settled Musqet sale.

        The Musqet terminal API is a thin command pipe: it enforces merchant scoping but NOT
        over-refund, rail correctness, or any sale<->refund linkage (Musqet/musqet#2094). So
        THIS method — not the browser — is the authority for a refund.

        ``original_payment_id`` (the Odoo id of the original pos.payment, supplied by the POS)
        is UNTRUSTED, like the rest of the payload. We re-read that payment server-side and
        refuse the refund unless it is a card-settled Musqet payment of this company, in the
        company currency, whose captured amount covers the request.

        type, mode and serial are set on the server; the currency must be the company
        currency; reference is required and validated; only known keys are forwarded. So a
        tampered payload (mode:"any", type:"sale", a foreign serial/amount, extra keys)
        cannot widen the action. Returns the create response or a clean ``{error}``.

        Over-refund cap and duplicate check: every refund Musqet accepts is recorded on the
        server (pos.musqet.refund) before this call returns, so the cap counts it at once,
        whether or not its refund order ever reaches Odoo. Refunds of the same original sale
        are decided one at a time under a transaction lock. A refund recorded as pending is
        re-read from Musqet first, so one that failed stops counting. Refund payments synced
        without a record (made before this version) still count, through musqet_refund_of.
        A refund Musqet accepted but whose response never arrived (a timeout) has no saleId
        and so no record; Odoo's own refund-quantity cap remains the backstop for that case.
        """
        error = self._musqet_check_terminal_request(payload)
        if error:
            return error
        currency = self._musqet_requested_currency(payload)
        if not currency:
            return {'error': {'message': _("The refund currency does not match the company currency.")}}
        original, error = self._musqet_refundable_original(original_payment_id, currency)
        if error:
            return error
        # Validate the requested amount against the captured amount, in the same minor units the
        # POS computed it: a positive whole number here, and no more than the remaining
        # refundable amount below.
        amount = self._musqet_parse_amount(payload)
        if amount is None:
            return {'error': {'message': _("The refund amount is invalid.")}}
        # The reference is required: without one the duplicate check below can't run.
        reference = payload.get('reference')
        if not self._musqet_valid_reference(reference):
            return {'error': {'message': _("The refund reference is invalid.")}}
        # `is not None`, not `or 2`: a zero-decimal currency (JPY) has decimal_places == 0,
        # and `0 or 2` would wrongly use 2, computing a cap 100x too loose. Mirrors the
        # frontend's Number.isInteger() check, which also preserves 0.
        decimals = original.currency_id.decimal_places
        if decimals is None:
            decimals = 2
        captured = int(round(original.amount * (10 ** decimals)))
        # One refund decision at a time per original sale: a second refund of the same sale
        # waits here until this transaction ends, then sees the refund recorded below.
        self._musqet_lock_original_sale(original)
        recorded = self._musqet_original_refunds(original)
        synced = self._musqet_unrecorded_synced_refunds(original, recorded)
        # A failed refund returned nothing, so it neither counts nor blocks a retry.
        recorded = recorded.filtered(lambda refund: refund.state != 'failed')
        # Cumulative cap: the Musqet API enforces no limit across refunds (Musqet/musqet#2094),
        # so cap against what is left after every refund already sent for this sale.
        already_refunded = sum(recorded.mapped('amount_minor')) + sum(
            abs(int(round(refund.amount * (10 ** decimals)))) for refund in synced)
        if amount > captured - already_refunded:
            return {'error': {'message': _("The refund amount exceeds the original payment.")}}
        # Duplicate check: the API has no idempotency key, so a refund re-sent with the same
        # order reference would refund twice. Synced orders are matched on pos_reference and
        # uuid, as the POS builds the reference from either (order.pos_reference || order.uuid).
        if reference in recorded.mapped('reference') or any(
            reference in (refund.pos_order_id.pos_reference, refund.pos_order_id.uuid)
            for refund in synced
        ):
            return {'error': {'message': _("This refund has already been processed.")}}
        # Forward only known fields and FORCE the refund-defining ones server-side. serial comes
        # from this method's record; currency is the company currency (the payload may only
        # repeat it).
        # The optional fields go first so the server-set fields after them always win.
        outgoing = {
            **self._musqet_optional_terminal_fields(payload),
            'serial': self.musqet_terminal_serial,
            'type': 'refund',
            'mode': 'card',
            'amountInCents': amount,
            'currency': currency,
            'reference': reference,
        }
        # Drop any buffered webhook result from a previous sale (mirrors musqet_create_sale).
        self.sudo().musqet_latest_response = ''
        response = self._musqet_request('POST', '/terminal/sales', payload=outgoing)
        self._musqet_record_refund(response, original, amount, reference)
        return response

    def musqet_get_sale(self, sale_id):
        """GET /terminal/sales/:id — poll a sale's status.

        Returns ``{status, updatedAt, metadata, ...}`` or ``{error}``.
        """
        response = self._musqet_request(
            'GET', '/terminal/sales/%s' % urllib.parse.quote(str(sale_id), safe=''))
        # Keep a recorded refund's state current with what Musqet reports for it. An {error}
        # response has no top-level status, so it leaves the record as it was.
        if isinstance(response, dict):
            self.env['pos.musqet.refund'].sudo().search([
                ('payment_method_id', '=', self.id),
                ('sale_id', '=', str(sale_id)),
            ])._musqet_apply_status(response.get('status'))
        return response

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
        if not signature_header:
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
        # Skip the decrypt for malformed or stale headers. This is not a guard: a well-formed,
        # fresh header with a bogus signature still reaches the decrypt.
        secret = self.sudo().musqet_webhook_secret
        if not secret:
            return False
        # Recompute over the raw bytes: b"<ts>." + body. Built from bytes so the body is
        # never re-encoded (which could change it and fail an otherwise-valid signature).
        if isinstance(raw_body, str):
            raw_body = raw_body.encode()
        signed_payload = timestamp.encode() + b'.' + (raw_body or b'')
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return consteq(expected, signature)
