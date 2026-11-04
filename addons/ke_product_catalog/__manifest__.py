{
    'name': 'KE Product Catalog',
    'version': '19.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Catalogo prodotti comune: galleria immagini come indirizzi (fonte, ruolo, ordine)',
    'description': """
Dati di catalogo condivisi da tutti i canali (Amazon, import fornitori,
PrestaShop), indipendenti da ciascuno di essi — come ke_product_dimensions.

Galleria immagini: per ogni prodotto/variante l'elenco degli indirizzi delle
immagini, con fonte (Amazon, fornitore, manuale), ruolo (es. MAIN, PT01,
SWCH per Amazon), dimensioni e ordine. Le immagini NON vengono scaricate in
Odoo: in Odoo resta solo l'immagine principale (image_1920 /
image_variant_1920); le altre si scaricano al momento della pubblicazione
(es. verso PrestaShop) partendo da questi indirizzi.

In futuro: marca generica (con nomi alternativi) e codice famiglia del
fornitore.
""",
    'author': 'KE',
    'depends': ['product'],
    'data': [
        'security/ir.model.access.csv',
        'views/product_media_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
