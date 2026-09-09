{
    'name': 'KE PrestaShop Sync',
    'version': '19.0.2.0.0',
    'category': 'Inventory/Configuration',
    'summary': 'Sincronizzazione prodotti, stock, prezzi, immagini e ordini con PrestaShop (WebService API)',
    'author': 'KE',
    'depends': ['stock', 'product', 'sale', 'base_setup'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/prestashop_sync_log_views.xml',
        'views/product_template_views.xml',
        'views/sale_order_views.xml',
        'wizard/ps_product_import_views.xml',
        'views/menu.xml',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
