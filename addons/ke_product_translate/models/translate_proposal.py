from collections import defaultdict

from odoo import api, fields, models

from odoo.addons.ke_product_catalog.models.product_website import _plain

FIELD_SELECTION = [
    ('ke_web_name', 'Nome sul sito (H1)'),
    ('ke_web_meta_title', 'Meta title'),
    ('ke_web_meta_description', 'Meta description'),
    ('ke_web_slug', 'URL (slug)'),
    ('ke_web_short_description', 'Descrizione breve'),
    ('ke_web_description', 'Descrizione'),
]


class TranslateProposal(models.Model):
    """Testo proposto per un campo del sito in una lingua, da accettare o scartare."""
    _name = 'ke.translate.proposal'
    _description = 'Proposta di testo'
    _order = 'id desc'
    _rec_name = 'product_tmpl_id'

    product_tmpl_id = fields.Many2one('product.template', 'Prodotto', required=True, ondelete='cascade', index=True)
    lang = fields.Selection(selection='_lang_selection', string='Lingua', required=True, index=True)
    field_name = fields.Selection(FIELD_SELECTION, string='Campo', required=True)
    current_value = fields.Text('Testo attuale')
    source_value = fields.Text('Testo di partenza')
    new_value = fields.Text('Testo proposto')
    operation = fields.Selection([('translate', 'Traduzione')], string='Operazione', default='translate')
    provider_id = fields.Many2one('ke.translate.provider', 'Fornitore', ondelete='set null')
    job_id = fields.Many2one('ke.translate.job', 'Lavoro', ondelete='set null')
    state = fields.Selection([('pending', 'Da valutare'), ('accepted', 'Accettata'), ('rejected', 'Scartata')],
                             string='Stato', default='pending', required=True, index=True)
    current_preview = fields.Html('Attuale', compute='_compute_previews', sanitize=True)
    new_preview = fields.Html('Proposto', compute='_compute_previews', sanitize=True)
    new_short = fields.Char('Proposto (inizio)', compute='_compute_previews')

    @api.model
    def _lang_selection(self):
        return self.env['res.lang'].get_installed()

    @api.depends('current_value', 'new_value')
    def _compute_previews(self):
        for rec in self:
            rec.current_preview = rec.current_value or ''
            rec.new_preview = rec.new_value or ''
            text = _plain(rec.new_value or '')
            rec.new_short = text[:120] + ('…' if len(text) > 120 else '')

    def action_accept(self):
        """Scrive i testi proposti (una scrittura per prodotto e lingua)."""
        pending = self.filtered(lambda p: p.state == 'pending')
        groups = defaultdict(dict)
        for rec in pending:
            groups[(rec.product_tmpl_id, rec.lang)][rec.field_name] = rec.new_value or ''
        for (tmpl, lang), vals in groups.items():
            tmpl.with_context(lang=lang, mail_notrack=True).write(vals)
        pending.write({'state': 'accepted'})
        return True

    def action_reject(self):
        self.filtered(lambda p: p.state == 'pending').write({'state': 'rejected'})
        return True
