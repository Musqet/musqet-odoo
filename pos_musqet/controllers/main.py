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
        dropped — and a verified one only triggers a status read from Musqet's API. We always reply 200 ``[accepted]`` so the response never doubles as an
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

        # The webhook is only a trigger: Odoo reads the sale back from Musqet's API
        # (musqet_get_sale), which settles the server's record of the sale or refund, and
        # hands that reply, never the body, to the till. So the signing secret alone (leaked,
        # say) can neither set a sale's captured amount or rail nor make the till accept a
        # payment. The read is made only while this method has a pending record of the sale:
        # once it is settled (or if Odoo never recorded it), the POS's own poll covers the
        # till, and a replayed webhook costs no call to Musqet.
        # In a savepoint, and never raised: the reply must stay [accepted] whatever happens.
        # A record left pending here is read back from Musqet the next time a refund of the
        # sale resolves it; until then its synced line shows Not verified.
        sale_id = sale.get('saleId')
        reply = None
        try:
            with request.env.cr.savepoint():
                # (An empty saleId has no record, so it is never read either.)
                if isinstance(sale_id, str) and method_sudo._musqet_has_pending_record(sale_id):
                    reply = method_sudo.musqet_get_sale(sale_id)
        except Exception:
            _logger.exception("Musqet webhook: could not settle sale %r", sale_id)
        if not isinstance(reply, dict) or 'error' in reply or reply.get('saleId', sale_id) != sale_id:
            return self._ack()
        # Buffer Musqet's reply and ping every open session that can show it. The frontend
        # pulls the buffer and settles the pending line idempotently (a poll may have already
        # settled it — see payment_musqet.js _finishSale).
        method_sudo.musqet_latest_response = json.dumps({**reply, 'saleId': sale_id})
        sessions = request.env['pos.session'].sudo().search(
            [('payment_method_ids', 'in', method_sudo.id), ('state', '=', 'opened')])
        for config in sessions.config_id:
            config._notify("MUSQET_LATEST_RESPONSE", config.id)
        return self._ack()

    def _ack(self):
        return request.make_json_response('[accepted]')
