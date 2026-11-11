from odoo import fields, models

from ..services.sp_api_client import amazon_writes_enabled


class AmazonSafeModeMixin(models.AbstractModel):
    """
    Campo "Modalità sicura Amazon" per nascondere nelle viste i pulsanti che
    scrivono su Amazon (vedi sp_api_client._amazon_write, che comunque blocca
    la chiamata anche se il pulsante venisse premuto).
    """
    _name = 'ke.amazon.safe.mode.mixin'
    _description = 'Modalità sicura Amazon'

    ke_amazon_safe_mode = fields.Boolean('Modalità sicura Amazon', compute='_compute_ke_amazon_safe_mode')

    def _compute_ke_amazon_safe_mode(self):
        safe = not amazon_writes_enabled(self.env)
        for rec in self:
            rec.ke_amazon_safe_mode = safe


class ProductTemplate(models.Model):
    _name = 'product.template'
    _inherit = ['product.template', 'ke.amazon.safe.mode.mixin']


class AmazonInboundPlan(models.Model):
    _name = 'ke.amazon.inbound.plan'
    _inherit = ['ke.amazon.inbound.plan', 'ke.amazon.safe.mode.mixin']
