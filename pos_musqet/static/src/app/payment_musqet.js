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
// Overall budget, aligned to the terminal's ~5-minute sale expiry. Past this we cancel the
// sale on the terminal, and let the cashier retry once Musqet confirms it ended (_tryGiveUp).
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

// Terminal statuses (canonical contract, issue #1). COMPLETE is the only success.
const SUCCESS_STATUS = "COMPLETE";
const FAILURE_STATUSES = ["ERROR", "CANCELLED", "CANCELLED_REMOTE", "TIMED_OUT"];

// Resilience (issue #8). The poll loop tolerates transient blips: a single failed
// musqet_get_sale (an Odoo RPC dropped, or the proxy briefly returning {error}) must not
// kill a card payment that may still be completing at the terminal. But a *sustained* run
// of failed reads means we've lost contact with Odoo/the proxy and are polling blind, so
// after this many CONSECUTIVE failures we try to give up rather than spin to the overall
// timeout (mirrors pos_adyen's _handleOdooConnectionFailure). A single clean poll resets the
// count. At POLL_INTERVAL_MS that's ~12s of sustained failure. Giving up cancels the sale on
// the terminal first, as the timeout does, and keeps waiting instead if Musqet answers but
// can't confirm the sale ended (_tryGiveUp).
const MAX_POLL_CONNECTION_FAILURES = 8;

// Shown when Musqet can't confirm a cancel, so the sale may still be paid. A function so the
// translation is looked up when shown, not at module load.
const CANNOT_CANCEL_MESSAGE = () =>
    _t(
        "The Musqet payment could not be cancelled. Cancel it on the terminal, or wait for the customer to finish."
    );

// §4.7: at most one sale/refund in flight per physical terminal. The Musqet API itself
// rejects a second command on the same (business, serial) with 409 CONFLICT while one is in
// flight (Musqet/musqet#2094), and the §2 serial-uniqueness constraint stops two payment
// methods sharing a serial — but we guard client-side first, keyed by serial, so the cashier
// gets a clear message instead of a raw API error. Module-level so the guard spans every
// PaymentMusqet instance, not just one payment method.
const inFlightSerials = new Set();

