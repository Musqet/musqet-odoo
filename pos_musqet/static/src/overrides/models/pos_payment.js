// Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { patch } from "@web/core/utils/patch";

// Refund-order support (issue #7), mirroring pos_stripe's stripePaymentIdToRefund. When a
// cashier rings a refund order, the payment screen matches the new Musqet line to the original
// order's Musqet payment line and calls updateRefundPaymentLine — which stashes the original
// sale id and the rail it settled on here, on the refund line's transient uiState. The Musqet
// driver reads them to drive the terminal refund (type:"refund") and to refuse Lightning
// refunds, which the terminal can't reverse (epic §7).
patch(PosPayment.prototype, {
    setup() {
        super.setup(...arguments);
        // uiState is transient (never serialized), so this only needs initialising, not a
        // stored field. Merge rather than overwrite — other modules patch uiState too.
        this.uiState = {
            ...(this.uiState ?? {}),
            musqetRefund: { saleId: null, rail: null, paymentId: null },
        };
    },

    updateRefundPaymentLine(refundedPaymentLine) {
        super.updateRefundPaymentLine(refundedPaymentLine);
        // transaction_id on a settled Musqet line is the Musqet saleId; musqet_rail is the
        // rail it settled on (both set in _finishSale); id is the original pos.payment's Odoo
        // id. Default to null so a missing field surfaces in the driver as "can't confirm a
        // card sale" rather than a bad refund. saleId/rail drive the frontend fast-fail; the
        // backend re-reads the payment by paymentId and is the actual refund authority.
        this.uiState.musqetRefund = {
            saleId: refundedPaymentLine?.transaction_id || null,
            rail: refundedPaymentLine?.musqet_rail || null,
            paymentId: refundedPaymentLine?.id || null,
        };
    },
});
