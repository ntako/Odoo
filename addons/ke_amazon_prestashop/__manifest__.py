{
    'name': 'KE Amazon ↔ PrestaShop',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Configuration',
    'summary': 'Categoria PrestaShop per categoria Amazon (modulo ponte)',
    'description': """
Modulo ponte tra ke_amazon_sync e ke_prestashop_sync, installato da solo
quando ci sono entrambi: colonna "Categoria PrestaShop" nelle corrispondenze
delle categorie Amazon e nel file di import; il prodotto con una categoria
Amazon mappata va in quella categoria PS, altrimenti vale la mappatura della
categoria Odoo.
""",
    'author': 'KE',
    'depends': ['ke_amazon_sync', 'ke_prestashop_sync'],
    'data': [
        'views/amazon_category_map_views.xml',
    ],
    'auto_install': True,
    'installable': True,
    'license': 'LGPL-3',
}
