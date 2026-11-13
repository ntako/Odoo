from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.product_texts import ProductTextTranslation
from ..services.translators import TranslatorError


class TranslateWizard(models.TransientModel):
    """Azione "Traduci testi sito" sui prodotti selezionati (2026-11-13)."""
    _name = 'ke.translate.wizard'
    _description = 'Traduci testi sito'

    # Fino a questo numero di prodotti si traduce subito; oltre, in coda.
    _DIRECT_MAX = 5

    product_ids = fields.Many2many('product.template', string='Prodotti')
    product_count = fields.Integer('Prodotti selezionati', compute='_compute_product_count')
    provider_id = fields.Many2one('ke.translate.provider', 'Fornitore', required=True,
                                  default=lambda self: self.env['ke.translate.provider'].search([], limit=1))
    source_lang = fields.Selection(selection='_lang_selection', string='Lingua di partenza', required=True,
                                   default=lambda self: self._default_source())
    target_lang_ids = fields.Many2many('res.lang', string='Lingue di arrivo',
                                       domain="[('active', '=', True), ('code', '!=', 'en_US')]",
                                       default=lambda self: self._default_targets())
    scope = fields.Selection([
        ('empty', 'Solo i campi senza testo proprio (vuoti o copia della lingua di partenza)'),
        ('all', 'Tutti i campi (sovrascrive anche i testi già tradotti)'),
    ], string='Campi', default='empty', required=True)
    apply = fields.Selection([
        ('write', 'Scrivi subito nei campi'),
        ('proposal', 'Crea proposte da approvare'),
    ], string='Applicazione', default='proposal', required=True)

    @api.model
    def _lang_selection(self):
        return [(c, n) for c, n in self.env['res.lang'].get_installed() if c != 'en_US']

    @api.model
    def _default_source(self):
        codes = [c for c, _n in self._lang_selection()]
        return 'it_IT' if 'it_IT' in codes else (codes[0] if codes else False)

    @api.model
    def _default_targets(self):
        source = self._default_source()
        return self.env['res.lang'].search([('active', '=', True), ('code', 'not in', ['en_US', source])])

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if ctx.get('active_model') == 'product.template' and ctx.get('active_ids'):
            res['product_ids'] = [(6, 0, ctx['active_ids'])]
        elif ctx.get('active_model') == 'product.product' and ctx.get('active_ids'):
            res['product_ids'] = [(6, 0, self.env['product.product'].browse(ctx['active_ids']).product_tmpl_id.ids)]
        return res

    @api.depends('product_ids')
    def _compute_product_count(self):
        for wiz in self:
            wiz.product_count = len(wiz.product_ids)

    def _targets(self):
        return [c for c in self.target_lang_ids.mapped('code') if c != self.source_lang]

    def action_translate(self):
        self.ensure_one()
        targets = self._targets()
        if not self.product_ids:
            raise UserError('Nessun prodotto selezionato.')
        if not targets:
            raise UserError('Scegli almeno una lingua di arrivo diversa da quella di partenza.')
        if len(self.product_ids) > self._DIRECT_MAX:
            job = self.env['ke.translate.job'].enqueue({
                'name': f'Traduzione di {len(self.product_ids)} prodotti ({", ".join(targets)})',
                'provider_id': self.provider_id.id, 'source_lang': self.source_lang,
                'target_langs': ','.join(targets), 'scope': self.scope, 'apply': self.apply,
            }, self.product_ids)
            return {'type': 'ir.actions.act_window', 'res_model': 'ke.translate.job', 'res_id': job.id,
                    'view_mode': 'form', 'target': 'current'}
        try:
            engine = ProductTextTranslation(self.env, self.provider_id._translator(), self.source_lang,
                                            self.scope, self.apply, provider=self.provider_id)
            done = sum(bool(engine.translate_product(t, targets)) for t in self.product_ids)
        except TranslatorError as exc:
            raise UserError(str(exc)) from exc
        if self.apply == 'proposal' and done:
            return {
                'type': 'ir.actions.act_window', 'name': 'Proposte di testo', 'res_model': 'ke.translate.proposal',
                'view_mode': 'list,form', 'target': 'current',
                'domain': [('product_tmpl_id', 'in', self.product_ids.ids), ('state', '=', 'pending')],
            }
        message = (f'{done} prodotti tradotti su {len(self.product_ids)}.' if done else
                   'Niente da tradurre: i campi hanno già un testo proprio nelle lingue scelte '
                   '(per rifarli scegli "Tutti i campi").')
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': 'Traduzione testi sito', 'type': 'success' if done else 'warning',
                       'sticky': False, 'message': message, 'next': {'type': 'ir.actions.act_window_close'}},
        }
