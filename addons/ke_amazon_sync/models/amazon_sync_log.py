import logging

from odoo import api, fields, models
from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine

_log = logging.getLogger('ke.amazon.sync.cron')


class AmazonSyncLog(models.Model):
    _name = 'ke.amazon.sync.log'
    _description = 'Amazon Sync Log'
    _order = 'create_date desc'
    _rec_name = 'name'

    name = fields.Char('Operazione', required=True)
    sync_type = fields.Selection([
        ('full_import', 'Import Completo'),
        ('check_new', 'Controllo Nuovi Prodotti'),
        ('price_qty', 'Sincronizzazione Prezzi/Quantità'),
        ('download_images', 'Download Immagini'),
        ('reconcile', 'Riconciliazione Store'),
    ], string='Tipo', required=True)
    state = fields.Selection([
        ('running', 'In corso'),
        ('done', 'Completato'),
        ('error', 'Errore'),
    ], string='Stato', default='running')

    products_total = fields.Integer('Prodotti totali')
    products_created = fields.Integer('Creati')
    products_updated = fields.Integer('Aggiornati')
    products_skipped = fields.Integer('Saltati')
    products_error = fields.Integer('Errori')

    start_date = fields.Datetime('Inizio', default=fields.Datetime.now)
    end_date = fields.Datetime('Fine')
    duration = fields.Float('Durata (s)', compute='_compute_duration', store=True)

    log_lines = fields.Text('Log dettagliato')
    error_summary = fields.Text('Sommario errori')

    @api.depends('start_date', 'end_date')
    def _compute_duration(self):
        for rec in self:
            if rec.start_date and rec.end_date:
                rec.duration = (rec.end_date - rec.start_date).total_seconds()
            else:
                rec.duration = 0.0

    def action_open(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_refresh(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_mark_done(self):
        self.ensure_one()
        self.write({'state': 'done', 'end_date': fields.Datetime.now()})

    def action_export_csv(self):
        import base64
        import csv
        import io
        import re

        self.ensure_one()

        # Pattern delle righe del log di riconciliazione
        patterns = [
            ('OK',          re.compile(r'^\[OK\]\s+(.+?)\s+SKU=(\S+)\s+stato=(\S+)\s+prezzo=([\d.]+)€$')),
            ('SOLO_ODOO',   re.compile(r'^\[SOLO ODOO\]\s+(.+?)\s+SKU=(\S+)')),
            ('SOLO_AMAZON', re.compile(r'^\[SOLO AMAZON\]\s+SKU=(\S+)\s+ASIN=(\S+)\s+"(.*)"$')),
            ('ERRORE',      re.compile(r'^\[ERRORE\]\s+(?:variante\s+)?SKU=(\S+):\s+(.+)$')),
        ]

        rows = []
        for line in (self.log_lines or '').splitlines():
            line = line.strip()
            if not line or line.startswith('Store Amazon:') or line.startswith('Download') or line.startswith('Riepilogo'):
                continue

            if line.startswith('[OK]'):
                m = patterns[0][1].match(line)
                if m:
                    rows.append({
                        'tipo': 'OK',
                        'nome': m.group(1),
                        'sku': m.group(2),
                        'asin': '',
                        'stato_amazon': m.group(3),
                        'prezzo_amazon': m.group(4),
                        'note': '',
                    })
            elif line.startswith('[SOLO ODOO]'):
                m = patterns[1][1].match(line)
                if m:
                    rows.append({
                        'tipo': 'SOLO_ODOO',
                        'nome': m.group(1),
                        'sku': m.group(2),
                        'asin': '',
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': 'Presente in Odoo ma non trovato su Amazon',
                    })
            elif line.startswith('[SOLO AMAZON]'):
                m = patterns[2][1].match(line)
                if m:
                    rows.append({
                        'tipo': 'SOLO_AMAZON',
                        'nome': m.group(3),
                        'sku': m.group(1),
                        'asin': m.group(2),
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': 'Presente su Amazon ma non importato in Odoo',
                    })
            elif line.startswith('[ERRORE]'):
                m = patterns[3][1].match(line)
                if m:
                    rows.append({
                        'tipo': 'ERRORE',
                        'nome': '',
                        'sku': m.group(1),
                        'asin': '',
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': m.group(2),
                    })

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=['tipo', 'nome', 'sku', 'asin', 'stato_amazon', 'prezzo_amazon', 'note'],
                                delimiter=';')
        writer.writeheader()
        writer.writerows(rows)

        filename = f'riconciliazione_amazon_{self.id}.csv'
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(('﻿' + buf.getvalue()).encode('utf-8')).decode(),
            'res_model': self._name,
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}/{filename}?download=true',
            'target': 'self',
        }

    def action_stop_import(self):
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.import_state', 'idle')
        self._disable_import_cron()
        self.write({'state': 'done', 'end_date': fields.Datetime.now()})

    def action_start_full_import(self):
        from odoo.exceptions import UserError
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.import_state') == 'running':
            raise UserError('Un import è già in corso. Controlla il log sincronizzazioni.')

        max_products = int(params.get_param('ke_amazon_sync.max_products', '0') or 0)
        limit_label = f' (limite {max_products})' if max_products else ''
        log_rec = self.create({
            'name': f'Import completo{limit_label} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'full_import',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        update_existing = params.get_param('ke_amazon_sync.import_update_existing_default', 'True') != 'False'

        params.set_param('ke_amazon_sync.import_state', 'running')
        params.set_param('ke_amazon_sync.import_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.import_page_token', '')
        params.set_param('ke_amazon_sync.import_update_existing', str(update_existing))
        params.set_param('ke_amazon_sync.import_max_products', str(max_products))
        params.set_param('ke_amazon_sync.import_report_id', '')
        params.set_param('ke_amazon_sync.import_report_attach_id', '')
        params.set_param('ke_amazon_sync.import_report_row_offset', '')

        cron = self.env.ref('ke_amazon_sync.cron_full_import_chunk', raise_if_not_found=False)
        if cron:
            cron.sudo().active = True

        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Import Amazon',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # Numero di righe del report elaborate per ogni invocazione del cron.
    _REPORT_CHUNK_SIZE = 200

    def _cron_full_import_chunk(self):
        """
        Cron che gestisce l'import completo con una state machine a tre fasi:
          Fase 0 – import limitato (max_products ≤ 1000): paginazione classica con page_token
          Fase 1 – nessun page_token e nessun report_id: crea il report Amazon
          Fase 2 – report_id presente ma nessun allegato: controlla se il report è pronto e lo scarica
          Fase 3 – allegato presente: elabora chunk di righe finché non sono esaurite
        """
        import base64
        import json

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.import_state') != 'running':
            self._disable_import_cron()
            return

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron full_import_chunk saltato: credenziali SP-API non configurate.')
            return

        log_id = int(params.get_param('ke_amazon_sync.import_log_id', '0') or 0)
        if not log_id:
            params.set_param('ke_amazon_sync.import_state', 'idle')
            self.env.cr.commit()
            self._disable_import_cron()
            return

        log_rec = self.browse(log_id)
        if not log_rec.exists():
            params.set_param('ke_amazon_sync.import_state', 'idle')
            self.env.cr.commit()
            self._disable_import_cron()
            return

        update_existing = params.get_param('ke_amazon_sync.import_update_existing', 'True') == 'True'
        max_products = int(params.get_param('ke_amazon_sync.import_max_products', '0') or 0)

        # Interrompi se abbiamo già raggiunto il limite
        if max_products and log_rec.products_total >= max_products:
            log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.import_state', 'idle')
            self.env.cr.commit()
            self._disable_import_cron()
            return

        # ---------------------------------------------------------------
        # Fase 0: import limitato (≤ 1000) → usa paginazione classica
        # ---------------------------------------------------------------
        if max_products and max_products <= 1000:
            page_token = params.get_param('ke_amazon_sync.import_page_token') or None
            try:
                next_token = SyncEngine(self.env, client).full_import_chunk(
                    log_rec, page_token=page_token, update_existing=update_existing,
                )
            except Exception as e:
                _log.error('Cron full_import_chunk (Fase 0) fallito: %s', e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.import_state', 'idle')
                params.set_param('ke_amazon_sync.import_page_token', '')
                self.env.cr.commit()
                self._disable_import_cron()
                return
            if next_token:
                params.set_param('ke_amazon_sync.import_page_token', next_token)
            else:
                log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.import_state', 'idle')
                params.set_param('ke_amazon_sync.import_page_token', '')
                self.env.cr.commit()
                self._disable_import_cron()
            return

        # ---------------------------------------------------------------
        # Fasi 1-3: import completo via Reports API (nessun limite)
        # ---------------------------------------------------------------
        report_id = params.get_param('ke_amazon_sync.import_report_id', '')
        attach_id_s = params.get_param('ke_amazon_sync.import_report_attach_id', '')
        row_offset_s = params.get_param('ke_amazon_sync.import_report_row_offset', '')

        # Fase 1: crea il report
        if not report_id:
            try:
                report_id = client.request_listings_report()
            except Exception as e:
                _log.error('Creazione report Amazon fallita: %s', e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.import_state', 'idle')
                self.env.cr.commit()
                self._disable_import_cron()
                return
            params.set_param('ke_amazon_sync.import_report_id', report_id)
            msg = f'Report Amazon richiesto (ID: {report_id}). In attesa di elaborazione (1-10 minuti)...'
            log_rec.write({'log_lines': msg})
            _log.info(msg)
            self.env.cr.commit()
            return

        # Fase 2: scarica il report quando è pronto
        if not attach_id_s:
            try:
                document_id = client.check_report_ready(report_id)
            except Exception as e:
                _log.error('Errore controllo stato report %s: %s', report_id, e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.import_state', 'idle')
                self.env.cr.commit()
                self._disable_import_cron()
                return

            if document_id is None:
                current_log = log_rec.log_lines or ''
                log_rec.write({'log_lines': current_log + '\nReport ancora in elaborazione, attendo...'})
                self.env.cr.commit()
                return

            try:
                rows = client.download_report_rows(document_id)
            except Exception as e:
                _log.error('Download report %s fallito: %s', document_id, e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.import_state', 'idle')
                self.env.cr.commit()
                self._disable_import_cron()
                return

            rows_json = json.dumps(rows, ensure_ascii=False)
            attachment = self.env['ir.attachment'].create({
                'name': f'amazon_report_{log_rec.id}.json',
                'type': 'binary',
                'datas': base64.b64encode(rows_json.encode('utf-8')).decode(),
                'res_model': 'ke.amazon.sync.log',
                'res_id': log_rec.id,
            })
            params.set_param('ke_amazon_sync.import_report_attach_id', str(attachment.id))
            params.set_param('ke_amazon_sync.import_report_row_offset', '0')
            msg = f'Report scaricato: {len(rows)} prodotti totali. Avvio elaborazione...'
            log_rec.write({'log_lines': (log_rec.log_lines or '') + '\n' + msg})
            _log.info(msg)
            self.env.cr.commit()
            return

        # Fase 3: elabora righe a chunk
        try:
            attach_id = int(attach_id_s)
            row_offset = int(row_offset_s or '0')
            attachment = self.env['ir.attachment'].browse(attach_id)
            rows = json.loads(base64.b64decode(attachment.datas).decode('utf-8'))
        except Exception as e:
            _log.error('Errore lettura allegato report: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.import_state', 'idle')
            self.env.cr.commit()
            self._disable_import_cron()
            return

        try:
            next_offset = SyncEngine(self.env, client).full_import_report_rows(
                log_rec, rows, row_offset, self._REPORT_CHUNK_SIZE, update_existing,
            )
        except Exception as e:
            _log.error('Elaborazione report rows fallita: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.import_state', 'idle')
            self.env.cr.commit()
            self._disable_import_cron()
            return

        if next_offset is not None:
            params.set_param('ke_amazon_sync.import_report_row_offset', str(next_offset))
            _log.info('Report import: %d/%d righe elaborate', next_offset, len(rows))
        else:
            # Tutte le righe elaborate: pulizia e chiusura
            attachment.unlink()
            params.set_param('ke_amazon_sync.import_report_id', '')
            params.set_param('ke_amazon_sync.import_report_attach_id', '')
            params.set_param('ke_amazon_sync.import_report_row_offset', '')
            log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.import_state', 'idle')
            self.env.cr.commit()
            self._disable_import_cron()

    def _disable_import_cron(self):
        cron = self.env.ref('ke_amazon_sync.cron_full_import_chunk', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo().active = False
            except Exception:
                # Non è possibile disabilitare il cron mentre è in esecuzione;
                # al prossimo ciclo troverà import_state='idle' e uscirà subito.
                _log.warning('_disable_import_cron: impossibile disabilitare da dentro il cron, '
                             'sarà idle al prossimo ciclo.')

    def _cron_check_new_products(self):
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron check_new_products saltato: credenziali SP-API non configurate.')
            return
        hours = int(self.env['ir.config_parameter'].sudo().get_param(
            'ke_amazon_sync.check_interval_hours', '24'))
        log_rec = self.create({
            'name': f'Controllo nuovi prodotti — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'check_new',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        try:
            client = SpApiClient(self.env)
            engine = SyncEngine(self.env, client)
            found, not_found, errors = engine.check_new_products(log_rec, hours_back=hours)
            _log.info('Controllo nuovi prodotti: %d trovati, %d non trovati, %d errori',
                      found, not_found, errors)
        except Exception as e:
            _log.error('Cron check_new_products fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})

    def _cron_reconcile_store(self):
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron reconcile_store saltato: credenziali SP-API non configurate.')
            return
        log_rec = self.create({
            'name': f'Riconciliazione store — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'reconcile',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        try:
            engine = SyncEngine(self.env, client)
            matched, only_odoo, only_amazon, errors = engine.reconcile_store(log_rec)
            _log.info('Riconciliazione: %d trovati, %d solo Odoo, %d solo Amazon, %d errori',
                      matched, only_odoo, only_amazon, errors)
        except Exception as e:
            _log.error('Cron reconcile_store fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})

    def _cron_sync_prices_quantities(self):
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron sync_prices_quantities saltato: credenziali SP-API non configurate.')
            return
        log_rec = self.create({
            'name': f'Sync prezzi/quantità — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'price_qty',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        try:
            client = SpApiClient(self.env)
            engine = SyncEngine(self.env, client)
            ok, errors = engine.sync_prices_and_quantities(log_rec)
            _log.info('Sync prezzi/quantità: %d ok, %d errori', ok, errors)
        except Exception as e:
            _log.error('Cron sync_prices_quantities fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
