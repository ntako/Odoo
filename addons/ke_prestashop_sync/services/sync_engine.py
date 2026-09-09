"""
Motore di sincronizzazione PrestaShop ↔ Odoo.

sync_stock_to_prestashop   – Odoo qty_available → PS stock_available (cron + manuale)
sync_prices_to_prestashop  – Odoo list_price / price_extra → PS price (cron + manuale)
import_products_from_ps    – PS catalogo → crea/aggiorna product.template in Odoo (wizard)
export_products_to_ps      – product.template Odoo → crea/aggiorna prodotto su PS (wizard)
import_orders_from_ps      – PS ordini → sale.order in Odoo (cron + manuale)
sync_images_to_prestashop  – Odoo image_1920 → PS immagine prodotto (cron + manuale)
"""

import base64
import logging
import re
from datetime import datetime, timedelta

from odoo import fields

_log = logging.getLogger(__name__)

_TIMEOUT_STOCK_ID = 30  # secondi per singola chiamata PS stock


def _strip_html(html):
    """Rimuove tag HTML da una stringa."""
    return re.sub(r'<[^>]+>', '', html or '').strip()


class SyncEngine:

    # Ogni quanti elementi scrivere l'avanzamento sul log e controllare
    # se è stata richiesta l'interruzione.
    _PROGRESS_EVERY = 5

    def __init__(self, env, client):
        self.env = env
        self.client = client

    def _param(self, key, default=None):
        return self.env['ir.config_parameter'].sudo().get_param(
            f'ke_prestashop_sync.{key}', default)

    def _flag(self, key):
        return self._param(key) in ('True', 'true', '1', True)

    def _warehouse(self):
        """Magazzino configurato per PrestaShop (fallback: il primo disponibile)."""
        wh_id = int(self._param('warehouse_id') or 0)
        if wh_id:
            wh = self.env['stock.warehouse'].browse(wh_id).exists()
            if wh:
                return wh
        return self.env['stock.warehouse'].search([], limit=1)

    def _wh_filter_active(self):
        """True se va sincronizzato solo l'assortimento del magazzino."""
        return self._flag('filter_by_warehouse') and bool(self._warehouse())

    def _wh_qty_context(self):
        """Context per calcolare qty_available nello scope del magazzino."""
        wh = self._warehouse()
        return {'warehouse': wh.id} if wh else {}

    def _product_templates(self, domain, apply_qty_filter=True):
        """search su product.template applicando (se attivo) il filtro
        'giacenza > 0 nel magazzino configurato' e il relativo context.
        """
        model = self.env['product.template']
        if self._wh_filter_active():
            model = model.with_context(**self._wh_qty_context())
            if apply_qty_filter:
                domain = list(domain) + [('qty_available', '>', 0.0)]
        return model.search(domain)

    def _tick(self, log_rec, done, total, lines, counters):
        """Aggiorna il log con l'avanzamento (ogni _PROGRESS_EVERY elementi),
        committa e restituisce True se l'utente ha chiesto l'interruzione.
        """
        if done % self._PROGRESS_EVERY and done != total:
            return False
        _log.info('PS sync %s: %s/%s', log_rec.sync_type, done, total)
        log_rec.write({
            'progress_done': done,
            'progress_total': total,
            'log_lines': '\n'.join(lines[-1000:]),
            **counters,
        })
        log_rec.env.cr.commit()
        log_rec.invalidate_recordset(['cancel_requested'])
        return log_rec.cancel_requested

    @staticmethod
    def _finalize_cancelled(log_rec, lines, counters):
        lines.append('[STOP] Sincronizzazione interrotta dall\'utente')
        log_rec.write({
            'state': 'cancelled',
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
            **counters,
        })
        log_rec.env.cr.commit()

    # ------------------------------------------------------------------
    # 1. Sync stock Odoo → PrestaShop
    # ------------------------------------------------------------------

    def sync_stock_to_prestashop(self, log_rec):
        """
        Per ogni prodotto Odoo con ps_product_id > 0 e ps_sync_enabled=True,
        legge la quantità disponibile e aggiorna lo stock su PS.
        Gestisce sia prodotti semplici che configurabili (combinazioni).
        """
        qty_ctx = self._wh_qty_context()

        products = self._product_templates([
            ('ps_product_id', '>', 0),
            ('ps_sync_enabled', '=', True),
            ('type', '=', 'consu'),
        ])

        total = len(products)
        ok = skip = err = 0
        lines = []

        for i, tmpl in enumerate(products, 1):
            ps_id = tmpl.ps_product_id
            variants = tmpl.product_variant_ids
            try:
                if len(variants) > 1:
                    # --- Prodotto configurabile: sync per combinazione ---
                    stock_recs = self.client.get_stock_availables(ps_id)
                    stock_by_combo = {
                        int(s['id_product_attribute']): s
                        for s in stock_recs
                        if int(s.get('id_product_attribute', 0)) != 0
                    }
                    for variant in variants:
                        combo_id = variant.ps_combination_id
                        if not combo_id:
                            skip += 1
                            continue
                        qty = variant.with_context(**qty_ctx).qty_available
                        stock_rec = stock_by_combo.get(combo_id)
                        if not stock_rec:
                            lines.append(
                                f'[SKIP] {variant.display_name}: combo {combo_id} non in PS')
                            skip += 1
                            continue
                        ps_qty = int(stock_rec.get('quantity', 0))
                        if ps_qty == int(qty):
                            skip += 1
                            continue
                        self.client.update_stock_available(
                            stock_id=int(stock_rec['id']),
                            ps_product_id=ps_id,
                            ps_attribute_id=combo_id,
                            quantity=qty,
                        )
                        lines.append(
                            f'[OK] {variant.display_name}: {ps_qty} → {int(qty)}')
                        ok += 1
                else:
                    # --- Prodotto semplice ---
                    qty = sum(
                        variants.mapped(
                            lambda v: v.with_context(**qty_ctx).qty_available
                        )
                    )
                    stock_recs = self.client.get_stock_availables(ps_id)
                    if not stock_recs:
                        lines.append(
                            f'[SKIP] {tmpl.name}: nessun stock_available su PS (id={ps_id})')
                        skip += 1
                        continue
                    main_stock = next(
                        (s for s in stock_recs if int(s.get('id_product_attribute', 0)) == 0),
                        stock_recs[0],
                    )
                    ps_qty = int(main_stock.get('quantity', 0))
                    if ps_qty == int(qty):
                        skip += 1
                        continue
                    self.client.update_stock_available(
                        stock_id=int(main_stock['id']),
                        ps_product_id=ps_id,
                        ps_attribute_id=int(main_stock.get('id_product_attribute', 0)),
                        quantity=qty,
                    )
                    tmpl.write({'ps_last_sync_date': fields.Datetime.now()})
                    lines.append(f'[OK] {tmpl.name}: {ps_qty} → {int(qty)}')
                    ok += 1

            except Exception as exc:
                _log.warning('sync_stock: errore %s (ps_id=%s): %s', tmpl.name, ps_id, exc)
                lines.append(f'[ERR] {tmpl.name} (ps_id={ps_id}): {exc}')
                err += 1

            counters = {'products_total': ok + skip + err, 'products_updated': ok,
                        'products_skipped': skip, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return ok, skip, err

        log_rec.write({
            'state': 'done' if err == 0 else 'error',
            'products_total': ok + skip + err,
            'products_updated': ok,
            'products_skipped': skip,
            'products_error': err,
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
        })
        self.env.cr.commit()
        return ok, skip, err

    # ------------------------------------------------------------------
    # 2. Sync prezzi Odoo → PrestaShop
    # ------------------------------------------------------------------

    def sync_prices_to_prestashop(self, log_rec):
        """
        Per ogni prodotto Odoo con ps_product_id > 0 e ps_sync_enabled=True,
        confronta list_price con il prezzo su PS e aggiorna se diverso.
        Per i prodotti configurabili allinea anche l'impatto prezzo (price_extra)
        di ogni combinazione collegata.
        """
        products = self._product_templates([
            ('ps_product_id', '>', 0),
            ('ps_sync_enabled', '=', True),
        ])

        total = len(products)
        ok = skip = err = 0
        lines = []

        for i, tmpl in enumerate(products, 1):
            ps_id = tmpl.ps_product_id
            try:
                ps_product = self.client.get_product(ps_id)
                ps_price = float(ps_product.get('price', 0) or 0)
                odoo_price = tmpl.list_price

                if abs(ps_price - odoo_price) < 0.001:
                    skip += 1
                else:
                    self.client.update_product_price(ps_id, odoo_price)
                    tmpl.write({'ps_last_sync_date': fields.Datetime.now()})
                    lines.append(f'[OK] {tmpl.name}: {ps_price:.4f} → {odoo_price:.4f}')
                    ok += 1

                # --- Combinazioni: impatto prezzo ---
                if len(tmpl.product_variant_ids) > 1:
                    for variant in tmpl.product_variant_ids:
                        if not variant.ps_combination_id:
                            continue
                        try:
                            impact = variant.price_extra
                            combo = self.client.get_combination(variant.ps_combination_id)
                            cur = float(combo.get('price', 0) or 0)
                            if abs(cur - impact) < 0.001:
                                skip += 1
                                continue
                            self.client.update_combination_price(
                                variant.ps_combination_id, impact)
                            lines.append(
                                f'[OK combo] {variant.display_name}: '
                                f'{cur:.4f} → {impact:.4f}')
                            ok += 1
                        except Exception as exc:
                            _log.warning('sync_prices combo %s: %s',
                                         variant.display_name, exc)
                            lines.append(
                                f'[ERR combo] {variant.display_name}: {exc}')
                            err += 1

            except Exception as exc:
                _log.warning('sync_prices: errore prodotto %s (ps_id=%s): %s', tmpl.name, ps_id, exc)
                lines.append(f'[ERR] {tmpl.name} (ps_id={ps_id}): {exc}')
                err += 1

            counters = {'products_total': ok + skip + err, 'products_updated': ok,
                        'products_skipped': skip, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return ok, skip, err

        log_rec.write({
            'state': 'done' if err == 0 else 'error',
            'products_total': ok + skip + err,
            'products_updated': ok,
            'products_skipped': skip,
            'products_error': err,
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
        })
        self.env.cr.commit()
        return ok, skip, err

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _barcode_taken(self, ean13, exclude_tmpl_id=None):
        """Restituisce True se l'EAN è già assegnato a un altro product.template."""
        if not ean13:
            return False
        domain = [('barcode', '=', ean13)]
        if exclude_tmpl_id:
            domain.append(('id', '!=', exclude_tmpl_id))
        return bool(self.env['product.template'].search(domain, limit=1))

    # ------------------------------------------------------------------
    # 3. Import prodotti PS → Odoo
    # ------------------------------------------------------------------

    def import_products_from_ps(self, log_rec, limit=0, update_existing=True):
        """
        Scarica il catalogo da PrestaShop e crea/aggiorna prodotti in Odoo.
        Matching: ps_product_id > reference (default_code) > ean13.
        """
        product_type = self._param('odoo_product_type', 'consu')
        wh_filter = self._wh_filter_active()
        qty_ctx = self._wh_qty_context()

        ps_ids = self.client.get_product_ids()
        if limit:
            ps_ids = ps_ids[:limit]

        total = len(ps_ids)
        created = updated = skipped = err = 0
        lines = []
        ProductTmpl = self.env['product.template']

        for i, ps_id in enumerate(ps_ids, 1):
            try:
                ps_prod = self.client.get_product(ps_id)
                name = self.client.get_product_name(ps_prod)
                reference = ps_prod.get('reference', '') or ''
                ean13 = ps_prod.get('ean13', '') or ''
                price = float(ps_prod.get('price', 0) or 0)
                description = _strip_html(
                    self.client.get_product_description(ps_prod, 'description_short'))

                # Cerca il prodotto esistente
                tmpl = ProductTmpl.search([('ps_product_id', '=', ps_id)], limit=1)
                if not tmpl and reference:
                    tmpl = ProductTmpl.search([('default_code', '=', reference)], limit=1)
                if not tmpl and ean13:
                    tmpl = ProductTmpl.search([('barcode', '=', ean13)], limit=1)

                if tmpl and not update_existing:
                    skipped += 1
                elif tmpl and wh_filter and tmpl.with_context(**qty_ctx).qty_available <= 0:
                    lines.append(f'[SKIP wh] {name} (ps_id={ps_id}): nessuna giacenza nel magazzino')
                    skipped += 1
                elif tmpl:
                    vals = {
                        'ps_product_id': ps_id,
                        'ps_last_sync_date': fields.Datetime.now(),
                    }
                    if not tmpl.list_price:
                        vals['list_price'] = price
                    if reference and not tmpl.default_code:
                        vals['default_code'] = reference
                    if ean13 and not tmpl.barcode:
                        if self._barcode_taken(ean13, exclude_tmpl_id=tmpl.id):
                            lines.append(f'[WARN] {name} (ps_id={ps_id}): EAN {ean13} già usato da altro prodotto, barcode non impostato')
                        else:
                            vals['barcode'] = ean13
                    tmpl.write(vals)
                    lines.append(f'[UPD] {name} (ps_id={ps_id})')
                    updated += 1
                else:
                    barcode_val = False
                    if ean13:
                        if self._barcode_taken(ean13):
                            lines.append(f'[WARN] {name} (ps_id={ps_id}): EAN {ean13} già usato da altro prodotto, barcode non impostato')
                        else:
                            barcode_val = ean13
                    vals = {
                        'name': name,
                        'type': product_type,
                        'list_price': price,
                        'default_code': reference,
                        'barcode': barcode_val,
                        'description_sale': description,
                        'ps_product_id': ps_id,
                        'ps_sync_enabled': True,
                        'ps_last_sync_date': fields.Datetime.now(),
                    }
                    ProductTmpl.create(vals)
                    lines.append(f'[NEW] {name} (ps_id={ps_id})')
                    created += 1

            except Exception as exc:
                _log.warning('import_products: errore ps_id=%s: %s', ps_id, exc)
                lines.append(f'[ERR] ps_id={ps_id}: {exc}')
                err += 1

            counters = {'products_total': created + updated + skipped + err,
                        'products_created': created, 'products_updated': updated,
                        'products_skipped': skipped, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return created, updated, skipped, err

        log_rec.write({
            'state': 'done' if err == 0 else 'error',
            'products_total': created + updated + skipped + err,
            'products_created': created,
            'products_updated': updated,
            'products_skipped': skipped,
            'products_error': err,
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
        })
        self.env.cr.commit()
        return created, updated, skipped, err

    # ------------------------------------------------------------------
    # 4. Export prodotti Odoo → PS
    # ------------------------------------------------------------------

    @staticmethod
    def _is_configurable(tmpl):
        return len(tmpl.product_variant_ids) > 1

    def export_candidates(self, product_ids=None, full_resync=False,
                          only_in_stock=False, warehouse_id=None):
        """Recordset dei product.template che verrebbero esportati con le
        opzioni date (stessa selezione usata da export_products_to_ps).

        only_in_stock=True → esclude i prodotti con disponibilità libera
        (`free_qty` = on-hand meno riservato) <= 0 nel magazzino indicato da
        warehouse_id (default: il Magazzino PrestaShop configurato). È forzato
        anche quando è attivo il filtro-per-magazzino nelle impostazioni.
        """
        want_stock = only_in_stock or self._wh_filter_active()
        if warehouse_id:
            wh = self.env['stock.warehouse'].browse(warehouse_id).exists()
        else:
            wh = self._warehouse()

        domain = [('type', '!=', 'service'), ('ps_sync_enabled', '=', True)]
        if product_ids:
            domain = [('id', 'in', product_ids)]
        if want_stock:
            domain.append(('id', 'in', self._in_stock_template_ids(wh)))

        products = self.env['product.template'].search(domain)
        if not product_ids and not full_resync:
            products = products.filtered(
                lambda t: not t.ps_product_id or self._is_configurable(t))
        return products

    def _in_stock_template_ids(self, warehouse):
        """ID dei product.template con almeno una variante a disponibilità
        libera > 0. `product.template` non espone free_qty: si passa da
        product.product, con il context del magazzino.
        """
        ctx = {'warehouse': warehouse.id} if warehouse else {}
        variants = self.env['product.product'].with_context(**ctx).search([
            ('type', '!=', 'service'),
            ('free_qty', '>', 0.0),
        ])
        return variants.product_tmpl_id.ids

    def export_products_to_ps(self, log_rec, product_ids=None, full_resync=False,
                              only_in_stock=False, warehouse_id=None):
        """
        Esporta i prodotti Odoo su PrestaShop.
        - Template con 1 variante → prodotto semplice PS.
        - Template con N varianti (attributi Odoo) → prodotto PS con combinazioni.

        Senza product_ids esplicito, per non ricaricare l'intero catalogo ad ogni
        run, si considerano solo i prodotti non ancora esportati e i configurabili
        (che potrebbero avere varianti nuove). full_resync=True forza tutti.
        only_in_stock=True limita ai prodotti con disponibilità libera > 0 nel
        magazzino warehouse_id.
        """
        products = self.export_candidates(
            product_ids, full_resync, only_in_stock, warehouse_id)

        total = len(products)
        created = updated = skipped = err = 0
        lines = []

        for i, tmpl in enumerate(products, 1):
            try:
                if self._is_configurable(tmpl):
                    res = self._export_configurable(tmpl, lines)
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    elif res == 'skipped':
                        skipped += 1
                    else:
                        err += 1
                else:
                    ps_id = tmpl.ps_product_id
                    if ps_id:
                        # Già collegato: aggiorna prezzo
                        self.client.update_product_price(ps_id, tmpl.list_price)
                        tmpl.write({'ps_sync_enabled': True,
                                    'ps_last_sync_date': fields.Datetime.now()})
                        lines.append(f'[UPD] {tmpl.name} → ps_id={ps_id}')
                        updated += 1
                    else:
                        # Cerca per reference per evitare duplicati
                        found_id = None
                        if tmpl.default_code:
                            found_id = self.client.find_product_by_reference(tmpl.default_code)
                        if found_id:
                            tmpl.write({'ps_product_id': found_id, 'ps_sync_enabled': True,
                                        'ps_last_sync_date': fields.Datetime.now()})
                            lines.append(f'[LINK] {tmpl.name} → ps_id={found_id}')
                            updated += 1
                        else:
                            new_ps_id = self.client.create_product(
                                name=tmpl.name,
                                reference=tmpl.default_code or '',
                                ean13=tmpl.barcode or '',
                                price=tmpl.list_price,
                                description_short=tmpl.description_sale or '',
                            )
                            if new_ps_id:
                                tmpl.write({'ps_product_id': new_ps_id, 'ps_sync_enabled': True,
                                            'ps_last_sync_date': fields.Datetime.now()})
                                lines.append(f'[NEW→PS] {tmpl.name} → ps_id={new_ps_id}')
                                created += 1
                            else:
                                lines.append(f'[ERR] {tmpl.name}: nessun ID restituito da PS')
                                err += 1

            except Exception as exc:
                _log.warning('export_products: errore %s: %s', tmpl.name, exc, exc_info=True)
                lines.append(f'[ERR] {tmpl.name}: {exc}')
                err += 1

            counters = {'products_total': created + updated + skipped + err,
                        'products_created': created, 'products_updated': updated,
                        'products_skipped': skipped, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return created, updated, err

        # --- Prodotti archiviati in Odoo → disattiva su PS ---
        if not product_ids:
            archived = self.env['product.template'].with_context(active_test=False).search([
                ('active', '=', False),
                ('ps_product_id', '>', 0),
            ])
            for tmpl in archived:
                try:
                    self.client.update_product_active(tmpl.ps_product_id, 0)
                    lines.append(f'[ARCHIVE→PS] {tmpl.name} → ps_id={tmpl.ps_product_id}')
                    updated += 1
                except Exception as exc:
                    _log.warning('export_products: disattivazione %s fallita: %s',
                                 tmpl.name, exc)
                    lines.append(f'[ERR] {tmpl.name} (disattivazione): {exc}')
                    err += 1

        log_rec.write({
            'state': 'done' if err == 0 else 'error',
            'products_total': created + updated + skipped + err,
            'products_created': created,
            'products_updated': updated,
            'products_skipped': skipped,
            'products_error': err,
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
        })
        self.env.cr.commit()
        return created, updated, err

    def _export_configurable(self, tmpl, lines):
        """
        Esporta un prodotto configurabile su PS.
        Crea il prodotto padre se mancante, poi crea le combinazioni per ogni
        variante Odoo che non ha ancora ps_combination_id.
        Restituisce: 'created' | 'updated' | 'skipped' | 'err'
        """
        ps_id = tmpl.ps_product_id
        created_parent = False

        if not ps_id:
            new_ps_id = self.client.create_product(
                name=tmpl.name,
                reference='',
                ean13='',
                price=tmpl.list_price,
                description_short=tmpl.description_sale or '',
            )
            if not new_ps_id:
                lines.append(f'[ERR] {tmpl.name}: creazione prodotto padre PS fallita')
                return 'err'
            ps_id = new_ps_id
            tmpl.write({'ps_product_id': ps_id, 'ps_sync_enabled': True,
                        'ps_last_sync_date': fields.Datetime.now()})
            created_parent = True

        # Varianti senza combinazione PS
        pending = tmpl.product_variant_ids.filtered(lambda v: not v.ps_combination_id)
        if not pending:
            # Tutte le varianti già sincronizzate
            return 'skipped'

        ok_combos = err_combos = 0
        for variant in pending:
            try:
                option_value_ids = []
                for ptav in variant.product_template_attribute_value_ids:
                    opt_id = self.client.get_or_create_product_option(
                        ptav.attribute_id.name)
                    ov_id = self.client.get_or_create_option_value(
                        opt_id, ptav.product_attribute_value_id.name)
                    option_value_ids.append(ov_id)

                combo_id = self.client.create_combination(
                    ps_product_id=ps_id,
                    option_value_ids=option_value_ids,
                    reference=variant.default_code or '',
                    ean13=variant.barcode or '',
                    price_impact=variant.price_extra,
                )
                if combo_id:
                    variant.write({'ps_combination_id': combo_id})
                    ok_combos += 1
                else:
                    lines.append(f'[ERR] {variant.display_name}: combinazione non creata')
                    err_combos += 1
            except Exception as exc:
                _log.warning('export_configurable variante %s: %s',
                             variant.display_name, exc, exc_info=True)
                lines.append(f'[ERR] {variant.display_name}: {exc}')
                err_combos += 1

        n_tot = len(tmpl.product_variant_ids)
        tag = '[NEW-CONFIG]' if created_parent else '[UPD-CONFIG]'
        lines.append(f'{tag} {tmpl.name} → ps_id={ps_id} | '
                     f'combinazioni: {ok_combos}/{n_tot}')
        if err_combos and not ok_combos:
            return 'err'
        return 'created' if created_parent else 'updated'

    # ------------------------------------------------------------------
    # 5. Sync immagini Odoo → PrestaShop
    # ------------------------------------------------------------------

    def sync_images_to_prestashop(self, log_rec, force=False):
        """
        Carica l'immagine principale (image_1920) di ogni prodotto collegato su PS.
        Se force=False, salta i prodotti con ps_image_synced=True.
        """
        domain = [('ps_product_id', '>', 0), ('ps_sync_enabled', '=', True)]
        if not force:
            domain.append(('ps_image_synced', '=', False))

        products = self._product_templates(domain)
        total = len(products)
        ok = skip = err = 0
        lines = []

        for i, tmpl in enumerate(products, 1):
            ps_id = tmpl.ps_product_id
            image_b64 = tmpl.image_1920
            if not image_b64:
                lines.append(f'[SKIP] {tmpl.name}: nessuna immagine in Odoo')
                skip += 1
            else:
                try:
                    image_bytes = base64.b64decode(image_b64)
                    self.client.upload_product_image(ps_id, image_bytes)
                    tmpl.write({
                        'ps_image_synced': True,
                        'ps_last_sync_date': fields.Datetime.now(),
                    })
                    lines.append(f'[OK] {tmpl.name} (ps_id={ps_id})')
                    ok += 1
                except Exception as exc:
                    _log.warning('sync_images: errore prodotto %s (ps_id=%s): %s',
                                 tmpl.name, ps_id, exc)
                    lines.append(f'[ERR] {tmpl.name} (ps_id={ps_id}): {exc}')
                    err += 1

            counters = {'products_total': ok + skip + err, 'products_updated': ok,
                        'products_skipped': skip, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return ok, skip, err

        log_rec.write({
            'state': 'done' if err == 0 else 'error',
            'products_total': ok + skip + err,
            'products_updated': ok,
            'products_skipped': skip,
            'products_error': err,
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
        })
        self.env.cr.commit()
        return ok, skip, err

    # ------------------------------------------------------------------
    # 6. Reset link orfani (ps_product_id punta a prodotto PS cancellato)
    # ------------------------------------------------------------------

    def reset_orphaned_ps_links(self):
        """
        Azzera ps_product_id sui prodotti Odoo il cui ID non esiste più su PS.
        Restituisce il numero di prodotti resettati.
        """
        ps_ids = set(self.client.get_product_ids())
        orphans = self.env['product.template'].search([
            ('ps_product_id', '>', 0),
        ]).filtered(lambda t: t.ps_product_id not in ps_ids)
        count = len(orphans)
        if orphans:
            orphans.write({'ps_product_id': 0, 'ps_sync_enabled': False})
            self.env.cr.commit()
        return count

    # ------------------------------------------------------------------
    # 7. Import ordini PS → Odoo
    # ------------------------------------------------------------------

    # Stati PS che costituiscono un ordine da importare (pagato/in lavorazione)
    _IMPORTABLE_STATES = [2, 3, 9]

    def import_orders_from_ps(self, log_rec, hours_back=24):
        """
        Importa gli ordini PrestaShop come sale.order in Odoo.
        Salta ordini già presenti (controllo su ps_order_id).
        """
        date_from = (datetime.now() - timedelta(hours=hours_back)).strftime('%Y-%m-%d %H:%M:%S')
        order_ids = self.client.get_order_ids(
            date_from=date_from,
            states=self._IMPORTABLE_STATES,
        )
        auto_confirm = self._param('order_auto_confirm') in ('True', 'true', '1', True)

        total = len(order_ids)
        created = skipped = err = 0
        lines = []
        SaleOrder = self.env['sale.order']

        for i, order_id in enumerate(order_ids, 1):
            try:
                # Verifica se già importato
                if SaleOrder.search([('ps_order_id', '=', order_id)], limit=1):
                    skipped += 1
                else:
                    ps_order = self.client.get_order(order_id)
                    sale = self._create_sale_order_from_ps(ps_order, auto_confirm=auto_confirm)
                    if sale:
                        lines.append(f'[NEW] Ordine PS #{order_id} → {sale.name}')
                        created += 1
                    else:
                        lines.append(f'[SKIP] Ordine PS #{order_id}: nessuna riga valida')
                        skipped += 1

            except Exception as exc:
                _log.warning('import_orders: errore ordine ps_id=%s: %s', order_id, exc)
                lines.append(f'[ERR] Ordine PS #{order_id}: {exc}')
                err += 1

            counters = {'products_total': created + skipped + err,
                        'products_created': created, 'products_skipped': skipped,
                        'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return created, skipped, err

        log_rec.write({
            'state': 'done' if err == 0 else 'error',
            'products_total': created + skipped + err,
            'products_created': created,
            'products_skipped': skipped,
            'products_error': err,
            'end_date': fields.Datetime.now(),
            'log_lines': '\n'.join(lines),
        })
        self.env.cr.commit()
        return created, skipped, err

    def _create_sale_order_from_ps(self, ps_order, auto_confirm=False):
        """Crea un sale.order a partire dal dict ordine PS."""
        order_id = int(ps_order.get('id', 0))
        customer_id = int(ps_order.get('id_customer', 0))
        delivery_addr_id = int(ps_order.get('id_address_delivery', 0))
        invoice_addr_id = int(ps_order.get('id_address_invoice', 0) or delivery_addr_id)

        partner = self._find_or_create_partner(customer_id)
        if not partner:
            raise ValueError(f'Impossibile trovare/creare il cliente PS #{customer_id}')

        shipping = self._find_or_create_address(partner, delivery_addr_id, 'delivery') or partner
        invoice = self._find_or_create_address(partner, invoice_addr_id, 'invoice') or partner

        # Righe ordine
        order_rows = ps_order.get('associations', {}).get('order_rows', [])
        if not order_rows:
            return None

        order_lines = []
        for row in order_rows:
            ps_product_id = int(row.get('product_id', 0))
            qty = float(row.get('product_quantity', 1))
            unit_price = float(row.get('unit_price_tax_excl', row.get('product_price', 0)))
            product_name = row.get('product_name', '')

            product = self.env['product.template'].search(
                [('ps_product_id', '=', ps_product_id)], limit=1)
            if not product:
                # Fallback: cerca per reference usando il nome
                product = self.env['product.template'].search(
                    [('name', '=ilike', product_name)], limit=1)

            if product:
                variant = product.product_variant_ids[:1]
                order_lines.append((0, 0, {
                    'product_id': variant.id,
                    'product_uom_qty': qty,
                    'price_unit': unit_price,
                    'name': product_name or variant.name,
                }))
            else:
                # Prodotto non trovato: crea riga con note
                order_lines.append((0, 0, {
                    'product_id': self._get_generic_product().id,
                    'product_uom_qty': qty,
                    'price_unit': unit_price,
                    'name': f'[PS:{ps_product_id}] {product_name}',
                }))

        if not order_lines:
            return None

        state_id = int(ps_order.get('current_state', 0))
        state_label = self.client.order_state_label(state_id)
        ps_reference = ps_order.get('reference', '')
        date_order = ps_order.get('date_add', fields.Datetime.now())

        # Normalizza il formato data
        if not (isinstance(date_order, str) and len(date_order) == 19):
            date_order = fields.Datetime.now()

        order_vals = {
            'partner_id': partner.id,
            'partner_shipping_id': shipping.id,
            'partner_invoice_id': invoice.id,
            'date_order': date_order,
            'client_order_ref': ps_reference,
            'ps_order_id': order_id,
            'ps_order_state': state_label,
            'ps_order_reference': ps_reference,
            'note': f'Ordine PrestaShop #{order_id} — Rif: {ps_reference} — Stato: {state_label}',
            'order_line': order_lines,
        }
        if int(self._param('warehouse_id') or 0):
            order_vals['warehouse_id'] = self._warehouse().id
        sale = self.env['sale.order'].create(order_vals)
        if auto_confirm:
            try:
                sale.action_confirm()
            except Exception as exc:
                _log.warning('import_orders: conferma ordine %s fallita: %s', sale.name, exc)
        self.env.cr.commit()
        return sale

    def _map_country(self, ps_country_id):
        """Mappa un id_country PrestaShop su un res.country Odoo (via ISO code)."""
        if not ps_country_id:
            return self.env['res.country']
        try:
            ps_country = self.client.get_country(ps_country_id)
        except Exception:
            return self.env['res.country']
        iso = (ps_country.get('iso_code') or '').upper()
        if iso:
            return self.env['res.country'].search([('code', '=', iso)], limit=1)
        return self.env['res.country']

    def _find_or_create_partner(self, ps_customer_id):
        """Trova o crea il partner Odoo (contatto commerciale) del cliente PS."""
        try:
            ps_customer = self.client.get_customer(ps_customer_id)
        except Exception:
            ps_customer = {}

        email = ps_customer.get('email', '')
        first_name = ps_customer.get('firstname', '')
        last_name = ps_customer.get('lastname', '')
        full_name = f'{first_name} {last_name}'.strip() or f'Cliente PS #{ps_customer_id}'

        if email:
            partner = self.env['res.partner'].search([('email', '=', email)], limit=1)
            if partner:
                return partner

        return self.env['res.partner'].create({
            'name': full_name,
            'email': email,
            'company_type': 'person',
        })

    def _find_or_create_address(self, parent, ps_address_id, addr_type):
        """Trova o crea l'indirizzo (delivery/invoice) figlio del partner."""
        if not ps_address_id:
            return self.env['res.partner']
        try:
            addr = self.client.get_address(ps_address_id)
        except Exception:
            return self.env['res.partner']
        if not addr:
            return self.env['res.partner']

        name = f"{addr.get('firstname', '')} {addr.get('lastname', '')}".strip() or parent.name
        street = addr.get('address1', '') or ''
        zip_code = addr.get('postcode', '') or ''
        country = self._map_country(int(addr.get('id_country', 0) or 0))

        existing = self.env['res.partner'].search([
            ('parent_id', '=', parent.id),
            ('type', '=', addr_type),
            ('street', '=', street),
            ('zip', '=', zip_code),
        ], limit=1)
        if existing:
            return existing

        vals = {
            'parent_id': parent.id,
            'type': addr_type,
            'name': name,
            'street': street,
            'street2': addr.get('address2', '') or '',
            'city': addr.get('city', '') or '',
            'zip': zip_code,
            'phone': addr.get('phone', '') or addr.get('phone_mobile', '') or '',
        }
        if country:
            vals['country_id'] = country.id
        return self.env['res.partner'].create(vals)

    def _get_generic_product(self):
        """Recupera o crea un prodotto generico per righe senza match."""
        product = self.env['product.product'].search(
            [('default_code', '=', 'PS-GENERIC')], limit=1)
        if not product:
            tmpl = self.env['product.template'].create({
                'name': 'Prodotto PrestaShop (generico)',
                'default_code': 'PS-GENERIC',
                'type': 'consu',
                'list_price': 0.0,
            })
            product = tmpl.product_variant_ids[:1]
        return product
