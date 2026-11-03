from odoo import api, fields, models

_FBA_PARAM = 'ke_amazon_sync.fba_warehouse_id'
_FBM_PARAM = 'ke_amazon_sync.fbm_warehouse_id'
_DEFAULT_PARAM = 'ke_amazon_sync.default_warehouse_id'


class ResCompany(models.Model):
    _inherit = 'res.company'

    # store=False: no DB column → no schema migration issues
    amazon_fba_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Magazzino FBA Amazon',
        compute='_compute_amazon_warehouses',
        inverse='_inverse_amazon_fba_warehouse',
        store=False,
        domain="[('company_id', '=', id)]",
        help='Magazzino Odoo per lo stock Amazon FBA. '
             'Le giacenze vengono aggiornate dalla Sync Giacenze FBA. '
             'Gli ordini AFN vengono assegnati a questo magazzino.',
    )
    amazon_fbm_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Magazzino FBM Amazon (allocazione)',
        compute='_compute_amazon_warehouses',
        inverse='_inverse_amazon_fbm_warehouse',
        store=False,
        domain="[('company_id', '=', id)]",
        help='Magazzino Odoo che rappresenta la quota di giacenza riservata/esposta '
             'ad Amazon FBM — non necessariamente tutto lo stock fisico che hai '
             "(vedi 'Magazzino Generale'). Le quantità qui presenti vengono spinte "
             'su Amazon durante la sync prezzi/qty. Gli ordini MFN vengono assegnati '
             'a questo magazzino.',
    )
    amazon_default_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Magazzino Generale (fisico)',
        compute='_compute_amazon_warehouses',
        inverse='_inverse_amazon_default_warehouse',
        store=False,
        domain="[('company_id', '=', id)]",
        help='Il magazzino dove tieni davvero la merce, condiviso tra tutti i canali '
             'di vendita (non solo Amazon). Da qui alloca la quota da esporre su '
             "Amazon FBM (magazzino 'FBM Amazon') o spedisci rifornimenti FBA — "
             'è la fonte di verità fisica, distinta dalle allocazioni per canale.',
    )

    @api.depends()
    def _compute_amazon_warehouses(self):
        params = self.env['ir.config_parameter'].sudo()
        fba_id = int(params.get_param(_FBA_PARAM, 0) or 0)
        fbm_id = int(params.get_param(_FBM_PARAM, 0) or 0)
        default_id = int(params.get_param(_DEFAULT_PARAM, 0) or 0)
        Warehouse = self.env['stock.warehouse']
        fba = Warehouse.browse(fba_id) if fba_id else Warehouse.browse()
        fbm = Warehouse.browse(fbm_id) if fbm_id else Warehouse.browse()
        default_wh = Warehouse.browse(default_id) if default_id else Warehouse.browse()
        for rec in self:
            rec.amazon_fba_warehouse_id = fba
            rec.amazon_fbm_warehouse_id = fbm
            rec.amazon_default_warehouse_id = default_wh

    def _inverse_amazon_fba_warehouse(self):
        params = self.env['ir.config_parameter'].sudo()
        for rec in self:
            params.set_param(_FBA_PARAM, rec.amazon_fba_warehouse_id.id or 0)

    def _inverse_amazon_fbm_warehouse(self):
        params = self.env['ir.config_parameter'].sudo()
        for rec in self:
            params.set_param(_FBM_PARAM, rec.amazon_fbm_warehouse_id.id or 0)

    def _inverse_amazon_default_warehouse(self):
        params = self.env['ir.config_parameter'].sudo()
        for rec in self:
            params.set_param(_DEFAULT_PARAM, rec.amazon_default_warehouse_id.id or 0)
