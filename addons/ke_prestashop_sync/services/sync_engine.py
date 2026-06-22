"""
Motore di sincronizzazione PrestaShop ↔ Odoo.

sync_stock_to_prestashop   – Odoo qty_available → PS stock_available (cron + manuale)
sync_prices_to_prestashop  – Odoo list_price → PS price (cron + manuale)
import_products_from_ps    – PS catalogo → crea/aggiorna product.template in Odoo (wizard)
export_products_to_ps      – product.template Odoo → crea/aggiorna prodotto su PS (wizard)
import_orders_from_ps      – PS ordini → sale.order in Odoo (cron + manuale)
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
    def __init__(self, env, client):
        self.env = env
        self.client = client

    # ------------------------------------------------------------------
    # 1. Sync stock Odoo → PrestaShop
    # ------------------------------------------------------------------

    def sync_stock_to_prestashop(self, log_rec):
        """
        Per ogni prodotto Odoo con ps_product_id > 0 e ps_sync_enabled=True,
        legge la quantità disponibile e aggiorna lo stock su PS.
        Gestisce sia prodotti semplici che configurabili (combinazioni).
        """
        params = self.env['ir.config_parameter'].sudo()
        warehouse_id = int(params.get_param('ke_prestashop_sync.warehouse_id') or 0)
        warehouse = self.env['stock.warehouse'].browse(warehouse_id) if warehouse_id else \
            self.env['stock.warehouse'].search([], limit=1)
        location = warehouse.lot_stock_id

        products = self.env['product.template'].search([
            ('ps_product_id', '>', 0),
            ('ps_sync_enabled', '=', True),
            ('type', '=', 'consu'),
        ])

        ok = skip = err = 0
        lines = []

        for tmpl in products:
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
                        qty = variant.with_context(location=location.id).qty_available
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
                            lambda v: v.with_context(location=location.id).qty_available
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
        """
        products = self.env['product.template'].search([
            ('ps_product_id', '>', 0),
            ('ps_sync_enabled', '=', True),
        ])

        ok = skip = err = 0
        lines = []

        for tmpl in products:
            ps_id = tmpl.ps_product_id
            try:
                ps_product = self.client.get_product(ps_id)
                ps_price = float(ps_product.get('price', 0))
                odoo_price = tmpl.list_price

                if abs(ps_price - odoo_price) < 0.001:
                    skip += 1
                    continue

                self.client.update_product_price(ps_id, odoo_price)
                tmpl.write({'ps_last_sync_date': fields.Datetime.now()})
                lines.append(f'[OK] {tmpl.name}: {ps_price:.4f} → {odoo_price:.4f}')
                ok += 1

            except Exception as exc:
                _log.warning('sync_prices: errore prodotto %s (ps_id=%s): %s', tmpl.name, ps_id, exc)
                lines.append(f'[ERR] {tmpl.name} (ps_id={ps_id}): {exc}')
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
        product_type = self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.odoo_product_type', 'consu')

        ps_ids = self.client.get_product_ids()
        if limit:
            ps_ids = ps_ids[:limit]

        created = updated = skipped = err = 0
        lines = []
        ProductTmpl = self.env['product.template']

        for ps_id in ps_ids:
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

                if tmpl:
                    if not update_existing:
                        skipped += 1
                        continue
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

                # Commit ogni 50 prodotti per non perdere tutto in caso di errore
                if (created + updated) % 50 == 0:
                    self.env.cr.commit()

            except Exception as exc:
                _log.warning('import_products: errore ps_id=%s: %s', ps_id, exc)
                lines.append(f'[ERR] ps_id={ps_id}: {exc}')
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
        return created, updated, skipped, err

    # ------------------------------------------------------------------
    # 4. Export prodotti Odoo → PS
    # ------------------------------------------------------------------

    @staticmethod
    def _is_configurable(tmpl):
        return len(tmpl.product_variant_ids) > 1

    def export_products_to_ps(self, log_rec, product_ids=None):
        """
        Esporta i prodotti Odoo su PrestaShop.
        - Template con 1 variante → prodotto semplice PS.
        - Template con N varianti (attributi Odoo) → prodotto PS con combinazioni.
        Per i configurabili controlla anche le varianti mancanti su prodotti già esportati.
        """
        if product_ids:
            domain = [('id', 'in', product_ids)]
        else:
            # Semplici: solo senza ps_product_id
            # Configurabili: anche già esportati (potrebbero avere varianti nuove)
            domain = [('type', '!=', 'service')]

        products = self.env['product.template'].search(domain)
        created = updated = skipped = err = 0
        lines = []

        for tmpl in products:
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
                            continue
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

        products = self.env['product.template'].search(domain)
        ok = skip = err = 0
        lines = []

        for tmpl in products:
            ps_id = tmpl.ps_product_id
            image_b64 = tmpl.image_1920
            if not image_b64:
                lines.append(f'[SKIP] {tmpl.name}: nessuna immagine in Odoo')
                skip += 1
                continue
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

            if (ok + err) % 50 == 0 and (ok + err) > 0:
                self.env.cr.commit()

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
    # 6. Import ordini PS → Odoo
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

        created = skipped = err = 0
        lines = []
        SaleOrder = self.env['sale.order']

        for order_id in order_ids:
            try:
                # Verifica se già importato
                if SaleOrder.search([('ps_order_id', '=', order_id)], limit=1):
                    skipped += 1
                    continue

                ps_order = self.client.get_order(order_id)
                sale = self._create_sale_order_from_ps(ps_order)
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

    def _create_sale_order_from_ps(self, ps_order):
        """Crea un sale.order a partire dal dict ordine PS."""
        order_id = int(ps_order.get('id', 0))
        customer_id = int(ps_order.get('id_customer', 0))
        address_id = int(ps_order.get('id_address_delivery', 0))

        partner = self._find_or_create_partner(customer_id, address_id)
        if not partner:
            raise ValueError(f'Impossibile trovare/creare il cliente PS #{customer_id}')

        # Righe ordine
        order_rows = ps_order.get('associations', {}).get('order_rows', [])
        if not order_rows:
            return None

        order_lines = []
        for row in order_rows:
            ps_product_id = int(row.get('product_id', 0))
            ps_attribute_id = int(row.get('product_attribute_id', 0))
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
        if isinstance(date_order, str) and len(date_order) == 19:
            pass  # già in formato corretto
        else:
            date_order = fields.Datetime.now()

        sale = self.env['sale.order'].create({
            'partner_id': partner.id,
            'partner_shipping_id': partner.id,
            'partner_invoice_id': partner.id,
            'date_order': date_order,
            'ps_order_id': order_id,
            'ps_order_state': state_label,
            'ps_order_reference': ps_reference,
            'note': f'Ordine PrestaShop #{order_id} — Rif: {ps_reference} — Stato: {state_label}',
            'order_line': order_lines,
        })
        self.env.cr.commit()
        return sale

    def _find_or_create_partner(self, ps_customer_id, ps_address_id):
        """Trova o crea il partner Odoo corrispondente al cliente PS."""
        try:
            ps_customer = self.client.get_customer(ps_customer_id)
        except Exception:
            ps_customer = {}

        email = ps_customer.get('email', '')
        first_name = ps_customer.get('firstname', '')
        last_name = ps_customer.get('lastname', '')
        full_name = f'{first_name} {last_name}'.strip() or f'Cliente PS #{ps_customer_id}'

        # Cerca per email
        if email:
            partner = self.env['res.partner'].search([('email', '=', email)], limit=1)
            if partner:
                return partner

        # Recupera l'indirizzo per dati aggiuntivi
        try:
            ps_addr = self.client.get_address(ps_address_id)
        except Exception:
            ps_addr = {}

        phone = ps_addr.get('phone', '') or ps_addr.get('phone_mobile', '')
        company = ps_addr.get('company', '')

        partner_vals = {
            'name': full_name,
            'email': email,
            'phone': phone,
            'company_type': 'company' if company else 'person',
        }
        if company:
            partner_vals['company_name'] = company

        return self.env['res.partner'].create(partner_vals)

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
