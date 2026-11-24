"""
Famiglie riorganizzate in Odoo dal catalogo del fornitore (ke_product_catalog, 2026-11-20): cosa serve
perché la sync Amazon continui a riconoscere tutto.

- Un prodotto semplice tiene SKU/ASIN/FNSKU sul prodotto, una variante di famiglia sulla variante: quando
  un prodotto semplice diventa variante (o famiglia) i suoi passano alla variante, se non ne ha.
- La sync tratta come famiglia solo un prodotto con x_amazon_parent_asin: una famiglia che non ne ha
  (nata da prodotti semplici o nuova) prende quello di una famiglia Amazon svuotata, o uno interno
  "ODOO-FAM-<id>" (mai mandato ad Amazon: le chiamate al Catalog usano l'ASIN di una variante).
  Senza, il Full Import la tratterebbe da prodotto semplice (e la "promuoverebbe" col nome Amazon).
- Le famiglie Amazon svuotate (archiviate) lasciano il loro parent ASIN alla famiglia che ne ha
  ricevuto le varianti (x_amazon_parent_asin_alias): il Full Import vi aggiunge le nuove varianti di
  quella famiglia Amazon invece di ricrearla (_MatchMaps).
Su Amazon non cambia niente: le inserzioni restano come sono.
"""
from odoo import fields, models

_SINGLE_FIELDS = ('x_amazon_sku', 'x_amazon_asin', 'x_amazon_fnsku', 'x_amazon_sku_previous')
_FAMILY_FIELDS = ('x_amazon_product_type', 'x_amazon_category', 'x_amazon_brand', 'x_amazon_fulfillment',
                  'x_amazon_status')


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    x_amazon_parent_asin_alias = fields.Char(
        'Altri parent ASIN', copy=False,
        help='Parent ASIN di famiglie Amazon unite a questa in Odoo (separati da virgola): il Full Import '
             'porta qui le loro varianti.')

    def _ke_alias_list(self):
        return [a.strip() for a in (self.x_amazon_parent_asin_alias or '').split(',') if a.strip()]

    @staticmethod
    def _ke_copy_single_ids(tmpl, variant):
        vals = {f: tmpl[f] for f in _SINGLE_FIELDS if tmpl[f] and not variant[f]}
        if vals:
            variant.write(vals)

    def _ke_family_before_move(self, variants):
        super()._ke_family_before_move(variants)
        if len(self.product_variant_ids) == 1:
            self._ke_copy_single_ids(self, self.product_variant_ids)
        for variant in variants:
            source = variant.product_tmpl_id
            if len(source.product_variant_ids) == 1:
                self._ke_copy_single_ids(source, variant)

    def _ke_family_after_move(self, variants, sources, emptied):
        super()._ke_family_after_move(variants, sources, emptied)
        vals = {}
        parents = [s.x_amazon_parent_asin for s in emptied.sorted('id') if s.x_amazon_parent_asin]
        if not self.x_amazon_parent_asin:
            vals['x_amazon_parent_asin'] = parents.pop(0) if parents else f'ODOO-FAM-{self.id}'
        own = vals.get('x_amazon_parent_asin') or self.x_amazon_parent_asin
        aliases = self._ke_alias_list()
        for alias in parents + [a for s in emptied for a in s._ke_alias_list()]:
            if alias != own and alias not in aliases:
                aliases.append(alias)
        if aliases != self._ke_alias_list():
            vals['x_amazon_parent_asin_alias'] = ','.join(aliases)
        for field in _FAMILY_FIELDS:
            if not self[field]:
                value = next((s[field] for s in sources if s[field]), False)
                if value:
                    vals[field] = value
        if vals:
            self.write(vals)
