"""
Motore di sincronizzazione Amazon ↔ Odoo.

Funzione 1 – full_import:
    Scarica tutti i listing dallo store Amazon e crea/aggiorna prodotti in Odoo.
    Solo manuale.

Funzione 2 – check_new_products:
    Per ogni prodotto in Odoo senza ASIN, cerca su Amazon per EAN.
    Se trovato: collega l'ASIN e imposta 'found_manufacturer'.
    Schedulata ogni N ore.

Funzione 3 – sync_prices_and_quantities:
    Per ogni prodotto con ASIN e sync_price/sync_qty attivo,
    aggiorna prezzo e/o quantità su Amazon via Listings PATCH API.
    Schedulata ogni N minuti.
"""

import base64
import logging
import re
from datetime import datetime

import requests

_log = logging.getLogger(__name__)


def _fetch_image_b64(url):
    """Downloads an image URL and returns base64-encoded bytes, or None on failure."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return base64.b64encode(resp.content)
    except Exception as exc:
        _log.warning('Impossibile scaricare immagine %s: %s', url, exc)
        return None


# ---------------------------------------------------------------------------
# Specifiche prodotto: mappa chiave Amazon → etichetta italiana
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Mappa variation_theme Amazon → (etichetta Odoo, chiavi attributo da cercare)
# ---------------------------------------------------------------------------

_VARIATION_THEME_MAP = {
    'SIZE':          ('Taglia',        ['size', 'size_name']),
    'SIZE_NAME':     ('Taglia',        ['size_name', 'size']),
    'COLOR':         ('Colore',        ['color', 'color_name']),
    'COLOR_NAME':    ('Colore',        ['color_name', 'color']),
    'FLAVOR':        ('Gusto',         ['flavor', 'flavor_name']),
    'FLAVOR_NAME':   ('Gusto',         ['flavor_name', 'flavor']),
    'STYLE':         ('Stile',         ['style', 'style_name']),
    'STYLE_NAME':    ('Stile',         ['style_name', 'style']),
    'PATTERN':       ('Modello',       ['pattern']),
    'MATERIAL':      ('Materiale',     ['material']),
    'CONFIGURATION': ('Configurazione',['configuration']),
    'UNIT_COUNT':    ('Unità',         ['unit_count']),
    'NUMBER_OF_ITEMS': ('Numero articoli', ['number_of_items']),
}


def _get_relationship_info(catalog):
    """
    Estrae (parent_asin, child_asins) dalle relationships del catalog SP-API.
    - parent_asin=None + child_asins → questo è il parent listing → da saltare
    - parent_asin→str + child_asins=[] → questo è un child variant
    - entrambi None → prodotto standalone
    """
    parent_asin = None
    child_asins = []
    for block in catalog.get('relationships', []):
        for rel in block.get('relationships', []):
            if rel.get('type') == 'VARIATION':
                parents = rel.get('parentAsins', [])
                children = rel.get('childAsins', [])
                if parents:
                    parent_asin = parents[0]
                if children:
                    child_asins.extend(children)
    return parent_asin, child_asins


def _get_variant_attr_info(product_dict, catalog):
    """
    Determina (attr_label, attr_value) per la dimensione di variante.

    Amazon variation_theme usa '/' come separatore tra dimensioni (es. 'SIZE/STYLE').
    Raccoglie TUTTE le dimensioni trovate con exact match sui componenti del tema:
    - 1 dimensione → restituisce (label, value) direttamente
    - N dimensioni → restituisce ('Variante', 'val1 – val2 – ...') composito
    Fallback: prova attributi comuni. Ultima risorsa: usa lo SKU.
    """
    cat_attrs = catalog.get('attributes', {})
    listing_attrs = product_dict.get('attributes', {})

    # variation_theme usa 'name' anziché 'value' come gli altri attributi
    vtheme_entries = (cat_attrs.get('variation_theme') or listing_attrs.get('variation_theme') or [])
    vtheme = (vtheme_entries[0].get('name', '') if vtheme_entries else '').upper()

    if vtheme:
        theme_parts = set(vtheme.split('/'))
        matched = []
        for theme_key, (label, attr_keys) in _VARIATION_THEME_MAP.items():
            if theme_key in theme_parts:
                for key in attr_keys:
                    val = _cat_val(cat_attrs, key) or _cat_val(listing_attrs, key)
                    if val:
                        matched.append((label, val))
                        break
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            composite = ' – '.join(v for _, v in matched)
            return 'Variante', composite

    # Fallback: cerca attributi comuni
    for key, label in [('size', 'Taglia'), ('size_name', 'Taglia'),
                        ('color', 'Colore'), ('color_name', 'Colore'),
                        ('flavor', 'Gusto'), ('style', 'Stile')]:
        val = _cat_val(cat_attrs, key) or _cat_val(listing_attrs, key)
        if val:
            return label, val

    # Ultima risorsa: usa lo SKU come valore
    return 'Variante', product_dict.get('sku', 'N/A')


def _strip_variant_suffix(name):
    """Rimuove il suffisso variante tra parentesi dal nome (es. '(Misura 2.5)')."""
    cleaned = re.sub(r'\s*\([^)]*\)\)*\s*$', '', name or '').strip()
    return cleaned or name


_SPEC_ATTRIBUTES = {
    'color': 'Colore',
    'size': 'Taglia',
    'material': 'Materiale',
    'style': 'Stile',
    'flavor': 'Gusto',
    'scent': 'Profumo',
    'pattern': 'Modello',
    'item_package_quantity': 'Pezzi per confezione',
    'unit_count': 'Unità',
    'age_range_description': 'Fascia età',
    'sport_type': 'Sport',
    'theme': 'Tema',
    'number_of_items': 'Numero articoli',
    'finish_type': 'Finitura',
    'configuration': 'Configurazione',
}


def _extract_spec_attrs(attrs):
    """Returns {label: value} for known spec attributes found in an Amazon attrs dict."""
    result = {}
    for key, label in _SPEC_ATTRIBUTES.items():
        val = _cat_val(attrs, key)
        if val:
            result[label] = val
    return result


# ---------------------------------------------------------------------------
# Helpers condivisi
# ---------------------------------------------------------------------------

def _parse_listing(listing):
    """Struttura normalizzata da un listing raw SP-API."""
    summaries = listing.get('summaries', [{}])
    summary = summaries[0] if summaries else {}
    attributes = listing.get('attributes', {})
    offers = listing.get('offers', [])
    fulfillment = listing.get('fulfillmentAvailability', [{}])
    channel = fulfillment[0].get('fulfillmentChannelCode', 'DEFAULT') if fulfillment else 'DEFAULT'

    price = None
    for offer in offers:
        if offer.get('offerType') == 'B2C':
            try:
                price = float(offer.get('price', {}).get('amount', 0))
            except (TypeError, ValueError):
                pass
            break
    if price is None and offers:
        try:
            price = float(offers[0].get('price', {}).get('amount', 0))
        except (TypeError, ValueError):
            price = 0.0

    # Extract description and specs directly from listing attributes as baseline
    bullets = [str(v.get('value', '')) for v in attributes.get('bullet_point', []) if v.get('value')]
    return {
        'sku': listing.get('sku', ''),
        'asin': summary.get('asin', ''),
        'name': summary.get('itemName', '') or _cat_val(attributes, 'item_name'),
        'status': ', '.join(summary.get('status', [])),
        'product_type': summary.get('productType', ''),
        'price': price or 0.0,
        'fulfillment': 'fba' if channel == 'AMAZON_NA' else 'fbm',
        'attributes': attributes,
        'description': _cat_val(attributes, 'product_description'),
        'bullet_points': bullets,
        'spec_attrs': _extract_spec_attrs(attributes),
    }


def _merge_catalog(product, catalog):
    """Arricchisce il dict prodotto con i dati del Catalog v2022."""
    if not catalog:
        return

    cat_attrs = catalog.get('attributes', {})
    summaries = catalog.get('summaries', [{}])
    summary = summaries[0] if summaries else {}

    product['brand'] = summary.get('brand', '') or _cat_val(cat_attrs, 'brand')
    product['model_number'] = summary.get('modelNumber', '') or _cat_val(cat_attrs, 'model_number')
    product['product_type'] = (
        (catalog.get('productTypes') or [{}])[0].get('productType', '')
        or product.get('product_type', '')
    )

    # EAN
    identifiers_data = catalog.get('identifiers', [{}])
    id_block = identifiers_data[0] if identifiers_data else {}
    for ident in id_block.get('identifiers', []):
        if ident.get('identifierType') in ('EAN', 'GTIN'):
            product['ean'] = ident.get('identifier', '')
            break
    if not product.get('ean'):
        for entry in cat_attrs.get('externally_assigned_product_identifier', []):
            if entry.get('type', '').lower() == 'ean':
                product['ean'] = str(entry.get('value', ''))
                break

    # Descrizione + bullet points (catalog ha la precedenza su listing)
    cat_desc = _cat_val(cat_attrs, 'product_description')
    if cat_desc:
        product['description'] = cat_desc
    cat_bullets = [str(v.get('value', '')) for v in cat_attrs.get('bullet_point', []) if v.get('value')]
    if cat_bullets:
        product['bullet_points'] = cat_bullets

    # Specifiche dal catalog (integrano quelle del listing)
    cat_specs = _extract_spec_attrs(cat_attrs)
    existing_specs = product.get('spec_attrs', {})
    existing_specs.update(cat_specs)
    product['spec_attrs'] = existing_specs

    # Dimensioni
    dims = catalog.get('dimensions', [{}])
    dim_block = dims[0] if dims else {}
    item_d = dim_block.get('item', {})
    product['weight_kg'] = _convert_weight(item_d.get('weight', {}))
    product['length_cm'] = _convert_length(item_d.get('length', {}))
    product['width_cm'] = _convert_length(item_d.get('width', {}))
    product['height_cm'] = _convert_length(item_d.get('height', {}))

    # Immagini (prima URL disponibile)
    images_data = catalog.get('images', [{}])
    images_block = images_data[0] if images_data else {}
    all_images = images_block.get('images', [])
    main_images = [i.get('link', '') for i in all_images if i.get('variant') == 'MAIN' and i.get('link')]
    product['image_url'] = main_images[0] if main_images else ''

    # Categoria
    summaries_block = catalog.get('summaries', [{}])
    s0 = summaries_block[0] if summaries_block else {}
    product['category'] = s0.get('browseClassification', {}).get('displayName', '')

    # Relazioni varianti
    parent_asin, child_asins = _get_relationship_info(catalog)
    product['parent_asin'] = parent_asin
    product['is_parent_listing'] = (parent_asin is None and bool(child_asins))
    if parent_asin:
        product['variation_attr'] = _get_variant_attr_info(product, catalog)


def _cat_val(attrs, key):
    val = attrs.get(key, [])
    if val and isinstance(val, list):
        return str(val[0].get('value', ''))
    return ''


def _convert_weight(w):
    if not w:
        return None
    try:
        val = float(w.get('value', 0))
    except (TypeError, ValueError):
        return None
    unit = w.get('unit', '').upper()
    factors = {'KILOGRAMS': 1, 'KG': 1, 'GRAMS': 0.001, 'G': 0.001,
               'POUNDS': 0.453592, 'LB': 0.453592, 'OUNCES': 0.0283495, 'OZ': 0.0283495}
    return round(val * factors.get(unit, 1), 3)


def _convert_length(d):
    if not d:
        return None
    try:
        val = float(d.get('value', 0))
    except (TypeError, ValueError):
        return None
    unit = d.get('unit', '').upper()
    factors = {'CENTIMETERS': 1, 'CM': 1, 'METERS': 100, 'M': 100,
               'INCHES': 2.54, 'IN': 2.54, 'FEET': 30.48, 'FT': 30.48,
               'MILLIMETERS': 0.1, 'MM': 0.1}
    return round(val * factors.get(unit, 1), 2)


def _get_or_create_category(env, name):
    if not name:
        ref = env.ref('product.product_category_all', raise_if_not_found=False)
        if ref:
            return ref
        return env['product.category'].search([], limit=1, order='id asc')
    cat = env['product.category'].search([('name', '=', name)], limit=1)
    if not cat:
        cat = env['product.category'].create({'name': name})
    return cat


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

def _resolve_product_type(env, requested):
    """Returns a valid product.template.type value for this Odoo installation."""
    field = env['product.template']._fields.get('type')
    if not field:
        return 'consu'
    sel = field.selection
    if callable(sel):
        sel = sel(env['product.template'].browse())
    valid = {k for k, _ in sel}
    if requested in valid:
        return requested
    for candidate in ('consu', 'storable', 'product', 'service'):
        if candidate in valid:
            _log.warning("product.template.type '%s' non valido, uso '%s'. Valori validi: %s",
                         requested, candidate, valid)
            return candidate
    return next(iter(valid), 'consu')


class SyncEngine:
    def __init__(self, env, client):
        self.env = env
        self.client = client

    # -----------------------------------------------------------------------
    # Funzione 1: Import completo (solo manuale)
    # -----------------------------------------------------------------------

    def full_import(self, log_rec, limit=None, update_existing=True, log_cb=None):
        """
        Scarica tutti i listing Amazon e crea/aggiorna product.template in Odoo.
        log_cb: callable(str) per messaggi di progresso in tempo reale.
        """
        def _log_line(msg):
            _log.info(msg)
            if log_cb:
                log_cb(msg)

        _log_line('Recupero mappa inventory FBA...')
        inventory_map = self.client.get_inventory_map()

        product_type_odoo = _resolve_product_type(
            self.env,
            self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.odoo_product_type', 'consu'
            )
        )

        # Pre-carica indici per matching rapido
        existing_by_sku = {
            p.x_amazon_sku: p for p in
            self.env['product.template'].search([('x_amazon_sku', '!=', False)])
        }
        existing_by_asin = {
            p.x_amazon_asin: p for p in
            self.env['product.template'].search([('x_amazon_asin', '!=', False)])
        }
        existing_by_barcode = {
            p.barcode: p for p in
            self.env['product.template'].search([('barcode', '!=', False)])
            if p.barcode
        }
        existing_by_default_code = {
            p.default_code: p for p in
            self.env['product.template'].search([('default_code', '!=', False)])
            if p.default_code
        }
        existing_by_parent_asin = {
            p.x_amazon_parent_asin: p for p in
            self.env['product.template'].search([('x_amazon_parent_asin', '!=', False)])
        }
        existing_variant_by_asin = {
            v.x_amazon_asin: v for v in
            self.env['product.product'].search([('x_amazon_asin', '!=', False)])
        }

        created = updated = skipped = errors = 0
        lines = []

        for listing in self.client.iter_seller_skus(max_products=limit):
            product = _parse_listing(listing)
            sku = product['sku']
            asin = product['asin']

            # Arricchisci con dati catalogo (include relationships)
            if asin:
                catalog = self.client.get_catalog_item(asin)
                _merge_catalog(product, catalog)

            product.setdefault('ean', '')
            product['qty_available'] = inventory_map.get(sku, 0)

            try:
                # Salta i listing parent (sono solo contenitori della famiglia)
                if product.get('is_parent_listing'):
                    skipped += 1
                    msg = f'SALTATO (parent listing): {sku} / {asin}'
                    lines.append(msg)
                    _log_line(msg)
                    continue

                parent_asin = product.get('parent_asin')

                if parent_asin:
                    # --- Prodotto con varianti ---
                    res, msg = self._process_variant_listing(
                        product, parent_asin, product_type_odoo,
                        existing_by_parent_asin, existing_variant_by_asin,
                        update_existing,
                    )
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    else:
                        skipped += 1
                else:
                    # --- Prodotto semplice ---
                    existing = (
                        existing_by_sku.get(sku)
                        or existing_by_asin.get(asin)
                        or (product['ean'] and existing_by_barcode.get(product['ean']))
                        or existing_by_default_code.get(sku)
                    )
                    if existing:
                        if update_existing:
                            self._update_product(existing, product)
                            updated += 1
                            msg = f'AGGIORNATO: {sku} / {asin} — {product["name"][:50]}'
                        else:
                            skipped += 1
                            msg = f'SALTATO (già esiste): {sku}'
                    else:
                        self._create_product(product, product_type_odoo)
                        created += 1
                        msg = f'CREATO: {sku} / {asin} — {product["name"][:50]}'

            except Exception as exc:
                errors += 1
                msg = f'ERRORE {sku}: {exc}'
                _log.error(msg, exc_info=True)

            lines.append(msg)
            _log_line(msg)

            # Commit ogni 50 prodotti per non perdere tutto in caso di errore
            if (created + updated) % 50 == 0:
                self.env.cr.commit()

        log_rec.write({
            'state': 'error' if errors and not (created + updated) else 'done',
            'products_total': created + updated + skipped + errors,
            'products_created': created,
            'products_updated': updated,
            'products_skipped': skipped,
            'products_error': errors,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return created, updated, skipped, errors

    @staticmethod
    def _parse_listing_report(row):
        """Converte una riga TSV del report listing in un dict prodotto (senza catalog API)."""
        try:
            price = float((row.get('price') or '0').strip())
        except (ValueError, TypeError):
            price = 0.0
        try:
            qty = int((row.get('quantity') or '0').strip())
        except (ValueError, TypeError):
            qty = 0
        fulfillment_ch = (row.get('fulfillment-channel') or 'DEFAULT').strip().upper()
        product_id_type = (row.get('product-id-type') or '').strip()
        product_id = (row.get('product-id') or '').strip()
        ean = product_id if product_id_type == '4' else ''
        is_fba = fulfillment_ch.startswith('AMAZON_')
        name_raw = (row.get('item-name') or row.get('﻿item-name') or '').strip()
        return {
            'sku': (row.get('seller-sku') or '').strip(),
            'asin': (row.get('asin1') or '').strip(),
            'name': name_raw,
            'status': (row.get('status') or '').strip().upper(),
            'product_type': '',
            'price': price,
            'fulfillment': 'fba' if is_fba else 'fbm',
            'attributes': {},
            'description': '',
            'bullet_points': [],
            'spec_attrs': {},
            'ean': ean,
            'qty_available': qty,
            'image_url': '',
            'parent_asin': None,
            'is_parent_listing': False,
        }

    def full_import_report_rows(self, log_rec, rows, row_offset, chunk_size, update_existing):
        """
        Processa un chunk di righe del report listing (senza chiamate catalog API).
        Ritorna il prossimo row_offset, oppure None se tutte le righe sono state elaborate.
        """
        product_type_odoo = _resolve_product_type(
            self.env,
            self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.odoo_product_type', 'consu'
            )
        )

        existing_by_sku = {
            p.x_amazon_sku: p for p in
            self.env['product.template'].search([('x_amazon_sku', '!=', False)])
        }
        existing_by_asin = {
            p.x_amazon_asin: p for p in
            self.env['product.template'].search([('x_amazon_asin', '!=', False)])
        }
        existing_by_barcode = {
            p.barcode: p for p in
            self.env['product.template'].search([('barcode', '!=', False)])
            if p.barcode
        }
        existing_by_default_code = {
            p.default_code: p for p in
            self.env['product.template'].search([('default_code', '!=', False)])
            if p.default_code
        }

        chunk = rows[row_offset:row_offset + chunk_size]
        if not chunk:
            return None

        created = updated = skipped = errors = 0
        lines = []

        for row in chunk:
            product = self._parse_listing_report(row)
            sku = product['sku']
            if not sku:
                continue
            try:
                existing = (
                    existing_by_sku.get(sku)
                    or existing_by_asin.get(product['asin'])
                    or (product['ean'] and existing_by_barcode.get(product['ean']))
                    or existing_by_default_code.get(sku)
                )
                if existing:
                    if update_existing:
                        self._update_product(existing, product)
                        updated += 1
                        msg = f'AGGIORNATO: {sku} / {product["asin"]} — {product["name"][:50]}'
                    else:
                        skipped += 1
                        msg = f'SALTATO (già esiste): {sku}'
                else:
                    self._create_product(product, product_type_odoo)
                    created += 1
                    msg = f'CREATO: {sku} / {product["asin"]} — {product["name"][:50]}'
            except Exception as exc:
                errors += 1
                msg = f'ERRORE {sku}: {exc}'
                _log.error(msg, exc_info=True)
            lines.append(msg)

        new_lines = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_total': log_rec.products_total + len(chunk),
            'products_created': log_rec.products_created + created,
            'products_updated': log_rec.products_updated + updated,
            'products_skipped': log_rec.products_skipped + skipped,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_lines,
        })
        self.env.cr.commit()

        next_offset = row_offset + len(chunk)
        return next_offset if next_offset < len(rows) else None

    def full_import_chunk(self, log_rec, page_token=None, update_existing=True):
        """
        Processa una pagina di listing Amazon (~20 prodotti).
        Ritorna il next_token per la pagina successiva, o None se l'import è completo.
        """
        items, next_token = self.client.fetch_listings_page(page_token)
        if not items:
            return None

        product_type_odoo = _resolve_product_type(
            self.env,
            self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.odoo_product_type', 'consu'
            )
        )

        existing_by_sku = {
            p.x_amazon_sku: p for p in
            self.env['product.template'].search([('x_amazon_sku', '!=', False)])
        }
        existing_by_asin = {
            p.x_amazon_asin: p for p in
            self.env['product.template'].search([('x_amazon_asin', '!=', False)])
        }
        existing_by_barcode = {
            p.barcode: p for p in
            self.env['product.template'].search([('barcode', '!=', False)])
            if p.barcode
        }
        existing_by_default_code = {
            p.default_code: p for p in
            self.env['product.template'].search([('default_code', '!=', False)])
            if p.default_code
        }
        existing_by_parent_asin = {
            p.x_amazon_parent_asin: p for p in
            self.env['product.template'].search([('x_amazon_parent_asin', '!=', False)])
        }
        existing_variant_by_asin = {
            v.x_amazon_asin: v for v in
            self.env['product.product'].search([('x_amazon_asin', '!=', False)])
        }

        created = updated = skipped = errors = 0
        lines = []

        for listing in items:
            product = _parse_listing(listing)
            sku = product['sku']
            asin = product['asin']

            if asin:
                catalog = self.client.get_catalog_item(asin)
                _merge_catalog(product, catalog)

            product.setdefault('ean', '')
            product['qty_available'] = 0

            try:
                if product.get('is_parent_listing'):
                    skipped += 1
                    msg = f'SALTATO (parent listing): {sku} / {asin}'
                    lines.append(msg)
                    _log.info(msg)
                    continue

                parent_asin = product.get('parent_asin')

                if parent_asin:
                    res, msg = self._process_variant_listing(
                        product, parent_asin, product_type_odoo,
                        existing_by_parent_asin, existing_variant_by_asin,
                        update_existing,
                    )
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    else:
                        skipped += 1
                else:
                    existing = (
                        existing_by_sku.get(sku)
                        or existing_by_asin.get(asin)
                        or (product['ean'] and existing_by_barcode.get(product['ean']))
                        or existing_by_default_code.get(sku)
                    )
                    if existing:
                        if update_existing:
                            self._update_product(existing, product)
                            updated += 1
                            msg = f'AGGIORNATO: {sku} / {asin} — {product["name"][:50]}'
                        else:
                            skipped += 1
                            msg = f'SALTATO (già esiste): {sku}'
                    else:
                        self._create_product(product, product_type_odoo)
                        created += 1
                        msg = f'CREATO: {sku} / {asin} — {product["name"][:50]}'
            except Exception as exc:
                errors += 1
                msg = f'ERRORE {sku}: {exc}'
                _log.error(msg, exc_info=True)

            lines.append(msg)
            _log.info(msg)

        new_total = log_rec.products_total + len(items)
        new_lines = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        update_vals = {
            'products_total': new_total,
            'products_created': log_rec.products_created + created,
            'products_updated': log_rec.products_updated + updated,
            'products_skipped': log_rec.products_skipped + skipped,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_lines,
        }
        if not next_token:
            update_vals.update({
                'state': 'done',
                'end_date': datetime.now(),
            })
        log_rec.write(update_vals)

        return next_token

    def _barcode_free(self, ean, exclude_product_ids=()):
        """Returns True if ean is not already assigned to another product.product."""
        if not ean:
            return False
        domain = [('barcode', '=', ean)]
        if exclude_product_ids:
            domain.append(('id', 'not in', list(exclude_product_ids)))
        return not self.env['product.product'].sudo().search(domain, limit=1)

    def _create_product(self, data, product_type_odoo):
        categ = _get_or_create_category(self.env, data.get('category', ''))
        description = self._format_description(data)
        ean = data.get('ean') or False
        if ean and not self._barcode_free(ean):
            _log.warning('Barcode %s già assegnato, salto per nuovo prodotto %s', ean, data['sku'])
            ean = False
        vals = {
            'name': data['name'] or data['sku'],
            'default_code': data['sku'],
            'barcode': ean,
            'list_price': data['price'],
            'type': product_type_odoo,
            'categ_id': categ.id,
            'weight': data.get('weight_kg') or 0.0,
            'description_sale': description,
            'x_amazon_sku': data['sku'],
            'x_amazon_asin': data['asin'],
            'x_amazon_status': 'active' if 'BUYABLE' in data.get('status', '') else 'inactive',
            'x_amazon_fulfillment': data['fulfillment'],
            'x_amazon_product_type': data.get('product_type', ''),
            'x_amazon_price': data['price'],
            'x_amazon_qty': data['qty_available'],
            'x_amazon_last_sync': datetime.now(),
        }
        if data.get('brand'):
            vals['x_amazon_found_by_manufacturer'] = False
        img = _fetch_image_b64(data.get('image_url'))
        if img:
            vals['image_1920'] = img
        template = self.env['product.template'].create(vals)
        if data.get('spec_attrs'):
            self._set_product_attributes(template, data['spec_attrs'])
        return template

    def _set_product_attributes(self, template, spec_attrs):
        """Creates product.attribute.line entries as specifications (no variant generation)."""
        for label, value in spec_attrs.items():
            if not value:
                continue
            attr = self.env['product.attribute'].search([('name', '=', label)], limit=1)
            if not attr:
                attr = self.env['product.attribute'].create({
                    'name': label,
                    'create_variant': 'no_variant',
                })
            elif attr.create_variant != 'no_variant':
                # L'attributo è già una dimensione di variante (create_variant='always'):
                # non tentare di convertirlo, salta la spec per questo prodotto.
                continue

            attr_val = self.env['product.attribute.value'].search([
                ('attribute_id', '=', attr.id),
                ('name', '=', value),
            ], limit=1)
            if not attr_val:
                attr_val = self.env['product.attribute.value'].create({
                    'attribute_id': attr.id,
                    'name': value,
                })

            existing_line = template.attribute_line_ids.filtered(
                lambda l: l.attribute_id.id == attr.id
            )
            if existing_line:
                if attr_val.id not in existing_line.value_ids.ids:
                    existing_line.write({'value_ids': [(4, attr_val.id)]})
            else:
                self.env['product.template.attribute.line'].create({
                    'product_tmpl_id': template.id,
                    'attribute_id': attr.id,
                    'value_ids': [(4, attr_val.id)],
                })

    def _update_product(self, product, data):
        vals = {
            'x_amazon_sku': data['sku'],
            'x_amazon_asin': data['asin'],
            'x_amazon_status': 'active' if 'BUYABLE' in data.get('status', '') else 'inactive',
            'x_amazon_fulfillment': data['fulfillment'],
            'x_amazon_product_type': data.get('product_type', ''),
            'x_amazon_price': data['price'],
            'x_amazon_qty': data['qty_available'],
            'x_amazon_last_sync': datetime.now(),
        }
        product.write(vals)
        if not product.barcode and data.get('ean'):
            if self._barcode_free(data['ean'], exclude_product_ids=product.product_variant_ids.ids):
                try:
                    product.write({'barcode': data['ean']})
                except Exception as exc:
                    _log.warning('Impossibile assegnare barcode %s a %s: %s',
                                 data['ean'], data['sku'], exc)

    # -----------------------------------------------------------------------
    # Gestione prodotti con varianti (famiglia Amazon)
    # -----------------------------------------------------------------------

    def _process_variant_listing(self, product, parent_asin, product_type_odoo,
                                  existing_by_parent_asin, existing_variant_by_asin,
                                  update_existing):
        """
        Processa un child ASIN Amazon come variante Odoo.
        Crea o aggiorna il product.template della famiglia e aggiunge la variante.
        Ritorna (stato, msg) dove stato è 'created'|'updated'|'skipped'.
        """
        sku = product['sku']
        asin = product['asin']
        attr_info = product.get('variation_attr')

        # Trova o crea il template della famiglia
        tmpl = existing_by_parent_asin.get(parent_asin)
        if not tmpl:
            tmpl = self._create_configurable_template(product, parent_asin, product_type_odoo)
            existing_by_parent_asin[parent_asin] = tmpl

        # Controlla se questa variante (child ASIN) esiste già
        existing_variant = existing_variant_by_asin.get(asin)
        if existing_variant:
            if update_existing:
                existing_variant.write({
                    'x_amazon_sku': sku,
                    'default_code': sku,
                })
                if not existing_variant.barcode and product.get('ean'):
                    if self._barcode_free(product['ean'], exclude_product_ids=[existing_variant.id]):
                        try:
                            existing_variant.write({'barcode': product['ean']})
                        except Exception as exc:
                            _log.warning('Impossibile assegnare barcode %s a variante %s: %s',
                                         product['ean'], sku, exc)
                tmpl.write({'x_amazon_last_sync': datetime.now()})
                msg = f'AGGIORNATO variante: {sku} / {asin} → {tmpl.name[:40]}'
                return 'updated', msg
            else:
                msg = f'SALTATO (variante già esiste): {sku}'
                return 'skipped', msg

        # Aggiunge la variante al template
        if attr_info:
            attr_label, attr_value = attr_info
        else:
            attr_label, attr_value = 'Variante', sku

        # Verifica conflitto di dimensione: se il template ha già una linea variante con
        # un attributo diverso, aggiungerne un secondo causerebbe l'esplosione cartesiana
        # di Odoo (es. Taglia×Stile genera combinazioni fantasma con barcode duplicati).
        # Soluzione: costruiamo un valore composito usando la dimensione già esistente,
        # cercando il suo valore negli spec_attrs del prodotto corrente.
        existing_variant_lines = tmpl.attribute_line_ids.filtered(
            lambda l: l.attribute_id.create_variant == 'always'
        )
        if existing_variant_lines:
            existing_labels = {l.attribute_id.name for l in existing_variant_lines}
            if attr_label not in existing_labels:
                existing_label = sorted(existing_labels)[0]
                existing_val = product.get('spec_attrs', {}).get(existing_label)
                if existing_val:
                    # Composito: agganciamo il nuovo valore alla dimensione esistente
                    attr_value = f'{existing_val} – {attr_value}'
                    attr_label = existing_label
                    _log.info(
                        'Composito variante %s / %s: variation_theme incoerente, '
                        'uso "%s=%s"', sku, asin, attr_label, attr_value
                    )
                else:
                    msg = (
                        f'SALTATO {sku} / {asin}: dimensione variante "{attr_label}={attr_value}" '
                        f'incompatibile con {sorted(existing_labels)} già usata dal template '
                        f'"{tmpl.name[:40]}" e nessun valore per "{existing_label}" in spec_attrs'
                    )
                    _log.warning(msg)
                    return 'skipped', msg

        variant = self._add_variant_to_template(tmpl, product, attr_label, attr_value)
        if variant:
            existing_variant_by_asin[asin] = variant
            msg = f'CREATO variante: {sku} / {asin} [{attr_label}={attr_value}] → {tmpl.name[:40]}'
            return 'created', msg

        msg = f'ERRORE creazione variante: {sku} / {asin}'
        return 'skipped', msg

    def _create_configurable_template(self, first_child, parent_asin, product_type_odoo):
        """Crea il product.template della famiglia (senza varianti ancora)."""
        categ = _get_or_create_category(self.env, first_child.get('category', ''))
        description = self._format_description(first_child)
        base_name = _strip_variant_suffix(first_child['name']) or first_child['name']
        vals = {
            'name': base_name,
            'list_price': first_child['price'],
            'type': product_type_odoo,
            'categ_id': categ.id,
            'weight': first_child.get('weight_kg') or 0.0,
            'description_sale': description,
            'x_amazon_parent_asin': parent_asin,
            'x_amazon_product_type': first_child.get('product_type', ''),
            'x_amazon_status': 'active',
            'x_amazon_fulfillment': first_child.get('fulfillment', 'fbm'),
            'x_amazon_last_sync': datetime.now(),
        }
        img = _fetch_image_b64(first_child.get('image_url'))
        if img:
            vals['image_1920'] = img
        return self.env['product.template'].create(vals)

    def _add_variant_to_template(self, tmpl, product, attr_label, attr_value):
        """
        Aggiunge una variante (product.product) al template configurabile.
        Crea l'attributo con create_variant='always' in modo che Odoo generi
        un product.product per ogni combinazione.
        """
        # Cerca un attributo con create_variant='always' (genera varianti reali).
        # NON modificare attributi no_variant esistenti: cerchiamo uno già corretto
        # o ne creiamo uno nuovo (possono coesistere due "Taglia" con mode diversa).
        attr = self.env['product.attribute'].search([
            ('name', '=', attr_label),
            ('create_variant', '=', 'always'),
        ], limit=1)
        if not attr:
            attr = self.env['product.attribute'].create({
                'name': attr_label,
                'create_variant': 'always',
            })

        # Valore attributo
        attr_val = self.env['product.attribute.value'].search([
            ('attribute_id', '=', attr.id),
            ('name', '=', attr_value),
        ], limit=1)
        if not attr_val:
            attr_val = self.env['product.attribute.value'].create({
                'attribute_id': attr.id,
                'name': attr_value,
            })

        # Riga attributo sul template
        attr_line = tmpl.attribute_line_ids.filtered(
            lambda l: l.attribute_id.id == attr.id)
        if not attr_line:
            self.env['product.template.attribute.line'].create({
                'product_tmpl_id': tmpl.id,
                'attribute_id': attr.id,
                'value_ids': [(4, attr_val.id)],
            })
        elif attr_val.id not in attr_line.value_ids.ids:
            attr_line.write({'value_ids': [(4, attr_val.id)]})

        # Forza aggiornamento del recordset varianti
        tmpl.invalidate_recordset()

        # Trova la product.product appena creata per questo valore
        variant = tmpl.product_variant_ids.filtered(
            lambda v: any(
                ptav.product_attribute_value_id.id == attr_val.id
                for ptav in v.product_template_attribute_value_ids
            )
        )
        if not variant:
            return None

        variant = variant[0]
        variant.write({
            'default_code': product['sku'],
            'x_amazon_asin': product['asin'],
            'x_amazon_sku': product['sku'],
        })
        if not variant.barcode and product.get('ean'):
            if self._barcode_free(product['ean'], exclude_product_ids=[variant.id]):
                try:
                    variant.write({'barcode': product['ean']})
                except Exception as exc:
                    _log.warning('Impossibile assegnare barcode %s a nuova variante %s: %s',
                                 product['ean'], product['sku'], exc)
        return variant

    @staticmethod
    def _format_description(data):
        parts = []
        if data.get('description'):
            parts.append(data['description'])
        bullets = data.get('bullet_points', [])
        if bullets:
            items = ''.join(f'<li>{b}</li>' for b in bullets if b)
            parts.append(f'<ul>{items}</ul>')
        return ''.join(parts)

    # -----------------------------------------------------------------------
    # Funzione: Download immagini mancanti
    # -----------------------------------------------------------------------

    def download_missing_images(self, log_rec):
        """Downloads images from Amazon catalog for products without image_1920."""
        domain = [
            ('x_amazon_asin', '!=', False),
            ('image_1920', '=', False),
        ]
        products = self.env['product.template'].search(domain)
        total = len(products)
        ok = errors = skipped = 0
        lines = []

        for product in products:
            asin = product.x_amazon_asin
            try:
                catalog = self.client.get_catalog_item(asin)
                images_data = catalog.get('images', [{}])
                images_block = images_data[0] if images_data else {}
                all_images = images_block.get('images', [])
                main_images = [i.get('link', '') for i in all_images
                               if i.get('variant') == 'MAIN' and i.get('link')]
                url = main_images[0] if main_images else ''
                img = _fetch_image_b64(url) if url else None
                if img:
                    product.write({'image_1920': img})
                    ok += 1
                    msg = f'OK {product.default_code or asin}: immagine scaricata'
                else:
                    skipped += 1
                    msg = f'SKIP {product.default_code or asin}: nessuna immagine disponibile'
            except Exception as exc:
                errors += 1
                msg = f'ERRORE {product.default_code or asin}: {exc}'
                _log.error(msg, exc_info=True)

            lines.append(msg)
            _log.info(msg)

            if ok % 20 == 0 and ok:
                self.env.cr.commit()

        log_rec.write({
            'state': 'error' if errors and not ok else 'done',
            'products_total': total,
            'products_updated': ok,
            'products_skipped': skipped,
            'products_error': errors,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return ok, skipped, errors

    # -----------------------------------------------------------------------
    # Funzione 2: Controllo nuovi prodotti (schedulata)
    # -----------------------------------------------------------------------

    def check_new_products(self, log_rec, hours_back=24):
        """
        Cerca su Amazon i prodotti Odoo senza ASIN che hanno un barcode EAN.
        Se trovati, collega l'ASIN e marca found_by_manufacturer=True.
        """
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=hours_back)

        domain = [
            ('x_amazon_asin', '=', False),
            ('barcode', '!=', False),
            ('create_date', '>=', cutoff.strftime('%Y-%m-%d %H:%M:%S')),
        ]
        products = self.env['product.template'].search(domain)
        total = len(products)
        found = not_found = errors = 0
        lines = []

        for product in products:
            ean = product.barcode
            if not ean:
                continue
            try:
                results = self.client.search_by_ean(ean)
                if results:
                    best = results[0]
                    product.write({
                        'x_amazon_asin': best['asin'],
                        'x_amazon_product_type': best['product_type'],
                        'x_amazon_status': 'found_manufacturer',
                        'x_amazon_found_by_manufacturer': True,
                        'x_amazon_last_sync': datetime.now(),
                    })
                    found += 1
                    msg = (f'TROVATO su Amazon: {product.default_code or product.name} '
                           f'EAN={ean} → ASIN={best["asin"]} ({best["name"][:40]})')
                else:
                    product.write({'x_amazon_status': 'not_listed'})
                    not_found += 1
                    msg = f'Non trovato su Amazon: {product.default_code or product.name} EAN={ean}'
            except Exception as exc:
                errors += 1
                msg = f'ERRORE {product.default_code}: {exc}'
                _log.error(msg, exc_info=True)

            lines.append(msg)
            _log.info(msg)

        log_rec.write({
            'state': 'error' if errors == total else 'done',
            'products_total': total,
            'products_updated': found,
            'products_skipped': not_found,
            'products_error': errors,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return found, not_found, errors

    # -----------------------------------------------------------------------
    # Funzione 3: Sync prezzi e quantità (schedulata)
    # -----------------------------------------------------------------------

    def sync_prices_and_quantities(self, log_rec):
        """
        Per ogni prodotto con ASIN e sync_price/sync_qty attivo,
        aggiorna prezzo e/o quantità su Amazon.
        Gestisce sia prodotti semplici (SKU sul template) sia famiglie con varianti
        (SKU sui product.product, template identificato da x_amazon_parent_asin).
        """
        # Prodotti semplici: SKU e ASIN direttamente sul template
        simple_domain = [
            ('x_amazon_asin', '!=', False),
            ('x_amazon_sku', '!=', False),
            '|',
            ('x_amazon_sync_price', '=', True),
            ('x_amazon_sync_qty', '=', True),
        ]
        simple_products = self.env['product.template'].search(simple_domain)

        # Famiglie con varianti: template con parent_asin, varianti con SKU
        variant_domain = [
            ('x_amazon_parent_asin', '!=', False),
            '|',
            ('x_amazon_sync_price', '=', True),
            ('x_amazon_sync_qty', '=', True),
        ]
        variant_templates = self.env['product.template'].search(variant_domain)

        total = len(simple_products) + sum(
            len(t.product_variant_ids.filtered(lambda v: v.x_amazon_sku))
            for t in variant_templates
        )
        ok = errors = 0
        lines = []

        # --- Prodotti semplici ---
        for product in simple_products:
            sku = product.x_amazon_sku
            product_type = product.x_amazon_product_type or 'PRODUCT'
            ops_done = []
            error_msg = ''

            if not product.x_amazon_product_type:
                try:
                    listing_data = self.client.get_listing_item(sku)
                    pt = listing_data.get('summaries', [{}])[0].get('productType', 'PRODUCT') \
                        if listing_data.get('summaries') else 'PRODUCT'
                    product.x_amazon_product_type = pt
                    product_type = pt
                except Exception:
                    pass

            if product.x_amazon_sync_price:
                try:
                    self.client.patch_price(sku, product_type, product.list_price)
                    ops_done.append(f'prezzo={product.list_price:.2f}€')
                except Exception as exc:
                    error_msg += f'prezzo: {exc}; '
                    _log.error('Errore patch_price %s: %s', sku, exc)

            if product.x_amazon_sync_qty:
                if product.x_amazon_fulfillment == 'fbm':
                    try:
                        qty = int(product.with_context(warehouse=False).qty_available)
                        self.client.patch_quantity(sku, product_type, qty)
                        ops_done.append(f'qty={qty}')
                    except Exception as exc:
                        error_msg += f'qty: {exc}; '
                        _log.error('Errore patch_quantity %s: %s', sku, exc)
                else:
                    ops_done.append('qty=skip(FBA)')

            now = datetime.now()
            if ops_done:
                ok += 1
                msg = f'OK {sku}: {", ".join(ops_done)}'
                product.write({'x_amazon_last_sync': now, 'x_amazon_sync_error': False})
            else:
                errors += 1
                msg = f'ERRORE {sku}: {error_msg}'
                product.write({
                    'x_amazon_last_sync': now,
                    'x_amazon_sync_error': error_msg,
                    'x_amazon_status': 'error',
                })

            lines.append(msg)
            _log.info(msg)

        # --- Famiglie con varianti ---
        for tmpl in variant_templates:
            product_type = tmpl.x_amazon_product_type or 'PRODUCT'
            variants = tmpl.product_variant_ids.filtered(lambda v: v.x_amazon_sku)
            if not variants:
                continue

            # Recupera productType se mancante, usando il primo SKU disponibile
            if not tmpl.x_amazon_product_type:
                try:
                    listing_data = self.client.get_listing_item(variants[0].x_amazon_sku)
                    pt = listing_data.get('summaries', [{}])[0].get('productType', 'PRODUCT') \
                        if listing_data.get('summaries') else 'PRODUCT'
                    tmpl.x_amazon_product_type = pt
                    product_type = pt
                except Exception:
                    pass

            tmpl_ok = tmpl_errors = 0
            tmpl_error_parts = []

            for variant in variants:
                sku = variant.x_amazon_sku
                ops_done = []
                variant_error = ''

                if tmpl.x_amazon_sync_price:
                    try:
                        self.client.patch_price(sku, product_type, variant.lst_price)
                        ops_done.append(f'prezzo={variant.lst_price:.2f}€')
                    except Exception as exc:
                        variant_error += f'prezzo: {exc}; '
                        _log.error('Errore patch_price variante %s: %s', sku, exc)

                if tmpl.x_amazon_sync_qty:
                    if tmpl.x_amazon_fulfillment == 'fbm':
                        try:
                            qty = int(variant.with_context(warehouse=False).qty_available)
                            self.client.patch_quantity(sku, product_type, qty)
                            ops_done.append(f'qty={qty}')
                        except Exception as exc:
                            variant_error += f'qty: {exc}; '
                            _log.error('Errore patch_quantity variante %s: %s', sku, exc)
                    else:
                        ops_done.append('qty=skip(FBA)')

                if ops_done:
                    tmpl_ok += 1
                    ok += 1
                    msg = f'OK variante {sku}: {", ".join(ops_done)}'
                else:
                    tmpl_errors += 1
                    errors += 1
                    tmpl_error_parts.append(f'{sku}: {variant_error}')
                    msg = f'ERRORE variante {sku}: {variant_error}'

                lines.append(msg)
                _log.info(msg)

            now = datetime.now()
            if tmpl_errors and not tmpl_ok:
                tmpl.write({
                    'x_amazon_last_sync': now,
                    'x_amazon_sync_error': '; '.join(tmpl_error_parts),
                    'x_amazon_status': 'error',
                })
            else:
                tmpl.write({
                    'x_amazon_last_sync': now,
                    'x_amazon_sync_error': '; '.join(tmpl_error_parts) if tmpl_error_parts else False,
                })

        log_rec.write({
            'state': 'error' if errors and not ok else 'done',
            'products_total': total,
            'products_updated': ok,
            'products_error': errors,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return ok, errors

    # -----------------------------------------------------------------------
    # Funzione 4: Riconciliazione store (manuale o schedulata)
    # -----------------------------------------------------------------------

    def reconcile_store(self, log_rec):
        """
        Confronta i prodotti nel nostro store Amazon con quelli in Odoo.
        - SKU in Odoo trovato su Amazon → aggiorna x_amazon_status e x_amazon_price
        - SKU in Odoo NON trovato su Amazon → marca x_amazon_status='inactive'
        - SKU su Amazon senza corrispondente in Odoo → segnalato nel log
        """
        log_rec.write({'log_lines': 'Download listing da Amazon in corso...'})
        self.env.cr.commit()

        amazon_by_sku = {}
        for item in self.client.iter_seller_skus():
            parsed = _parse_listing(item)
            sku = parsed['sku']
            if sku:
                amazon_by_sku[sku] = parsed

        total_amazon = len(amazon_by_sku)
        lines = [f'Store Amazon: {total_amazon} listing trovati.']
        _log.info('Riconciliazione: %d listing su Amazon', total_amazon)
        log_rec.write({'log_lines': lines[0]})
        self.env.cr.commit()

        simple_products = self.env['product.template'].search([('x_amazon_sku', '!=', False)])
        variant_products = self.env['product.product'].search([('x_amazon_sku', '!=', False)])

        matched = updated = only_odoo = only_amazon = errors = 0
        odoo_skus_seen = set()

        for product in simple_products:
            sku = product.x_amazon_sku
            odoo_skus_seen.add(sku)
            amazon_data = amazon_by_sku.get(sku)
            try:
                if not amazon_data:
                    only_odoo += 1
                    old_status = product.x_amazon_status
                    product.write({'x_amazon_status': 'inactive'})
                    note = f' (era: {old_status})' if old_status and old_status != 'inactive' else ''
                    msg = f'[SOLO ODOO] {product.default_code or product.name} SKU={sku}{note}'
                else:
                    matched += 1
                    new_status = 'active' if 'BUYABLE' in amazon_data.get('status', '') else 'inactive'
                    new_price = amazon_data.get('price') or 0.0
                    vals = {'x_amazon_status': new_status, 'x_amazon_price': new_price}
                    if product.x_amazon_status != new_status or product.x_amazon_price != new_price:
                        product.write(vals)
                        updated += 1
                    msg = (f'[OK] {product.default_code or product.name} SKU={sku} '
                           f'stato={new_status} prezzo={new_price:.2f}€')
            except Exception as exc:
                errors += 1
                msg = f'[ERRORE] SKU={sku}: {exc}'
                _log.error(msg, exc_info=True)
            lines.append(msg)
            _log.info(msg)

        for variant in variant_products:
            sku = variant.x_amazon_sku
            if sku in odoo_skus_seen:
                continue
            odoo_skus_seen.add(sku)
            tmpl = variant.product_tmpl_id
            amazon_data = amazon_by_sku.get(sku)
            try:
                if not amazon_data:
                    only_odoo += 1
                    msg = (f'[SOLO ODOO] variante {tmpl.default_code or tmpl.name} '
                           f'/ {variant.display_name} SKU={sku}')
                else:
                    matched += 1
                    new_status = 'active' if 'BUYABLE' in amazon_data.get('status', '') else 'inactive'
                    new_price = amazon_data.get('price') or 0.0
                    tmpl.write({'x_amazon_status': new_status, 'x_amazon_price': new_price})
                    updated += 1
                    msg = (f'[OK] variante {tmpl.default_code or tmpl.name} '
                           f'/ {variant.display_name} SKU={sku} stato={new_status}')
            except Exception as exc:
                errors += 1
                msg = f'[ERRORE] variante SKU={sku}: {exc}'
                _log.error(msg, exc_info=True)
            lines.append(msg)
            _log.info(msg)

        for sku, amazon_data in amazon_by_sku.items():
            if sku not in odoo_skus_seen:
                only_amazon += 1
                msg = (f'[SOLO AMAZON] SKU={sku} ASIN={amazon_data.get("asin", "?")} '
                       f'"{amazon_data.get("name", "")[:60]}"')
                lines.append(msg)
                _log.info(msg)

        summary = (
            f'\nRiepilogo: {matched} trovati su entrambi ({updated} aggiornati in Odoo), '
            f'{only_odoo} solo in Odoo (non su Amazon), '
            f'{only_amazon} solo su Amazon (non importati), {errors} errori.'
        )
        lines.append(summary)
        _log.info(summary)

        log_rec.write({
            'state': 'done',
            'products_total': len(odoo_skus_seen),
            'products_updated': updated,
            'products_skipped': only_odoo,
            'products_created': only_amazon,
            'products_error': errors,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return matched, only_odoo, only_amazon, errors
