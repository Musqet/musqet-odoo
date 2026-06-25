# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
from odoo import fields, models, api, _
from odoo.exceptions import ValidationError

DEFAULT_MUSQET_BASE_URL = 'https://api.musqet.tech/api/v1'


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [('musqet', 'Musqet')]

    # Musqet
    # The API key authenticates the Odoo backend to the Musqet terminal API. It is
    # read server-side only (self.sudo().musqet_api_key) and is NEVER surfaced to the
    # browser / POS JS. groups= restricts it to ERP managers in the form view.
    musqet_api_key = fields.Char(
        string="Musqet API Key",
        help="Bearer token used by Odoo to authenticate to the Musqet terminal API. "
             "Stored on the backend only and never exposed to the POS frontend.",
        copy=False,
        groups='base.group_erp_manager',
    )
    musqet_base_url = fields.Char(
        string="Musqet API URL",
        help="Base URL of the Musqet terminal API.",
        default=DEFAULT_MUSQET_BASE_URL,
    )
    musqet_terminal_serial = fields.Char(
        string="Musqet Terminal Serial",
        help="Serial number of the physical Musqet terminal bound to this payment method.",
        copy=False,
    )

    @api.model
    def _load_pos_data_fields(self, config):
        # Surface only non-sensitive fields to the POS frontend. The API key is
        # deliberately omitted so it never reaches the browser.
        params = super()._load_pos_data_fields(config)
        params += ['musqet_terminal_serial', 'musqet_base_url']
        return params

    @api.constrains('musqet_terminal_serial')
    def _check_musqet_terminal_serial(self):
        for payment_method in self:
            if not payment_method.musqet_terminal_serial:
                continue
            # sudo() to search across all companies so the serial is globally unique.
            existing = self.sudo().search([
                ('id', '!=', payment_method.id),
                ('musqet_terminal_serial', '=', payment_method.musqet_terminal_serial),
            ])
            same_company = existing.filtered(lambda m: m.company_id == payment_method.company_id)
            if same_company:
                raise ValidationError(_(
                    'Terminal %(terminal)s is already used on payment method %(payment_method)s.',
                    terminal=payment_method.musqet_terminal_serial,
                    payment_method=same_company[0].display_name))
            if existing:
                # Don't disclose the other company's name/method to a user without access to it.
                raise ValidationError(_(
                    'Terminal %(terminal)s is already in use elsewhere.',
                    terminal=payment_method.musqet_terminal_serial))
