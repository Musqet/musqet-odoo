# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import json
import logging

from werkzeug.exceptions import RequestEntityTooLarge

from odoo import http
from odoo.http import request

from ..models.musqet_common import reply_is_for
from ..models.pos_payment_method import MUSQET_LIVE_SESSION_STATES

_logger = logging.getLogger(__name__)

# A sale notification is a few hundred bytes; a body whose Content-Length is over this is
# refused (HTTP 413) unread. A chunked one is cut at this size as it is read (see the note on
# reading the body). pos_musqet/docs/INSTALL.md §5 states this limit too.
MUSQET_WEBHOOK_MAX_BYTES = 64 * 1024
# Odoo parses these as form fields before the handler runs, leaving no raw body to verify.
MUSQET_WEBHOOK_FORM_MIMETYPES = ('application/x-www-form-urlencoded', 'multipart/form-data')


def _plain_string(value):
    """Whether a body value can go into a database search: a non-empty string with no NUL
    (PostgreSQL can't take one), so a bad value is refused, not raised by the search."""
    return isinstance(value, str) and bool(value) and '\x00' not in value


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
        signature validity and forged traffic can't trigger a retry storm.
        The one exception: a body whose Content-Length is over MUSQET_WEBHOOK_MAX_BYTES is
        refused (HTTP 413) before anything else is checked. Any other failure is logged and
        still answered [accepted].
        Query or form parameters are ignored: the signed body is the only input.
        """
        # The route's max_content_length refuses this as the body is read only on Werkzeug
        # 2.3+; checked here too so the 413 holds on older Werkzeug (and before the guard).
        length = request.httprequest.content_length
        if length is not None and length > MUSQET_WEBHOOK_MAX_BYTES:
            raise RequestEntityTooLarge()
        if request.httprequest.mimetype in MUSQET_WEBHOOK_FORM_MIMETYPES:
            _logger.warning("Musqet webhook: body sent as a form, not JSON")
            return self._ack()
        # The exact bytes, needed to verify the HMAC. A chunked body (no Content-Length) can't
        # be refused up front: Werkzeug 2.3+ (Odoo's packages on Python 3.12) stops reading it
        # at max_content_length, so it is dropped and answered [accepted] like any other bad
        # body. Older Werkzeug doesn't bound that read, and Odoo's request doesn't expose the
        # raw stream to bound it here, so INSTALL §5 has the proxy cap the body size.
        raw_body = request.httprequest.data
        # The one guard behind every step below: whatever raises, the answer is [accepted].
        # The push catches its own, so a failure there doesn't undo the settled record.
        try:
            with request.env.cr.savepoint():
                return self._musqet_handle_webhook(raw_body)
        except Exception:
            _logger.exception("Musqet webhook: could not handle the request")
            return self._ack()

    def _musqet_handle_webhook(self, raw_body):
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
        if not _plain_string(serial):
            _logger.warning("Musqet webhook: terminal serial missing or not a plain string")
            return self._ack()

        # The serial only selects which method's secret to verify against; the HMAC is the
        # actual authentication, so a forged serial gets no further without the secret.
        method_sudo = request.env['pos.payment.method'].sudo().search(
            [('musqet_terminal_serial', '=', serial)], limit=1)
        if not method_sudo:
            _logger.warning("Musqet webhook for an unregistered terminal: %r", serial)
            return self._ack()

        signature = request.httprequest.headers.get('x-webhook-signature', '')
        if not method_sudo._musqet_verify_webhook(signature, raw_body):
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
        # In its own savepoint, so a failed read is rolled back and logged against its sale.
        # A record a read could still settle, left so here, is read back from Musqet the next
        # time a refund of the sale resolves it; until then its synced line shows Not
        # verified (or, for a sale with no rail, can't be refunded).
        sale_id = sale.get('saleId')
        reply = None
        try:
            with request.env.cr.savepoint():
                # An empty saleId names no sale (an unanswered record has none), and one that
                # isn't a plain string can't be searched (see _plain_string), so neither is read.
                if _plain_string(sale_id) and method_sudo._musqet_needs_status_read(sale_id):
                    reply = method_sudo.musqet_get_sale(sale_id)
        except Exception:
            _logger.exception("Musqet webhook: could not settle sale %r", sale_id)
        if not reply_is_for(sale_id, reply):
            return self._ack()
        # Buffer Musqet's reply and ping every live session that can show it (one being closed
        # may still have the sale in progress). Each pinged till asks for the buffered result
        # of its own saleId; only the one waiting on this sale takes it and settles its line
        # idempotently (a poll may have already settled it, see payment_musqet.js _finishSale).
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
