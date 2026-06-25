// Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import { _t } from "@web/core/l10n/translation";
import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { register_payment_method } from "@point_of_sale/app/services/pos_store";
import { logPosMessage } from "@point_of_sale/app/utils/pretty_console_log";

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
        // Per-paymentline poll bookkeeping, keyed by line uuid:
        //   { saleId, cancelled }
        // so sendPaymentCancel()/close() can stop an in-flight poll loop.
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
        // Refunds/reversals are out of scope here (issue #7); a Musqet sale is a debit.
        if (line.amount < 0) {
            this._showError(_t("Musqet cannot process a negative amount."));
            line.setPaymentStatus("retry");
            return false;
        }

        // Register poll state BEFORE the create round-trip. If the cashier cancels while
        // the create RPC is in flight, sendPaymentCancel must find a state object to flag —
        // otherwise the abort is dropped and the just-created sale is left armed on the
        // terminal. saleId is filled in once the create returns. This method owns the
        // state's whole lifecycle (the finally below removes it on every exit path).
        const state = { cancelled: false, saleId: null };
        this.pollState[line.uuid] = state;
        try {
            line.setPaymentStatus("waiting");
            const response = await this._call("musqet_create_sale", [
                [this.payment_method_id.id],
                this._createSalePayload(order, line),
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
                this._call("musqet_cancel_sale", [[this.payment_method_id.id], saleId]);
                return false;
            }

            line.setPaymentStatus("waitingCard");
            return await this._pollSale(saleId, line, state);
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
            // flag once it does and aborts then. The full cancel UX (surfacing failures,
            // reversals) is issue #7; here we just fire and forget.
            if (state.saleId) {
                this._call("musqet_cancel_sale", [
                    [this.payment_method_id.id],
                    state.saleId,
                ]);
            }
        }
        return true;
    }

    close() {
        super.close();
        // Closing the payment screen abandons any in-flight polls.
        for (const state of Object.values(this.pollState)) {
            state.cancelled = true;
        }
    }

    // -- create→poll engine ---------------------------------------------------

    _createSalePayload(order, line) {
        const currency = this.pos.currency;
        // decimal_places is the currency's minor-unit exponent (2 for USD/EUR, 0 for JPY);
        // fall back to 2 so a malformed currency can never yield NaN cents (a wrong charge).
        const decimals = Number.isInteger(currency.decimal_places) ? currency.decimal_places : 2;
        return {
            serial: this.payment_method_id.musqet_terminal_serial,
            // Integer minor units of the business currency (§4.1) — yields cents/sen
            // without assuming two decimals. Math.round absorbs binary-float drift.
            amountInCents: Math.round(line.amount * Math.pow(10, decimals)),
            currency: currency.name,
            // The terminal prompts the rail and handles QR + FX for Bitcoin/Lightning.
            mode: "any",
            type: "sale",
            // Let the terminal print its own card slip; Odoo's receipt stays minimal.
            shouldPrint: true,
            // Stable, unique Odoo order ref — used to reconcile and to keep result
            // handling idempotent if a webhook and a poll both land later (#6).
            reference: order.pos_reference || order.uuid,
            language: (this.pos.user?.lang || "en").split("_")[0],
        };
    }

    /**
     * Poll musqet_get_sale until a terminal status or the overall timeout. Resolves the
     * paymentline standalone — no webhook required. Returns true on COMPLETE, false on a
     * failure status / timeout / cancel. ``state`` is owned by sendPaymentRequest (which
     * registered and will remove it) so a cancel landing mid-poll is observed here.
     */
    async _pollSale(saleId, line, state) {
        const start = Date.now();
        while (!state.cancelled && Date.now() - start < POLL_TIMEOUT_MS) {
            // Poll first, then sleep, so a sale the terminal resolves immediately settles
            // without waiting a full interval up front.
            const sale = await this._call("musqet_get_sale", [
                [this.payment_method_id.id],
                saleId,
            ]);
            if (state.cancelled) {
                // Cancelled during the poll round-trip — don't act on a stale result.
                break;
            }
            // A transient transport blip (proxy {error}) or RPC failure shouldn't kill the
            // sale — keep polling until the overall timeout. Persistent-failure hardening
            // is issue #8.
            if (sale && !sale.error) {
                const status = sale.status;
                if (status === SUCCESS_STATUS) {
                    // transaction_id = saleId for reconciliation against Musqet.
                    line.transaction_id = sale.saleId || saleId;
                    // Record which rail the terminal settled on, straight from the
                    // top-level field ("card" | "bitcoin") — never inferred from metadata.
                    // Persisted on pos.payment so it survives to a later-session refund,
                    // which #7 gates on (card vs Lightning reverse differently).
                    line.musqet_rail = sale.rail || false;
                    line.setReceiptInfo(this._receiptInfo(sale));
                    line.setPaymentStatus("done");
                    return true;
                }
                if (FAILURE_STATUSES.includes(status)) {
                    this._showError(this._statusErrorMessage(status));
                    line.setPaymentStatus("retry");
                    return false;
                }
                // PENDING / PROCESSING / anything unrecognised → keep polling.
            }
            await this._sleep(POLL_INTERVAL_MS);
        }
        if (state.cancelled) {
            return false;
        }
        this._showError(this._statusErrorMessage("TIMED_OUT"));
        line.setPaymentStatus("retry");
        return false;
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
                info += "\n" + _t("%s sats", this._formatSats(sats));
            }
        }
        return info;
    }

    _formatSats(sats) {
        // Group thousands for readability (1234567 -> "1,234,567"). Sats are whole units;
        // round defensively in case the API ever sends a fractional value.
        return Math.round(sats).toLocaleString();
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
