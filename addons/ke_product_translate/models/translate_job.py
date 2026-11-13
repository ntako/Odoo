import json
import logging
import time
from datetime import timedelta

from odoo import api, fields, models

from ..services.product_texts import ProductTextTranslation
from ..services.translators import TranslatorError

_log = logging.getLogger(__name__)


class TranslateJob(models.Model):
    """
    Traduzione di molti prodotti, in background (2026-11-13). Il cron lavora
    a turni di _TIME_BUDGET_S secondi (Odoo interrompe un cron dopo 120 s) e
    riprende dal punto in cui si è fermato; ogni prodotto viene salvato
    appena tradotto.
    """
    _name = 'ke.translate.job'
    _description = 'Lavoro di traduzione'
    _order = 'id desc'

    _TIME_BUDGET_S = 75
    _STALE_AFTER = timedelta(minutes=15)

    name = fields.Char('Descrizione', required=True)
    state = fields.Selection([
        ('queued', 'In coda'), ('running', 'In corso'), ('done', 'Completato'),
        ('failed', 'Interrotto per errore'), ('cancelled', 'Annullato'),
    ], string='Stato', default='queued', required=True, index=True)
    provider_id = fields.Many2one('ke.translate.provider', 'Fornitore', required=True, ondelete='restrict')
    operation = fields.Selection([('translate', 'Traduzione')], default='translate', required=True, string='Operazione')
    source_lang = fields.Char('Lingua di partenza', required=True)
    target_langs = fields.Char('Lingue di arrivo', required=True, help='Codici separati da virgola.')
    scope = fields.Selection([('empty', 'Solo campi senza testo proprio'), ('all', 'Tutti i campi')],
                             string='Campi', default='empty', required=True)
    apply = fields.Selection([('write', 'Scrivi'), ('proposal', 'Proposte da approvare')],
                             string='Applicazione', default='write', required=True)
    product_ids = fields.Many2many('product.template', 'ke_translate_job_product_rel', 'job_id', 'product_id',
                                   string='Prodotti')
    remaining_ids = fields.Text('Da fare', default='[]')
    total_count = fields.Integer('Prodotti')
    done_count = fields.Integer('Fatti')
    translated_count = fields.Integer('Tradotti')
    error_count = fields.Integer('Errori')
    chars_count = fields.Integer('Caratteri inviati')
    log_lines = fields.Text('Registro', default='')
    start_date = fields.Datetime('Inizio')
    end_date = fields.Datetime('Fine')
    proposal_count = fields.Integer(compute='_compute_proposal_count')

    def _compute_proposal_count(self):
        data = dict(self.env['ke.translate.proposal']._read_group(
            [('job_id', 'in', self.ids)], ['job_id'], ['__count']))
        for job in self:
            job.proposal_count = data.get(job, 0)

    @api.model
    def enqueue(self, vals, products):
        job = self.create(dict(vals, product_ids=[(6, 0, products.ids)], remaining_ids=json.dumps(products.ids),
                               total_count=len(products)))
        self.env.ref('ke_product_translate.cron_run_translate_jobs')._trigger()
        return job

    def _add_log(self, line):
        self.log_lines = (self.log_lines or '') + line + '\n'

    def _langs(self):
        return [c.strip() for c in (self.target_langs or '').split(',') if c.strip()]

    # ------------------------------------------------------------------
    # Esecuzione
    # ------------------------------------------------------------------

    @api.model
    def _cron_run(self):
        stale = fields.Datetime.now() - self._STALE_AFTER
        self.search([('state', '=', 'running'), ('write_date', '<', stale)]).write({'state': 'queued'})
        job = self.search([('state', '=', 'queued')], order='id', limit=1)
        if job:
            job._run_turn()

    def _run_turn(self, budget=None):
        """Un turno di lavoro: traduce finché c'è tempo, poi si riaccoda."""
        self.ensure_one()
        budget = self._TIME_BUDGET_S if budget is None else budget
        self.write({'state': 'running', 'start_date': self.start_date or fields.Datetime.now()})
        self.env.cr.commit()
        try:
            translator = self.provider_id._translator()
        except (TranslatorError, Exception) as exc:
            self._add_log(f'[ERRORE] Fornitore non utilizzabile: {exc}')
            self.write({'state': 'failed', 'end_date': fields.Datetime.now()})
            self.env.cr.commit()
            return
        engine = ProductTextTranslation(self.env, translator, self.source_lang, self.scope, self.apply,
                                        provider=self.provider_id, job=self)
        remaining = json.loads(self.remaining_ids or '[]')
        Template = self.env['product.template'].with_context(active_test=False)
        started = time.monotonic()
        consecutive_errors = 0
        processed = 0
        while remaining:
            # Almeno un prodotto per turno: il lavoro avanza sempre.
            if processed and time.monotonic() - started > budget:
                break
            processed += 1
            tmpl = Template.browse(remaining[0]).exists()
            chars_before = engine.chars
            try:
                if tmpl:
                    with self.env.cr.savepoint():
                        done = engine.translate_product(tmpl, self._langs())
                    if done:
                        self.translated_count += 1
                consecutive_errors = 0
            except TranslatorError as exc:
                self.error_count += 1
                consecutive_errors += 1
                self._add_log(f'[ERRORE] {tmpl.display_name}: {exc}')
                if consecutive_errors >= 5:
                    self._add_log('[STOP] 5 errori di fila dal fornitore: lavoro interrotto. Si può riprendere con "Riprendi".')
                    self.write({'state': 'failed', 'remaining_ids': json.dumps(remaining)})
                    self.env.cr.commit()
                    return
            except Exception as exc:  # errore su un prodotto: si registra e si va avanti
                _log.exception('Traduzione prodotto %s fallita', tmpl.id)
                self.error_count += 1
                self._add_log(f'[ERRORE] {tmpl.display_name}: {exc}')
            remaining.pop(0)
            self.write({'done_count': self.done_count + 1, 'remaining_ids': json.dumps(remaining),
                        'chars_count': self.chars_count + engine.chars - chars_before})
            self.env.cr.commit()
        if remaining:
            self.write({'state': 'queued'})
            self.env.cr.commit()
            self.env.ref('ke_product_translate.cron_run_translate_jobs')._trigger()
            return
        self._add_log(f'[FINE] {self.translated_count} prodotti tradotti su {self.total_count}, '
                  f'{self.error_count} errori, {self.chars_count:,} caratteri inviati.'.replace(',', '.'))
        self.write({'state': 'done', 'end_date': fields.Datetime.now()})
        self.env.cr.commit()

    # ------------------------------------------------------------------
    # Pulsanti
    # ------------------------------------------------------------------

    def action_cancel(self):
        self.filtered(lambda j: j.state in ('queued', 'running', 'failed')).write(
            {'state': 'cancelled', 'end_date': fields.Datetime.now()})

    def action_resume(self):
        self.filtered(lambda j: j.state in ('failed', 'cancelled') and json.loads(j.remaining_ids or '[]')).write(
            {'state': 'queued', 'end_date': False})
        self.env.ref('ke_product_translate.cron_run_translate_jobs')._trigger()

    def action_open_proposals(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'Proposte', 'res_model': 'ke.translate.proposal',
            'view_mode': 'list,form', 'domain': [('job_id', '=', self.id)],
            'context': {'search_default_pending': 1},
        }
