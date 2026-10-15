import base64
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


class AmazonReport(models.Model):
    _name = 'ke.amazon.report'
    _description = 'Report Amazon scaricato (per il Full Import)'
    _order = 'create_date desc'
    _rec_name = 'display_name'

    # Nata dalla richiesta dell'utente di poter rivedere/riusare i report
    # grezzi scaricati per il Full Import invece di scartarli subito dopo
    # l'elaborazione (vedi TASK.md) — prima non esisteva nessuna traccia
    # persistente del contenuto originale di un report, rendendo impossibile
    # verificare a posteriori un'anomalia (es. uno SKU con dati incoerenti
    # tra due righe dello stesso giro).
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace', required=True,
        ondelete='cascade', index=True,
    )
    state = fields.Selection([
        ('requested', 'Richiesto'),
        ('ready', 'Pronto (non ancora scaricato)'),
        ('downloaded', 'Scaricato'),
        ('error', 'Errore'),
    ], string='Stato', default='requested', required=True, index=True)
    amazon_report_id = fields.Char('ID report Amazon', readonly=True)
    amazon_document_id = fields.Char('ID documento Amazon', readonly=True)
    request_date = fields.Datetime('Richiesto il', default=fields.Datetime.now, readonly=True)
    download_date = fields.Datetime('Scaricato il', readonly=True)
    row_count = fields.Integer('Righe', readonly=True)
    # Il TSV grezzo (prima del parsing) — vedi SpApiClient.download_report_raw,
    # separato apposta da download_report_rows per poter conservare qui il
    # testo originale invece di scartarlo subito dopo il parsing.
    attachment_id = fields.Many2one(
        'ir.attachment', string='File TSV', readonly=True, ondelete='set null',
    )
    error_message = fields.Text('Errore', readonly=True)
    selected_for_import = fields.Boolean(
        'Usa nel prossimo Full Import', default=False,
        help='Se selezionato, il prossimo Full Import userà questo report '
             'invece di richiederne uno nuovo per questo marketplace. Il '
             'flag si disattiva da solo una volta usato, per non rischiare '
             'di rielaborare per sbaglio un report vecchio in un giro futuro.',
    )

    display_name = fields.Char(compute='_compute_display_name')

    @api.depends('marketplace_id', 'request_date', 'state', 'row_count')
    def _compute_display_name(self):
        for rec in self:
            date_str = rec.request_date.strftime('%d/%m/%Y %H:%M') if rec.request_date else ''
            rows_str = f' ({rec.row_count} righe)' if rec.state == 'downloaded' else ''
            rec.display_name = f'{rec.marketplace_id.name or "?"} — {date_str}{rows_str}'

    def unlink(self):
        attachments = self.mapped('attachment_id')
        res = super().unlink()
        attachments.unlink()
        return res

    def action_download(self):
        """Serve il TSV grezzo per il download diretto dal browser."""
        self.ensure_one()
        if not self.attachment_id:
            raise UserError('Nessun file disponibile per questo report (non ancora scaricato o in errore).')
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self.attachment_id.id}?download=true',
            'target': 'self',
        }

    def get_rows(self):
        """Riparsa il TSV grezzo salvato in attachment_id. Usato dal Full
        Import quando questo report è selezionato invece di richiederne uno
        nuovo ad Amazon."""
        self.ensure_one()
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        if not self.attachment_id or not self.attachment_id.datas:
            return []
        content = base64.b64decode(self.attachment_id.datas).decode('utf-8', errors='replace')
        return SpApiClient.parse_report_text(content)

    @api.model
    def action_request_new(self, marketplace_ids=None):
        """
        Crea un record per ciascun marketplace indicato (o per tutti i
        marketplace attivi se marketplace_ids è vuoto) e avvia un thread di
        download in background per ciascuno — stesso pattern _start_thread
        già usato per le sync principali (vedi amazon_sync_log.py), qui
        duplicato in piccolo perché il ciclo richiesta→poll→download di un
        singolo report è troppo semplice per giustificare una dipendenza
        incrociata dal modello ke.amazon.sync.log.
        """
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        if marketplace_ids:
            marketplaces = self.env['ke.amazon.marketplace'].browse(marketplace_ids)
        else:
            marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            raise UserError('Nessun marketplace attivo.')

        records = self.env['ke.amazon.report']
        for marketplace in marketplaces:
            rec = self.create({
                'marketplace_id': marketplace.id,
                'state': 'requested',
            })
            records |= rec
            self.env.cr.commit()
            self._start_thread(rec.id)
        return records

    def _start_thread(self, report_id):
        import threading
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        t = threading.Thread(
            target=self._thread_download_report,
            args=(db, uid, ctx, report_id),
            daemon=True,
        )
        t.start()

    def _thread_download_report(self, db, uid, ctx, report_id):
        """
        Richiede→attende→scarica un report per un marketplace, lo salva come
        ir.attachment e aggiorna lo stato — stesso schema poll-stretto già
        usato da _thread_full_import (SyncLog._REPORT_POLL_INTERVAL_S/
        _REPORT_POLL_MAX_MINUTES), riusato qui via import diretto invece di
        duplicare le costanti.
        """
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        from odoo.addons.ke_amazon_sync.models.amazon_sync_log import AmazonSyncLog
        import time

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            rec = env['ke.amazon.report'].browse(report_id)
            try:
                marketplace = rec.marketplace_id
                client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)

                report_amazon_id = client.request_listings_report()
                rec.write({'amazon_report_id': report_amazon_id})
                env.cr.commit()

                document_id = None
                deadline = time.monotonic() + AmazonSyncLog._REPORT_POLL_MAX_MINUTES * 60
                while time.monotonic() < deadline:
                    time.sleep(AmazonSyncLog._REPORT_POLL_INTERVAL_S)
                    document_id = client.check_report_ready(report_amazon_id)
                    if document_id:
                        break
                if not document_id:
                    raise RuntimeError(
                        f'Report {report_amazon_id} non pronto dopo '
                        f'{AmazonSyncLog._REPORT_POLL_MAX_MINUTES} minuti di attesa.'
                    )

                rec.write({'amazon_document_id': document_id, 'state': 'ready'})
                env.cr.commit()

                content = client.download_report_raw(document_id)
                rows = SpApiClient.parse_report_text(content)
                attachment = env['ir.attachment'].create({
                    'name': f'amazon_report_{marketplace.code}_{rec.id}.tsv',
                    'type': 'binary',
                    'datas': base64.b64encode(content.encode('utf-8')),
                    'res_model': 'ke.amazon.report',
                    'res_id': rec.id,
                })
                rec.write({
                    'state': 'downloaded',
                    'attachment_id': attachment.id,
                    'row_count': len(rows),
                    'download_date': fields.Datetime.now(),
                })
                env.cr.commit()
            except Exception as exc:
                _log.error('Download report Amazon fallito: %s', exc, exc_info=True)
                env.cr.rollback()
                rec.write({'state': 'error', 'error_message': str(exc)})
                env.cr.commit()
