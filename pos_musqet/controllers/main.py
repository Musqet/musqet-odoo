# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import json
import logging

from odoo import http
from odoo.http import request

from ..models.musqet_common import reply_is_for
from ..models.pos_payment_method import MUSQET_LIVE_SESSION_STATES

_logger = logging.getLogger(__name__)

# A sale notification is a few hundred bytes; anything far larger is refused (HTTP 413) before
# the body is read, so an unauthenticated caller can't make Odoo parse megabytes of JSON.
# pos_musqet/docs/INSTALL.md §5 states this limit too.
MUSQET_WEBHOOK_MAX_BYTES = 64 * 1024
# Odoo parses these as form fields before the handler runs, leaving no raw body to verify.
FORM_MIMETYPES = ('application/x-www-form-urlencoded', 'multipart/form-data')


class MusqetController(http.Controller):

    # type='http', not 'jsonrpc': Musqet posts a plain JSON body, not a JSON-RPC call, and a
    # jsonrpc route wraps whatever the handler returns in a JSON-RPC envelope.
    @http.route('/musqet/webhook', type='http', methods=['POST'], auth='public', csrf=False,
                save_session=False, max_content_length=MUSQET_WEBHOOK_MAX_BYTES)
    def musqet_webhook(self, **_params):
        """Receive a signed Musqet sale notification; read the sale back and push the reply.

        For publicly-reachable deployments only: the pilot is behind NAT and settles via
        polling (#4), so a blocked or lost webhook never breaks it. The HMAC over the raw
        body is the only authentication — an unverified, stale or malformed request is
        dropped — and a verified one only triggers a status read from Musqet's API. We
        always reply 200 ``[accepted]`` so the response never doubles as an oracle for
        signature validity and forged traffic can't trigger a retry storm. Only a body over
        MUSQET_WEBHOOK_MAX_BYTES is refused (HTTP 413), before anything else is checked; any
        other failure is logged and still answered [accepted].
        Query or form parameters are ignored: the signed body is the only input.
        """
        if request.httprequest.mimetype in FORM_MIMETYPES:
            _logger.warning("Musqet webhook: body sent as a form, not JSON")
            return self._ack()
        raw_body = request.httprequest.data  # exact bytes — needed to verify the HMAC.
        try:
            payload = json.loads(raw_body or b'{}')
        except Exception:
            # Any error, not only ValueError: a deeply nested body raises RecursionError, and
            # the reply must stay [accepted] whatever the body.
            _logger.warning("Musqet webhook: body was not valid JSON")
            return self._ack()
        if not isinstance(payload, dict):
            return self._ack()

        # The body carries the canonical sale shape (same as the poll result), optionally
        # wrapped as {event, data}. Unwrap to the sale object either way.
        sale = payload['data'] if isinstance(payload.get('data'), dict) else payload
        serial = sale.get('serial')
        if not isinstance(serial, str) or not serial:
            _logger.warning("Musqet webhook: terminal serial missing or not a string")
            return self._ack()

        # The serial only selects which method's secret to verify against; the HMAC is the
        # actual authentication, so a forged serial gets no further without the secret.
        method_sudo = request.env['pos.payment.method'].sudo().search(
            [('musqet_terminal_serial', '=', serial)], limit=1)
        if not method_sudo:
            _logger.warning("Musqet webhook for an unregistered terminal: %r", serial)
            return self._ack()

        signature = request.httprequest.headers.get('x-webhook-signature', '')
        try:
            verified = method_sudo._musqet_verify_webhook(signature, raw_body)
        except Exception:
            # The check rejects malformed headers itself, so this would be a bug in it; caught
            # so it still answers [accepted], not an error only a registered serial reaches.
            _logger.warning("Musqet webhook: signature check raised for %r", serial)
            return self._ack()
        if not verified:
            _logger.warning("Musqet webhook: signature verification failed for %r", serial)
            return self._ack()

        # The webhook is only a trigger: Odoo reads the sale back from Musqet's API
        # (musqet_get_sale), which settles the server's record of the sale or refund, and
        # hands that reply, never the body, to the till. So the signing secret alone (leaked,
        # say) can neither set a sale's captured amount or rail nor make the till accept a
        # payment. The read is made only while this method has a record of the sale that a
        # read could still settle (pending, or a done sale with no rail yet): once it is
        # settled (or if Odoo never recorded it), the POS's own poll covers the till, and a
        # replayed webhook costs no call to Musqet.
        # In a savepoint, and never raised: the reply must stay [accepted] whatever happens.
        # A record a read could still settle, left so here, is read back from Musqet the next
        # time a refund of the sale resolves it; until then its synced line shows Not
        # verified (or, for a sale with no rail, can't be refunded).
        sale_id = sale.get('saleId')
        reply = None
        try:
            with request.env.cr.savepoint():
                # An empty saleId names no sale (an unanswered record has none), so it is
                # never read.
                if (sale_id and isinstance(sale_id, str)
                        and method_sudo._musqet_needs_status_read(sale_id)):
                    reply = method_sudo.musqet_get_sale(sale_id)
        except Exception:
            _logger.exception("Musqet webhook: could not settle sale %r", sale_id)
        if not reply_is_for(sale_id, reply):
            return self._ack()
        # Buffer Musqet's reply and ping every live session that can show it (one being closed
        # may still have the sale in progress). The frontend pulls the buffer and settles the
        # pending line idempotently (a poll may have already settled it — see
        # payment_musqet.js _finishSale).
        # The record is already settled, so a failure here costs only the push: the poll
        # loop still settles the till.
        try:
            with request.env.cr.savepoint():
                method_sudo.musqet_latest_response = json.dumps({**reply, 'saleId': sale_id})
                sessions = method_sudo._musqet_sessions(MUSQET_LIVE_SESSION_STATES)
                for config in sessions.config_id:
                    config._notify("MUSQET_LATEST_RESPONSE", config.id)
        except Exception:
            _logger.exception("Musqet webhook: could not push sale %r to the till", sale_id)
        return self._ack()

    def _ack(self):
        return request.make_response(
            '[accepted]', headers=[('Content-Type', 'text/plain; charset=utf-8')])
