# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import ipaddress
import logging
import pprint
import urllib.parse

import requests

from odoo import fields, models, api, _
from odoo.exceptions import ValidationError, AccessDenied

_logger = logging.getLogger(__name__)

DEFAULT_MUSQET_BASE_URL = 'https://api.musqet.tech/api/v1'
# Network timeout (seconds) for every server-side call to the Musqet API.
MUSQET_TIMEOUT = 10
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
        return self._musqet_request('POST', '/terminal/sales', payload=payload)

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
