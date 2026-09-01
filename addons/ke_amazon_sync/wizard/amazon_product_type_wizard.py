from odoo import fields, models
from odoo.exceptions import UserError


class AmazonProductTypeWizard(models.TransientModel):
    _name = 'ke.amazon.product.type.wizard'
    _description = 'Ricerca tipo prodotto Amazon'

    product_id = fields.Many2one('product.template', required=True, readonly=True)
    keyword = fields.Char('Parola chiave', required=True)
    result_ids = fields.One2many(
        'ke.amazon.product.type.result', 'wizard_id', string='Risultati',
    )

    def action_search(self):
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        try:
            results = client.search_product_types(keywords=self.keyword.strip())
        except Exception as exc:
            raise UserError(f'Errore ricerca Amazon: {exc}') from exc

        self.result_ids.unlink()
        for item in results:
            self.env['ke.amazon.product.type.result'].create({
                'wizard_id': self.id,
                'type_name': item['name'],
                'type_label': item['display_name'],
            })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


class AmazonProductTypeResult(models.TransientModel):
    _name = 'ke.amazon.product.type.result'
    _description = 'Risultato ricerca tipo prodotto Amazon'
    _rec_name = 'type_name'

    wizard_id = fields.Many2one('ke.amazon.product.type.wizard', required=True, ondelete='cascade')
    type_name = fields.Char('Codice Amazon', readonly=True)
    type_label = fields.Char('Nome', readonly=True)

    def action_select(self):
        self.wizard_id.product_id.write({'x_amazon_product_type': self.type_name})
        return {'type': 'ir.actions.act_window_close'}


class AmazonBrowseNodeWizard(models.TransientModel):
    _name = 'ke.amazon.browse.node.wizard'
    _description = 'Ricerca categoria (browse node) Amazon'

    product_id = fields.Many2one('product.template', required=True, readonly=True)
    keyword = fields.Char('Parola chiave', required=True)
    result_ids = fields.One2many(
        'ke.amazon.browse.node.result', 'wizard_id', string='Risultati',
    )

    def action_search(self):
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        try:
            results = client.search_browse_nodes(self.keyword.strip())
        except Exception as exc:
            raise UserError(f'Errore ricerca Amazon: {exc}') from exc

        self.result_ids.unlink()
        for item in results:
            self.env['ke.amazon.browse.node.result'].create({
                'wizard_id': self.id,
                'classification_id': item['classification_id'],
                'category_name': item['display_name'],
            })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


class AmazonBrowseNodeResult(models.TransientModel):
    _name = 'ke.amazon.browse.node.result'
    _description = 'Risultato ricerca browse node Amazon'
    _rec_name = 'classification_id'

    wizard_id = fields.Many2one('ke.amazon.browse.node.wizard', required=True, ondelete='cascade')
    classification_id = fields.Char('Browse Node ID', readonly=True)
    category_name = fields.Char('Categoria', readonly=True)

    def action_select(self):
        self.wizard_id.product_id.write({
            'x_amazon_category': self.category_name,
            'x_amazon_browse_node_id': self.classification_id,
        })
        return {'type': 'ir.actions.act_window_close'}
