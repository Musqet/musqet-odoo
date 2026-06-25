// Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/services/pos_store";

// Push path for publicly-reachable deployments. When the backend webhook controller buffers
// a verified sale result it pings this channel; we hand off to the Musqet terminal handler to
// settle the pending line. The pilot is behind NAT and settles via polling instead, so this
// channel simply never fires there — the poll loop remains the source of truth.
patch(PosStore.prototype, {
    async setup() {
        await super.setup(...arguments);
        this.data.connectWebSocket("MUSQET_LATEST_RESPONSE", () => {
            const pendingLine = this.getPendingPaymentLine("musqet");
            if (pendingLine) {
                pendingLine.payment_method_id.payment_terminal.handleMusqetStatusResponse();
            }
        });
    },
});
