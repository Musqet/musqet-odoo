# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo import models

from .musqet_common import MUSQET_TERMINAL


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def write(self, vals):
        result = super().write(vals)
        # A Musqet line is checked once its order is paid (and cleared if the order is
        # cancelled), and must be on the order its sale was sent for, by either form of the
        # order's reference; see pos.payment.musqet_settle_check.
        if {'state', 'pos_reference', 'uuid'} & vals.keys():
            self.payment_ids.filtered(
                lambda payment: payment.payment_method_id.use_payment_terminal == MUSQET_TERMINAL,
            )._musqet_check_settles()
        return result
