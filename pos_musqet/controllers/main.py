# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MusqetController(http.Controller):

    @http.route('/musqet/webhook', type='jsonrpc', methods=['POST'], auth='public',
                csrf=False, save_session=False)
    def musqet_webhook(self):
        """Receive a signed Musqet sale notification and push it into the live POS session.

        For publicly-reachable deployments only: the pilot is behind NAT and settles via
        polling (#4), so a blocked or lost webhook never breaks it. The HMAC over the raw
        body is the only authentication — an unverified, stale or malformed request is
        dropped. We always reply 200 ``[accepted]`` so the response never doubles as an
        oracle for signature validity and forged traffic can't trigger a retry storm.
        """
        raw_body = request.httprequest.data  # exact bytes — needed to verify the HMAC.
        try:
            payload = json.loads(raw_body or b'{}')
        except (ValueError, TypeError):
            _logger.warning("Musqet webhook: body was not valid JSON")
            return self._ack()
        if not isinstance(payload, dict):
            return self._ack()

        # The body carries the canonical sale shape (same as the poll result), optionally
        # wrapped as {event, data}. Unwrap to the sale object either way.
        sale = payload['data'] if isinstance(payload.get('data'), dict) else payload
        serial = sale.get('serial')
        if not serial:
            _logger.warning("Musqet webhook: no terminal serial in payload")
            return self._ack()

        # The serial only selects which method's secret to verify against; the HMAC is the
        # actual authentication, so a forged serial gets no further without the secret.
        method_sudo = request.env['pos.payment.method'].sudo().search(
            [('musqet_terminal_serial', '=', serial)], limit=1)
        if not method_sudo:
            _logger.warning("Musqet webhook for an unregistered terminal: %s", serial)
            return self._ack()

        signature = request.httprequest.headers.get('x-webhook-signature', '')
        if not method_sudo._musqet_verify_webhook(signature, raw_body):
            _logger.warning("Musqet webhook: signature verification failed for %s", serial)
            return self._ack()

        # Buffer the verified result and ping every open session that can show it. The
        # frontend pulls the buffer and settles the pending line idempotently (a poll may
        # have already settled it — see payment_musqet.js _finishSale).
        method_sudo.musqet_latest_response = json.dumps(sale)
        sessions = request.env['pos.session'].sudo().search(
            [('payment_method_ids', 'in', method_sudo.id), ('state', '=', 'opened')])
        for config in sessions.config_id:
            config._notify("MUSQET_LATEST_RESPONSE", config.id)
        return self._ack()

    def _ack(self):
        return request.make_json_response('[accepted]')
