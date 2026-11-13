from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.translators import DeepLTranslator, LibreTranslateTranslator, TranslatorError


class TranslateProvider(models.Model):
    """
    Servizio di traduzione configurato (2026-11-13). Fase 1: traduttori
    semplici gratuiti; i fornitori AI arriveranno come altri tipi.
    """
    _name = 'ke.translate.provider'
    _description = 'Fornitore di traduzione'
    _order = 'sequence, id'

    name = fields.Char('Nome', required=True)
    kind = fields.Selection([
        ('libretranslate', 'LibreTranslate (gratuito, sul nostro server)'),
        ('deepl', 'DeepL (gratuito fino a 500.000 caratteri al mese)'),
    ], string='Tipo', required=True, default='libretranslate')
    url = fields.Char(
        'Indirizzo',
        help='LibreTranslate: indirizzo del servizio, es. http://libretranslate:5000. '
             'DeepL: vuoto (si ricava dalla chiave: le chiavi gratuite finiscono con ":fx").')
    api_key = fields.Char('Chiave API', groups='base.group_system', copy=False)
    has_api_key = fields.Boolean('Chiave inserita', compute='_compute_has_api_key')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    last_check = fields.Char('Ultima prova', readonly=True, copy=False)
    notes = fields.Text('Note')

    @api.depends('kind')
    def _compute_has_api_key(self):
        for rec in self:
            rec.has_api_key = bool(rec.sudo().api_key)

    def _translator(self):
        """Il traduttore di questo fornitore (la chiave si legge come amministratore)."""
        self.ensure_one()
        rec = self.sudo()
        if rec.kind == 'libretranslate':
            return LibreTranslateTranslator(rec.url, rec.api_key)
        if rec.kind == 'deepl':
            return DeepLTranslator(rec.api_key, rec.url)
        raise UserError(f'Tipo di fornitore non gestito: {rec.kind}')

    def action_check(self):
        """Prova il servizio: stato e traduzione di una frase di esempio in inglese."""
        self.ensure_one()
        try:
            translator = self._translator()
            info = translator.check()
            sample = translator.translate(['Mulinello da spinning con frizione anteriore'], 'it_IT', 'en_GB')[0]
        except TranslatorError as exc:
            self.last_check = f'Errore: {exc}'[:250]
            raise UserError(str(exc)) from exc
        self.last_check = f'OK – {info}'[:250]
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': self.name, 'type': 'success', 'sticky': True,
                       'message': f'{info}\nProva: "Mulinello da spinning con frizione anteriore" → "{sample}"'},
        }
