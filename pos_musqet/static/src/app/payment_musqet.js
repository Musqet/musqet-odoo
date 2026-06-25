// Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import { _t } from "@web/core/l10n/translation";
import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { register_payment_method } from "@point_of_sale/app/services/pos_store";
import { logPosMessage } from "@point_of_sale/app/utils/pretty_console_log";
import { formatInteger } from "@web/views/fields/formatters";

// Poll cadence for the create→poll result engine. Polling — not the webhook — is the
// primary result mechanism for the pilot: the merchant is behind NAT with no public
// HTTPS URL, so #2–#5 must settle the order on polling alone (see issue #4 / #1).
const POLL_INTERVAL_MS = 1500;
// Overall budget, aligned to the terminal's ~5-minute sale expiry. Past this we stop
// polling and let the cashier retry rather than waiting forever.
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

// Terminal statuses (canonical contract, issue #1). COMPLETE is the only success.
const SUCCESS_STATUS = "COMPLETE";
const FAILURE_STATUSES = ["ERROR", "CANCELLED", "CANCELLED_REMOTE", "TIMED_OUT"];

export class PaymentMusqet extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        // Per-paymentline bookkeeping, keyed by line uuid:
        //   { cancelled, saleId, settled, resolve }
        // cancelled/saleId let sendPaymentCancel()/close() stop an in-flight poll loop;
        // settled makes the shared settler run exactly once across the poll loop and an
        // inbound webhook; resolve is the request promise both paths resolve through.
        this.pollState = {};
    }

    /**
     * Require the cashier to confirm the amount before driving the terminal —
     * the terminal then prompts for the rail (card or Lightning) itself.
     */
    get fastPayments() {
        return false;
    }

    async sendPaymentRequest(uuid) {
        await super.sendPaymentRequest(uuid);
        const order = this.pos.getOrder();
        const line = order.getSelectedPaymentline();
        if (!line) {
            return false;
        }

        // A refund order rings a negative payment line. Odoo 19 routes it through this same
        // sendPaymentRequest (no shipped terminal driver implements sendPaymentReversal, and
        // supports_reversals is unused in 19.0), so we mirror pos_stripe: a negative amount
        // means refund the original Musqet sale via type:"refund". Everything downstream —
        // the create→poll engine and the shared settle-back — is identical to a sale.
        const isRefund = line.amount < 0;
        const payload = isRefund
            ? this._refundPreflight(order, line)
            : this._createSalePayload(order, line);
        if (!payload) {
            // Refund preflight refused (no original Musqet sale to reverse, or a non-card
            // rail the terminal can't refund). It has already messaged the cashier and reset
            // the line, so just abort here.
            return false;
        }

        // Register poll state BEFORE the create round-trip. If the cashier cancels while
        // the create RPC is in flight, sendPaymentCancel must find a state object to flag —
        // otherwise the abort is dropped and the just-created sale is left armed on the
        // terminal. saleId is filled in once the create returns. This method owns the
        // state's whole lifecycle (the finally below removes it on every exit path).
        const state = { cancelled: false, saleId: null, settled: false, resolve: null };
        this.pollState[line.uuid] = state;
        try {
            line.setPaymentStatus("waiting");
            // A refund goes through musqet_create_refund, which re-reads the original payment
            // server-side and is the authority on rail + amount (the API enforces neither —
            // Musqet/musqet#2094); the frontend gate in _refundPreflight is only a fast-fail.
            // A sale uses the plain create proxy.
            const response = isRefund
                ? await this._call("musqet_create_refund", [
                      [this.payment_method_id.id],
                      payload,
                      line.uiState?.musqetRefund?.paymentId,
                  ])
                : await this._call("musqet_create_sale", [
                      [this.payment_method_id.id],
                      payload,
                  ]);
            if (!response || response.error) {
                this._showError(this._errorMessage(response));
                line.setPaymentStatus("retry");
                return false;
            }

            const saleId = response.saleId;
            if (!saleId) {
                this._showError(_t("The Musqet terminal did not return a sale reference."));
                line.setPaymentStatus("retry");
                return false;
            }
            state.saleId = saleId;

            // The cashier may have cancelled while the create RPC was in flight. The sale
            // now exists on the terminal, so abort it rather than start polling a sale the
            // cashier already abandoned.
            if (state.cancelled) {
                this._cancelRemoteSale(saleId);
                return false;
            }

            line.setPaymentStatus("waitingCard");
            // The sale settles when EITHER the poll loop or an inbound webhook reports a
            // terminal status — whichever lands first. Both funnel through _finishSale,
            // which settles exactly once, so the slower path is a harmless no-op. Polling is
            // the path the pilot relies on; the webhook only fires on reachable deployments.
            // resolve is captured synchronously here, before _pollSale is fired and before
            // any webhook can match this saleId — so state.resolve is always set by the time
            // either path settles. The optional-chaining on the resolve calls below is purely
            // defensive against an unforeseen reordering, not a real nullable window.
            const settled = new Promise((resolve) => {
                state.resolve = resolve;
            });
            // _pollSale drives `settled` itself and is fire-and-forget so a webhook can
            // settle first; this catch only guards against an unexpected throw leaving the
            // request hung forever (the awaited #4 version surfaced such errors directly).
            this._pollSale(saleId, line, state).catch((error) => {
                logPosMessage("PaymentMusqet", "_pollSale", "poll loop crashed", false, [error]);
                if (!state.settled) {
                    state.settled = true;
                    line.setPaymentStatus("retry");
                    state.resolve?.(false);
                }
            });
            return await settled;
        } finally {
            delete this.pollState[line.uuid];
        }
    }

    async sendPaymentCancel(order, uuid) {
        await super.sendPaymentCancel(order, uuid);
        // Stop the local poll loop so it doesn't resolve a line that's being removed.
        const state = this.pollState[uuid];
        if (state) {
            state.cancelled = true;
            // Best-effort: free the physical terminal. If the create RPC hasn't returned
            // yet there's no saleId to cancel — sendPaymentRequest re-checks the cancelled
            // flag once it does and aborts then. We return true unconditionally so the core
            // payment screen sets the line back to "retry".
            if (state.saleId) {
                this._cancelRemoteSale(state.saleId);
            }
        }
        return true;
    }

    close() {
        super.close();
        // Closing the payment screen abandons any in-flight payment. Stop the poll loop and
        // free the physical terminal for each sale that's already armed (has a saleId) and not
        // yet settled — otherwise it would sit waiting for a card the cashier walked away from.
        for (const state of Object.values(this.pollState)) {
            state.cancelled = true;
            if (state.saleId && !state.settled) {
                this._cancelRemoteSale(state.saleId);
            }
        }
    }

    _cancelRemoteSale(saleId) {
        // Best-effort, fire-and-forget cancel of an armed sale on the terminal. The remote
        // cancel only takes effect while the sale is PENDING/PROCESSING (§4.3) — exactly the
        // in-flight/abandoned case every caller here is in.
        this._call("musqet_cancel_sale", [[this.payment_method_id.id], saleId]);
    }

    // -- create→poll engine ---------------------------------------------------

    _amountInCents(amount) {
        const currency = this.pos.currency;
        // decimal_places is the currency's minor-unit exponent (2 for USD/EUR, 0 for JPY);
        // fall back to 2 so a malformed currency can never yield NaN cents (a wrong charge).
        const decimals = Number.isInteger(currency.decimal_places) ? currency.decimal_places : 2;
        // Integer minor units of the business currency (§4.1) — yields cents/sen without
        // assuming two decimals. Math.round absorbs binary-float drift.
        return Math.round(amount * Math.pow(10, decimals));
    }

    _commonPayload(order) {
        return {
            serial: this.payment_method_id.musqet_terminal_serial,
            currency: this.pos.currency.name,
            // The terminal prompts the rail and handles QR + FX for Bitcoin/Lightning.
            mode: "any",
            // Let the terminal print its own slip; Odoo's receipt stays minimal.
            shouldPrint: true,
            // Stable, unique Odoo order ref — used to reconcile and to keep result handling
            // idempotent if a webhook and a poll both land later (#6).
            reference: order.pos_reference || order.uuid,
            language: (this.pos.user?.lang || "en").split("_")[0],
        };
    }

    _createSalePayload(order, line) {
        return {
            ...this._commonPayload(order),
            amountInCents: this._amountInCents(line.amount),
            type: "sale",
        };
    }

    /**
     * Frontend fast-fail for a refund-order line: validate it and build the create-refund
     * payload, or return null after messaging the cashier. The original Musqet sale id, rail
     * and pos.payment id are carried onto the refund line's uiState by updateRefundPaymentLine
     * (see the pos.payment override). We refund only card-settled sales: the terminal can't
     * reverse Lightning (epic §7), and an unknown rail can't be confirmed as card — both are
     * sent to a manual refund rather than guessed at. This gate is UX only; musqet_create_refund
     * re-reads the original payment server-side and is the authority (the API enforces neither
     * the rail nor the amount — Musqet/musqet#2094).
     */
    _refundPreflight(order, line) {
        const refund = line.uiState?.musqetRefund;
        const originalSaleId = refund?.saleId;
        if (!originalSaleId) {
            this._showError(
                _t(
                    "This order has no original Musqet card payment to refund. Refund it with the same method that took it."
                )
            );
            line.setPaymentStatus("retry");
            return null;
        }
        if (refund.rail !== "card") {
            const body =
                refund.rail === "bitcoin"
                    ? _t(
                          "Lightning payments can't be refunded automatically. Please refund this payment manually in the Musqet app."
                      )
                    : _t(
                          "This Musqet payment can't be refunded automatically. Please refund it manually in the Musqet app."
                      );
            this._showError(body);
            line.setPaymentStatus("retry");
            return null;
        }
        // Record which original sale this refund reverses, on the refund payment itself, so it
        // survives sync for reconciliation and cumulative-refund accounting (#9). The API keeps
        // no sale↔refund linkage of its own (Musqet/musqet#2094), so this is the link.
        line.musqet_refund_of = originalSaleId;
        return {
            ...this._commonPayload(order),
            // Pin the rail to card. The terminal can't reverse Lightning, and the API does NOT
            // reject mode:"any" on a refund (Musqet/musqet#2094) — so "any" could let the device
            // attempt a rail it can't refund. originalSaleId is intentionally not sent: the API
            // strips unknown keys and keeps no sale↔refund linkage of its own (#2094); the
            // original-sale link lives in Odoo, and the gate above is what enforces card-only.
            mode: "card",
            // Positive magnitude of the refund; type:"refund" denotes the direction.
            amountInCents: this._amountInCents(Math.abs(line.amount)),
            type: "refund",
        };
    }

    /**
     * Poll musqet_get_sale until a terminal status or the overall timeout, settling the line
     * through _finishSale (which resolves sendPaymentRequest's promise). Fire-and-forget: the
     * result is delivered via ``state.resolve``, not a return value, so an inbound webhook can
     * settle the same line first. ``state`` is owned by sendPaymentRequest (which registered
     * and will remove it) so a cancel — or a webhook that already settled — is observed here.
     */
    async _pollSale(saleId, line, state) {
        const start = Date.now();
        while (!state.cancelled && !state.settled && Date.now() - start < POLL_TIMEOUT_MS) {
            // Poll first, then sleep, so a sale the terminal resolves immediately settles
            // without waiting a full interval up front.
            const sale = await this._call("musqet_get_sale", [
                [this.payment_method_id.id],
                saleId,
            ]);
            if (state.settled) {
                // A webhook settled this line while the poll was in flight — nothing to do.
                return;
            }
            if (state.cancelled) {
                // Cancelled during the poll round-trip — don't act on a stale result.
                break;
            }
            // A transient transport blip (proxy {error}) or RPC failure shouldn't kill the
            // sale — keep polling until the overall timeout. Persistent-failure hardening
            // is issue #8.
            if (sale && !sale.error) {
                this._finishSale(line, sale, state);
                if (state.settled) {
                    return;
                }
                // PENDING / PROCESSING / anything unrecognised → keep polling.
            }
            await this._sleep(POLL_INTERVAL_MS);
        }
        if (state.settled) {
            return;
        }
        if (state.cancelled) {
            // Resolve as non-success; pay()'s handlePaymentResponse(false) sets the line
            // back to "retry". The remote terminal cancel was already fired by the cancel path.
            state.resolve?.(false);
            return;
        }
        // Overall budget exhausted.
        state.settled = true;
        this._showError(this._statusErrorMessage("TIMED_OUT"));
        line.setPaymentStatus("retry");
        state.resolve?.(false);
    }

    /**
     * Apply a terminal sale result to the line exactly once. Both the poll loop and an
     * inbound webhook funnel through here; ``state.settled`` makes the second arrival a
     * no-op, so a sale is never double-settled when a webhook and a poll both land
     * (the idempotency requirement of #6). Non-terminal statuses are ignored (keep waiting).
     */
    _finishSale(line, sale, state) {
        // Settle at most once (settled) and never settle a line the cashier already
        // cancelled (cancelled). The poll loop observes cancellation on its own wake, but a
        // webhook can funnel a success in here during the cancel→cleanup window — guard it
        // here too so the shared funnel can't break the cancel invariant the other paths hold.
        if (state.settled || state.cancelled) {
            return;
        }
        const status = sale.status;
        if (status === SUCCESS_STATUS) {
            state.settled = true;
            // transaction_id = saleId for reconciliation against Musqet.
            line.transaction_id = sale.saleId || state.saleId;
            // Record which rail the terminal settled on, straight from the top-level field
            // ("card" | "bitcoin") — never inferred from metadata. Persisted on pos.payment so
            // it survives to a later-session refund, which the refund preflight gates on (a
            // refund is only auto-driven for a card-settled original; see _refundPreflight).
            line.musqet_rail = sale.rail || false;
            line.setReceiptInfo(this._receiptInfo(sale));
            line.setPaymentStatus("done");
            state.resolve?.(true);
        } else if (FAILURE_STATUSES.includes(status)) {
            state.settled = true;
            this._showError(this._statusErrorMessage(status));
            line.setPaymentStatus("retry");
            state.resolve?.(false);
        }
        // PENDING / PROCESSING / anything unrecognised → not terminal, leave the line waiting.
    }

    /**
     * Handle an inbound webhook (push path). Invoked by the PosStore websocket patch once the
     * backend controller has verified a signature and buffered the result. Pulls the buffer
     * and settles the pending line. Only fires on publicly-reachable deployments; the pilot
     * settles via the poll loop and never reaches here.
     */
    async handleMusqetStatusResponse() {
        const sale = await this._call("musqet_get_latest_status", [
            [this.payment_method_id.id],
        ]);
        if (!sale || sale.error) {
            // Nothing buffered, or a transient RPC failure — the poll loop is the backstop.
            return;
        }
        const line = this.pos.getPendingPaymentLine("musqet");
        if (!line) {
            return;
        }
        const state = this.pollState[line.uuid];
        if (state) {
            // Only settle if this buffered result is for the sale this line is waiting on.
            // The contract guarantees a webhook carries saleId, so require an exact match — a
            // missing/foreign saleId, or the window before our own create has returned
            // (state.saleId still null), means we can't confirm it's ours, so we leave it to
            // the poll loop rather than settle on a guess.
            if (sale.saleId !== state.saleId) {
                return;
            }
            this._finishSale(line, sale, state);
            return;
        }
        // The in-memory poll state was lost (e.g. the page was refreshed mid-payment). We no
        // longer have our saleId, so match by order reference and refuse to settle on "can't
        // confirm" — never settle a line the result can't be positively tied to.
        const order = line.pos_order_id;
        const reference = order?.pos_reference || order?.uuid;
        if (!reference || sale.reference !== reference) {
            return;
        }
        const success = sale.status === SUCCESS_STATUS;
        if (success) {
            line.transaction_id = sale.saleId || line.transaction_id;
            line.musqet_rail = sale.rail || false;
            line.setReceiptInfo(this._receiptInfo(sale));
        } else if (!FAILURE_STATUSES.includes(sale.status)) {
            // Non-terminal status buffered — nothing to settle yet.
            return;
        } else {
            this._showError(this._statusErrorMessage(sale.status));
        }
        line.handlePaymentResponse(success);
    }

    _receiptInfo(sale) {
        // Minimal by design: the terminal prints the card slip. Use the top-level rail
        // (never the opaque metadata.card blob), mapped to a friendly label rather than
        // the raw enum token.
        const labels = { card: _t("Card"), bitcoin: _t("Lightning") };
        const label = labels[sale.rail];
        let info = label ? _t("Paid via Musqet (%s)", label) : _t("Paid via Musqet");
        // Supplementary Lightning detail: the sats the terminal settled, if it reported
        // any. Gate on the top-level rail and read only the bitcoin metadata block — the
        // terminal still prints the authoritative slip, so this is a convenience line.
        if (sale.rail === "bitcoin") {
            const sats = Number(sale.metadata?.bitcoin?.satsAmount);
            if (Number.isFinite(sats) && sats > 0) {
                // formatInteger groups thousands using the POS session's Odoo locale (and
                // rounds to a whole unit), so the sats line matches the rest of the
                // receipt's number formatting rather than the raw JS runtime locale.
                info += "\n" + _t("%s sats", formatInteger(sats));
            }
        }
        return info;
    }

    // -- helpers --------------------------------------------------------------

    /**
     * Call a backend proxy method. Returns the proxy's parsed result (which may itself be
     * a ``{error}`` dict), or a synthetic ``{error}`` on an Odoo RPC/connection failure —
     * so callers only ever branch on the result, never on a thrown exception.
     */
    async _call(method, args) {
        try {
            return await this.pos.data.call("pos.payment.method", method, args);
        } catch (error) {
            logPosMessage("PaymentMusqet", method, "Odoo RPC call failed", false, [error]);
            return {
                error: {
                    message: _t(
                        "Could not reach the Odoo server. Please check the connection and try again."
                    ),
                },
            };
        }
    }

    _sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    _errorMessage(response) {
        const error = response && response.error;
        if (error && error.message) {
            return error.message;
        }
        return _t("Could not start the Musqet payment. Please try again.");
    }

    _statusErrorMessage(status) {
        const messages = {
            ERROR: _t("The Musqet payment failed. Please try again."),
            CANCELLED: _t("The Musqet payment was cancelled."),
            CANCELLED_REMOTE: _t("The Musqet payment was cancelled on the terminal."),
            TIMED_OUT: _t("The Musqet payment timed out. Please try again."),
        };
        return messages[status] || _t("The Musqet payment did not complete.");
    }

    _showError(msg, title) {
        this.env.services.dialog.add(AlertDialog, {
            title: title || _t("Musqet Error"),
            body: msg,
        });
    }
}

register_payment_method("musqet", PaymentMusqet);
