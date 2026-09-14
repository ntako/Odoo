{
    'name': 'KE Amazon Sync',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Configuration',
    'summary': 'Sincronizzazione prodotti con Amazon Seller Central',
    'author': 'KE',
    # sale_management (non solo 'sale'): serve per avere l'app Vendite completa
    # con il menu Configurazione → Listini Prezzi, usato per il listino "Amazon".
    'depends': ['stock', 'product', 'base_setup', 'sale', 'sale_management', 'ke_product_dimensions'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/default_credentials.xml',
        'data/amazon_pricelist.xml',
        'data/amazon_marketplaces.xml',
        'wizard/amazon_full_import_views.xml',
        'wizard/amazon_product_type_wizard_views.xml',
        'wizard/amazon_ship_to_fba_wizard_views.xml',
        'wizard/amazon_receive_from_fba_wizard_views.xml',
        'wizard/amazon_allocate_fbm_wizard_views.xml',
        'wizard/amazon_move_to_general_wizard_views.xml',
        'views/amazon_marketplace_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_company_views.xml',
        'views/amazon_sync_log_views.xml',
        'views/product_template_views.xml',
        'views/product_inventory_list_views.xml',
        'views/sale_order_views.xml',
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
