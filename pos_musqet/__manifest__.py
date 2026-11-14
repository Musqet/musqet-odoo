# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
{
    'name': 'POS Musqet',
    'version': '20.0.1.7.0',
    'category': 'Sales/Point of Sale',
    'sequence': 6,
    'summary': 'Integrate your POS with a Musqet dual-rail (card + Bitcoin Lightning) payment terminal',
    'data': [
        # Odoo 20 merges access rights and record rules into ir.access: managers read the
        # Musqet sale and refund records, and see only their own companies' records.
        'security/ir.access.csv',
        'views/pos_payment_method_views.xml',
        'views/pos_payment_views.xml',
        'views/pos_musqet_sale_views.xml',
        'views/pos_musqet_refund_views.xml',
    ],
    'depends': ['point_of_sale'],
    'external_dependencies': {'python': ['cryptography']},
    'installable': True,
    'assets': {
        'point_of_sale._assets_pos': [
            # POS payment handler (PaymentInterface) + create→poll result engine.
            'pos_musqet/static/src/**/*',
        ],
    },
    'author': 'Musqet',
    'license': 'LGPL-3',
}
