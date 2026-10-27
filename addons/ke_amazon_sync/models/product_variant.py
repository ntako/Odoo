from odoo import fields, models

from .product_template import compute_amazon_sale_price


class ProductProduct(models.Model):
    _inherit = 'product.product'

    x_amazon_asin = fields.Char(
        'Amazon ASIN (variante)', index=True, copy=False,
        help='ASIN del child ASIN Amazon per questa specifica variante.',
    )
    x_amazon_sku = fields.Char(
        'Amazon SKU (variante)', index=True, copy=False,
        help='SKU Amazon per questa specifica variante.',
    )
    x_amazon_sku_previous = fields.Char(
        'SKU Amazon precedente (variante)', copy=False, readonly=True,
        help='Ultimo SKU sostituito dalla riconciliazione quando Amazon riassegna lo '
             'SKU di questa variante (es. rifornimento). Solo indicazione informativa.',
    )
    x_amazon_fnsku = fields.Char(
        'FNSKU (variante)', index=True, copy=False,
        help='FNSKU di questa specifica variante, per le famiglie con più varianti '
             '(ogni variante ha un suo SKU e quindi un suo FNSKU). Per un prodotto '
             'semplice resta vuoto: l\'FNSKU è quello del template. Popolato da '
             '"Import FNSKU" per i marketplace con magazzino FBA condiviso (Pan-EU).',
    )

    # Uso esclusivo di sync_fba_quantities_page per il ramo POOLED (pool
    # fisico unico condiviso da più marketplace, es. IT/DE/ES/FR Pan-EU):
    # per decidere quanto consumare dal transito serve un "precedente" per
    # VARIANTE E POOL FISICO, non per singolo marketplace — altrimenti due
    # marketplace pooled sincronizzati nello stesso giro (stesso ASIN,
    # stessa giacenza fisica reale) leggono ciascuno il proprio "precedente"
    # mai aggiornato dall'altro e consumano ENTRAMBI lo stesso incremento
    # reale dal transito (doppio consumo per una sola spedizione davvero
    # ricevuta). Mai scritto da Full Import/riconciliazione, a differenza
    # di ke.amazon.listing.qty. Trovato in revisione, vedi TASK.md
    # "Giacenze".
    x_amazon_fba_qty_snapshot = fields.Integer(
        'Ultima giacenza FBA pooled sincronizzata (interno)', copy=False,
        help='Uso interno di sync_fba_quantities_page (marketplace pooled) per il confronto '
             'con il transito — condiviso fra tutti i marketplace dello stesso pool fisico.',
    )

    # Un'inserzione per variante × marketplace — vedi amazon_listing.py.
    # Copre sia i marketplace "pooled" (via _upsert_listing, sku/asin/prezzo/
    # stato) sia quelli "dedicati" come UK (anche fnsku/qty, non duplicabili
    # sui campi condivisi x_amazon_fnsku/x_amazon_qty del pool).
    x_amazon_listing_ids = fields.One2many(
        'ke.amazon.listing', 'product_id', string='Inserzioni per marketplace',
    )

    def _get_amazon_fnsku(self):
        """
        FNSKU da stampare in etichetta per questa variante: il suo, se ce l'ha
        (famiglia con più varianti), altrimenti quello del template — ma solo
        se il template ha un'unica variante: su una famiglia l'FNSKU del
        template non identifica nessuna variante precisa, meglio vuoto che
        un'etichetta sbagliata su un'unità fisica reale.
        """
        self.ensure_one()
        if self.x_amazon_fnsku:
            return self.x_amazon_fnsku
        if len(self.product_tmpl_id.product_variant_ids) <= 1:
            return self.product_tmpl_id.x_amazon_fnsku or ''
        return ''

    def get_amazon_sale_price(self):
        """
        Prezzo IVA inclusa da comunicare ad Amazon per questa variante: lst_price
        (prezzo template + extra variante) con il markup del listino 'Amazon' e
        l'IVA reintegrata — aliquota e convenzione netto/lordo lette dalle tasse
        di vendita configurate sul prodotto.
        """
        self.ensure_one()
        return compute_amazon_sale_price(self, self.lst_price)
