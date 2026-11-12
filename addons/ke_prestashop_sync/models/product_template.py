from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ps_product_id = fields.Integer(
        string='ID Prodotto PrestaShop',
        index=True,
        copy=False,
        help='ID del prodotto nel catalogo PrestaShop. 0 = non ancora sincronizzato.',
    )
    ps_sync_enabled = fields.Boolean(
        string='Sync PS attiva',
        default=True,
        help='Include questo prodotto nella sincronizzazione con PrestaShop.',
    )
    ps_last_sync_date = fields.Datetime(
        string='Ultima sync PS',
        readonly=True,
        copy=False,
    )
    ps_image_synced = fields.Boolean(
        string='Immagine caricata su PS',
        default=False,
        copy=False,
        help='True se l\'immagine principale è stata caricata su PrestaShop.',
    )

    # Categoria PS scelta sul prodotto (2026-11-12, chiesto dall'utente): per i
    # gruppi di prodotti che nell'albero del negozio vanno altrove rispetto a
    # categoria Odoo e corrispondenza Amazon, senza spostarli di categoria Odoo.
    ps_category_override_id = fields.Many2one(
        'ke.prestashop.category', string='Categoria PrestaShop forzata', ondelete='set null',
        help='Categoria principale del prodotto sul negozio, al posto di quella automatica '
             '(corrispondenza Amazon, mappatura della categoria Odoo, predefinita).')
    ps_extra_category_ids = fields.Many2many(
        'ke.prestashop.category', 'ke_ps_product_extra_category_rel', 'product_tmpl_id', 'category_id',
        string='Categorie PrestaShop aggiuntive',
        help='Altre categorie del negozio in cui compare il prodotto (es. "Offerte"), oltre alla principale.')
    ps_category_effective_id = fields.Many2one(
        'ke.prestashop.category', string='Categoria PrestaShop usata', compute='_compute_ps_category_effective',
        help='Categoria principale che l\'export manda al negozio. Vuota: la categoria PS predefinita.')
    ps_category_source = fields.Char('Origine categoria PS', compute='_compute_ps_category_effective')

    @api.depends('ps_category_override_id', 'categ_id', 'categ_id.ps_category_id')
    def _compute_ps_category_effective(self):
        for tmpl in self:
            categ, source = tmpl._ke_ps_category_with_source()
            tmpl.ps_category_effective_id = categ
            tmpl.ps_category_source = source

    def _ke_ps_category(self):
        """Categoria PS principale del prodotto; vuota = predefinita."""
        return self._ke_ps_category_with_source()[0]

    def _ke_ps_category_with_source(self):
        """(categoria PS, origine): prima quella forzata sul prodotto, poi le regole automatiche."""
        self.ensure_one()
        if self.ps_category_override_id:
            return self.ps_category_override_id, 'Forzata sul prodotto'
        return self._ke_ps_category_auto()

    def _ke_ps_category_auto(self):
        """
        Categoria PS automatica: quella mappata sulla categoria Odoo o sulla
        prima categoria padre mappata; vuota = predefinita. Estesa da
        ke_amazon_prestashop (categoria PS della corrispondenza Amazon).
        """
        categ = self.categ_id
        while categ:
            if categ.ps_category_id:
                source = 'Mappatura categoria Odoo' + (
                    '' if categ == self.categ_id else f' (da "{categ.display_name}")')
                return categ.ps_category_id, source
            categ = categ.parent_id
        return self.env['ke.prestashop.category'], 'Predefinita del negozio'

    def _ke_ps_extra_category_ids(self, main_id=0):
        """Id PS delle categorie aggiuntive, senza la principale."""
        self.ensure_one()
        return [c.ps_id for c in self.ps_extra_category_ids if c.ps_id and c.ps_id != main_id]
