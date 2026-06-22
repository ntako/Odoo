{
    'name': 'KE Amazon Sync',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Configuration',
    'summary': 'Sincronizzazione prodotti con Amazon Seller Central',
    'author': 'KE',
    'depends': ['stock', 'product', 'base_setup'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/default_credentials.xml',
        'wizard/amazon_full_import_views.xml',
        'views/res_config_settings_views.xml',
        'views/amazon_sync_log_views.xml',
        'views/product_template_views.xml',
        'views/menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ke_amazon_sync/static/src/js/amazon_sync_log_form.js',
        ],
    },
    'external_dependencies': {
        'python': ['sp_api', 'requests'],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
