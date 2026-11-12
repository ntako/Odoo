from odoo import api, fields, models


class PsCategoryAssignWizard(models.TransientModel):
    """
    Categoria PrestaShop per un gruppo di prodotti (2026-11-12, chiesto
    dall'utente): quando l'albero del negozio non coincide con la categoria
    Odoo o con la corrispondenza Amazon, si sceglie la categoria del negozio
    sui prodotti selezionati, senza spostarli di categoria Odoo.
    """
    _name = 'ke.ps.category.assign'
    _description = 'Imposta categoria PrestaShop sui prodotti'

    product_ids = fields.Many2many('product.template', string='Prodotti')
    product_count = fields.Integer('Prodotti selezionati', compute='_compute_product_count')
    main_action = fields.Selection([
        ('keep', 'Non cambiare'),
        ('set', 'Forza questa categoria'),
        ('auto', 'Torna alla categoria automatica'),
    ], string='Categoria principale', default='set', required=True)
    category_id = fields.Many2one('ke.prestashop.category', string='Categoria PrestaShop')
    extra_action = fields.Selection([
        ('keep', 'Non cambiare'),
        ('add', 'Aggiungi queste'),
        ('remove', 'Togli queste'),
        ('replace', 'Sostituisci con queste'),
    ], string='Categorie aggiuntive', default='keep', required=True)
    extra_category_ids = fields.Many2many('ke.prestashop.category', string='Categorie aggiuntive scelte')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if ctx.get('active_model') == 'product.template' and ctx.get('active_ids'):
            res['product_ids'] = [(6, 0, ctx['active_ids'])]
        elif ctx.get('active_model') == 'product.product' and ctx.get('active_ids'):
            tmpls = self.env['product.product'].browse(ctx['active_ids']).product_tmpl_id
            res['product_ids'] = [(6, 0, tmpls.ids)]
        return res

    @api.depends('product_ids')
    def _compute_product_count(self):
        for wiz in self:
            wiz.product_count = len(wiz.product_ids)

    def action_apply(self):
        self.ensure_one()
        products = self.product_ids.with_context(mail_notrack=True)
        if self.main_action == 'set' and self.category_id:
            products.write({'ps_category_override_id': self.category_id.id})
        elif self.main_action == 'auto':
            products.write({'ps_category_override_id': False})
        extras = self.extra_category_ids
        if self.extra_action == 'add' and extras:
            products.write({'ps_extra_category_ids': [(4, c.id) for c in extras]})
        elif self.extra_action == 'remove' and extras:
            products.write({'ps_extra_category_ids': [(3, c.id) for c in extras]})
        elif self.extra_action == 'replace':
            products.write({'ps_extra_category_ids': [(6, 0, extras.ids)]})
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': 'Categoria PrestaShop', 'type': 'success', 'sticky': False,
                'message': f'{len(products)} prodotti aggiornati. Sul negozio la categoria cambia '
                           'al prossimo export di questi prodotti.',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
