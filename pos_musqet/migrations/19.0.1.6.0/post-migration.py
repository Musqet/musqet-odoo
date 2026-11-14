# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
"""Give each Musqet payment method with no company the company of its POS configs.

19.0.1.6.0 requires a Musqet method's company: it records its sales and refunds in it, and
only that company's cashiers may drive its terminal. A method its configs can't place (on
none, or on configs of several companies) is logged, and starts no sale or refund until a
manager sets one (see pos.payment.method._musqet_set_missing_companies).
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return  # fresh install: every method is created under the rule
    api.Environment(cr, SUPERUSER_ID, {})['pos.payment.method']._musqet_set_missing_companies()
