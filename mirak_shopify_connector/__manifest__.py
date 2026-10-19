# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
{
    'name': 'Mirak Connector for Shopify (Lite)',
    'version': '19.0.2.0.3',
    'category': 'Sales/Sales',
    'summary': 'Free Shopify connector: products, images, stock and orders between Odoo and one Shopify store',
    'description': """
Mirak Connector for Shopify - Lite edition (free).
Full version: contact iwasmirak@gmail.com or +974 66999180.
""",
    'author': 'Bouchibane Karim',
    'maintainer': 'Bouchibane Karim',
    'license': 'OPL-1',
    'support': 'iwasmirak@gmail.com',
    'images': ['static/description/banner.png', 'static/description/icon.png'],
    'depends': ['sale_stock'],
    'external_dependencies': {'python': ['cryptography']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/mirak_shopify_store_views.xml',
        'views/mirak_shopify_product_map_views.xml',
        'views/mirak_shopify_log_views.xml',
        'views/product_template_views.xml',
        'views/sale_views.xml',
        'views/dashboard_views.xml',
        'views/menus.xml',
        'views/mirak_shopify_extra_views.xml',
        'views/mirak_lite_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mirak_shopify_connector/static/src/dashboard/*',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': True,
}
