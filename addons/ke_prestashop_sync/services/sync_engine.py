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
import time
from datetime import datetime, timedelta

from odoo import fields

from .ps_api_client import PsResourceNotFound

_log = logging.getLogger(__name__)

_TIMEOUT_STOCK_ID = 30  # secondi per singola chiamata PS stock


def _strip_html(html):
    """Rimuove tag HTML da una stringa."""
    return re.sub(r'<[^>]+>', '', html or '').strip()


class SyncEngine:

    # Ogni quanti elementi scrivere l'avanzamento sul log e controllare
    # se è stata richiesta l'interruzione.
    _PROGRESS_EVERY = 5

    # Prodotti per singola esecuzione dell'export "Solo nuovi": oltre, si
    # riaccoda un nuovo job (evita il timeout del cron worker).
    _EXPORT_BATCH = 400
    # L'export gira nel cron della coda, che Odoo interrompe dopo 120 s
    # (limit_time_real): 2026-11-11 un export di 334 prodotti si è fermato a
    # 25, ucciso a metà. Si lavora a turni e si riaccoda il resto.
    _EXPORT_TIME_BUDGET_S = 75
    # Oltre questo tempo dall'inizio del turno non si carica più nessuna
    # immagine (2026-11-13): un prodotto con molte foto (3-4 s l'una) iniziato
    # a fine turno superava i 120 s e Odoo uccideva il cron, perdendo il job.
    _TURN_HARD_LIMIT_S = 100

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

    def _export_only_in_stock(self):
        """Default (2026-11-05): l'export crea solo i prodotti con giacenza > 0."""
        return not self._flag('export_all')

    def _product_templates(self, domain):
        """Prodotti già su PS per le sync di giacenze/prezzi/immagini: tutti,
        anche a giacenza zero (va azzerata anche su PS)."""
        return self.env['product.template'].search(domain)

    # ------------------------------------------------------------------
    # Giacenza, prezzo, categoria, marca
    # ------------------------------------------------------------------

    def _free_qty_by_variant(self, variants, warehouse=None):
        """
        Disponibilità libera (quantità − riservato) per variante nel magazzino,
        dalle ubicazioni interne sotto la sua ubicazione vista — regola di
        CLAUDE.md: mai qty_available con context 'warehouse' sul template,
        che su questo Odoo ignora il magazzino.
        """
        warehouse = warehouse or self._warehouse()
        result = dict.fromkeys(variants.ids, 0)
        if not warehouse or not variants:
            return result
        groups = self.env['stock.quant']._read_group(
            [('product_id', 'in', variants.ids),
             ('location_id', 'child_of', warehouse.view_location_id.id),
             ('location_id.usage', '=', 'internal')],
            ['product_id'], ['quantity:sum', 'reserved_quantity:sum'],
        )
        for product, qty, reserved in groups:
            result[product.id] = max(int((qty or 0) - (reserved or 0)), 0)
        return result

    def _pricelist(self):
        pl_id = int(self._param('pricelist_id') or 0)
        return self.env['product.pricelist'].browse(pl_id).exists() if pl_id else self.env['product.pricelist']

    def _ps_price(self, product):
        """
        Prezzo per PrestaShop, IVA esclusa (l'IVA la aggiunge PS dal gruppo
        regole): dal "Listino PrestaShop" se impostato, altrimenti il prezzo
        di vendita (per una variante lst_price, che comprende il prezzo
        della variante di ke_product_catalog).
        """
        pricelist = self._pricelist()
        if pricelist:
            price = pricelist._get_product_price(product, 1.0, currency=self.env.company.currency_id)
        else:
            price = product.lst_price if product._name == 'product.product' else product.list_price
        tax = product.taxes_id.filtered(lambda t: t.type_tax_use == 'sale' and t.amount_type == 'percent')[:1]
        if tax and tax.price_include:
            price = price / (1 + tax.amount / 100.0)
        return round(price, 6)

    def _ps_category_id(self, tmpl):
        """Categoria PS del prodotto (product.template._ke_ps_category); 0 = predefinita."""
        return tmpl._ke_ps_category().ps_id or 0

    def _force_texts(self):
        return self.env['ir.config_parameter'].sudo().get_param('ke_prestashop_sync.force_texts') in ('True', '1')

    def _find_existing(self, reference, barcode, exclude=()):
        """Prodotto già su PS con questo riferimento o, in mancanza, questo EAN; 0 se nessuno."""
        ps_id = self.client.find_product_by_reference(reference) if reference else None
        if not ps_id and barcode and hasattr(self.client, 'find_product_by_barcode'):
            ps_id = self.client.find_product_by_barcode(barcode)
        return 0 if not ps_id or ps_id in exclude else int(ps_id)

    def _separate_variants(self):
        """Modalità "varianti come prodotti separati" (impostazione, 2026-11-13)."""
        return (getattr(self, '_family_mode', None) or self._param('family_mode')) == 'separate'

    def _keep_legacy(self):
        return self.env['ir.config_parameter'].sudo().get_param('ke_prestashop_sync.keep_legacy_products') in ('True', '1')

    def _retire_legacy_variants(self, tmpl, ps_id, lines):
        """
        Varianti di una famiglia che sul negozio erano prodotti separati
        (catalogo di produzione, 2026-11-13: 3.058 prodotti semplici, nessuna
        combinazione): disattivati con redirect 301 alla famiglia appena
        pubblicata. Solo varianti con la combinazione già creata.
        """
        if self._keep_legacy():
            return 0
        retired = 0
        for variant in tmpl.product_variant_ids.filtered(lambda v: v.ps_combination_id and not v.ps_legacy_product_id):
            legacy = (variant.ps_variant_product_id if variant.ps_variant_product_id != ps_id else 0) or \
                self._find_existing(self._ps_reference(variant.default_code), variant.barcode or '', exclude=(ps_id,))
            if not legacy:
                continue
            try:
                self.client.retire_product(legacy, ps_id)
            except Exception as exc:
                lines.append(f'[ERR] {variant.display_name}: prodotto PS {legacy} non disattivato: {exc}')
                continue
            variant.write({'ps_legacy_product_id': legacy})
            lines.append(f'[SOSTITUITO] {variant.display_name}: prodotto PS {legacy} disattivato, redirect 301 → {ps_id}')
            retired += 1
        return retired

    def _force_category(self, tmpl=None):
        """Sostituire la categoria sul negozio: con l'opzione delle Impostazioni o se è forzata sul prodotto (scelta esplicita)."""
        if tmpl is not None and tmpl.ps_category_override_id:
            return True
        return self.env['ir.config_parameter'].sudo().get_param('ke_prestashop_sync.force_category') in ('True', '1')

    def _ps_manufacturer_id(self, brand, lines):
        """
        Marca PS della marca di catalogo: quella salvata sulla marca, poi per
        nome normalizzato (anche nomi alternativi) tra le marche PS, se manca
        la crea (deciso con l'utente il 2026-11-05). 0 se il prodotto non ha marca.
        """
        if not brand:
            return 0
        if brand.ps_manufacturer_id:
            return brand.ps_manufacturer_id
        from odoo.addons.ke_product_catalog.models.product_brand import brand_key
        if not hasattr(self, '_ps_manufacturers'):
            self._ps_manufacturers = {brand_key(m['name']): m['id'] for m in self.client.get_manufacturers()}
        ps_id = next((self._ps_manufacturers[k] for k in
                      [brand_key(brand.name)] + [brand_key(a.name) for a in brand.alias_ids]
                      if k in self._ps_manufacturers), 0)
        if not ps_id:
            ps_id = self.client.create_manufacturer(brand.name)
            if ps_id:
                self._ps_manufacturers[brand_key(brand.name)] = ps_id
                lines.append(f'[MARCA→PS] {brand.name} creata su PS (id={ps_id})')
        if ps_id:
            brand.sudo().ps_manufacturer_id = ps_id
        return ps_id

    def _ps_lang(self):
        """Lingua Odoo dei testi per PS (il negozio è in italiano)."""
        return self._param('odoo_lang') or 'it_IT'

    @staticmethod
    def _odoo_lang_for(ps_lang, odoo_codes):
        """Lingua Odoo di una lingua PS: stesso codice ("fr-fr" → fr_FR), altrimenti la stessa lingua (fr_BE)."""
        code = (ps_lang.get('language_code') or '').replace('-', '_')
        exact = f'{code[:2].lower()}_{code[3:].upper()}' if len(code) == 5 else ''
        if exact in odoo_codes:
            return exact
        prefix = (code[:2] or ps_lang.get('iso_code', '')[:2]).lower()
        return next((c for c in sorted(odoo_codes) if c.split('_')[0] == prefix), '')

    def _ps_lang_map(self):
        """
        {id lingua PS: lingua Odoo} delle lingue attive del negozio oltre alla
        principale (export multilingua, 2026-11-13). Mai en_US: è il ripiego
        tecnico di Odoo, spesso con i testi della lingua principale.
        """
        if getattr(self, '_lang_map', None) is None:
            client = getattr(self, 'client', None)
            try:
                ps_langs = client.get_languages() if hasattr(client, 'get_languages') else []
            except Exception as exc:
                _log.warning('Lingue PrestaShop non lette, export solo nella lingua principale: %s', exc)
                ps_langs = []
            odoo_codes = set(self.env['res.lang'].search([('active', '=', True)]).mapped('code')) - {'en_US'}
            main = self._ps_lang()
            main_id = getattr(client, '_lang_id', 1)
            self._lang_map = {}
            for lang in ps_langs:
                if not lang.get('active') or lang['id'] == main_id:
                    continue
                code = self._odoo_lang_for(lang, odoo_codes)
                if code and code != main:
                    self._lang_map[lang['id']] = code
        return self._lang_map

    def _ps_translations(self, tmpl):
        """Testi del prodotto nelle altre lingue del negozio, solo quelli propri di ogni lingua in Odoo."""
        main = self._ps_lang()
        out = {}
        for ps_lang_id, code in self._ps_lang_map().items():
            texts = tmpl._ke_web_texts_lang(code, main)
            if texts:
                out[ps_lang_id] = {
                    'name': texts.get('name', ''),
                    'link_rewrite': texts.get('slug', ''),
                    'description_short': texts.get('short_description', ''),
                    'description': texts.get('description', ''),
                    'meta_title': texts.get('meta_title', ''),
                    'meta_description': texts.get('meta_description', ''),
                }
        return out

    def _ps_product_vals(self, tmpl, lines, reference='', ean13=''):
        """Dati del prodotto (padre) da creare o aggiornare su PS."""
        tmpl = tmpl.with_context(lang=self._ps_lang())
        category_id = self._ps_category_id(tmpl)
        if not category_id and tmpl.categ_id:
            lines.append(f'[CATEGORIA] {tmpl.name}: categoria Odoo "{tmpl.categ_id.display_name}" '
                         f'non mappata, usata la categoria PS predefinita')
        # Testi del sito (scheda "Sito web", ke_product_catalog): quelli
        # compilati, altrimenti la proposta automatica (URL breve e pulito,
        # punti elenco veri); il nome resta quello del prodotto finché non si
        # compila "Nome sul sito".
        texts = tmpl._ke_web_texts()
        return {
            'name': texts['name'],
            'reference': self._ps_reference(reference),
            'ean13': ean13,
            'price': self._ps_price(tmpl),
            'description_short': texts['short_description'] or tmpl.description_sale or '',
            'description': texts['description'],
            'meta_title': texts['meta_title'],
            'meta_description': texts['meta_description'],
            'link_rewrite': texts['slug'],
            'category_id': category_id,
            'extra_category_ids': tmpl._ke_ps_extra_category_ids(category_id),
            'translations': self._ps_translations(tmpl),
            'manufacturer_id': self._ps_manufacturer_id(tmpl.ke_brand_id, lines),
            **self._ps_dimensions(tmpl),
        }

    @staticmethod
    def _ps_reference(code):
        """Riferimento sul sito: lo SKU senza il "+" finale (convenzione Amazon dei pezzi rietichettati)."""
        return (code or '').strip().rstrip('+')

    @staticmethod
    def _ps_dimensions(tmpl):
        """Dimensioni (cm) e peso (kg) per il calcolo della spedizione su PS (ke_product_dimensions)."""
        get = lambda field: float(tmpl[field] or 0.0) if field in tmpl._fields else 0.0
        return {'width': get('product_width'), 'height': get('product_height'),
                'depth': get('product_length'), 'weight': get('weight')}

    def _push_stock(self, tmpl, lines, qty_by_variant=None):
        """Allinea su PS la giacenza del prodotto (o di ogni combinazione). Ritorna (ok, skip)."""
        variants = tmpl.product_variant_ids
        qty_by_variant = qty_by_variant or self._free_qty_by_variant(variants)
        stock_recs = self.client.get_stock_availables(tmpl.ps_product_id)
        ok = skip = 0
        if self._is_configurable(tmpl):
            by_combo = {int(s['id_product_attribute']): s for s in stock_recs
                        if int(s.get('id_product_attribute', 0)) != 0}
            targets = [(by_combo.get(v.ps_combination_id), v.ps_combination_id, qty_by_variant.get(v.id, 0), v.display_name)
                       for v in variants if v.ps_combination_id]
        else:
            main = next((s for s in stock_recs if int(s.get('id_product_attribute', 0)) == 0),
                        stock_recs[0] if stock_recs else None)
            targets = [(main, 0, sum(qty_by_variant.get(v.id, 0) for v in variants), tmpl.name)]
        for stock_rec, combo_id, qty, label in targets:
            if not stock_rec:
                lines.append(f'[SKIP] {label}: nessuna giacenza PS per la combinazione {combo_id}')
                skip += 1
                continue
            ps_qty = int(stock_rec.get('quantity', 0) or 0)
            if ps_qty == int(qty):
                skip += 1
                continue
            self.client.update_stock_available(
                stock_id=int(stock_rec['id']), ps_product_id=tmpl.ps_product_id,
                ps_attribute_id=int(stock_rec.get('id_product_attribute', 0) or 0), quantity=qty,
            )
            lines.append(f'[STOCK] {label}: {ps_qty} → {int(qty)}')
            ok += 1
        if self._is_configurable(tmpl):
            self._ensure_default_in_stock(
                tmpl, [(combo_id, int(qty), label) for stock_rec, combo_id, qty, label in targets if stock_rec],
                lines)
        return ok, skip

    def _ensure_default_in_stock(self, tmpl, combos, lines):
        """Combinazione predefinita sempre disponibile, se ce n'è una.

        PS apre la pagina sulla predefinita e, quando è a zero mentre altre
        hanno giacenza, nelle liste mostra "Prodotto disponibile con diverse
        opzioni". Si cambia solo se la predefinita attuale è a zero: con
        giacenza resta quella scelta (anche a mano dal back office).
        combos: [(combo_id, quantità, etichetta)] nell'ordine delle varianti Odoo.
        """
        available = [c for c in combos if c[1] > 0]
        if not available or len(available) == len(combos):
            return
        qty_by_combo = {c[0]: c[1] for c in combos}
        current = self.client.get_default_combination(tmpl.ps_product_id)
        if qty_by_combo.get(current, 0) > 0:
            return
        combo_id, _qty, label = available[0]
        self.client.set_default_combination(tmpl.ps_product_id, combo_id)
        lines.append(f'[PREDEFINITA] {tmpl.name}: combinazione predefinita → {label} (disponibile)')

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
        Per ogni prodotto Odoo già su PS (ps_product_id > 0, Sync PS attiva)
        allinea la giacenza su PS alla disponibilità libera nel magazzino
        PrestaShop, anche quando è zero. Prodotti semplici e combinazioni.
        """
        products = self._product_templates([
            ('ps_product_id', '>', 0),
            ('ps_sync_enabled', '=', True),
            ('type', '=', 'consu'),
        ])
        qty_by_variant = self._free_qty_by_variant(products.product_variant_ids)

        total = len(products)
        ok = skip = err = 0
        lines = []

        for i, tmpl in enumerate(products, 1):
            ps_id = tmpl.ps_product_id
            try:
                done, skipped = self._push_stock(tmpl, lines, qty_by_variant)
                ok += done
                skip += skipped
                if done:
                    tmpl.write({'ps_last_sync_date': fields.Datetime.now()})
            except Exception as exc:
                _log.warning('sync_stock: errore %s (ps_id=%s): %s', tmpl.name, ps_id, exc)
                lines.append(f'[ERR] {tmpl.name} (ps_id={ps_id}): {exc}')
                err += 1

            counters = {'products_total': ok + skip + err, 'products_updated': ok,
                        'products_skipped': skip, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return ok, skip, err

        # Modalità prodotti separati: le varianti col loro prodotto PS.
        variants = self.env['product.product'].search([
            ('ps_variant_product_id', '>', 0), ('product_tmpl_id.ps_sync_enabled', '=', True)])
        if variants:
            qty_v = self._free_qty_by_variant(variants)
            for variant in variants:
                try:
                    if self._push_variant_stock(variant, lines, qty_v.get(variant.id, 0)):
                        ok += 1
                    else:
                        skip += 1
                except Exception as exc:
                    lines.append(f'[ERR] {variant.display_name} (ps_id={variant.ps_variant_product_id}): {exc}')
                    err += 1

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
                odoo_price = self._ps_price(tmpl)

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
                            impact = round(self._ps_price(variant) - odoo_price, 6)
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

            except PsResourceNotFound:
                tmpl.write({'ps_product_id': 0})
                lines.append(
                    f'[ORPHAN] {tmpl.name}: ps_id={ps_id} inesistente su PS, '
                    f'link azzerato (riesporta per ricrearlo)')
                skip += 1
            except Exception as exc:
                _log.warning('sync_prices: errore prodotto %s (ps_id=%s): %s', tmpl.name, ps_id, exc)
                lines.append(f'[ERR] {tmpl.name} (ps_id={ps_id}): {exc}')
                err += 1

            counters = {'products_total': ok + skip + err, 'products_updated': ok,
                        'products_skipped': skip, 'products_error': err}
            if self._tick(log_rec, i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return ok, skip, err

        for variant in self.env['product.product'].search([
                ('ps_variant_product_id', '>', 0), ('product_tmpl_id.ps_sync_enabled', '=', True)]):
            try:
                ps_price = float(self.client.get_product(variant.ps_variant_product_id).get('price', 0) or 0)
                odoo_price = self._ps_price(variant)
                if abs(ps_price - odoo_price) < 0.001:
                    skip += 1
                    continue
                self.client.update_product_price(variant.ps_variant_product_id, odoo_price)
                lines.append(f'[OK] {variant.display_name}: {ps_price:.4f} → {odoo_price:.4f}')
                ok += 1
            except PsResourceNotFound:
                variant.write({'ps_variant_product_id': 0})
                lines.append(f'[ORPHAN] {variant.display_name}: prodotto PS inesistente, link azzerato')
                skip += 1
            except Exception as exc:
                lines.append(f'[ERR] {variant.display_name}: {exc}')
                err += 1

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
        # Prodotti scelti a mano: vale solo la spunta del wizard, non il default.
        want_stock = only_in_stock or (not product_ids and self._export_only_in_stock())
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
        libera > 0 nel magazzino (da stock.quant, vedi _free_qty_by_variant)."""
        if not warehouse:
            return []
        quants = self.env['stock.quant'].search([
            ('location_id', 'child_of', warehouse.view_location_id.id),
            ('location_id.usage', '=', 'internal'),
            ('quantity', '>', 0),
        ])
        variants = quants.product_id
        qty = self._free_qty_by_variant(variants, warehouse)
        return variants.filtered(lambda v: qty.get(v.id, 0) > 0).product_tmpl_id.ids

    def _start_turn(self):
        """Inizio di un turno del cron: azzera il limite rigido per le immagini."""
        started = time.monotonic()
        self._deadline = started + self._TURN_HARD_LIMIT_S
        self._images_cut = False
        return started

    def _out_of_time(self):
        deadline = getattr(self, '_deadline', None)
        return deadline is not None and time.monotonic() > deadline

    def _requeue_export(self, rest, lines, **kwargs):
        lines.append(f'[CONTINUA] turno finito: restano {len(rest)} prodotti, accodato il turno successivo')
        self.env['ke.prestashop.sync.log'].enqueue(
            'export_products', 'export_products_to_ps',
            label=f'Export prodotti → PS (segue, {len(rest)} prodotti) — '
                  f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            product_ids=rest.ids, **kwargs)

    def export_products_to_ps(self, log_rec, product_ids=None, full_resync=False,
                              only_in_stock=False, warehouse_id=None, family_mode=None):
        """
        family_mode: 'combinations' | 'separate' scelto nel wizard per questo
        export (2026-11-13); vuoto = quello delle Impostazioni.
        Esporta i prodotti Odoo su PrestaShop.
        - Template con 1 variante → prodotto semplice PS.
        - Template con N varianti (attributi Odoo) → prodotto PS con combinazioni.

        Senza product_ids esplicito, per non ricaricare l'intero catalogo ad ogni
        run, si considerano solo i prodotti non ancora esportati e i configurabili
        (che potrebbero avere varianti nuove). full_resync=True forza tutti.
        only_in_stock=True limita ai prodotti con disponibilità libera > 0 nel
        magazzino warehouse_id.
        """
        self._family_mode = family_mode or None
        all_candidates = self.export_candidates(
            product_ids, full_resync, only_in_stock, warehouse_id)

        # In modalità "Solo nuovi" la lista si riduce da sola man mano che i
        # prodotti ricevono ps_product_id: si lavora a lotti e si riaccoda un
        # export per il resto, così non si supera il limite di tempo del cron.
        auto_continue = not product_ids and not full_resync
        products = all_candidates[:self._EXPORT_BATCH] if auto_continue else all_candidates
        remaining = len(all_candidates) - len(products)

        total = len(products)
        created = updated = skipped = err = 0
        lines = []
        started = self._start_turn()
        time_cut = False
        requeue = dict(full_resync=full_resync, only_in_stock=only_in_stock,
                       warehouse_id=warehouse_id, family_mode=family_mode)

        for i, tmpl in enumerate(products, 1):
            if i > 1 and time.monotonic() - started > self._EXPORT_TIME_BUDGET_S:
                self._requeue_export(all_candidates[i - 1:], lines, **requeue)
                time_cut = True
                break
            try:
                if self._is_configurable(tmpl):
                    res = (self._export_variants_separately(tmpl, lines) if self._separate_variants() else self._export_configurable(tmpl, lines))
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    elif res == 'skipped':
                        skipped += 1
                    else:
                        err += 1
                else:
                    res = self._export_simple(tmpl, lines)
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    else:
                        err += 1

            except Exception as exc:
                _log.warning('export_products: errore %s: %s', tmpl.name, exc, exc_info=True)
                lines.append(f'[ERR] {tmpl.name}: {exc}')
                err += 1

            counters = {'products_total': created + updated + skipped + err,
                        'products_created': created, 'products_updated': updated,
                        'products_skipped': skipped, 'products_error': err}
            if self._images_cut:
                # Immagini interrotte a fine turno: il prodotto si riprende
                # (con le immagini mancanti) nel turno successivo.
                self._requeue_export(all_candidates[i - 1:], lines, **requeue)
                time_cut = True
            if self._tick(log_rec, total if time_cut else i, total, lines, counters):
                self._finalize_cancelled(log_rec, lines, counters)
                return created, updated, err
            if time_cut:
                break

        # --- Lotto successivo: riaccoda un export per i prodotti rimanenti ---
        if not time_cut and auto_continue and remaining > 0 and (created + updated) > 0:
            lines.append(
                f'[CONTINUA] restano ~{remaining} prodotti da esportare: '
                f'accodato un nuovo job')
            self.env['ke.prestashop.sync.log'].enqueue(
                'export_products', 'export_products_to_ps',
                label=f'Export prodotti → PS (segue) — '
                      f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
                only_in_stock=only_in_stock, warehouse_id=warehouse_id, family_mode=family_mode,
            )

        # --- Prodotti archiviati in Odoo → disattiva su PS (solo ultimo lotto) ---
        if not time_cut and not product_ids and remaining == 0:
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

    def _export_simple(self, tmpl, lines):
        """
        Prodotto semplice: crea su PS o, se già collegato (o trovato per
        riferimento), aggiorna nome, prezzo, categoria, marca, codici e
        descrizione breve; poi allinea la giacenza.
        Restituisce: 'created' | 'updated' | 'err'
        """
        variant = tmpl.product_variant_id
        vals = self._ps_product_vals(tmpl, lines, reference=tmpl.default_code or variant.default_code or '',
                                     ean13=tmpl.barcode or variant.barcode or '')
        ps_id = tmpl.ps_product_id
        result = 'updated'
        if ps_id:
            try:
                self.client.update_product(ps_id, replace_category=self._force_category(tmpl),
                                           replace_texts=self._force_texts(), product_type='standard', **vals)
            except PsResourceNotFound:
                lines.append(f'[RELINK] {tmpl.name}: ps_id={ps_id} inesistente su PS, ricreo')
                tmpl.write({'ps_product_id': 0, 'ps_image_synced': False})
                ps_id = 0
        if not ps_id and (vals['reference'] or vals['ean13']):
            ps_id = self._find_existing(vals['reference'], vals['ean13'])
            if ps_id:
                tmpl.write({'ps_product_id': ps_id})
                self.client.update_product(ps_id, replace_category=self._force_category(tmpl),
                                           replace_texts=self._force_texts(), product_type='standard', **vals)
                lines.append(f'[LINK] {tmpl.name} → ps_id={ps_id}')
        if not ps_id:
            ps_id = self.client.create_product(**vals)
            if not ps_id:
                lines.append(f'[ERR] {tmpl.name}: nessun ID restituito da PS')
                return 'err'
            tmpl.write({'ps_product_id': ps_id})
            lines.append(f'[NEW→PS] {tmpl.name} → ps_id={ps_id}')
            result = 'created'
        elif result == 'updated':
            lines.append(f'[UPD] {tmpl.name} → ps_id={ps_id}')
        tmpl.write({'ps_sync_enabled': True, 'ps_last_sync_date': fields.Datetime.now()})
        self._push_stock(tmpl, lines)
        if not tmpl.ps_image_synced:
            self._push_images(tmpl, lines)
        return result

    def _variant_suffix(self, variant, lang=None):
        """Valori della variante per il nome ("Rosso, XL"), nella lingua indicata."""
        v = variant.with_context(lang=lang) if lang else variant
        return ', '.join(v.product_template_attribute_value_ids.mapped('name'))

    def _variant_product_vals(self, tmpl, variant, lines):
        """Dati del prodotto semplice PS di una variante (modalità prodotti separati)."""
        vals = self._ps_product_vals(tmpl, lines, reference=variant.default_code or '', ean13=variant.barcode or '')
        suffix = self._variant_suffix(variant, self._ps_lang())
        if suffix:
            vals['name'] = f"{vals['name']} - {suffix}"
            vals['link_rewrite'] = f"{vals['link_rewrite']}-{suffix}"
        vals['price'] = self._ps_price(variant)
        lang_by_ps = self._ps_lang_map()
        for ps_lang_id, texts in vals.get('translations', {}).items():
            loc_suffix = self._variant_suffix(variant, lang_by_ps.get(ps_lang_id))
            if loc_suffix and texts.get('name'):
                texts['name'] = f"{texts['name']} - {loc_suffix}"
                texts['link_rewrite'] = f"{texts.get('link_rewrite') or texts['name']}-{loc_suffix}"
        return vals

    def _export_variants_separately(self, tmpl, lines):
        """
        Modalità "varianti come prodotti separati" (2026-11-13, com'è il
        negozio di produzione: 3.058 prodotti semplici, nessuna combinazione):
        ogni variante è un prodotto semplice PS, collegato in
        ps_variant_product_id; quello già sul negozio si trova per
        riferimento o EAN e si aggiorna. Giacenza e immagini per variante.
        Restituisce: 'created' | 'updated' | 'err'
        """
        created = updated = errors = 0
        for variant in tmpl.product_variant_ids:
            try:
                vals = self._variant_product_vals(tmpl, variant, lines)
                ps_id = variant.ps_variant_product_id
                if ps_id:
                    try:
                        self.client.update_product(ps_id, replace_category=self._force_category(tmpl),
                                                   replace_texts=self._force_texts(), product_type='standard', **vals)
                    except PsResourceNotFound:
                        lines.append(f'[RELINK] {variant.display_name}: ps_id={ps_id} inesistente su PS, ricreo')
                        ps_id = 0
                if not ps_id:
                    ps_id = self._find_existing(vals['reference'], vals['ean13'])
                    if ps_id:
                        self.client.update_product(ps_id, replace_category=self._force_category(tmpl),
                                                   replace_texts=self._force_texts(), product_type='standard', **vals)
                        lines.append(f'[LINK] {variant.display_name} → ps_id={ps_id}')
                if not ps_id:
                    ps_id = self.client.create_product(**vals)
                    if not ps_id:
                        lines.append(f'[ERR] {variant.display_name}: nessun ID restituito da PS')
                        errors += 1
                        continue
                    lines.append(f'[NEW→PS] {variant.display_name} → ps_id={ps_id}')
                    created += 1
                else:
                    updated += 1
                variant.write({'ps_variant_product_id': ps_id})
                self._push_variant_stock(variant, lines)
                self._push_variant_images(variant, ps_id, lines)
            except Exception as exc:
                _log.warning('export variante separata %s: %s', variant.display_name, exc, exc_info=True)
                lines.append(f'[ERR] {variant.display_name}: {exc}')
                errors += 1
        tmpl.write({'ps_sync_enabled': True, 'ps_last_sync_date': fields.Datetime.now()})
        if errors and not (created or updated):
            return 'err'
        return 'created' if created else 'updated'

    def _push_variant_stock(self, variant, lines, qty=None):
        """Giacenza del prodotto semplice PS di una variante (modalità prodotti separati)."""
        ps_id = variant.ps_variant_product_id
        if qty is None:
            qty = self._free_qty_by_variant(variant).get(variant.id, 0)
        recs = self.client.get_stock_availables(ps_id)
        rec = next((r for r in recs if int(r.get('id_product_attribute', 0)) == 0), recs[0] if recs else None)
        if not rec:
            lines.append(f'[SKIP] {variant.display_name}: nessuna giacenza PS')
            return 0
        if int(rec.get('quantity', 0) or 0) == int(qty):
            return 0
        self.client.update_stock_available(stock_id=int(rec['id']), ps_product_id=ps_id,
                                           ps_attribute_id=0, quantity=qty)
        lines.append(f'[STOCK] {variant.display_name}: {int(rec.get("quantity", 0) or 0)} → {int(qty)}')
        return 1

    def _push_variant_images(self, variant, ps_id, lines):
        """Immagini della variante sul suo prodotto PS, solo se non ne ha già."""
        if self.client.get_product_image_ids(ps_id):
            return 0
        if self._out_of_time():
            # Senza immagini su PS: le carica il turno successivo.
            self._images_cut = True
            lines.append(f'[IMG] {variant.display_name}: immagini al prossimo turno (tempo del turno finito)')
            return 0
        uploaded = 0
        for kind, value in self._image_sources(variant)[:self._MAX_IMAGES]:
            try:
                if self.client.upload_product_image(ps_id, self._image_bytes(kind, value)):
                    uploaded += 1
            except Exception as exc:
                _log.warning('Immagine %s per %s non caricata: %s', str(value)[:80], variant.display_name, exc)
        if uploaded:
            lines.append(f'[IMG] {variant.display_name}: {uploaded} immagini')
        return uploaded

    def _export_configurable(self, tmpl, lines):
        """
        Prodotto con varianti: prodotto padre su PS (creato o aggiornato come
        un semplice, senza codici: stanno sulle combinazioni), una
        combinazione per variante — create quelle nuove, aggiornati impatto
        prezzo, riferimento ed EAN di quelle esistenti — poi la giacenza.
        Impatto prezzo = prezzo PS della variante − prezzo PS del padre
        (comprende il prezzo variante di ke_product_catalog e il listino PS).
        Restituisce: 'created' | 'updated' | 'err'
        """
        vals = self._ps_product_vals(tmpl, lines)
        ps_id = tmpl.ps_product_id
        created_parent = False

        if ps_id:
            try:
                self.client.update_product(ps_id, replace_category=self._force_category(tmpl),
                                           replace_texts=self._force_texts(), product_type='combinations', **vals)
            except PsResourceNotFound:
                lines.append(f'[RELINK] {tmpl.name}: ps_id={ps_id} inesistente su PS, ricreo')
                tmpl.write({'ps_product_id': 0, 'ps_image_synced': False})
                tmpl.product_variant_ids.write({'ps_combination_id': 0})
                ps_id = 0

        if not ps_id:
            ps_id = self.client.create_product(product_type='combinations', **vals)
            if not ps_id:
                lines.append(f'[ERR] {tmpl.name}: creazione prodotto padre PS fallita')
                return 'err'
            tmpl.write({'ps_product_id': ps_id})
            created_parent = True
        tmpl.write({'ps_sync_enabled': True, 'ps_last_sync_date': fields.Datetime.now()})

        parent_price = vals['price']
        ok_combos = err_combos = 0
        for variant in tmpl.product_variant_ids:
            impact = round(self._ps_price(variant) - parent_price, 6)
            try:
                if variant.ps_combination_id:
                    try:
                        self.client.update_combination(
                            variant.ps_combination_id, price_impact=impact,
                            reference=self._ps_reference(variant.default_code), ean13=variant.barcode or '')
                        ok_combos += 1
                        continue
                    except PsResourceNotFound:
                        variant.write({'ps_combination_id': 0})
                option_value_ids = []
                for ptav in variant.product_template_attribute_value_ids:
                    opt_id = self.client.get_or_create_product_option(ptav.attribute_id.name)
                    ov_id = self.client.get_or_create_option_value(
                        opt_id, ptav.product_attribute_value_id.name)
                    option_value_ids.append(ov_id)
                combo_id = self.client.create_combination(
                    ps_product_id=ps_id,
                    option_value_ids=option_value_ids,
                    reference=self._ps_reference(variant.default_code),
                    ean13=variant.barcode or '',
                    price_impact=impact,
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

        if ok_combos:
            self._retire_legacy_variants(tmpl, ps_id, lines)
        n_tot = len(tmpl.product_variant_ids)
        tag = '[NEW-CONFIG]' if created_parent else '[UPD-CONFIG]'
        lines.append(f'{tag} {tmpl.name} → ps_id={ps_id} | combinazioni: {ok_combos}/{n_tot}')
        self._push_stock(tmpl, lines)
        if not tmpl.ps_image_synced:
            self._push_images(tmpl, lines)
        if err_combos and not ok_combos:
            return 'err'
        return 'created' if created_parent else 'updated'

    # ------------------------------------------------------------------
    # 5. Sync immagini Odoo → PrestaShop
    # ------------------------------------------------------------------

    @staticmethod
    def _order_row_variant(product, row):
        """
        Variante della riga d'ordine PS: quella della combinazione acquistata
        (product_attribute_id), poi per riferimento (anche con il "+" tolto
        sul sito), poi la prima. Prima del 2026-11-11 era sempre la prima.
        """
        variants = product.product_variant_ids
        combo_id = int(row.get('product_attribute_id') or 0)
        variant = combo_id and variants.filtered(lambda v: v.ps_combination_id == combo_id)[:1]
        if not variant:
            ref = (row.get('product_reference') or '').strip()
            variant = ref and variants.filtered(lambda v: (v.default_code or '') in (ref, ref + '+'))[:1]
        return variant or variants[:1]

    _MAX_IMAGES = 40          # per prodotto PS (famiglie con molte varianti)
    _IMAGE_TIMEOUT_S = 20

    def _image_sources(self, variant):
        """Immagini di una variante: galleria (principale prima, poi PT01…) o, senza galleria, l'immagine Odoo."""
        media = self.env['ke.product.media'].sudo().search(
            [('product_id', '=', variant.id)]).sorted(lambda m: (m.role != 'MAIN', m.role or '', m.sequence, m.id))
        sources = [('url', m.url) for m in media if m.url]
        if not sources:
            image = variant.image_variant_1920 or variant.product_tmpl_id.image_1920
            if image:
                sources.append(('b64', image))
        return sources

    def _image_bytes(self, kind, value):
        if kind == 'b64':
            return base64.b64decode(value)
        import requests
        resp = requests.get(value, timeout=self._IMAGE_TIMEOUT_S)
        resp.raise_for_status()
        return resp.content

    def _push_images(self, tmpl, lines, force=False, quiet=False):
        """
        Immagini del prodotto su PS (2026-11-11, prima solo l'immagine
        principale e solo dalla sync immagini): tutta la galleria
        (ke.product.media, principale per prima = copertina); nelle famiglie
        quella di ogni variante, associata alla sua combinazione (immagini in
        comune caricate una volta sola). force: cancella prima le immagini già
        su PS. quiet: niente riga di log per i prodotti con le foto già a
        posto (controllo immagini). Ritorna il numero di immagini caricate.
        """
        ps_id = tmpl.ps_product_id
        if not ps_id:
            return 0
        existing = self.client.get_product_image_ids(ps_id)
        resume = []
        if existing and tmpl.ps_image_incomplete:
            # Caricamento interrotto a fine turno (anche di una sync forzata):
            # le prime immagini ci sono già (PS le numera in ordine di
            # caricamento), si prosegue da lì.
            resume = sorted(existing)
        elif force:
            for image_id in existing:
                self.client.delete_product_image(ps_id, image_id)
        elif existing:
            # Prodotto che sul negozio ha già le sue foto (es. trovato per
            # riferimento nel catalogo di produzione, 2026-11-13): non si
            # aggiungono doppioni; per sostituirle, sync immagini forzata.
            # Solo se le foto ci sono davvero: registrate senza file (DB
            # copiato senza img/) sul sito appaiono rotte. Tutte rotte → si
            # tolgono e si carica la galleria Odoo; alcune rotte → solo
            # segnalate, le buone non si toccano mai.
            broken = [i for i in existing if self.client.image_missing_on_disk(ps_id, i)]
            if len(broken) < len(existing):
                tmpl.write({'ps_image_synced': True})
                if broken:
                    lines.append(f'[IMG] {tmpl.name}: {len(broken)} immagini su {len(existing)} senza file su PS '
                                 f'(id {", ".join(map(str, broken))}), lasciate: le altre sono buone')
                elif not quiet:
                    lines.append(f'[IMG] {tmpl.name}: {len(existing)} immagini già su PS, non caricate')
                return 0
            for image_id in broken:
                self.client.delete_product_image(ps_id, image_id)
            lines.append(f'[IMG] {tmpl.name}: {len(broken)} immagini registrate su PS senza file, '
                         f'tolte; carico quelle di Odoo')
        configurable = self._is_configurable(tmpl)
        variants = tmpl.product_variant_ids.filtered(lambda v: not configurable or v.ps_combination_id)
        uploaded, by_combo, errors, cut = {}, {}, 0, False
        for variant in variants:
            for kind, value in self._image_sources(variant):
                key = value if kind == 'url' else f'b64:{variant.id}'
                if key not in uploaded and len(uploaded) < self._MAX_IMAGES:
                    if resume:
                        uploaded[key] = resume.pop(0)
                    elif self._out_of_time():
                        cut = True
                        break
                    else:
                        try:
                            uploaded[key] = self.client.upload_product_image(ps_id, self._image_bytes(kind, value))
                        except PsResourceNotFound:
                            raise
                        except Exception as exc:
                            errors += 1
                            _log.warning('Immagine %s per %s non caricata: %s', key[:80], tmpl.name, exc)
                            continue
                if configurable and uploaded.get(key):
                    by_combo.setdefault(variant.ps_combination_id, []).append(uploaded[key])
            if cut:
                break
        if cut:
            self._images_cut = True
            tmpl.write({'ps_image_synced': False, 'ps_image_incomplete': True})
            lines.append(f'[IMG] {tmpl.name}: {len(uploaded)} immagini caricate, '
                         f'le altre al prossimo turno (tempo del turno finito)')
            return len(uploaded)
        for combo_id, image_ids in by_combo.items():
            try:
                self.client.set_combination_images(combo_id, image_ids)
            except Exception as exc:
                errors += 1
                _log.warning('Immagini della combinazione %s non associate: %s', combo_id, exc)
        if uploaded:
            tmpl.write({'ps_image_synced': True, 'ps_image_incomplete': False})
        lines.append(f'[IMG] {tmpl.name}: {len(uploaded)} immagini' + (f', {errors} errori' if errors else ''))
        return len(uploaded)

    def sync_images_to_prestashop(self, log_rec, force=False, product_ids=None, check=False):
        """
        Carica le immagini (galleria, vedi _push_images) di ogni prodotto collegato su PS.
        Se force=False, salta i prodotti con ps_image_synced=True; con force
        sostituisce quelle già su PS. A turni come l'export (2026-11-13): finito
        il tempo, il resto (product_ids) va in un nuovo job.
        check=True ("Ripara immagini mancanti", 2026-11-13): anche i prodotti
        già sincronizzati; si controlla che le foto su PS abbiano il file e a
        quelli con tutte le foto rotte si carica la galleria Odoo.
        """
        domain = [('ps_product_id', '>', 0), ('ps_sync_enabled', '=', True)]
        if product_ids:
            domain.append(('id', 'in', product_ids))
        if not force and not check:
            domain.append(('ps_image_synced', '=', False))

        products = self._product_templates(domain)
        total = len(products)
        ok = skip = err = 0
        lines = []
        started = self._start_turn()

        for i, tmpl in enumerate(products, 1):
            ps_id = tmpl.ps_product_id
            if not any(self._image_sources(v) for v in tmpl.product_variant_ids):
                if not check:
                    lines.append(f'[SKIP] {tmpl.name}: nessuna immagine in Odoo')
                skip += 1
            else:
                try:
                    if self._push_images(tmpl, lines, force=force, quiet=check):
                        tmpl.write({'ps_last_sync_date': fields.Datetime.now()})
                        ok += 1
                    elif tmpl.ps_image_synced:
                        skip += 1   # foto già su PS e buone
                    else:
                        err += 1
                except PsResourceNotFound:
                    tmpl.write({'ps_product_id': 0, 'ps_image_synced': False})
                    lines.append(
                        f'[ORPHAN] {tmpl.name}: ps_id={ps_id} inesistente su PS, link azzerato')
                    skip += 1
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
            # Finito il turno (o immagini interrotte): il resto in un nuovo job;
            # il prodotto interrotto riparte dalle immagini mancanti.
            cut = self._images_cut
            if cut or (i < total and time.monotonic() - started > self._EXPORT_TIME_BUDGET_S):
                rest = products[i - 1 if cut else i:]
                lines.append(f'[CONTINUA] turno finito: restano {len(rest)} prodotti, accodato il turno successivo')
                self.env['ke.prestashop.sync.log'].enqueue(
                    'sync_images', 'sync_images_to_prestashop',
                    label=f'Sync immagini → PS (segue, {len(rest)} prodotti) — '
                          f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
                    force=force, product_ids=rest.ids, check=check)
                break

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
        Azzera ps_product_id (e ps_combination_id delle varianti) sui prodotti
        Odoo il cui ID non esiste più su PS. Lascia ps_sync_enabled invariato,
        così un successivo export li ricrea. Restituisce il numero di prodotti.
        """
        ps_ids = set(self.client.get_product_ids())
        orphans = self.env['product.template'].search([
            ('ps_product_id', '>', 0),
        ]).filtered(lambda t: t.ps_product_id not in ps_ids)
        count = len(orphans)
        if orphans:
            orphans.write({'ps_product_id': 0, 'ps_image_synced': False})
            orphans.product_variant_ids.filtered('ps_combination_id').write(
                {'ps_combination_id': 0})
            self.env.cr.commit()
        return count

    def reset_all_ps_links(self):
        """Azzera TUTTI i collegamenti PrestaShop (ps_product_id,
        ps_combination_id, ps_image_synced) senza chiamate API. Da usare per
        ripartire da zero dopo aver svuotato il catalogo su PrestaShop.
        Restituisce (n_prodotti, n_varianti).
        """
        templates = self.env['product.template'].search([
            '|', ('ps_product_id', '!=', 0), ('ps_image_synced', '=', True),
        ])
        variants = self.env['product.product'].with_context(active_test=False).search(
            ['|', '|', ('ps_combination_id', '!=', 0), ('ps_variant_product_id', '!=', 0), ('ps_legacy_product_id', '!=', 0)])
        n_t, n_v = len(templates), len(variants)
        if templates:
            templates.write({'ps_product_id': 0, 'ps_image_synced': False})
        if variants:
            variants.write({'ps_combination_id': 0, 'ps_variant_product_id': 0, 'ps_legacy_product_id': 0})
        self.env.cr.commit()
        return n_t, n_v

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

            # Prodotto PS di una variante (modalità prodotti separati) o
            # sostituito dalla famiglia con combinazioni: la variante è quella.
            own_variant = self.env['product.product'].search(
                ['|', ('ps_variant_product_id', '=', ps_product_id),
                 ('ps_legacy_product_id', '=', ps_product_id)], limit=1) if ps_product_id else None
            product = own_variant.product_tmpl_id if own_variant else self.env['product.template'].search(
                [('ps_product_id', '=', ps_product_id)], limit=1)
            if not product:
                # Fallback: cerca per reference usando il nome
                product = self.env['product.template'].search(
                    [('name', '=ilike', product_name)], limit=1)

            if product:
                variant = own_variant or self._order_row_variant(product, row)
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
