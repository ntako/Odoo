{
    'name': 'KE Amazon Sync',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Configuration',
    'summary': 'Sincronizzazione prodotti con Amazon Seller Central',
    # Esplicita per evitare che Odoo usi README.md come descrizione: senza
    # 'description' qui, Odoo lo legge automaticamente E LO RENDERIZZA COME
    # RST — il README è in Markdown (tabelle |---|---|, separatori ---),
    # RST non le capisce e genera decine di errori di parsing sulla pagina
    # descrizione dell'app (verificato con un'installazione locale reale).
    'description': 'Sincronizzazione bidirezionale prodotti/ordini/giacenze con '
                    'Amazon Seller Central (SP-API). Vedi README.md nel modulo per i dettagli.',
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
        'report/amazon_fba_label_report.xml',
        'wizard/amazon_fba_prep_wizard_views.xml',
        'views/amazon_marketplace_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_company_views.xml',
        'views/amazon_sync_log_views.xml',
        'views/product_template_views.xml',
        'views/product_inventory_list_views.xml',
        'views/sale_order_views.xml',
        'views/stock_picking_views.xml',
        'views/amazon_fba_label_log_views.xml',
        'views/menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ke_amazon_sync/static/src/js/amazon_sync_log_form.js',
        ],
    },
    'external_dependencies': {
        # Nome pacchetto PyPI (python-amazon-sp-api), non il nome di import
        # (sp_api): da Odoo 19 la verifica cerca prima il nome PyPI via
        # importlib.metadata, e con solo 'sp_api' emette un warning esplicito
        # "does not appear to be a valid PyPI package" (funziona comunque via
        # fallback sull'import, ma verificato con un'installazione locale
        # reale che questa è la forma corretta/raccomandata).
        'python': ['python-amazon-sp-api', 'requests'],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
