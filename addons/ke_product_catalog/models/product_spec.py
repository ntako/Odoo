from odoo import api, fields, models

# Priorità delle fonti (2026-11-17): una fonte non sovrascrive i valori di una più affidabile.
SOURCE_RANK = {'amazon': 1, 'supplier': 2, 'manual': 3}


class ProductSpecKey(models.Model):
    """
    Una specifica tecnica (es. "Rapporto di recupero", "Diametro"), con il
    nome nelle lingue del catalogo (2026-11-17). Indipendente dalla fonte:
    i valori arrivano da Amazon, dai fornitori o a mano. "Mostra sul sito"
    decide se finisce nella scheda prodotto dei canali di vendita.
    """
    _name = 'ke.product.spec.key'
    _description = 'Specifica tecnica'
    _order = 'sequence, id'

    name = fields.Char('Specifica', required=True, translate=True)
    code = fields.Char(
        'Codice', required=True, index=True,
        help='Identificativo stabile usato da chi importa (es. gear_ratio).')
    sequence = fields.Integer('Ordine', default=10)
    website_visible = fields.Boolean(
        'Mostra sul sito', default=True,
        help='Tolta, la specifica resta in Odoo ma non va nelle schede dei canali di vendita.')
    active = fields.Boolean(default=True)
    value_count = fields.Integer('Prodotti', compute='_compute_value_count')

    _code_uniq = models.Constraint('unique(code)', 'Esiste già una specifica con questo codice.')

    def _compute_value_count(self):
        counts = {k.id: n for k, n in self.env['ke.product.spec']._read_group(
            [('key_id', 'in', self.ids)], ['key_id'], ['product_tmpl_id:count_distinct'])}
        for key in self:
            key.value_count = counts.get(key.id, 0)

    @api.model
    def _ensure(self, code, labels, sequence=10):
        """La specifica con questo codice, creata se manca; labels: {lingua: nome}, la prima è la principale."""
        key = self.with_context(active_test=False).search([('code', '=', code)], limit=1)
        if key:
            return key
        langs = set(self.env['res.lang'].search([]).mapped('code'))
        first_lang, first_label = next(iter(labels.items()))
        key = self.with_context(lang=first_lang).create({'code': code, 'name': first_label, 'sequence': sequence})
        for lang, label in labels.items():
            if lang in langs and lang != first_lang:
                key.with_context(lang=lang).name = label
        return key


class ProductSpec(models.Model):
    """
    Valore di una specifica per un prodotto o per una sua variante (nelle
    famiglie diametro, carico, misura cambiano da variante a variante). Il
    valore è tradotto; ogni fonte aggiorna solo i valori suoi e mai quelli
    scritti a mano.
    """
    _name = 'ke.product.spec'
    _description = 'Specifica tecnica del prodotto'
    _order = 'product_tmpl_id, sequence, key_id, product_id'

    product_tmpl_id = fields.Many2one(
        'product.template', string='Prodotto', required=True, index=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', string='Variante', index=True, ondelete='cascade',
        domain="[('product_tmpl_id', '=', product_tmpl_id)]",
        help='Vuoto: vale per tutte le varianti del prodotto.')
    key_id = fields.Many2one('ke.product.spec.key', string='Specifica', required=True, index=True,
                             ondelete='cascade')
    value = fields.Char('Valore', required=True, translate=True)
    source = fields.Selection(
        [('amazon', 'Amazon'), ('supplier', 'Fornitore'), ('manual', 'Manuale')],
        string='Fonte', required=True, default='manual')
    sequence = fields.Integer(related='key_id.sequence', store=True)
    website_visible = fields.Boolean(related='key_id.website_visible')

    _spec_uniq = models.Constraint(
        'UNIQUE NULLS NOT DISTINCT (product_tmpl_id, product_id, key_id)',
        'Questa specifica ha già un valore per il prodotto (o la variante).')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('product_id') and not vals.get('product_tmpl_id'):
                vals['product_tmpl_id'] = self.env['product.product'].browse(vals['product_id']).product_tmpl_id.id
        return super().create(vals_list)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ke_spec_ids = fields.One2many('ke.product.spec', 'product_tmpl_id', string='Specifiche tecniche')

    def _ke_set_specs(self, values, source, lang, variant=None):
        """
        Scrive le specifiche di una fonte. values: {spec.key: valore}; valore
        vuoto = la fonte non ce l'ha più (si toglie, se era sua). Non tocca i
        valori di una fonte più affidabile (manuale > fornitore > Amazon).
        Ritorna il numero di valori cambiati.
        """
        self.ensure_one()
        Spec = self.env['ke.product.spec'].with_context(lang=lang)
        existing = {s.key_id.id: s for s in Spec.search([
            ('product_tmpl_id', '=', self.id), ('product_id', '=', variant.id if variant else False)])}
        # Valori salvati per lingua, senza il ripiego di Odoo sull'inglese di base:
        # confrontando col valore mostrato, una lingua il cui valore coincideva
        # col ripiego non veniva mai scritta e poi mostrava quello en_US
        # ("11.2 kg" in italiano, 2026-11-17).
        raw = {}
        if existing:
            self.env.flush_all()
            self.env.cr.execute('SELECT id, value FROM ke_product_spec WHERE id = ANY(%s)',
                                ([r.id for r in existing.values()],))
            raw = {rid: (val or {}).get(lang) for rid, val in self.env.cr.fetchall()}
        changed = 0
        for key, value in values.items():
            row = existing.get(key.id)
            value = (value or '').strip()
            if row and SOURCE_RANK.get(row.source, 0) > SOURCE_RANK.get(source, 0):
                continue
            if not value:
                if row and row.source == source:
                    row.unlink()
                    changed += 1
                continue
            if not row:
                Spec.create({'product_tmpl_id': self.id, 'product_id': variant.id if variant else False,
                             'key_id': key.id, 'value': value, 'source': source})
                changed += 1
            elif raw.get(row.id) != value or row.source != source:
                row.write({'value': value, 'source': source})
                changed += 1
        return changed
