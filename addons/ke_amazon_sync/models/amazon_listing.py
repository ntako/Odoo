from odoo import fields, models


class AmazonListing(models.Model):
    _name = 'ke.amazon.listing'
    _description = 'Inserzione Amazon (prodotto × marketplace)'
    _rec_name = 'sku'

    # Fase 1 (fondamenta multi-marketplace): questo modello affianca, non
    # sostituisce ancora, i campi x_amazon_* su product.template — sync_engine.py
    # e le viste esistenti continuano a leggere/scrivere quelli finché non
    # vengono riscritti (fasi successive, vedi TASK.md). Le righe qui sono
    # popolate dalla migrazione una tantum (Impostazioni → Amazon → Migra a
    # modello multi-marketplace) e resteranno inutilizzate fino ad allora.
    product_id = fields.Many2one(
        'product.template', string='Prodotto', required=True, ondelete='cascade', index=True,
    )
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace', required=True, index=True,
    )

    sku = fields.Char('SKU', index=True)
    asin = fields.Char('ASIN', index=True)
    fnsku = fields.Char('FNSKU')
    product_type = fields.Char('Amazon Product Type')

    status = fields.Selection([
        ('active', 'Attivo'),
        ('inactive', 'Inattivo'),
        ('not_listed', 'Non listato'),
        ('found_manufacturer', 'Trovato (produttore)'),
        ('pending', 'In attesa'),
        ('error', 'Errore'),
    ], string='Stato Amazon', index=True)

    fulfillment = fields.Selection([
        ('fba', 'FBA (Amazon Logistics)'),
        ('fbm', 'FBM (Merchant)'),
    ], string='Fulfillment', help='FBA: Amazon gestisce le spedizioni. FBM: spedisci tu.')

    price = fields.Float('Prezzo Amazon (attuale)', readonly=True)
    qty = fields.Integer('Quantità Amazon (FBA)', readonly=True)

    category = fields.Char('Categoria Amazon')
    browse_node_id = fields.Char('Amazon Browse Node ID')

    condition = fields.Selection([
        ('new_new', 'Nuovo'),
        ('used_like_new', 'Usato — Come nuovo'),
        ('used_very_good', 'Usato — Molto buono'),
        ('used_good', 'Usato — Buono'),
        ('used_acceptable', 'Usato — Accettabile'),
    ], string='Condizione Amazon', default='new_new')

    publish_state = fields.Selection([
        ('not_published', 'Non pubblicato'),
        ('pending', 'In elaborazione'),
        ('published', 'Pubblicato'),
        ('error', 'Errore'),
    ], string='Stato pubblicazione', default='not_published', readonly=True)
    publish_error = fields.Text('Errore pubblicazione', readonly=True)

    found_by_manufacturer = fields.Boolean(
        'Presente su Amazon (produttore)',
        help='True se il prodotto è già listato su questo marketplace da un altro venditore.',
    )

    sync_price = fields.Boolean('Sync prezzo', default=False)
    sync_qty = fields.Boolean('Sync quantità', default=False,
                               help='Attivo solo per FBM.')
    last_sync = fields.Datetime('Ultima sync', readonly=True)
    sync_error = fields.Text('Errore sync', readonly=True)

    # Sintassi Odoo 19: vedi commento in amazon_marketplace.py.
    _product_marketplace_uniq = models.Constraint(
        'unique(product_id, marketplace_id)',
        "Esiste già un'inserzione per questo prodotto su questo marketplace.",
    )
