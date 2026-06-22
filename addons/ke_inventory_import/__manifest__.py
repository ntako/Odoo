{
    'name': 'KE Inventory Import',
    'version': '19.0.1.0',
    'installable': True,
    'application': True,
    'summary': 'Import inventory products from CSV',
    "description": """
        This module allows importing inventory products from a CSV file.
    """,
    'category': 'Supply Chain/Inventory',
    'depends': ['stock', 'product' ],
    'data': [
        'security/ir.model.access.csv',
        'views/import_inventory_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ke_inventory_import/static/src/xml/csv_upload_widget.xml',
            'ke_inventory_import/static/src/js/csv_upload_widget.js',
            'ke_inventory_import/static/src/js/auto_refresh_widget.js',
            'ke_inventory_import/static/src/js/jobs_list_view.js',
        ],
    },
    'author': 'Matteo Nuzzachi',
    'license': 'LGPL-3',
}
