import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

# Suffissi societari ignorati nel confronto ("Trabucco International Srl" =
# "Trabucco"). Tolti anche in sequenza ("international srl").
_LEGAL_SUFFIXES = re.compile(r'(srl|spa|sas|snc|gmbh|ltd|inc|international|italia)$')


def brand_key(text):
    """
    Chiave di confronto di una marca: senza maiuscole, spazi, punteggiatura e
    ragione sociale in coda. "YAMASHITA", "Yamashita " e "yamashita" danno la
    stessa chiave; i caratteri non latini (es. "マリア") restano, così anche i
    nomi giapponesi hanno una chiave propria (da collegare con un nome
    alternativo).
    """
    key = re.sub(r'[\W_]+', '', (text or '').casefold())
    base = key
    while True:
        stripped = _LEGAL_SUFFIXES.sub('', base)
        if stripped == base or not stripped:
            break
        base = stripped
    return base or key


class ProductBrand(models.Model):
    """
    Marca del prodotto, indipendente dai canali di vendita: un solo nome in
    Odoo (quello che va su PrestaShop), più i nomi alternativi con cui la
    stessa marca arriva da Amazon, dai file dei fornitori o dalle fatture.
    Gli import la riconoscono con find_or_create (confronto normalizzato,
    brand_key); una marca sconosciuta viene creata "Da verificare" e si
    conferma o si unisce a un'altra (wizard di unione).
    """
    _name = 'ke.product.brand'
    _description = 'Marca prodotto'
    _order = 'name'

    name = fields.Char('Marca', required=True)
    key = fields.Char('Chiave di confronto', compute='_compute_key', store=True, index=True)
    alias_ids = fields.One2many('ke.product.brand.alias', 'brand_id', string='Nomi alternativi')
    to_review = fields.Boolean(
        'Da verificare', default=False, index=True,
        help='Creata da un import perché il nome non corrispondeva a nessuna marca nota: '
             'confermala (togli la spunta) o uniscila alla marca giusta.',
    )
    active = fields.Boolean(default=True)
    product_tmpl_ids = fields.One2many('product.template', 'ke_brand_id', string='Prodotti')
    product_count = fields.Integer('Prodotti', compute='_compute_product_count')

    _key_unique = models.Constraint('UNIQUE(key)', 'Esiste già una marca con questo nome.')

    @api.depends('name')
    def _compute_key(self):
        for brand in self:
            brand.key = brand_key(brand.name)

    def _compute_product_count(self):
        counts = dict(self.env['product.template'].with_context(active_test=False)._read_group(
            [('ke_brand_id', 'in', self.ids)], ['ke_brand_id'], ['__count'],
        ))
        for brand in self:
            brand.product_count = counts.get(brand, 0)

    @api.constrains('key')
    def _check_key_not_alias(self):
        Alias = self.env['ke.product.brand.alias']
        for brand in self:
            clash = Alias.search([('key', '=', brand.key), ('brand_id', '!=', brand.id)], limit=1)
            if clash:
                raise ValidationError(
                    f'"{brand.name}" è già un nome alternativo di "{clash.brand_id.name or "(non è una marca)"}".')

    @api.model
    def _match(self, name):
        """
        Marca riconosciuta per 'name' (nome o nome alternativo, confronto
        normalizzato). Ritorna (marca, ignorato): 'ignorato' è True se quel
        nome è segnato come "non è una marca" (es. "Generico").
        """
        key = brand_key(name)
        if not key:
            return self.browse(), True
        brand = self.with_context(active_test=False).search([('key', '=', key)], limit=1)
        if brand:
            return brand, False
        alias = self.env['ke.product.brand.alias'].search([('key', '=', key)], limit=1)
        if alias:
            return alias.brand_id, not alias.brand_id
        return self.browse(), False

    @api.model
    def find_or_create(self, name):
        """Marca per 'name'; se sconosciuta la crea "Da verificare". Vuoto se il nome non è una marca."""
        name = (name or '').strip()
        brand, ignored = self._match(name)
        if brand or ignored:
            return brand
        return self.create({'name': name, 'to_review': True})

    def action_open_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': f'Prodotti {self.name}',
            'res_model': 'product.template', 'view_mode': 'list,form',
            'domain': [('ke_brand_id', '=', self.id)],
        }

    def action_confirm(self):
        self.write({'to_review': False})

    @api.model
    def _reassign_products_for_name(self, name, old_brand, new_brand):
        """
        Il nome alternativo 'name' è passato da 'old_brand' a 'new_brand' (vuoto
        = "non è una marca"): sposta i prodotti che avevano 'old_brand' PER
        QUEL nome. Qui non si sa da quale nome viene la marca di un prodotto
        (dipende dalla fonte): ogni modulo che assegna marche lo estende (es.
        ke_amazon_sync, dalla marca Amazon del prodotto).
        """
        return False

    def _merge_into(self, target):
        """
        Unisce queste marche in 'target': prodotti spostati, il nome e i nomi
        alternativi diventano nomi alternativi di 'target', le marche unite
        vengono eliminate. Chiamato dal wizard di unione.
        """
        target.ensure_one()
        sources = self - target
        if not sources:
            return
        Alias = self.env['ke.product.brand.alias']
        self.env['product.template'].with_context(active_test=False).search(
            [('ke_brand_id', 'in', sources.ids)]).write({'ke_brand_id': target.id})
        names = [(s.name) for s in sources]
        sources.alias_ids.write({'brand_id': target.id})
        sources.unlink()
        for name in names:
            if brand_key(name) != target.key and not Alias.search([('key', '=', brand_key(name))], limit=1):
                Alias.create({'name': name, 'brand_id': target.id})


class ProductBrandAlias(models.Model):
    """
    Nome alternativo di una marca (es. "YAMASHITA", "マリア", "Tubertini Srl").
    Senza marca = "non è una marca" (es. "Generico", "Unbekannt"): gli import
    non creano nulla per quel nome.
    """
    _name = 'ke.product.brand.alias'
    _description = 'Nome alternativo marca'
    _order = 'name'

    name = fields.Char('Nome trovato', required=True)
    key = fields.Char('Chiave di confronto', compute='_compute_key', store=True, index=True)
    brand_id = fields.Many2one(
        'ke.product.brand', string='Marca', ondelete='cascade', index=True,
        help='Vuoto: questo nome non è una marca (es. "Generico") e viene ignorato dagli import.',
    )

    _key_unique = models.Constraint('UNIQUE(key)', 'Questo nome alternativo esiste già.')

    @api.depends('name')
    def _compute_key(self):
        for alias in self:
            alias.key = brand_key(alias.name)
