from odoo import api, fields, models


class AmazonResellOffer(models.Model):
    """
    Offerta "usato" creata da Amazon con il programma Grade & Resell: i resi
    del venditore rivenduti come usati, a suo nome, con uno SKU di Amazon
    (`amzn.gr.<SKU originale>-…`) sullo STESSO ASIN del prodotto. Non è un
    prodotto a sé: è collegata al prodotto originale, e le sue unità stanno
    nell'ubicazione "FBA – Usato" del magazzino FBA (vedi
    SyncEngine._get_fba_used_location). Modello separato da ke.amazon.listing
    perché quella ha un vincolo unique(product_id, marketplace_id) su cui si
    appoggia mezzo modulo — richiesto dall'utente il 2026-11-03 per tenere
    traccia dell'usato e collegarne le vendite.
    """
    _name = 'ke.amazon.resell.offer'
    _description = 'Offerta usato Amazon (Grade & Resell)'
    _order = 'product_id, marketplace_id, sku'
    _rec_name = 'sku'

    sku = fields.Char('SKU Amazon (usato)', required=True, index=True, readonly=True)
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace', required=True, ondelete='cascade', index=True,
    )
    product_id = fields.Many2one(
        'product.product', string='Prodotto originale', ondelete='set null', index=True,
        help="Prodotto di cui questa offerta rivende unità usate: dall'ASIN, altrimenti "
             "dallo SKU originale contenuto nello SKU di Amazon. Vuoto se non trovato in Odoo "
             "— le sue unità non entrano allora in nessuna giacenza.",
    )
    product_tmpl_id = fields.Many2one(
        related='product_id.product_tmpl_id', string='Prodotto', store=True, index=True,
    )
    asin = fields.Char('ASIN', readonly=True)
    condition = fields.Selection([
        ('used_like_new', 'Usato — Come nuovo'),
        ('used_very_good', 'Usato — Molto buono'),
        ('used_good', 'Usato — Buono'),
        ('used_acceptable', 'Usato — Accettabile'),
    ], string='Condizione', readonly=True,
        help="Dall'inventario FBA (sync giacenze); in mancanza, dal suffisso dello SKU (es. -LN, -VG).",
    )
    status = fields.Char('Stato annuncio', readonly=True)
    fulfillment = fields.Char('Canale', readonly=True)
    price = fields.Float('Prezzo', readonly=True)
    currency_id = fields.Many2one(related='marketplace_id.currency_id', string='Valuta')
    qty = fields.Integer(
        'Quantità usato (FBA)', readonly=True,
        help='Unità usate vendibili secondo l\'ultima sync giacenze FBA del marketplace.',
    )
    last_sync = fields.Datetime('Ultima sync', readonly=True)

    _sku_marketplace_uniq = models.Constraint(
        'unique(sku, marketplace_id)',
        "Esiste già un'offerta usato con questo SKU su questo marketplace.",
    )

    @api.depends('sku', 'marketplace_id')
    def _compute_display_name(self):
        for offer in self:
            offer.display_name = f'[{offer.marketplace_id.code}] {offer.sku}' if offer.marketplace_id else offer.sku
