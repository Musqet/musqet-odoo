# Part of the Musqet POS integration. See LICENSE file for full copyright and licensing details.
{
    'name': 'POS Musqet',
    'version': '19.0.1.0.0',
    'category': 'Sales/Point of Sale',
    'sequence': 6,
    'summary': 'Integrate your POS with a Musqet dual-rail (card + Bitcoin Lightning) payment terminal',
    'data': [
        'views/pos_payment_method_views.xml',
    ],
    'depends': ['point_of_sale'],
    'installable': True,
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_musqet/static/src/**/*',
        ],
    },
    'author': 'Musqet',
    'license': 'LGPL-3',
}