export class PaymentMusqet extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        // Per-paymentline bookkeeping, keyed by line uuid:
        //   { cancelled, cancelRequested, saleId, created, ending, waitStart,
        //     connectionFailures, settled, resolve }
        // cancelled/saleId let sendPaymentCancel()/close() stop an in-flight poll loop;
        // cancelRequested holds off settling while a cancel finds out how the sale ended;
        // created resolves to the saleId (or null) once the create round-trip returns;
        // ending is the cancel-and-read in flight (_endSaleOnce), shared by a cancel and a give-up;
        // waitStart/connectionFailures are the poll loop's give-up counters (_pollSale);
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

        // §4.7: refuse to fire a second command at a terminal that's already taking a
        // payment. The terminal's own 409 CONFLICT is the backstop, but guarding here gives
        // the cashier a clear message up front instead of a failed create round-trip.
        const serial = this.payment_method_id.musqet_terminal_serial;
        if (serial && inFlightSerials.has(serial)) {
            this._showError(
                _t(
                    "This Musqet terminal is already taking a payment. Wait for it to finish before starting another."
                )
            );
            line.setPaymentStatus("retry");
            return false;
        }

        // Register poll state BEFORE the create round-trip. If the cashier cancels while
        // the create RPC is in flight, sendPaymentCancel must find a state object to flag —
        // otherwise the abort is dropped and the just-created sale is left armed on the
        // terminal. saleId is filled in once the create returns. This method owns the
        // state's whole lifecycle (the finally below removes it, and releases the terminal
        // lock, on every exit path). The lock is held for the whole in-flight window because
        // the try body awaits `settled`, which resolves only once the sale settles, times
        // out, or is cancelled.
        let markCreated;
        const state = {
            cancelled: false,
            cancelRequested: false,
            saleId: null,
            created: new Promise((resolve) => {
                markCreated = resolve;
            }),
            ending: null,
            waitStart: null,
            connectionFailures: 0,
            settled: false,
            resolve: null,
        };
        this.pollState[line.uuid] = state;
        if (serial) {
            inFlightSerials.add(serial);
        }
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
                      this._lineSaleIds(order, line),
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
            // A cancel sent while the create RPC was in flight has waited for this, and now
            // cancels the sale; the poll loop below holds off until it knows the outcome.
            markCreated(saleId);

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
            // Nothing was armed if the create failed: let a waiting cancel through.
            markCreated(null);
            delete this.pollState[line.uuid];
            if (serial) {
                inFlightSerials.delete(serial);
            }
        }
    }

    /**
     * Cancel the line's sale on the terminal, and say whether it ended. True: it can no
     * longer be paid, and the core payment screen puts the line back to "retry" (or removes
     * it). False: it completed first, or Musqet couldn't confirm the cancel, so the customer
     * may still pay it; the line keeps waiting and the poll loop settles it. Never answering
     * true while a sale can still be paid is what stops a retry from charging twice.
     */
    async sendPaymentCancel(order, uuid) {
        await super.sendPaymentCancel(order, uuid);
        const state = this.pollState[uuid];
        if (!state) {
            return true;
        }
        // The core screen sets the line's status from our answer, after we return, so the
        // poll loop must not settle the line meanwhile: a "done" set now would be overwritten.
        state.cancelRequested = true;
        let outcome;
        try {
            // A cancel during the create round-trip waits for its saleId.
            if (!(await state.created)) {
                // The create failed, so nothing was armed on the terminal.
                return true;
            }
            outcome = await this._endSaleOnce(state);
            if (outcome.ended) {
                state.cancelled = true;
                return true;
            }
        } finally {
            state.cancelRequested = false;
        }
        if (outcome.sale?.status !== SUCCESS_STATUS) {
            this._showError(CANNOT_CANCEL_MESSAGE());
        }
        this._restartWait(state);
        // Keep waiting on the sale. (The core screen does this itself after a cancel button,
        // but leaves a deleted line "waitingCancel".) Not if it settled before the cancel
        // began: its status is already final.
        if (!state.settled) {
            order.payment_ids.find((line) => line.uuid === uuid)?.setPaymentStatus("waitingCard");
        }
        return false;
    }

    close() {
        super.close();
        // Closing the POS abandons any in-flight payment. Stop the poll loop and free the
        // physical terminal for each sale not yet settled, armed already or once its create
        // returns its saleId (state.created) — otherwise it would sit waiting for a card the
        // cashier walked away from. Fire-and-forget: the POS is going away, so nothing can
        // wait for the answer. If the cancel fails and the customer pays anyway, a retry of
        // the order resumes that sale rather than charging again (musqet_create_sale).
        for (const state of Object.values(this.pollState)) {
            state.cancelled = true;
            if (!state.settled) {
                state.created.then((saleId) => saleId && this._cancelSale(saleId));
            }
        }
    }

    /**
     * Cancel the line's armed sale and find out how it ended, once: a cancel and a give-up
     * running at the same time share the one round-trip. Resolves to ``{ ended, answered, sale }``
     * (see _cancelOrReadSale).
     */
    _endSaleOnce(state) {
        if (!state.ending) {
            state.ending = this._cancelOrReadSale(state.saleId).finally(() => {
                state.ending = null;
            });
        }
        return state.ending;
    }

    /**
     * Cancel sale ``saleId`` on the terminal, and read its status if the cancel failed.
     * Resolves to ``{ ended, answered, sale }``: ``ended`` once Musqet says the sale can no
     * longer be paid (it accepted the cancel, or reports a failure status); ``answered`` if
     * Musqet answered either call (so contact is back); ``sale`` is the status read, if one
     * was made and succeeded. A sale that completed first has not ended: the
     * customer paid, and the line must settle with it.
     */
    async _cancelOrReadSale(saleId) {
        const cancel = await this._cancelSale(saleId);
        const answered = Boolean(cancel && !cancel.error);
        if (answered && FAILURE_STATUSES.includes(cancel.status)) {
            return { ended: true, answered, sale: null };
        }
        // Musqet refuses to cancel a sale that already ended (409 CONFLICT), and a failed
        // call says nothing either way: read where the sale stands.
        const sale = await this._readSale(saleId);
        if (!sale || sale.error) {
            return { ended: false, answered, sale: null };
        }
        return { ended: FAILURE_STATUSES.includes(sale.status), answered: true, sale };
    }

    /** Read sale ``saleId``'s status from Musqet, through the server (musqet_get_sale). */
    _readSale(saleId) {
        return this._call("musqet_get_sale", [[this.payment_method_id.id], saleId]);
    }

    /** Ask Musqet to cancel sale ``saleId``, through the server (musqet_cancel_sale). */
    _cancelSale(saleId) {
        return this._call("musqet_cancel_sale", [[this.payment_method_id.id], saleId]);
    }

    /**
     * The saleIds already on ``order``'s payment lines other than ``line``. Sent with a new
     * sale so the server never resumes one of them onto this line (musqet_create_sale).
     */
    _lineSaleIds(order, line) {
        return order.payment_ids
            .filter((other) => other !== line && other.transaction_id)
            .map((other) => other.transaction_id);
    }

    // -- create→poll engine ---------------------------------------------------

    _amountInCents(amount) {
        const currency = this.pos.currency;
        // decimal_places is the currency's minor-unit exponent (2 for USD/EUR, 0 for JPY);
        // fall back to 2 so a malformed currency can never yield NaN cents (a wrong charge).
        const decimals = Number.isInteger(currency.decimal_places) ? currency.decimal_places : 2;
        // Integer minor units of the company currency (§4.1) — yields cents/sen without
        // assuming two decimals. Math.round absorbs binary-float drift.
        return Math.round(amount * Math.pow(10, decimals));
    }

    _commonPayload(order) {
        // serial and type are not sent: the server sets both from its own records, whatever
        // the request says (musqet_create_sale / musqet_create_refund).
        return {
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
        return {
            ...this._commonPayload(order),
            // Pin the rail to card. The terminal can't reverse Lightning, and the API does NOT
            // reject mode:"any" on a refund (Musqet/musqet#2094) — so "any" could let the device
            // attempt a rail it can't refund. originalSaleId is intentionally not sent: the API
            // strips unknown keys and keeps no sale↔refund linkage of its own (#2094); the
            // original-sale link lives in Odoo, and the gate above is what enforces card-only.
            mode: "card",
            // Positive magnitude of the refund; the server sends type:"refund" for the direction.
            amountInCents: this._amountInCents(Math.abs(line.amount)),
        };
    }

    /**
     * Poll musqet_get_sale until a terminal status, or until the wait can be given up
     * (_tryGiveUp: out of time or contact, and the sale ended or can't be asked about),
     * settling the line through _finishSale (which resolves sendPaymentRequest's promise). Fire-and-forget: the
     * result is delivered via ``state.resolve``, not a return value, so an inbound webhook can
     * settle the same line first. ``state`` is owned by sendPaymentRequest (which registered
     * and will remove it) so a cancel — or a webhook that already settled — is observed here.
     */
    async _pollSale(saleId, line, state) {
        // state.waitStart is when the current wait began, and state.connectionFailures counts
        // consecutive failed status reads (any clean poll resets it): a sustained run means
        // we've lost contact with Odoo/the proxy (issue #8 — see MAX_POLL_CONNECTION_FAILURES)
        // and should stop rather than poll blind. A cancel or give-up Musqet couldn't confirm
        // starts a new wait (_restartWait), so the loop doesn't cancel again straight away.
        this._restartWait(state);
        while (!state.cancelled && !state.settled) {
            if (state.cancelRequested) {
                // A cancel is finding out how the sale ended; the core screen sets the line's
                // status from its answer. Wait for it: it either ends the sale (cancelled) or
                // leaves it to this loop.
                await this._sleep(POLL_INTERVAL_MS);
                continue;
            }
            const timedOut = Date.now() - state.waitStart >= POLL_TIMEOUT_MS;
            if (timedOut || state.connectionFailures >= MAX_POLL_CONNECTION_FAILURES) {
                if (await this._tryGiveUp(line, state, timedOut)) {
                    return;
                }
                continue;
            }
            // Poll first, then sleep, so a sale the terminal resolves immediately settles
            // without waiting a full interval up front.
            const sale = await this._readSale(saleId);
            if (state.settled) {
                // A webhook settled this line while the poll was in flight — nothing to do.
                return;
            }
            if (state.cancelled) {
                // Cancelled during the poll round-trip — don't act on a stale result.
                break;
            }
            // A single transient transport blip (proxy {error}) or RPC failure shouldn't kill
            // the sale — keep polling so a card payment that's still completing settles.
            if (sale && !sale.error) {
                state.connectionFailures = 0;
                this._finishSale(line, sale, state);
                if (state.settled) {
                    return;
                }
                // PENDING / PROCESSING / anything unrecognised, or a cancel began during the
                // round-trip → keep polling.
            } else {
                state.connectionFailures++;
            }
            await this._sleep(POLL_INTERVAL_MS);
        }
        if (state.cancelled && !state.settled) {
            // Resolve as non-success; pay()'s handlePaymentResponse(false) sets the line
            // back to "retry". The cancel path already ended the sale on the terminal.
            state.resolve?.(false);
        }
    }

    /**
     * Start a new wait on the line's sale: the give-up counters start again, so the poll
     * loop doesn't cancel again straight after the cashier was told to wait.
     */
    _restartWait(state) {
        state.waitStart = Date.now();
        state.connectionFailures = 0;
    }

    /**
     * Stop waiting on the line's sale, out of time (``timedOut``) or after losing contact,
     * and return whether the line was settled (else the poll loop carries on).
     *
     * The sale is still armed on the terminal, so it is cancelled first: otherwise the
     * customer could pay a sale the till has stopped waiting for, and pay again by another
     * means. The line is released only once Musqet confirms the sale ended. If the customer
     * paid first, the line settles as paid. If Musqet can't confirm, the till keeps waiting
     * (a new wait, so the loop doesn't cancel again at once), as a refused cashier cancel
     * does; Musqet times the sale out in the end. Except when contact was lost and the status
     * read failed as well: the till can't wait on a server it can't reach, so it releases the
     * line, warning that the sale may still be live. An answer from Musqet to the cancel or
     * the read shows contact is back, and the till keeps waiting.
     */
    async _tryGiveUp(line, state, timedOut) {
        const { ended, answered, sale } = await this._endSaleOnce(state);
        if (state.cancelRequested || state.cancelled || state.settled) {
            // The cashier cancelled meanwhile, or a webhook settled the line: that decides,
            // and the poll loop picks it up.
            return false;
        }
        if (sale?.status === SUCCESS_STATUS) {
            // The customer paid before the cancel landed.
            this._finishSale(line, sale, state);
            return true;
        }
        if (!ended && (timedOut || answered)) {
            // Not confirmed ended, and either out of time or Musqet just answered (so contact
            // is back): the sale may still be paid, so keep waiting on it.
            this._showError(CANNOT_CANCEL_MESSAGE());
            this._restartWait(state);
            return false;
        }
        let message;
        if (!ended) {
            // Contact lost, and neither call was answered: nothing more the till can find out.
            message = _t(
                "Lost contact with Odoo or Musqet, and the Musqet payment could not be cancelled: it may still be live on the terminal. Cancel it there before taking another payment for this order."
            );
        } else if (timedOut) {
            // Say how it ended, if the read told us (the terminal may have failed it first).
            message = this._statusErrorMessage(sale?.status || "TIMED_OUT");
        } else {
            message = _t(
                "Contact with Odoo or Musqet dropped while waiting for the Musqet terminal, so the payment was cancelled. Please try again."
            );
        }
        state.settled = true;
        this._showError(message);
        line.setPaymentStatus("retry");
        state.resolve?.(false);
        return true;
    }

    /**
     * Apply a terminal sale result to the line exactly once. Both the poll loop and an
     * inbound webhook funnel through here; ``state.settled`` makes the second arrival a
     * no-op, so a sale is never double-settled when a webhook and a poll both land
     * (the idempotency requirement of #6). Non-terminal statuses are ignored (keep waiting).
     */
    _finishSale(line, sale, state) {
        // Settle at most once (settled) and never settle a line the cashier already
        // cancelled (cancelled), or is cancelling (cancelRequested: the core screen sets the
        // line's status from the cancel's answer, after it). The poll loop observes both on
        // its own wake, but a webhook can funnel a result in here at any time, so guard it
        // here too: the shared funnel can't break the cancel invariant the other paths hold.
        if (state.settled || state.cancelled || state.cancelRequested) {
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
            // On a settled refund, record which original sale it reversed — only once the
            // refund actually completes, so an abandoned/failed attempt never leaves a stale
            // link. The API keeps no sale↔refund linkage of its own (Musqet/musqet#2094); this
            // is what cumulative-refund accounting (#9) reconciles against. Null on a sale.
            const refundOf = line.uiState?.musqetRefund?.saleId;
            if (refundOf) {
                line.musqet_refund_of = refundOf;
            }
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
            // The server buffers Musqet's status-read reply with its saleId set, so require an
            // exact match — a missing/foreign saleId, or the window before our own create has
            // returned (state.saleId still null), means we can't confirm it's ours, so we
            // leave it to the poll loop rather than settle on a guess.
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
