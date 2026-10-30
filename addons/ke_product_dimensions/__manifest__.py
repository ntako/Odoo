{
    'name': 'KE Product Dimensions',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Dimensioni fisiche del prodotto (lunghezza/larghezza/altezza) e volume',
    'description': """
Aggiunge a product.template i campi lunghezza/larghezza/altezza (cm) e
ricalcola automaticamente il campo nativo 'volume' (m³) a partire da questi.
Aggiunge inoltre a stock.warehouse il volume totale giacente nel magazzino.

Modulo neutro, indipendente da qualunque canale di vendita: pensato per
essere condiviso da più integrazioni (es. marketplace, ecommerce) che hanno
bisogno delle stesse dimensioni fisiche del prodotto, senza doversi
accoppiare l'una all'altra.

Se una futura versione di Odoo (o un altro modulo) introduce campi nativi
con lo stesso nome (product_length/product_width/product_height) e un
proprio compute per 'volume', il conflitto va verificato/risolto qui: è
l'unico punto in cui questo repository definisce il compute di 'volume'.
""",
    'author': 'KE',
    'depends': ['product', 'stock'],
    'data': [
        'views/product_template_views.xml',
        'views/stock_warehouse_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
