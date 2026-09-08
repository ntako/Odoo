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
        ('apply_cat_mapping', 'Aggiornamento da Catalog API'),
        ('import_orders', 'Import Ordini Amazon'),
        ('sync_fba', 'Sync Giacenze FBA'),
        ('returns', 'Import Resi Amazon'),
        ('seed_fbm_stock', 'Recupero Giacenza FBM da Amazon'),
        ('move_to_general', 'Spostamento Giacenza verso Magazzino Generale'),
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
        """Segna il log come terminato e ferma il processo associato se in corso."""
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        if self.sync_type == 'import_orders':
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            attach_id = int(params.get_param('ke_amazon_sync.orders_import_page_attach_id', '0') or 0)
            if attach_id:
                self.env['ir.attachment'].browse(attach_id).unlink()
            params.set_param('ke_amazon_sync.orders_import_page_attach_id', '')
            params.set_param('ke_amazon_sync.orders_import_offset', '0')
            self._disable_orders_import_cron()
        elif self.sync_type == 'sync_fba':
            # La sync gira in un thread di background (non un cron a pagine):
            # qui possiamo solo liberare lo stato per permettere un nuovo avvio,
            # non fermare un thread già in esecuzione (terminerà da solo).
            params.set_param('ke_amazon_sync.fba_sync_state', 'idle')
            params.set_param('ke_amazon_sync.fba_sync_log_id', '')
        elif self.sync_type == 'returns':
            params.set_param('ke_amazon_sync.returns_state', 'idle')
            self._disable_returns_cron()
        self.write({'state': 'done', 'end_date': fields.Datetime.now()})

    def action_export_csv(self):
        self.ensure_one()
        if self.sync_type == 'full_import':
            return self._export_csv_full_import()
        return self._export_csv_reconcile()

    def _export_csv_reconcile(self):
        import base64
        import csv
        import io
        import re

        self.ensure_one()

        # Pattern delle righe del log di riconciliazione
        pat_ok          = re.compile(r'^\[OK\]\s+(.+?)\s+SKU=(\S+)\s+stato=(\S+)\s+prezzo=([\d.]+)€')
        pat_solo_odoo   = re.compile(r'^\[SOLO ODOO\]\s+(.+?)\s+SKU=(\S+)(?:\s+ASIN=(\S+))?')
        pat_solo_amazon = re.compile(r'^\[SOLO AMAZON\]\s+SKU=(\S+)\s+ASIN=(\S+)\s+"(.*)"$')
        pat_sku_upd_asin = re.compile(r'^\[SKU AGGIORNATO\]\s+(.+?)\s+ASIN=(\S+)\s+(\S+)\s+→\s+(\S+)')
        pat_sku_upd_plus = re.compile(
            r'^\[SKU AGGIORNATO\]\s+(.+?)\s+SKU=(\S+)\s+→\s+(\S+)'
            r'(?:\s+stato=(\S+))?(?:\s+prezzo=([\d.]+)€)?$'
        )
        pat_errore      = re.compile(r'^\[ERRORE\]\s+(?:variante\s+)?SKU=(\S+):\s+(.+)$')

        rows = []
        for line in (self.log_lines or '').splitlines():
            line = line.strip()
            if not line or line.startswith('Store Amazon:') or line.startswith('Download') or line.startswith('Riepilogo'):
                continue

            if line.startswith('[OK]'):
                m = pat_ok.match(line)
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
                m = pat_solo_odoo.match(line)
                if m:
                    rows.append({
                        'tipo': 'SOLO_ODOO',
                        'nome': m.group(1),
                        'sku': m.group(2),
                        'asin': m.group(3) or '',
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': 'Presente in Odoo ma non trovato su Amazon',
                    })
            elif line.startswith('[SOLO AMAZON]'):
                m = pat_solo_amazon.match(line)
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
            elif line.startswith('[SKU AGGIORNATO]'):
                if 'ASIN=' in line:
                    m = pat_sku_upd_asin.match(line)
                    if m:
                        rows.append({
                            'tipo': 'SKU_AGGIORNATO',
                            'nome': m.group(1),
                            'sku': m.group(4),
                            'asin': m.group(2),
                            'stato_amazon': '',
                            'prezzo_amazon': '',
                            'note': f'SKU aggiornato da {m.group(3)} (rifornimento)',
                        })
                else:
                    m = pat_sku_upd_plus.match(line)
                    if m:
                        rows.append({
                            'tipo': 'SKU_AGGIORNATO',
                            'nome': m.group(1),
                            'sku': m.group(3),
                            'asin': '',
                            'stato_amazon': m.group(4) or '',
                            'prezzo_amazon': m.group(5) or '',
                            'note': f'SKU aggiornato da {m.group(2)} (rifornimento)',
                        })
            elif line.startswith('[ERRORE]'):
                m = pat_errore.match(line)
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

    def _export_csv_full_import(self):
        import base64
        import csv
        import io
        import re

        self.ensure_one()

        # Pattern delle righe del log di import (full_import / full_import_chunk / full_import_report_rows)
        pat_creato           = re.compile(r'^CREATO:\s+(\S+)\s+/\s+(\S+)\s+—\s+(.*)$')
        pat_creato_variante  = re.compile(r'^CREATO variante:\s+(\S+)\s+/\s+(\S+)\s+\[(.*?)\]\s+→\s+(.*)$')
        pat_aggiornato       = re.compile(r'^AGGIORNATO:\s+(\S+)\s+/\s+(\S+)\s+—\s+(.*)$')
        pat_aggiornato_var   = re.compile(r'^AGGIORNATO variante:\s+(\S+)\s+/\s+(\S+)\s+→\s+(.*)$')
        pat_saltato_sost     = re.compile(r'^SALTATO \(sostituito da (\S+)\):\s+(\S+)$')
        pat_saltato_esiste   = re.compile(r'^SALTATO \((?:già esiste|variante già esiste)\):\s+(\S+)$')
        pat_saltato_parent   = re.compile(r'^SALTATO \(parent listing\):\s+(\S+)\s+/\s+(\S+)$')
        pat_saltato_dim      = re.compile(r'^SALTATO\s+(\S+)\s+/\s+(\S+):\s+(.*)$')
        pat_errore_var_crea  = re.compile(r'^ERRORE creazione variante:\s+(\S+)\s+/\s+(\S+)$')
        pat_errore_variante  = re.compile(r'^ERRORE variante\s+(\S+):\s+(.*)$')
        pat_errore           = re.compile(r'^ERRORE\s+(\S+):\s+(.*)$')

        rows = []
        unmatched = 0
        for raw_line in (self.log_lines or '').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('Report ') or line.startswith('Store Amazon'):
                continue

            m = pat_creato_variante.match(line)
            if m:
                rows.append({'tipo': 'CREATO_VARIANTE', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(4), 'note': m.group(3)})
                continue
            m = pat_creato.match(line)
            if m:
                rows.append({'tipo': 'CREATO', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(3), 'note': ''})
                continue
            m = pat_aggiornato_var.match(line)
            if m:
                rows.append({'tipo': 'AGGIORNATO_VARIANTE', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(3), 'note': ''})
                continue
            m = pat_aggiornato.match(line)
            if m:
                rows.append({'tipo': 'AGGIORNATO', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(3), 'note': ''})
                continue
            m = pat_saltato_sost.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(2), 'asin': '', 'nome': '',
                             'note': f'sostituito da {m.group(1)}'})
                continue
            m = pat_saltato_esiste.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(1), 'asin': '', 'nome': '',
                             'note': 'già esistente in Odoo'})
                continue
            m = pat_saltato_parent.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(1), 'asin': m.group(2), 'nome': '',
                             'note': 'parent listing'})
                continue
            m = pat_saltato_dim.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(1), 'asin': m.group(2), 'nome': '',
                             'note': m.group(3)})
                continue
            m = pat_errore_var_crea.match(line)
            if m:
                rows.append({'tipo': 'ERRORE', 'sku': m.group(1), 'asin': m.group(2), 'nome': '',
                             'note': 'errore creazione variante'})
                continue
            m = pat_errore_variante.match(line)
            if m:
                rows.append({'tipo': 'ERRORE', 'sku': m.group(1), 'asin': '', 'nome': '',
                             'note': m.group(2)})
                continue
            m = pat_errore.match(line)
            if m:
                rows.append({'tipo': 'ERRORE', 'sku': m.group(1), 'asin': '', 'nome': '',
                             'note': m.group(2)})
                continue

            # Riga non riconosciuta dal parser: esportata comunque per non perdere informazioni
            unmatched += 1
            rows.append({'tipo': 'ALTRO', 'sku': '', 'asin': '', 'nome': '', 'note': line})

        if unmatched:
            _log.info('Export CSV import log %d: %d righe non riconosciute dal parser (tipo=ALTRO)',
                       self.id, unmatched)

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=['tipo', 'sku', 'asin', 'nome', 'note'], delimiter=';')
        writer.writeheader()
        writer.writerows(rows)

        filename = f'import_amazon_{self.id}.csv'
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

    def _cron_reconcile_store(self, name_prefix='Riconciliazione store'):
        """
        Avvia una nuova riconciliazione via Reports API (nessun limite di prodotti).
        Crea il log, imposta i parametri di stato e abilita il cron di elaborazione chunk.
        Usata sia dal cron automatico sia dal trigger manuale (impostazioni).
        Ritorna il log creato, o None se non è stato possibile avviarla.
        """
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron reconcile_store saltato: credenziali SP-API non configurate.')
            return None
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.reconcile_state') == 'running':
            _log.info('Riconciliazione già in corso, salto avvio.')
            return None
        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'reconcile',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        params.set_param('ke_amazon_sync.reconcile_state', 'running')
        params.set_param('ke_amazon_sync.reconcile_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.reconcile_report_id', '')
        params.set_param('ke_amazon_sync.reconcile_report_attach_id', '')
        self.env.cr.commit()
        cron = self.env.ref('ke_amazon_sync.cron_reconcile_chunk', raise_if_not_found=False)
        if cron:
            cron.sudo().active = True
        return log_rec

    def _cron_reconcile_chunk(self):
        """
        Cron di elaborazione riconciliazione (ogni 2 min, abilitato da _cron_reconcile_store).
        State machine a tre fasi:
          Fase 1 – nessun report_id: richiede il report Amazon
          Fase 2 – report_id presente ma nessun allegato: controlla/scarica il report
          Fase 3 – allegato presente: esegue la riconciliazione e chiude
        """
        import base64
        import json

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.reconcile_state') != 'running':
            self._disable_reconcile_cron()
            return

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron reconcile_chunk saltato: credenziali SP-API non configurate.')
            return

        log_id = int(params.get_param('ke_amazon_sync.reconcile_log_id', '0') or 0)
        if not log_id:
            params.set_param('ke_amazon_sync.reconcile_state', 'idle')
            self.env.cr.commit()
            self._disable_reconcile_cron()
            return

        log_rec = self.browse(log_id)
        if not log_rec.exists():
            params.set_param('ke_amazon_sync.reconcile_state', 'idle')
            self.env.cr.commit()
            self._disable_reconcile_cron()
            return

        report_id = params.get_param('ke_amazon_sync.reconcile_report_id', '')
        attach_id_s = params.get_param('ke_amazon_sync.reconcile_report_attach_id', '')

        # Fase 1: crea il report
        if not report_id:
            try:
                report_id = client.request_listings_report()
            except Exception as e:
                _log.error('Creazione report per riconciliazione fallita: %s', e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.reconcile_state', 'idle')
                self.env.cr.commit()
                self._disable_reconcile_cron()
                return
            params.set_param('ke_amazon_sync.reconcile_report_id', report_id)
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
                params.set_param('ke_amazon_sync.reconcile_state', 'idle')
                self.env.cr.commit()
                self._disable_reconcile_cron()
                return

            if document_id is None:
                current_log = log_rec.log_lines or ''
                log_rec.write({'log_lines': current_log + '\nReport ancora in elaborazione, attendo...'})
                self.env.cr.commit()
                return

            try:
                rows = client.download_report_rows(document_id)
            except Exception as e:
                _log.error('Download report riconciliazione %s fallito: %s', document_id, e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.reconcile_state', 'idle')
                self.env.cr.commit()
                self._disable_reconcile_cron()
                return

            rows_json = json.dumps(rows, ensure_ascii=False)
            attachment = self.env['ir.attachment'].create({
                'name': f'amazon_reconcile_report_{log_rec.id}.json',
                'type': 'binary',
                'datas': base64.b64encode(rows_json.encode('utf-8')).decode(),
                'res_model': 'ke.amazon.sync.log',
                'res_id': log_rec.id,
            })
            params.set_param('ke_amazon_sync.reconcile_report_attach_id', str(attachment.id))
            msg = f'Report scaricato: {len(rows)} listing totali. Avvio riconciliazione...'
            log_rec.write({'log_lines': (log_rec.log_lines or '') + '\n' + msg})
            _log.info(msg)
            self.env.cr.commit()
            return

        # Fase 3: esegui la riconciliazione
        try:
            attach_id = int(attach_id_s)
            attachment = self.env['ir.attachment'].browse(attach_id)
            rows = json.loads(base64.b64decode(attachment.datas).decode('utf-8'))
        except Exception as e:
            _log.error('Errore lettura allegato report riconciliazione: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.reconcile_state', 'idle')
            self.env.cr.commit()
            self._disable_reconcile_cron()
            return

        try:
            engine = SyncEngine(self.env, client)
            matched, only_odoo, only_amazon, errors = engine.reconcile_from_report_rows(log_rec, rows)
            _log.info('Riconciliazione: %d trovati, %d solo Odoo, %d solo Amazon, %d errori',
                      matched, only_odoo, only_amazon, errors)
        except Exception as e:
            _log.error('Cron reconcile_chunk fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.reconcile_state', 'idle')
            self.env.cr.commit()
            self._disable_reconcile_cron()
            return

        attachment.unlink()
        params.set_param('ke_amazon_sync.reconcile_report_id', '')
        params.set_param('ke_amazon_sync.reconcile_report_attach_id', '')
        params.set_param('ke_amazon_sync.reconcile_state', 'idle')
        params.set_param('ke_amazon_sync.reconcile_log_id', '')
        self.env.cr.commit()
        self._disable_reconcile_cron()

    def _disable_reconcile_cron(self):
        cron = self.env.ref('ke_amazon_sync.cron_reconcile_chunk', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo().active = False
            except Exception:
                _log.warning('_disable_reconcile_cron: impossibile disabilitare da dentro il cron, '
                             'sarà idle al prossimo ciclo.')

    _CAT_MAP_CHUNK_SIZE = 50

    def action_start_apply_category_mapping(self):
        from odoo.exceptions import UserError

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.cat_map_state') == 'running':
            raise UserError('Mapping categorie già in corso. Controlla il log sincronizzazioni.')

        log_rec = self.create({
            'name': f'Aggiornamento da Catalog API — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'apply_cat_mapping',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        params.set_param('ke_amazon_sync.cat_map_state', 'running')
        params.set_param('ke_amazon_sync.cat_map_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.cat_map_offset', '0')
        self.env.cr.commit()

        cron = self.env.ref('ke_amazon_sync.cron_apply_category_mapping', raise_if_not_found=False)
        if cron:
            cron.sudo().active = True

        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Mapping Categorie',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _cron_apply_category_mapping(self):
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.cat_map_state') != 'running':
            self._disable_cat_map_cron()
            return

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron apply_category_mapping saltato: credenziali SP-API non configurate.')
            return

        log_id = int(params.get_param('ke_amazon_sync.cat_map_log_id', '0') or 0)
        if not log_id:
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            return

        log_rec = self.browse(log_id)
        if not log_rec.exists():
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            return

        offset = int(params.get_param('ke_amazon_sync.cat_map_offset', '0') or 0)
        try:
            engine = SyncEngine(self.env, client)
            next_offset, total = engine.apply_category_mapping(log_rec, offset, self._CAT_MAP_CHUNK_SIZE)
        except Exception as e:
            _log.error('Cron apply_category_mapping fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            return

        if next_offset is None:
            log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            params.set_param('ke_amazon_sync.cat_map_offset', '')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            _log.info('apply_category_mapping completato: %d prodotti totali.', total)
        else:
            params.set_param('ke_amazon_sync.cat_map_offset', str(next_offset))
            _log.info('apply_category_mapping: %d/%d prodotti elaborati.', next_offset, total)

    def _disable_cat_map_cron(self):
        cron = self.env.ref('ke_amazon_sync.cron_apply_category_mapping', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo().active = False
            except Exception:
                _log.warning('_disable_cat_map_cron: impossibile disabilitare da dentro il cron, '
                             'sarà idle al prossimo ciclo.')

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

    # ------------------------------------------------------------------
    # Import Ordini Amazon
    # ------------------------------------------------------------------

    def _cron_import_orders_store(self):
        """
        Avvia automaticamente un nuovo import ordini, se non ce n'è già uno in
        corso. Schedulato periodicamente per tenere sincronizzati gli ordini senza
        bisogno di avviarlo manualmente ogni volta (stesso pattern di
        _cron_reconcile_store per i prodotti).
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.orders_import_state') == 'running':
            _log.info('Import ordini già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron import_orders_store saltato: credenziali SP-API non configurate.')
            return
        self.action_start_import_orders(name_prefix='Import Ordini Amazon (automatico)')

    def action_start_import_orders(self, name_prefix='Import Ordini Amazon'):
        """Avvia l'import degli ordini Amazon degli ultimi N giorni."""
        from datetime import timedelta, timezone
        params = self.env['ir.config_parameter'].sudo()
        days_back = int(params.get_param('ke_amazon_sync.orders_days_back', '30'))
        created_after = (
            fields.Datetime.now().replace(tzinfo=timezone.utc) - timedelta(days=days_back)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'import_orders',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Importazione ordini degli ultimi {days_back} giorni (da {created_after[:10]}).',
        })
        params.set_param('ke_amazon_sync.orders_import_state', 'running')
        params.set_param('ke_amazon_sync.orders_import_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.orders_import_next_token', '')
        params.set_param('ke_amazon_sync.orders_import_created_after', created_after)
        params.set_param('ke_amazon_sync.orders_import_page_attach_id', '')
        params.set_param('ke_amazon_sync.orders_import_offset', '0')
        self.env.cr.commit()

        cron = self.env.ref('ke_amazon_sync.cron_import_orders_chunk', raise_if_not_found=False)
        if cron:
            cron.sudo().write({'active': True, 'nextcall': fields.Datetime.now()})

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _cron_import_orders_chunk(self):
        """
        State machine a due fasi, per evitare che una singola invocazione superi
        il --limit-time-real-cron del server (il rate-limit Amazon di 2s per ordine
        può far durare una pagina intera diversi minuti):
          Fase 1 – nessuna pagina in cache: scarica una pagina di ordini Amazon
                   (fino a 100) e la mette in cache in un ir.attachment
          Fase 2 – pagina in cache: elabora un blocco di _ORDERS_CHUNK_SIZE ordini
                   per invocazione, finché la pagina non è esaurita
        """
        import base64
        import json

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.orders_import_state') != 'running':
            self._disable_orders_import_cron()
            return

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as e:
            _log.warning('Cron import_orders saltato: %s', e)
            log_id = int(params.get_param('ke_amazon_sync.orders_import_log_id', '0') or 0)
            if log_id:
                log_rec = self.browse(log_id)
                if log_rec.exists():
                    log_rec.write({'state': 'error', 'error_summary': str(e),
                                   'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            self.env.cr.commit()
            self._disable_orders_import_cron()
            return

        log_id = int(params.get_param('ke_amazon_sync.orders_import_log_id', '0') or 0)
        if not log_id:
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            self.env.cr.commit()
            self._disable_orders_import_cron()
            return

        log_rec = self.browse(log_id)
        if not log_rec.exists():
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            self.env.cr.commit()
            self._disable_orders_import_cron()
            return

        created_after = params.get_param('ke_amazon_sync.orders_import_created_after', '')
        page_attach_id_s = params.get_param('ke_amazon_sync.orders_import_page_attach_id', '')
        engine = SyncEngine(self.env, client)

        # Fase 1: nessuna pagina in cache → scaricane una nuova
        if not page_attach_id_s:
            next_token = params.get_param('ke_amazon_sync.orders_import_next_token') or None
            try:
                orders, new_next_token = engine.fetch_orders_page(created_after, next_token)
            except Exception as e:
                _log.error('Download pagina ordini fallito: %s', e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.orders_import_state', 'idle')
                self.env.cr.commit()
                self._disable_orders_import_cron()
                return

            if not orders:
                log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.orders_import_state', 'idle')
                params.set_param('ke_amazon_sync.orders_import_next_token', '')
                self.env.cr.commit()
                self._disable_orders_import_cron()
                return

            orders_json = json.dumps(orders, ensure_ascii=False)
            attachment = self.env['ir.attachment'].create({
                'name': f'amazon_orders_page_{log_rec.id}.json',
                'type': 'binary',
                'datas': base64.b64encode(orders_json.encode('utf-8')).decode(),
                'res_model': 'ke.amazon.sync.log',
                'res_id': log_rec.id,
            })
            params.set_param('ke_amazon_sync.orders_import_page_attach_id', str(attachment.id))
            params.set_param('ke_amazon_sync.orders_import_offset', '0')
            params.set_param('ke_amazon_sync.orders_import_next_token', new_next_token or '')
            self.env.cr.commit()
            return

        # Fase 2: pagina in cache → elabora un blocco
        try:
            attach_id = int(page_attach_id_s)
            attachment = self.env['ir.attachment'].browse(attach_id)
            orders = json.loads(base64.b64decode(attachment.datas).decode('utf-8'))
        except Exception as e:
            _log.error('Errore lettura pagina ordini in cache: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            self.env.cr.commit()
            self._disable_orders_import_cron()
            return

        offset = int(params.get_param('ke_amazon_sync.orders_import_offset', '0') or 0)

        try:
            new_offset = engine.import_orders_chunk(log_rec, orders, offset)
        except Exception as e:
            _log.error('Cron import_orders_chunk fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            self.env.cr.commit()
            self._disable_orders_import_cron()
            return

        if new_offset is not None:
            params.set_param('ke_amazon_sync.orders_import_offset', str(new_offset))
            return

        # Pagina esaurita: pulizia; se non c'è un'altra pagina, l'import è completo
        attachment.unlink()
        params.set_param('ke_amazon_sync.orders_import_page_attach_id', '')
        params.set_param('ke_amazon_sync.orders_import_offset', '0')
        next_token = params.get_param('ke_amazon_sync.orders_import_next_token', '')
        if not next_token:
            log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            self.env.cr.commit()
            self._disable_orders_import_cron()
        else:
            self.env.cr.commit()

    def _disable_orders_import_cron(self):
        cron = self.env.ref('ke_amazon_sync.cron_import_orders_chunk', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo().active = False
            except Exception:
                _log.warning('_disable_orders_import_cron: impossibile disabilitare il cron.')

    # ------------------------------------------------------------------
    # Sync Giacenze FBA
    # ------------------------------------------------------------------

    def action_start_sync_fba_quantities(self, name_prefix='Sync Giacenze FBA'):
        """
        Avvia la sincronizzazione delle giacenze FBA da Amazon verso Odoo in un
        thread di background (stesso pattern di _thread_seed_fbm_stock ecc. in
        res_config_settings.py), non tramite cron a pagine: il nextToken
        restituito dall'Inventory API scade in pochi minuti, quindi se le
        pagine venissero richieste a distanza di tick di cron (ogni 2 min) il
        token risulterebbe già scaduto ("Next token is invalid or expired").
        Un thread continua a scorrere le pagine senza pause tra una chiamata e
        l'altra (a parte il rate-limit di ~0.5s), e non è soggetto al limite
        di tempo reale del cron perché non è un thread cron.
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.fba_sync_state') == 'running':
            log_id = int(params.get_param('ke_amazon_sync.fba_sync_log_id', '0') or 0)
            if log_id and self.browse(log_id).exists():
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': self._name,
                    'res_id': log_id,
                    'view_mode': 'form',
                    'target': 'current',
                }

        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_fba',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': 'Avvio sync giacenze FBA...',
        })
        params.set_param('ke_amazon_sync.fba_sync_state', 'running')
        params.set_param('ke_amazon_sync.fba_sync_log_id', str(log_rec.id))
        self.env.cr.commit()

        self._start_fba_sync_thread(log_rec.id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _start_fba_sync_thread(self, log_id):
        import threading
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        t = threading.Thread(
            target=self._thread_sync_fba_quantities,
            args=(db, uid, ctx, log_id),
            daemon=True,
        )
        t.start()

    # Se il nextToken risulta scaduto/non valido nonostante l'elaborazione
    # continua (es. una pagina particolarmente lenta), ripartiamo da capo
    # invece di fallire l'intera sync: l'aggiornamento quantità è idempotente,
    # quindi rielaborare pagine già fatte è solo tempo perso, non un errore.
    # Limitato a poche ripartenze per non girare all'infinito se la causa
    # fosse in realtà un altro problema permanente.
    _FBA_SYNC_MAX_TOKEN_RESTARTS = 5

    def _thread_sync_fba_quantities(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            params = env['ir.config_parameter'].sudo()
            client = SpApiClient(env)
            engine = SyncEngine(env, client)
            page_token = None
            token_restarts = 0
            try:
                while True:
                    try:
                        new_page_token = engine.sync_fba_quantities_page(log_rec, page_token)
                    except Exception as exc:
                        msg = str(exc).lower()
                        is_token_error = 'token' in msg and ('expired' in msg or 'invalid' in msg)
                        if is_token_error and token_restarts < self._FBA_SYNC_MAX_TOKEN_RESTARTS:
                            token_restarts += 1
                            _log.warning(
                                'sync_fba: nextToken scaduto/non valido, riparto dalla prima '
                                'pagina (tentativo %d/%d): %s',
                                token_restarts, self._FBA_SYNC_MAX_TOKEN_RESTARTS, exc,
                            )
                            page_token = None
                            continue
                        raise
                    if not new_page_token:
                        log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
                        break
                    page_token = new_page_token
            except Exception as exc:
                _log.error('Thread sync_fba_quantities fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': fields.Datetime.now()})
            finally:
                params.set_param('ke_amazon_sync.fba_sync_state', 'idle')
                params.set_param('ke_amazon_sync.fba_sync_log_id', '')
                cr.commit()

    def _cron_sync_fba_quantities_store(self):
        """
        Avvia automaticamente una nuova sync giacenze FBA, se non ce n'è già una
        in corso. Schedulata periodicamente (ogni N ore) — stesso pattern di
        _cron_reconcile_store/_cron_import_orders_store, ma qui la sync vera e
        propria gira in un thread di background (vedi action_start_sync_fba_quantities),
        non in ulteriori cron a pagine.
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.fba_sync_state') == 'running':
            _log.info('Sync giacenze FBA già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron sync_fba_store saltato: credenziali SP-API non configurate.')
            return
        self.action_start_sync_fba_quantities(name_prefix='Sync Giacenze FBA (automatico)')

    # ------------------------------------------------------------------
    # Download immagini mancanti
    # ------------------------------------------------------------------

    def _cron_download_images_store(self):
        """
        Avvia automaticamente il download delle immagini mancanti, se non ce
        n'è già uno in corso. Schedulata periodicamente — stesso pattern di
        _cron_sync_fba_quantities_store, ma qui il lavoro vero e proprio gira
        nel thread già usato dal pulsante manuale "Scarica immagini mancanti"
        (Impostazioni → Amazon Seller Central), riusato tale e quale.
        Scarica solo per i prodotti che ancora non hanno un'immagine — non
        ricontrolla quelle già scaricate (vedi download_missing_images).
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.download_images_state') == 'running':
            _log.info('Download immagini già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron download_images_store saltato: credenziali SP-API non configurate.')
            return

        log_rec = self.create({
            'name': f'Download immagini mancanti (automatico) — '
                    f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'download_images',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        params.set_param('ke_amazon_sync.download_images_state', 'running')
        self.env.cr.commit()
        self.env['res.config.settings']._start_thread('_thread_download_images', log_rec.id)

    # ------------------------------------------------------------------
    # Import Resi Amazon
    # ------------------------------------------------------------------

    def action_start_returns_report(self):
        """Avvia l'import dei resi cliente FBA degli ultimi N giorni."""
        from datetime import timedelta, timezone
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.returns_state') == 'running':
            log_id = int(params.get_param('ke_amazon_sync.returns_log_id', '0') or 0)
            if log_id and self.browse(log_id).exists():
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': self._name,
                    'res_id': log_id,
                    'view_mode': 'form',
                    'target': 'current',
                }
        days_back = int(params.get_param('ke_amazon_sync.returns_days_back', '30'))
        data_start_time = (
            fields.Datetime.now().replace(tzinfo=timezone.utc) - timedelta(days=days_back)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        log_rec = self.create({
            'name': f'Import Resi Amazon — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'returns',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Richiesta report resi degli ultimi {days_back} giorni (da {data_start_time[:10]}).',
        })
        params.set_param('ke_amazon_sync.returns_state', 'running')
        params.set_param('ke_amazon_sync.returns_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.returns_report_id', '')
        params.set_param('ke_amazon_sync.returns_report_attach_id', '')
        params.set_param('ke_amazon_sync.returns_data_start_time', data_start_time)
        self.env.cr.commit()

        cron = self.env.ref('ke_amazon_sync.cron_returns_chunk', raise_if_not_found=False)
        if cron:
            cron.sudo().write({'active': True, 'nextcall': fields.Datetime.now()})

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _cron_returns_chunk(self):
        """
        Cron di elaborazione resi (ogni 2 min, abilitato da action_start_returns_report).
        State machine a tre fasi, stesso schema di _cron_reconcile_chunk:
          Fase 1 – nessun report_id: richiede il report resi ad Amazon
          Fase 2 – report_id presente ma nessun allegato: controlla/scarica il report
          Fase 3 – allegato presente: abbina i resi agli ordini e chiude
        """
        import base64
        import json

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.returns_state') != 'running':
            self._disable_returns_cron()
            return

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron returns_chunk saltato: credenziali SP-API non configurate.')
            return

        log_id = int(params.get_param('ke_amazon_sync.returns_log_id', '0') or 0)
        if not log_id:
            params.set_param('ke_amazon_sync.returns_state', 'idle')
            self.env.cr.commit()
            self._disable_returns_cron()
            return

        log_rec = self.browse(log_id)
        if not log_rec.exists():
            params.set_param('ke_amazon_sync.returns_state', 'idle')
            self.env.cr.commit()
            self._disable_returns_cron()
            return

        report_id = params.get_param('ke_amazon_sync.returns_report_id', '')
        attach_id_s = params.get_param('ke_amazon_sync.returns_report_attach_id', '')

        # Fase 1: crea il report
        if not report_id:
            data_start_time = params.get_param('ke_amazon_sync.returns_data_start_time', '')
            try:
                report_id = client.request_returns_report(data_start_time)
            except Exception as e:
                _log.error('Creazione report resi fallita: %s', e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.returns_state', 'idle')
                self.env.cr.commit()
                self._disable_returns_cron()
                return
            params.set_param('ke_amazon_sync.returns_report_id', report_id)
            msg = f'Report resi richiesto (ID: {report_id}). In attesa di elaborazione (1-10 minuti)...'
            log_rec.write({'log_lines': (log_rec.log_lines or '') + '\n' + msg})
            _log.info(msg)
            self.env.cr.commit()
            return

        # Fase 2: scarica il report quando è pronto
        if not attach_id_s:
            try:
                document_id = client.check_report_ready(report_id)
            except Exception as e:
                _log.error('Errore controllo stato report resi %s: %s', report_id, e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.returns_state', 'idle')
                self.env.cr.commit()
                self._disable_returns_cron()
                return

            if document_id is None:
                current_log = log_rec.log_lines or ''
                log_rec.write({'log_lines': current_log + '\nReport ancora in elaborazione, attendo...'})
                self.env.cr.commit()
                return

            try:
                rows = client.download_report_rows(document_id)
            except Exception as e:
                _log.error('Download report resi %s fallito: %s', document_id, e, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.returns_state', 'idle')
                self.env.cr.commit()
                self._disable_returns_cron()
                return

            rows_json = json.dumps(rows, ensure_ascii=False)
            attachment = self.env['ir.attachment'].create({
                'name': f'amazon_returns_report_{log_rec.id}.json',
                'type': 'binary',
                'datas': base64.b64encode(rows_json.encode('utf-8')).decode(),
                'res_model': 'ke.amazon.sync.log',
                'res_id': log_rec.id,
            })
            params.set_param('ke_amazon_sync.returns_report_attach_id', str(attachment.id))
            msg = f'Report scaricato: {len(rows)} resi totali. Abbinamento agli ordini...'
            log_rec.write({'log_lines': (log_rec.log_lines or '') + '\n' + msg})
            _log.info(msg)
            self.env.cr.commit()
            return

        # Fase 3: abbina i resi agli ordini
        try:
            attach_id = int(attach_id_s)
            attachment = self.env['ir.attachment'].browse(attach_id)
            rows = json.loads(base64.b64decode(attachment.datas).decode('utf-8'))
        except Exception as e:
            _log.error('Errore lettura allegato report resi: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.returns_state', 'idle')
            self.env.cr.commit()
            self._disable_returns_cron()
            return

        try:
            engine = SyncEngine(self.env, client)
            matched, not_found, errors = engine.process_returns_report_rows(log_rec, rows)
            _log.info('Import resi: %d abbinati, %d ordini non trovati, %d errori',
                      matched, not_found, errors)
        except Exception as e:
            _log.error('Cron returns_chunk fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.returns_state', 'idle')
            self.env.cr.commit()
            self._disable_returns_cron()
            return

        attachment.unlink()
        params.set_param('ke_amazon_sync.returns_report_id', '')
        params.set_param('ke_amazon_sync.returns_report_attach_id', '')
        params.set_param('ke_amazon_sync.returns_state', 'idle')
        params.set_param('ke_amazon_sync.returns_log_id', '')
        self.env.cr.commit()
        self._disable_returns_cron()

    def _disable_returns_cron(self):
        cron = self.env.ref('ke_amazon_sync.cron_returns_chunk', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo().active = False
            except Exception:
                _log.warning('_disable_returns_cron: impossibile disabilitare il cron.')
