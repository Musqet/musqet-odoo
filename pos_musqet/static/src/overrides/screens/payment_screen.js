// Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { patch } from "@web/core/utils/patch";

// Refund-order support (issue #7), mirroring pos_stripe. Core does not wire
// updateRefundPaymentLine generically — each terminal addon matches its own lines. On a
// refund order, find the original order's Musqet payment line and hand it to the new refund
// line so the driver knows which sale to reverse (and on which rail). Without this,
// updateRefundPaymentLine never fires and the refund has no original sale to reverse.
patch(PaymentScreen.prototype, {
    async addNewPaymentLine(paymentMethod) {
        let matchedPaymentLine = null;
        if (paymentMethod.use_payment_terminal === "musqet" && this.isRefundOrder) {
            const refundedOrder = this.currentOrder.lines[0]?.refunded_orderline_id?.order_id;
            const amountDue = Math.abs(this.currentOrder.remainingDue);
            // Candidate original sales: settled Musqet lines (transaction_id present) big enough
            // to cover this refund. amount is positive on a sale, so `>= amountDue` also excludes
            // refund lines (negative) — a refund-of-refund finds no candidate and bails.
            const candidates = (refundedOrder?.payment_ids ?? []).filter(
                (line) =>
                    line.payment_method_id.use_payment_terminal === "musqet" &&
                    line.transaction_id &&
                    line.amount >= amountDue
            );
            // Prefer a card sale (the only rail we can auto-refund). Fall back to any Musqet
            // sale so a Lightning-only original still stashes its rail and the driver shows the
            // precise "refund manually" message instead of a generic "no original sale" error.
            matchedPaymentLine =
                candidates.find((line) => line.musqet_rail === "card") || candidates[0] || null;
        }
        const added = await super.addNewPaymentLine(paymentMethod);
        if (added && matchedPaymentLine) {
            this.paymentLines.at(-1).updateRefundPaymentLine(matchedPaymentLine);
        }
        return added;
    },
});
