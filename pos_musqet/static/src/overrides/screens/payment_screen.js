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
        if (paymentMethod.use_payment_terminal === "musqet" && this.isRefundOrder) {
            const refundedOrder = this.currentOrder.lines[0]?.refunded_orderline_id?.order_id;
            const amountDue = Math.abs(this.currentOrder.remainingDue);
            // Match a Musqet line on the original order big enough to cover this refund. amount
            // is positive on the original sale; >= so a partial refund still matches its sale.
            const matchedPaymentLine = refundedOrder?.payment_ids.find(
                (line) =>
                    line.payment_method_id.use_payment_terminal === "musqet" &&
                    line.amount >= amountDue
            );
            if (matchedPaymentLine) {
                const added = await super.addNewPaymentLine(paymentMethod);
                if (added) {
                    this.paymentLines.at(-1).updateRefundPaymentLine(matchedPaymentLine);
                }
                return added;
            }
        }
        return await super.addNewPaymentLine(paymentMethod);
    },
});
