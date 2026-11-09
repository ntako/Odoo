from odoo import fields, models, tools


class AmazonPriceAnalysis(models.Model):
    """
    Analisi prezzi Amazon (2026-11-06, richiesta dall'utente): una riga per
    annuncio attivo con prezzo, calcolata dal database a ogni apertura (vista
    SQL, nessun dato salvato): prezzo netto in euro (senza l'IVA del paese
    del marketplace, convertito col tasso della valuta), costo e prezzo base
    del prodotto, commissioni Amazon stimate (ke.amazon.listing.fee_*,
    "Aggiorna commissioni Amazon"), ricarico sul costo e sul prezzo base,
    margine = netto − commissioni − costo.

    Le commissioni sono trattate come costo senza IVA (fatturate da Amazon
    EU con inversione contabile); il margine non comprende spedizione FBM,
    resi, pubblicità e stoccaggio. Senza tasso di cambio (es. GBP non
    configurata) i valori in euro restano vuoti invece di convertire 1:1.
    """
    _name = 'ke.amazon.price.analysis'
    _description = 'Analisi prezzi Amazon'
    _auto = False
    _order = 'marketplace_id, sku'
    _rec_name = 'sku'

    listing_id = fields.Many2one('ke.amazon.listing', string='Annuncio', readonly=True)
    marketplace_id = fields.Many2one('ke.amazon.marketplace', string='Marketplace', readonly=True)
    product_id = fields.Many2one('product.product', string='Variante', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', string='Prodotto', readonly=True)
    brand_id = fields.Many2one('ke.product.brand', string='Marca', readonly=True)
    categ_id = fields.Many2one('product.category', string='Categoria', readonly=True)
    sku = fields.Char('SKU', readonly=True)
    asin = fields.Char('ASIN', readonly=True)
    fulfillment = fields.Selection([('fba', 'FBA'), ('fbm', 'FBM')], string='Fulfillment', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Valuta', readonly=True)

    price = fields.Float('Prezzo Amazon', readonly=True, aggregator='avg',
                         help='Prezzo dell\'annuncio, IVA inclusa, nella valuta del marketplace.')
    vat_rate = fields.Float('IVA %', readonly=True, aggregator='avg')
    price_net = fields.Float('Netto (€)', readonly=True, aggregator='avg',
                             help='Prezzo senza l\'IVA del paese, in euro.')
    cost = fields.Float('Costo (€)', readonly=True, aggregator='avg')
    base_price = fields.Float('Prezzo base (€)', readonly=True, aggregator='avg')
    fee_total = fields.Float('Commissioni (€)', readonly=True, aggregator='avg')
    fee_referral = fields.Float('Comm. vendita (€)', readonly=True, aggregator='avg')
    fee_fulfillment = fields.Float('Logistica FBA (€)', readonly=True, aggregator='avg')
    fee_pct = fields.Float('Commissioni % sul prezzo', readonly=True, aggregator='avg',
                           help='Commissioni sul prezzo IVA inclusa (la commissione di vendita Amazon si calcola su quello).')
    net_after_fees = fields.Float('Netto dopo commissioni (€)', readonly=True, aggregator='avg')
    margin = fields.Float('Margine (€)', readonly=True, aggregator='avg',
                          help='Netto − commissioni − costo.')
    margin_pct = fields.Float('Margine % sul netto', readonly=True, aggregator='avg')
    markup_cost_pct = fields.Float('Ricarico % sul costo', readonly=True, aggregator='avg')
    markup_base_pct = fields.Float('Ricarico % sul prezzo base', readonly=True, aggregator='avg')
    price_sync = fields.Float('Prezzo con sync', readonly=True, aggregator='avg',
                              help='Prezzo che "Sync prezzo" manderebbe ad Amazon (listino e IVA del marketplace), '
                                   'nella valuta del marketplace. Si aggiorna con "Ricalcola prezzi con sync".')
    price_sync_diff_pct = fields.Float('Variazione %', readonly=True, aggregator='avg',
                                       help='Prezzo con sync rispetto al prezzo attuale su Amazon.')
    has_manual_price = fields.Boolean('Prezzo manuale', readonly=True)
    has_cost = fields.Boolean('Con costo', readonly=True)
    has_fees = fields.Boolean('Con commissioni', readonly=True)
    fee_date = fields.Datetime('Data stima commissioni', readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS
            WITH comp AS (
                SELECT id, currency_id FROM res_company ORDER BY id LIMIT 1
            ), base AS (
                SELECT l.id, l.marketplace_id, l.product_id, p.product_tmpl_id, t.ke_brand_id AS brand_id,
                       t.categ_id, l.sku, l.asin, l.fulfillment,
                       COALESCE(m.currency_id, comp.currency_id) AS currency_id,
                       l.price::numeric AS price,
                       COALESCE(NULLIF(m.vat_rate, 0), 22)::numeric AS vat_rate,
                       CASE WHEN COALESCE(m.currency_id, comp.currency_id) = comp.currency_id THEN 1::numeric
                            ELSE (SELECT r.rate FROM res_currency_rate r
                                   WHERE r.currency_id = m.currency_id
                                     AND (r.company_id = comp.id OR r.company_id IS NULL)
                                   ORDER BY r.name DESC LIMIT 1)::numeric END AS rate,
                       NULLIF((p.standard_price ->> comp.id::text)::numeric, 0) AS cost,
                       NULLIF(COALESCE(NULLIF(p.ke_list_price, 0), t.list_price), 0)::numeric AS base_price,
                       CASE WHEN l.fee_date IS NOT NULL AND COALESCE(l.fee_error, '') = '' THEN l.fee_total::numeric END AS fee_total,
                       CASE WHEN l.fee_date IS NOT NULL AND COALESCE(l.fee_error, '') = '' THEN l.fee_referral::numeric END AS fee_referral,
                       CASE WHEN l.fee_date IS NOT NULL AND COALESCE(l.fee_error, '') = '' THEN l.fee_fulfillment::numeric END AS fee_fulfillment,
                       l.fee_date,
                       NULLIF(l.price_sync, 0)::numeric AS price_sync,
                       COALESCE(l.price_manual, 0) > 0 AS has_manual_price
                  FROM ke_amazon_listing l
                  JOIN ke_amazon_marketplace m ON m.id = l.marketplace_id
                  JOIN product_product p ON p.id = l.product_id
                  JOIN product_template t ON t.id = p.product_tmpl_id
                  CROSS JOIN comp
                 WHERE l.status = 'active' AND l.price > 0 AND p.active
            ), net AS (
                SELECT b.*,
                       b.price / (1 + b.vat_rate / 100) / NULLIF(b.rate, 0) AS price_net,
                       b.fee_total / NULLIF(b.rate, 0) AS fee_total_eur,
                       b.fee_referral / NULLIF(b.rate, 0) AS fee_referral_eur,
                       b.fee_fulfillment / NULLIF(b.rate, 0) AS fee_fulfillment_eur
                  FROM base b
            )
            SELECT n.id, n.id AS listing_id, n.marketplace_id, n.product_id, n.product_tmpl_id, n.brand_id,
                   n.categ_id, n.sku, n.asin, n.fulfillment, n.currency_id, n.price, n.vat_rate,
                   ROUND(n.price_net, 2) AS price_net,
                   ROUND(n.cost, 2) AS cost,
                   ROUND(n.base_price, 2) AS base_price,
                   ROUND(n.fee_total_eur, 2) AS fee_total,
                   ROUND(n.fee_referral_eur, 2) AS fee_referral,
                   ROUND(n.fee_fulfillment_eur, 2) AS fee_fulfillment,
                   ROUND(100 * n.fee_total / NULLIF(n.price, 0), 1) AS fee_pct,
                   ROUND(n.price_net - n.fee_total_eur, 2) AS net_after_fees,
                   ROUND(n.price_net - n.fee_total_eur - n.cost, 2) AS margin,
                   ROUND(100 * (n.price_net - n.fee_total_eur - n.cost) / NULLIF(n.price_net, 0), 1) AS margin_pct,
                   ROUND(100 * (n.price_net / n.cost - 1), 1) AS markup_cost_pct,
                   ROUND(100 * (n.price_net / n.base_price - 1), 1) AS markup_base_pct,
                   n.cost IS NOT NULL AS has_cost,
                   n.fee_total IS NOT NULL AS has_fees,
                   n.fee_date,
                   n.price_sync,
                   ROUND(100 * (n.price_sync / n.price - 1), 1) AS price_sync_diff_pct,
                   n.has_manual_price
              FROM net n
        """)

    def action_fix_current_price(self):
        """Il prezzo attuale su Amazon diventa il prezzo manuale degli annunci selezionati."""
        self.listing_id.action_fix_current_price()

    def action_clear_manual_price(self):
        self.listing_id.action_clear_manual_price()
