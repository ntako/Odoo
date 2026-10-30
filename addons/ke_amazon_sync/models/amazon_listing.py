from odoo import fields, models


class AmazonListing(models.Model):
    _name = 'ke.amazon.listing'
    _description = 'Inserzione Amazon (prodotto × marketplace)'
    _rec_name = 'sku'

    # Scoped per VARIANTE (product.product), non per template: ogni variante
    # di una famiglia ha il proprio SKU/ASIN/FNSKU/prezzo/quantità per
    # marketplace — un'unica riga per famiglia avrebbe fatto sovrascrivere
    # silenziosamente i dati di una sorella dall'altra (bug reale corretto
    # nel refactor del 2026-10-23, vedi TASK.md). Per i prodotti semplici
    # (1 sola variante) non cambia nulla in pratica.
    # ondelete='cascade': se Odoo elimina una variante (es. ricalcolo
    # combinazioni attributi), la riga listing collegata sparisce con lei —
    # comportamento corretto, nessun orfano.
    product_id = fields.Many2one(
        'product.product', string='Variante', required=True, ondelete='cascade', index=True,
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
    # Campo SEPARATO da 'qty', uso esclusivo di sync_fba_quantities_page
    # (marketplace dedicato, non pooled) per decidere quanto consumare dal
    # transito — mai scritto da Full Import/riconciliazione, a differenza
    # di 'qty' che riflette invece l'ultimo dato Amazon visto da QUALSIASI
    # sync (voluto, per la visualizzazione). Prima la sync FBA leggeva/
    # scriveva 'qty' per questo stesso scopo: un Full Import nel frattempo
    # lo sovrascriveva con la propria 'qty_available' (fonte e momento
    # diversi dall'inventario FBA reale), facendo leggere alla sync FBA
    # successiva un "precedente" che non rifletteva l'ultimo valore
    # davvero sincronizzato da lei — causa di un consumo spurio o mancato
    # dal transito indipendente dal bug fra sorelle già corretto. Trovato
    # in revisione, vedi TASK.md "Giacenze".
    fba_qty_snapshot = fields.Integer(
        'Ultima giacenza FBA sincronizzata (interno)', readonly=True,
        help="Uso interno di sync_fba_quantities_page per il confronto con il transito — "
             "non toccato da nessun'altra sync, a differenza di 'Quantità Amazon (FBA)'.",
    )

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
