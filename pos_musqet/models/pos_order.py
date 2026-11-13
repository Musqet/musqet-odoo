# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo import models


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def write(self, vals):
        result = super().write(vals)
        # A Musqet line is checked once its order is paid (and cleared if the order is
        # cancelled); see pos.payment.musqet_settle_check.
        if 'state' in vals:
            self.payment_ids._musqet_check_settles()
        return result
