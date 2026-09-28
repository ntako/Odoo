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
from datetime import datetime, timezone

import requests

from odoo.exceptions import UserError

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
        'sku': (listing.get('sku') or '').strip(),
        'asin': (summary.get('asin') or '').strip(),
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

    # Dimensioni — preferisce 'item' (prodotto nudo), ma Amazon spesso
    # popola solo 'package' (prodotto imballato) e lascia 'item' assente:
    # senza fallback il prodotto risultava sempre senza dimensioni anche
    # quando Amazon le aveva (verificato dal vivo su un ASIN reale — vedi
    # TASK.md). 'package' è leggermente più grande del prodotto nudo ma è
    # comunque un dato reale, meglio di niente per volume/logistica.
    dims = catalog.get('dimensions', [{}])
    dim_block = dims[0] if dims else {}
    item_d = dim_block.get('item') or dim_block.get('package') or {}
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
    browse_cls = s0.get('browseClassification', {})
    product['category'] = browse_cls.get('displayName', '')
    product['browse_node_id'] = browse_cls.get('classificationId', '')

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

def _storable_vals(env):
    """
    Da Odoo 17 in poi, 'type' da solo non basta più a rendere un prodotto
    tracciabile a magazzino: serve anche il campo booleano is_storable=True
    ('Traccia inventario'), altrimenti Odoo rifiuta di creare quant per quel
    prodotto ('Impossibile creare quantitativi per consumabili o servizi').
    Ritorna {'is_storable': True} solo se il campo esiste su questa
    installazione, altrimenti {} (versioni dove 'type'='product' bastava già).
    """
    if 'is_storable' in env['product.template']._fields:
        return {'is_storable': True}
    return {}


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

    def _current_marketplace(self):
        """
        Risolve il ke.amazon.marketplace corrispondente al marketplace_id (codice
        SP-API, es. APJ6JRA9NG5V4) del client di questa sync. Usato per taggare
        gli ordini importati (fase 2) e, da qui in poi, dal full import (fase 3)
        per popolare ke.amazon.listing. Le altre sync (prezzi/quantità,
        riconciliazione, ecc.) restano mono-marketplace finché non vengono
        riscritte (vedi TASK.md).
        Ritorna un recordset vuoto se il client non è configurato (es. chiamato
        fuori da un contesto di sync) o se il marketplace_id non è tra quelli
        censiti in ke.amazon.marketplace — non è un errore bloccante, il campo
        sull'ordine resta semplicemente vuoto.
        """
        if not self.client:
            return self.env['ke.amazon.marketplace']
        return self.env['ke.amazon.marketplace'].search(
            [('marketplace_id', '=', self.client.marketplace_id)], limit=1,
        )

    def _is_primary_marketplace(self):
        """
        True se il client di questa sync è sul marketplace "primario" — quello
        ancora configurato nel vecchio campo Impostazioni → Amazon → Marketplace
        ID. Durante la transizione a multi-marketplace (fase 3), solo il
        marketplace primario continua a scrivere sui vecchi campi x_amazon_*
        del prodotto: riconciliazione, sync prezzi/quantità e le viste non sono
        ancora stati convertiti a leggere/scrivere ke.amazon.listing, quindi
        smettere di scrivere quei campi per QUALSIASI marketplace li romperebbe.
        Senza client (contesto non multi-marketplace, es. wizard interni) si
        comporta come "sempre primario", per non cambiare nulla lì.
        """
        if not self.client:
            return True
        primary_id = self.env['ir.config_parameter'].sudo().get_param('ke_amazon_sync.marketplace_id')
        return not primary_id or self.client.marketplace_id == primary_id

    def _upsert_listing(self, product_tmpl, data):
        """
        Crea o aggiorna la riga ke.amazon.listing per (product_tmpl, marketplace
        corrente della sync). Non fa nulla se il marketplace corrente non è
        censito in ke.amazon.marketplace (non dovrebbe succedere durante un
        import in coda, ma non deve bloccare l'import se capita).
        """
        marketplace = self._current_marketplace()
        if not marketplace:
            return
        Listing = self.env['ke.amazon.listing'].sudo()
        vals = {
            'sku': data.get('sku'),
            'asin': data.get('asin'),
            'fnsku': data.get('fnsku'),
            'product_type': data.get('product_type', ''),
            'status': 'active' if 'BUYABLE' in (data.get('status') or '') else 'inactive',
            'fulfillment': data.get('fulfillment'),
            'price': data.get('price') or 0.0,
            'qty': data.get('qty_available') or 0,
            'category': data.get('category', ''),
            'browse_node_id': data.get('browse_node_id', ''),
        }
        listing = Listing.search([
            ('product_id', '=', product_tmpl.id), ('marketplace_id', '=', marketplace.id),
        ], limit=1)
        if listing:
            listing.write(vals)
        else:
            vals.update({'product_id': product_tmpl.id, 'marketplace_id': marketplace.id})
            Listing.create(vals)

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
        existing_by_barcode = self._build_barcode_map()
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
                    # Il match per ASIN copre già il caso di rifornimento (SKU cambiato,
                    # ASIN invariato), qualunque sia il pattern esatto del vecchio SKU.
                    existing = (
                        existing_by_sku.get(sku)
                        or existing_by_asin.get(asin)
                        or (product['ean'] and existing_by_barcode.get(product['ean']))
                        or existing_by_default_code.get(sku)
                    )
                    # Non sovrascrivere un SKU con '+' usando uno senza '+'
                    if existing and not sku.endswith('+') and (existing.x_amazon_sku or '').endswith('+'):
                        skipped += 1
                        msg = f'SALTATO (sostituito da {existing.x_amazon_sku}): {sku}'
                    elif existing:
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
        # Nel report Amazon 'product-id-type' è testuale ('EAN', 'GTIN', 'JAN', 'UPC', 'ASIN', ...),
        # non un codice numerico: il confronto con '4' non ha mai riscontro e lasciava
        # 'ean' sempre vuoto, disabilitando di fatto il matching per barcode.
        product_id_type = (row.get('product-id-type') or '').strip().upper()
        product_id = (row.get('product-id') or '').strip()
        ean = product_id if product_id_type in ('EAN', 'GTIN', 'JAN') else ''
        is_fba = fulfillment_ch.startswith('AMAZON_')
        name_raw = (row.get('item-name') or row.get('﻿item-name') or '').strip()
        # Il report usa 'Active'/'Inactive'; la Listings API usa 'BUYABLE'.
        # Normalizziamo in 'BUYABLE' per rendere il controllo downstream uniforme.
        raw_status = (row.get('status') or '').strip().upper()
        status = 'BUYABLE' if raw_status == 'ACTIVE' else raw_status
        return {
            'sku': (row.get('seller-sku') or '').strip(),
            'asin': (row.get('asin1') or '').strip(),
            'name': name_raw,
            'status': status,
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
        existing_by_barcode = self._build_barcode_map()
        existing_by_default_code = {
            p.default_code: p for p in
            self.env['product.template'].search([('default_code', '!=', False)])
            if p.default_code
        }
        # Fase 3 multi-marketplace: un prodotto creato/aggiornato SOLO da un
        # marketplace non primario non ha i campi legacy x_amazon_* popolati
        # (vedi _is_primary_marketplace) — senza questo, un secondo marketplace
        # per lo stesso prodotto non lo troverebbe nelle mappe sopra e ne
        # creerebbe un duplicato. ke.amazon.listing copre tutti i marketplace,
        # a prescindere da quale sia primario.
        for listing in self.env['ke.amazon.listing'].sudo().search([]):
            if listing.sku and listing.sku not in existing_by_sku:
                existing_by_sku[listing.sku] = listing.product_id
            if listing.asin and listing.asin not in existing_by_asin:
                existing_by_asin[listing.asin] = listing.product_id

        # ASIN → SKU "buono" nel report: a parità di ASIN vince sempre la versione '+'
        # (rifornimento), indipendentemente dal pattern esatto del vecchio SKU
        # (typo, punto, ecc. — non ci si affida più al confronto testuale sku+'+').
        best_sku_by_asin = {}
        for r in rows:
            r_sku = (r.get('seller-sku') or '').strip()
            r_asin = (r.get('asin1') or '').strip()
            if not r_sku or not r_asin:
                continue
            current = best_sku_by_asin.get(r_asin)
            if current is None or (r_sku.endswith('+') and not current.endswith('+')):
                best_sku_by_asin[r_asin] = r_sku

        chunk = rows[row_offset:row_offset + chunk_size]
        if not chunk:
            return None

        created = updated = skipped = errors = 0
        lines = []

        for row in chunk:
            product = self._parse_listing_report(row)
            sku = product['sku']
            asin = product['asin']
            if not sku:
                continue

            # SKU superato dalla versione '+' con lo stesso ASIN: marca inattivo e salta
            best_sku = best_sku_by_asin.get(asin) if asin else None
            if best_sku and best_sku != sku and best_sku.endswith('+'):
                existing = existing_by_sku.get(sku) or existing_by_default_code.get(sku)
                if existing:
                    existing.write({'x_amazon_status': 'inactive', 'x_amazon_last_sync': datetime.now()})
                skipped += 1
                lines.append(f'SALTATO (sostituito da {best_sku}): {sku}')
                continue

            try:
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
                        msg = f'AGGIORNATO: {sku} / {product["asin"]} — {product["name"][:50]}'
                    else:
                        skipped += 1
                        msg = f'SALTATO (già esiste): {sku}'
                    result_tmpl = existing
                else:
                    result_tmpl = self._create_product(product, product_type_odoo)
                    created += 1
                    msg = f'CREATO: {sku} / {product["asin"]} — {product["name"][:50]}'

                # Aggiorna le mappe in-memory: senza questo, un ASIN che compare più
                # volte nello stesso chunk sotto SKU diversi (listing raggruppati
                # 'amzn.gr.*', varianti 'stickerless', typo nello SKU) non verrebbe
                # riconosciuto come lo stesso prodotto e finirebbe duplicato.
                existing_by_sku[sku] = result_tmpl
                if asin:
                    existing_by_asin[asin] = result_tmpl
                if product['ean']:
                    existing_by_barcode[product['ean']] = result_tmpl
                existing_by_default_code[sku] = result_tmpl
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
        # Fase 3 multi-marketplace (stesso fix già fatto in full_import_report_rows,
        # mancava qui — vedi TASK.md): un prodotto creato/aggiornato SOLO da un
        # marketplace non primario non ha i campi legacy x_amazon_* popolati
        # (vedi _is_primary_marketplace), quindi non comparirebbe nelle mappe
        # sopra e un secondo marketplace lo duplicherebbe invece di aggiornarlo.
        # ke.amazon.listing copre tutti i marketplace, a prescindere da quale
        # sia primario. Non serve estendere existing_by_parent_asin/
        # existing_variant_by_asin: le famiglie con varianti non passano mai
        # da _upsert_listing (vedi _process_variant_listing), quindi non hanno
        # comunque righe ke.amazon.listing da cui attingere.
        for mp_listing in self.env['ke.amazon.listing'].sudo().search([]):
            if mp_listing.sku and mp_listing.sku not in existing_by_sku:
                existing_by_sku[mp_listing.sku] = mp_listing.product_id
            if mp_listing.asin and mp_listing.asin not in existing_by_asin:
                existing_by_asin[mp_listing.asin] = mp_listing.product_id
        existing_by_barcode = self._build_barcode_map()
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
                    # Il match per ASIN copre già il caso di rifornimento (SKU cambiato,
                    # ASIN invariato), qualunque sia il pattern esatto del vecchio SKU.
                    existing = (
                        existing_by_sku.get(sku)
                        or existing_by_asin.get(asin)
                        or (product['ean'] and existing_by_barcode.get(product['ean']))
                        or existing_by_default_code.get(sku)
                    )
                    # Non sovrascrivere un SKU con '+' usando uno senza '+'
                    if existing and not sku.endswith('+') and (existing.x_amazon_sku or '').endswith('+'):
                        skipped += 1
                        msg = f'SALTATO (sostituito da {existing.x_amazon_sku}): {sku}'
                        result_tmpl = None
                    elif existing:
                        if update_existing:
                            self._update_product(existing, product)
                            updated += 1
                            msg = f'AGGIORNATO: {sku} / {asin} — {product["name"][:50]}'
                        else:
                            skipped += 1
                            msg = f'SALTATO (già esiste): {sku}'
                        result_tmpl = existing
                    else:
                        result_tmpl = self._create_product(product, product_type_odoo)
                        created += 1
                        msg = f'CREATO: {sku} / {asin} — {product["name"][:50]}'

                    # Aggiorna le mappe in-memory: senza questo, un ASIN che compare più
                    # volte nello stesso chunk sotto SKU diversi (listing raggruppati,
                    # varianti 'stickerless', typo nello SKU) non verrebbe riconosciuto
                    # come lo stesso prodotto e finirebbe duplicato.
                    if result_tmpl:
                        existing_by_sku[sku] = result_tmpl
                        if asin:
                            existing_by_asin[asin] = result_tmpl
                        if product['ean']:
                            existing_by_barcode[product['ean']] = result_tmpl
                        existing_by_default_code[sku] = result_tmpl
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

    def _build_barcode_map(self):
        """Mappa barcode -> product.template. Logga (senza bloccare) i barcode duplicati:
        in caso di duplicato l'ultimo prodotto trovato sovrascrive il precedente nella mappa,
        con effetto sul matching a valle (import via EAN)."""
        by_barcode = {}
        for product in self.env['product.template'].search([('barcode', '!=', False)]):
            if not product.barcode:
                continue
            previous = by_barcode.get(product.barcode)
            if previous is not None:
                _log.warning(
                    'Barcode duplicato %s: %s (id=%d) sovrascrive %s (id=%d) nella mappa di matching import',
                    product.barcode, product.display_name, product.id,
                    previous.display_name, previous.id,
                )
            by_barcode[product.barcode] = product
        return by_barcode

    def _net_price(self, gross):
        """Strip VAT from an Amazon gross price using the configured default VAT rate."""
        rate = float(self.env['ir.config_parameter'].sudo().get_param(
            'ke_amazon_sync.default_vat_rate', '22'
        ))
        if rate <= 0:
            return gross
        return gross / (1.0 + rate / 100.0)

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
            'list_price': self._net_price(data['price']),
            'type': product_type_odoo,
            **_storable_vals(self.env),
            'categ_id': categ.id,
            'weight': data.get('weight_kg') or 0.0,
            'description_sale': description,
            'product_length': data.get('length_cm') or 0.0,
            'product_width': data.get('width_cm') or 0.0,
            'product_height': data.get('height_cm') or 0.0,
        }
        # Campi Amazon "legacy" (x_amazon_*): scritti solo se questa sync è sul
        # marketplace primario (fase 3, transizione multi-marketplace — vedi
        # _is_primary_marketplace). Il dato vero, per QUALSIASI marketplace,
        # va comunque sempre su ke.amazon.listing (_upsert_listing sotto).
        if self._is_primary_marketplace():
            vals.update({
                'x_amazon_sku': data['sku'],
                'x_amazon_asin': data['asin'],
                'x_amazon_status': 'active' if 'BUYABLE' in data.get('status', '') else 'inactive',
                'x_amazon_fulfillment': data['fulfillment'],
                'x_amazon_product_type': data.get('product_type', ''),
                'x_amazon_price': data['price'],
                'x_amazon_qty': data['qty_available'],
                'x_amazon_last_sync': datetime.now(),
                'x_amazon_category': data.get('category', ''),
                'x_amazon_browse_node_id': data.get('browse_node_id', ''),
            })
            if data.get('brand'):
                vals['x_amazon_found_by_manufacturer'] = False
        img = _fetch_image_b64(data.get('image_url'))
        if img:
            vals['image_1920'] = img
        template = self.env['product.template'].create(vals)
        if data.get('spec_attrs'):
            self._set_product_attributes(template, data['spec_attrs'])
        self._init_product_warehouse_quant(
            template.product_variant_id, data['fulfillment'], data.get('qty_available') or 0,
        )
        self._upsert_listing(template, data)
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
        new_sku = data['sku']
        # Attributi fisici del prodotto: sempre aggiornati, indipendentemente
        # dal marketplace (non sono dati di listing, sono lo stesso prodotto).
        vals = {}
        if data.get('weight_kg') is not None:
            vals['weight'] = data['weight_kg']
        if data.get('length_cm') is not None:
            vals['product_length'] = data['length_cm']
        if data.get('width_cm') is not None:
            vals['product_width'] = data['width_cm']
        if data.get('height_cm') is not None:
            vals['product_height'] = data['height_cm']

        # Campi Amazon "legacy": solo se questa sync è sul marketplace primario
        # (fase 3, transizione — vedi _is_primary_marketplace). Il dato vero,
        # per QUALSIASI marketplace, va comunque su ke.amazon.listing sotto.
        if self._is_primary_marketplace():
            vals.update({
                'x_amazon_sku': new_sku,
                'x_amazon_asin': data['asin'],
                'x_amazon_status': 'active' if 'BUYABLE' in data.get('status', '') else 'inactive',
                'x_amazon_fulfillment': data['fulfillment'],
                'x_amazon_product_type': data.get('product_type', ''),
                'x_amazon_price': data['price'],
                'x_amazon_qty': data['qty_available'],
                'x_amazon_last_sync': datetime.now(),
            })
            if data.get('category'):
                vals['x_amazon_category'] = data['category']
            if data.get('browse_node_id'):
                vals['x_amazon_browse_node_id'] = data['browse_node_id']
            # Aggiorna default_code se era allineato al vecchio SKU Amazon o se non c'era SKU precedente
            # (non sovrascrivere codici impostati manualmente dall'utente)
            old_sku = product.x_amazon_sku or ''
            if new_sku != old_sku and (not old_sku or product.default_code == old_sku):
                vals['default_code'] = new_sku
            # Indicazione informativa (non usata per nessun match): tiene visibile
            # l'ultimo SKU sostituito, per capire a colpo d'occhio quali prodotti
            # sono stati riassegnati (es. rifornimento con '+') senza dover cercare
            # nel log sincronizzazioni.
            if new_sku != old_sku and old_sku:
                vals['x_amazon_sku_previous'] = old_sku

        if vals:
            product.write(vals)
        if not product.barcode and data.get('ean'):
            if self._barcode_free(data['ean'], exclude_product_ids=product.product_variant_ids.ids):
                try:
                    product.write({'barcode': data['ean']})
                except Exception as exc:
                    _log.warning('Impossibile assegnare barcode %s a %s: %s',
                                 data['ean'], data['sku'], exc)
        self._upsert_listing(product, data)

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
            self._init_product_warehouse_quant(
                variant, product.get('fulfillment', 'fbm'), product.get('qty_available') or 0,
            )
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
            'list_price': self._net_price(first_child['price']),
            'type': product_type_odoo,
            **_storable_vals(self.env),
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
        # Progresso scritto su log_rec periodicamente (non solo a fine ciclo):
        # con centinaia/migliaia di prodotti senza immagine, l'utente vedrebbe
        # altrimenti tutti i contatori a zero per lungo tempo, senza modo di
        # distinguere "sta lavorando" da "è bloccato".
        _PROGRESS_EVERY = 20

        log_rec.write({'products_total': total})
        self.env.cr.commit()

        for idx, product in enumerate(products, start=1):
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

            if idx % _PROGRESS_EVERY == 0:
                log_rec.write({
                    'products_updated': ok,
                    'products_skipped': skipped,
                    'products_error': errors,
                    'log_lines': '\n'.join(lines),
                })
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

    def _qty_in_warehouse(self, variant, warehouse):
        """
        Giacenza di una variante in un magazzino specifico, via query diretta a
        stock.quant (child_of ubicazione di vista del magazzino, usage='internal')
        — NON usare product.with_context(warehouse=...).qty_available: su questa
        versione di Odoo non scorpora correttamente per product.template (bug
        verificato più volte in questa sessione, stesso fix già applicato a
        x_amazon_stock_*/x_amazon_odoo_fbm_qty e nel modulo ke_product_dimensions).
        Senza un magazzino specificato ritorna il totale non filtrato.
        """
        if not warehouse:
            return variant.qty_available
        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', 'child_of', warehouse.view_location_id.id),
            ('location_id.usage', '=', 'internal'),
        ])
        return sum(quants.mapped('quantity'))

    # -----------------------------------------------------------------------
    # Funzione 3: Sync prezzi e quantità (schedulata)
    # -----------------------------------------------------------------------

    def sync_prices_and_quantities(self, log_rec):
        """
        Per ogni prodotto con ASIN e sync_price/sync_qty attivo, aggiorna prezzo
        e/o quantità su Amazon SUL MARKETPLACE di questa sync (self.client).
        Sul marketplace primario usa ancora i campi legacy x_amazon_sync_price/
        x_amazon_sync_qty (comportamento storico, invariato). Sui marketplace
        secondari (fase 3 multi-marketplace) usa invece ke.amazon.listing.sync_price/
        sync_qty — non scrive MAI sui campi legacy in quel caso, solo sulla riga
        listing del marketplace corrente.
        Non chiude il log (log_rec.state) qui: chi chiama itera sui marketplace
        attivi chiamando questo metodo una volta per marketplace e chiude il log
        una volta sola alla fine (vedi _thread_sync_price_qty / _cron_sync_prices_quantities
        in res_config_settings.py / amazon_sync_log.py) — i contatori si sommano
        tra le chiamate (stesso pattern di _do_reconcile).
        """
        if not self._is_primary_marketplace():
            return self._sync_prices_and_quantities_secondary(log_rec)

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

        # Quantità FBM: usa il magazzino FBM configurato su res.company, altrimenti totale globale
        fbm_warehouse = self.env.company.amazon_fbm_warehouse_id

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
                    amazon_price = product.get_amazon_sale_price()
                    self.client.patch_price(sku, product_type, amazon_price)
                    ops_done.append(f'prezzo={amazon_price:.2f}€')
                except Exception as exc:
                    error_msg += f'prezzo: {exc}; '
                    _log.error('Errore patch_price %s: %s', sku, exc)

            if product.x_amazon_sync_qty:
                if product.x_amazon_fulfillment == 'fbm':
                    try:
                        qty = int(self._qty_in_warehouse(product.product_variant_id, fbm_warehouse))
                        self.client.patch_quantity(sku, product_type, qty)
                        ops_done.append(f'qty={qty}')
                    except Exception as exc:
                        error_msg += f'qty: {exc}; '
                        _log.error('Errore patch_quantity %s: %s', sku, exc)
                else:
                    ops_done.append('qty=skip(FBA)')

            now = datetime.now()
            # Un successo parziale (es. prezzo fallito ma quantità riuscita) va
            # comunque segnalato come errore, non "OK": prima bastava un solo
            # elemento in ops_done per classificare il prodotto come OK e
            # azzerare x_amazon_sync_error, nascondendo silenziosamente l'errore.
            if error_msg:
                errors += 1
                msg = f'ERRORE {sku}: {error_msg}'
                if ops_done:
                    msg += f' (riusciti: {", ".join(ops_done)})'
                product.write({
                    'x_amazon_last_sync': now,
                    'x_amazon_sync_error': error_msg,
                    'x_amazon_status': 'error',
                })
            elif ops_done:
                ok += 1
                msg = f'OK {sku}: {", ".join(ops_done)}'
                product.write({'x_amazon_last_sync': now, 'x_amazon_sync_error': False})
            else:
                errors += 1
                msg = f'ERRORE {sku}: nessuna operazione eseguita'
                product.write({'x_amazon_last_sync': now, 'x_amazon_status': 'error'})

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
                        amazon_price = variant.get_amazon_sale_price()
                        self.client.patch_price(sku, product_type, amazon_price)
                        ops_done.append(f'prezzo={amazon_price:.2f}€')
                    except Exception as exc:
                        variant_error += f'prezzo: {exc}; '
                        _log.error('Errore patch_price variante %s: %s', sku, exc)

                if tmpl.x_amazon_sync_qty:
                    if tmpl.x_amazon_fulfillment == 'fbm':
                        try:
                            qty = int(self._qty_in_warehouse(variant, fbm_warehouse))
                            self.client.patch_quantity(sku, product_type, qty)
                            ops_done.append(f'qty={qty}')
                        except Exception as exc:
                            variant_error += f'qty: {exc}; '
                            _log.error('Errore patch_quantity variante %s: %s', sku, exc)
                    else:
                        ops_done.append('qty=skip(FBA)')

                # Stesso principio dei prodotti semplici: un successo parziale
                # (es. prezzo fallito ma quantità riuscita) resta un errore, non
                # va classificato come OK solo perché ops_done non è vuoto.
                if variant_error:
                    tmpl_errors += 1
                    errors += 1
                    tmpl_error_parts.append(f'{sku}: {variant_error}')
                    msg = f'ERRORE variante {sku}: {variant_error}'
                    if ops_done:
                        msg += f' (riusciti: {", ".join(ops_done)})'
                elif ops_done:
                    tmpl_ok += 1
                    ok += 1
                    msg = f'OK variante {sku}: {", ".join(ops_done)}'
                else:
                    tmpl_errors += 1
                    errors += 1
                    tmpl_error_parts.append(f'{sku}: nessuna operazione eseguita')
                    msg = f'ERRORE variante {sku}: nessuna operazione eseguita'

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

        existing_log = (log_rec.log_lines or '').rstrip('\n')
        log_rec.write({
            'products_total': log_rec.products_total + total,
            'products_updated': log_rec.products_updated + ok,
            'products_error': log_rec.products_error + errors,
            'log_lines': (existing_log + '\n' + '\n'.join(lines)).lstrip('\n') if lines else existing_log,
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return ok, errors

    def _sync_prices_and_quantities_secondary(self, log_rec):
        """
        Variante di sync_prices_and_quantities per un marketplace NON primario:
        i prodotti/varianti da sincronizzare e i relativi flag sync_price/sync_qty
        vengono da ke.amazon.listing (marketplace-scoped), non dai campi legacy
        x_amazon_* del prodotto (che appartengono al marketplace primario). I
        risultati si scrivono solo sulla riga listing, mai sui campi legacy.
        """
        marketplace = self._current_marketplace()
        if not marketplace:
            return 0, 0

        fbm_warehouse = self.env.company.amazon_fbm_warehouse_id
        listings = self.env['ke.amazon.listing'].sudo().search([
            ('marketplace_id', '=', marketplace.id),
            ('sku', '!=', False),
            '|', ('sync_price', '=', True), ('sync_qty', '=', True),
        ])
        ok = errors = 0
        lines = []

        for listing in listings:
            product = listing.product_id
            sku = listing.sku
            product_type = listing.product_type or 'PRODUCT'
            ops_done = []
            error_msg = ''

            if listing.sync_price:
                try:
                    amazon_price = product.get_amazon_sale_price()
                    self.client.patch_price(sku, product_type, amazon_price)
                    ops_done.append(f'prezzo={amazon_price:.2f}€')
                except Exception as exc:
                    error_msg += f'prezzo: {exc}; '
                    _log.error('Errore patch_price %s (%s): %s', sku, marketplace.code, exc)

            if listing.sync_qty:
                if listing.fulfillment == 'fbm':
                    try:
                        qty = int(self._qty_in_warehouse(product.product_variant_id, fbm_warehouse))
                        self.client.patch_quantity(sku, product_type, qty)
                        ops_done.append(f'qty={qty}')
                    except Exception as exc:
                        error_msg += f'qty: {exc}; '
                        _log.error('Errore patch_quantity %s (%s): %s', sku, marketplace.code, exc)
                else:
                    ops_done.append('qty=skip(FBA)')

            now = datetime.now()
            if error_msg:
                errors += 1
                msg = f'ERRORE [{marketplace.code}] {sku}: {error_msg}'
                if ops_done:
                    msg += f' (riusciti: {", ".join(ops_done)})'
                listing.write({'last_sync': now, 'sync_error': error_msg})
            elif ops_done:
                ok += 1
                msg = f'OK [{marketplace.code}] {sku}: {", ".join(ops_done)}'
                listing.write({'last_sync': now, 'sync_error': False})
            else:
                errors += 1
                msg = f'ERRORE [{marketplace.code}] {sku}: nessuna operazione eseguita'
                listing.write({'last_sync': now})

            lines.append(msg)
            _log.info(msg)

        existing_log = (log_rec.log_lines or '').rstrip('\n')
        log_rec.write({
            'products_total': log_rec.products_total + len(listings),
            'products_updated': log_rec.products_updated + ok,
            'products_error': log_rec.products_error + errors,
            'log_lines': (existing_log + '\n' + '\n'.join(lines)).lstrip('\n') if lines else existing_log,
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return ok, errors

    # -----------------------------------------------------------------------
    # Funzione 4: Riconciliazione store (manuale o schedulata)
    # -----------------------------------------------------------------------

    _CAT_MAP_BATCH_SIZE = 20  # max identifiers per chiamata searchCatalogItems

    def apply_category_mapping(self, log_rec, offset, chunk_size, max_id=None):
        """
        Arricchisce i prodotti con ASIN tramite Catalog API:
        - categoria Amazon (x_amazon_category)
        - dimensioni (lunghezza, larghezza, altezza, peso)
        - brand (x_amazon_brand) e descrizione/bullet point (description_sale),
          solo se attualmente vuoti — mai sovrascritti se già impostati a mano
        Chiama il Catalog API solo se manca uno qualsiasi di questi campi, in
        gruppi da _CAT_MAP_BATCH_SIZE ASIN per chiamata (searchCatalogItems,
        get_catalog_items_batch in sp_api_client.py) invece di un ASIN per
        chiamata — stesso rate limit di getCatalogItem, ma fino a 20 prodotti
        per richiesta: era questo il vero collo di bottiglia del backfill su
        cataloghi da migliaia di SKU, non il rate limit in sé (vedi
        _CAT_MAP_CHUNK_SIZE in amazon_sync_log.py per il perché è stato
        alzato in coppia con questa modifica).

        max_id (opzionale, id massimo di product.template congelato da
        action_start_apply_category_mapping): senza, 'total' viene ricalcolato
        da una search live ad ogni tick — se un import/riconciliazione crea
        migliaia di prodotti nuovi con ASIN mentre questo giro è a metà, il
        primo tick può vedere un 'total' molto più basso di quello reale e
        il giro si segna concluso non appena il suo offset lo raggiunge,
        anche se arrivano molti altri prodotti subito dopo (bug osservato in
        produzione: giro segnato 'done' dopo 700 prodotti su un catalogo di
        5.884, partito a ridosso della fine di un import completo che ne
        aveva appena creati 8.579). Con max_id il perimetro del giro resta
        fisso dall'avvio in poi, indipendentemente da cosa viene creato dopo.
        Ritorna (next_offset, total) o (None, total) se completato.
        """
        domain = [('x_amazon_asin', '!=', False)]
        if max_id:
            domain.append(('id', '<=', max_id))
        products = self.env['product.template'].search(domain, order='id asc')
        total = len(products)
        chunk = products[offset:offset + chunk_size]
        if not chunk:
            return None, total

        updated = skipped = errors = 0
        msg_by_product_id = {}
        to_fetch = []

        for product in chunk:
            ref = product.default_code or product.x_amazon_asin
            needs_catalog = (
                not product.x_amazon_category
                or not product.product_length
                or not product.x_amazon_product_type
                or not product.x_amazon_browse_node_id
                or not product.x_amazon_brand
                or not product.description_sale
            )
            if needs_catalog:
                to_fetch.append(product)
            else:
                skipped += 1
                msg_by_product_id[product.id] = f'SKIP {ref}: dati già presenti'

        for i in range(0, len(to_fetch), self._CAT_MAP_BATCH_SIZE):
            batch = to_fetch[i:i + self._CAT_MAP_BATCH_SIZE]
            try:
                catalog_by_asin = self.client.get_catalog_items_batch([p.x_amazon_asin for p in batch])
            except Exception as exc:
                # La chiamata batch è fallita per TUTTO il gruppo (throttling
                # dopo i retry, errore di rete, ecc.) — non è "Amazon non ha
                # questi dati", è "non siamo riusciti a chiederglieli". Segnare
                # ERRORE (non SKIP) fa sì che needs_catalog resti vero per
                # questi prodotti e vengano ritentati al prossimo giro,
                # invece di essere scambiati per genuinamente privi di dati.
                errors += len(batch)
                for product in batch:
                    ref = product.default_code or product.x_amazon_asin
                    msg = f'ERRORE {ref}: chiamata batch fallita ({exc}), da ritentare'
                    _log.error(msg)
                    msg_by_product_id[product.id] = msg
                continue

            for product in batch:
                asin = product.x_amazon_asin
                ref = product.default_code or asin
                try:
                    catalog = catalog_by_asin.get(asin, {})

                    summaries = catalog.get('summaries', [{}])
                    s0 = summaries[0] if summaries else {}
                    browse_cls = s0.get('browseClassification', {})
                    category = browse_cls.get('displayName', '')
                    browse_node_id = browse_cls.get('classificationId', '')

                    product_types = catalog.get('productTypes') or [{}]
                    product_type = product_types[0].get('productType', '')

                    # Fallback 'item'->'package' — vedi stesso commento/fix in
                    # _update_product qui sopra, stesso bug, stessa causa.
                    dims = catalog.get('dimensions', [{}])
                    dim_block = dims[0] if dims else {}
                    item_d = dim_block.get('item') or dim_block.get('package') or {}

                    # Brand/descrizione/bullet point: stessi campi che
                    # _merge_catalog estrae per il percorso a piccolo
                    # catalogo (full_import/full_import_chunk, mai chiamato
                    # per cataloghi grandi come questo, vedi TASK.md) — 'attributes'
                    # è già richiesto in includedData dalla chiamata batch,
                    # nessuna chiamata Amazon aggiuntiva, solo dati già
                    # scaricati e finora scartati.
                    cat_attrs = catalog.get('attributes', {})
                    brand = s0.get('brand', '') or _cat_val(cat_attrs, 'brand')
                    description = _cat_val(cat_attrs, 'product_description')
                    bullets = [str(v.get('value', '')) for v in cat_attrs.get('bullet_point', []) if v.get('value')]

                    vals = {}
                    if category:
                        vals['x_amazon_category'] = category
                    if browse_node_id:
                        vals['x_amazon_browse_node_id'] = browse_node_id
                    if product_type:
                        vals['x_amazon_product_type'] = product_type
                    weight_kg = _convert_weight(item_d.get('weight', {}))
                    length_cm = _convert_length(item_d.get('length', {}))
                    width_cm = _convert_length(item_d.get('width', {}))
                    height_cm = _convert_length(item_d.get('height', {}))
                    if weight_kg is not None:
                        vals['weight'] = weight_kg
                    if length_cm is not None:
                        vals['product_length'] = length_cm
                    if width_cm is not None:
                        vals['product_width'] = width_cm
                    if height_cm is not None:
                        vals['product_height'] = height_cm
                    # Solo se vuoti: non sovrascrivere una descrizione/brand
                    # impostati a mano in Odoo con quanto trovato su Amazon.
                    if brand and not product.x_amazon_brand:
                        vals['x_amazon_brand'] = brand
                    if (description or bullets) and not product.description_sale:
                        formatted = self._format_description({'description': description, 'bullet_points': bullets})
                        if formatted:
                            vals['description_sale'] = formatted

                    if vals:
                        product.write(vals)
                        updated += 1
                        msg = f'OK {ref}: type="{product_type}" cat="{category}" L={length_cm} W={width_cm} H={height_cm}'
                        if 'x_amazon_brand' in vals or 'description_sale' in vals:
                            msg += f' brand={"sì" if "x_amazon_brand" in vals else "-"} desc={"sì" if "description_sale" in vals else "-"}'
                    else:
                        skipped += 1
                        msg = f'SKIP {ref}: nessun dato disponibile su Amazon'

                except Exception as exc:
                    errors += 1
                    msg = f'ERRORE {ref}: {exc}'
                    _log.error(msg, exc_info=True)
                msg_by_product_id[product.id] = msg

        lines = [msg_by_product_id[p.id] for p in chunk]
        new_lines = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_total': total,
            'products_updated': log_rec.products_updated + updated,
            'products_skipped': log_rec.products_skipped + skipped,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_lines,
        })
        self.env.cr.commit()

        next_offset = offset + len(chunk)
        return (next_offset if next_offset < total else None), total

    # ------------------------------------------------------------------
    # Magazzini FBA / FBM
    # ------------------------------------------------------------------

    def _get_warehouse_location(self, company_field):
        """Ritorna la stock.location interna del magazzino configurato su res.company, o None."""
        warehouse = getattr(self.env.company, company_field, False)
        return warehouse.lot_stock_id if warehouse else None

    def _get_fba_warehouse_location(self, marketplace=None):
        """
        Ritorna la stock.location FBA da usare: quella del magazzino dedicato del
        marketplace se impostato (inventario non pooled, es. UK/US), altrimenti
        quella del magazzino FBA condiviso Pan-EU configurato in azienda — stesso
        comportamento di _get_warehouse_location('amazon_fba_warehouse_id') di
        sempre quando nessun marketplace ha un magazzino dedicato (nessuna
        modifica per il caso EU pooled oggi in uso).
        'marketplace' esplicito per i chiamanti che non passano da un client
        SP-API scoped (es. i wizard di spedizione/rientro, client=None); se
        omesso viene risolto da self._current_marketplace() come al solito.
        """
        marketplace = marketplace if marketplace is not None else self._current_marketplace()
        warehouse = marketplace.get_fba_warehouse() if marketplace else self.env.company.amazon_fba_warehouse_id
        return warehouse.lot_stock_id if warehouse else None

    def _init_product_warehouse_quant(self, variant, fulfillment, qty=0):
        """
        Alla creazione di un prodotto (o di una variante), crea un quant nel
        magazzino Amazon configurato (FBA o FBM, in base a 'fulfillment') con la
        quantità dichiarata nel report Amazon ('qty', 0 se non disponibile) —
        così è subito associato al magazzino corretto con una giacenza di
        partenza reale, invece di comparire senza ubicazione fino al primo
        conteggio manuale (che di norma finisce nel magazzino di default).
        Solo alla creazione: i sync successivi non toccano più questo valore
        (stessa logica di list_price) — per FBA verrà comunque corretto al
        valore reale dal prossimo giro di sync_fba_quantities.
        Non fa nulla se il magazzino corrispondente non è ancora configurato.
        """
        field = 'amazon_fba_warehouse_id' if fulfillment == 'fba' else 'amazon_fbm_warehouse_id'
        location = self._get_warehouse_location(field)
        if not location or not variant:
            return
        existing = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', '=', location.id),
        ], limit=1)
        if existing:
            return
        self.env['stock.quant'].sudo().create({
            'product_id': variant.id,
            'location_id': location.id,
            'quantity': qty or 0,
        })

    def _set_product_stock(self, product_variant, location, new_qty):
        """Imposta la giacenza assoluta per product_variant in location tramite stock.quant."""
        quant = self.env['stock.quant'].search([
            ('product_id', '=', product_variant.id),
            ('location_id', '=', location.id),
        ], limit=1)
        current_qty = quant.quantity if quant else 0.0
        delta = new_qty - current_qty
        if abs(delta) < 0.001:
            return
        self.env['stock.quant'].sudo()._update_available_quantity(
            product_variant, location, delta,
        )

    def seed_fbm_stock_from_amazon_qty(self, log_rec):
        """
        Operazione di recupero: imposta la giacenza dei prodotti FBM nel magazzino
        FBM configurato usando x_amazon_qty (la quantità dichiarata su Amazon, letta
        dal report listing e già salvata a ogni sync). Utile quando la giacenza reale
        non è mai stata registrata nel magazzino corretto in Odoo, e su questi
        prodotti non è mai girata la sync automatica quantità verso Amazon
        (altrimenti x_amazon_qty rifletterebbe solo ciò che Odoo ha già spinto,
        non il dato originale del venditore).
        Salta (senza toccarli) i prodotti che hanno già giacenza reale in un altro
        magazzino (es. Generale) — evita di duplicare gli stessi pezzi fisici in due
        posti se nel frattempo un sync li ha abbinati e classificati come FBM. Questo
        la rende ragionevolmente sicura da rilanciare più volte, ma resta pensata
        come operazione di recupero occasionale, non una sync ricorrente.
        """
        fbm_location = self._get_warehouse_location('amazon_fbm_warehouse_id')
        if not fbm_location:
            msg = 'Magazzino FBM non configurato (Impostazioni → Amazon → Magazzini).'
            log_rec.write({'state': 'error', 'error_summary': msg, 'end_date': datetime.now()})
            self.env.cr.commit()
            return 0, 0, 0

        products = self.env['product.template'].search([
            ('x_amazon_fulfillment', '=', 'fbm'),
        ])
        total = len(products)
        updated = skipped = errors = 0
        lines = []

        for product in products:
            variant = product.product_variant_id
            if not variant:
                skipped += 1
                continue
            try:
                quant = self.env['stock.quant'].search([
                    ('product_id', '=', variant.id),
                    ('location_id', '=', fbm_location.id),
                ], limit=1)
                current_qty = quant.quantity if quant else 0.0

                # Sicurezza anti-doppio-conteggio: se il prodotto ha già giacenza
                # reale altrove (es. Magazzino Generale), non toccarlo — vorrebbe
                # dire che è già stato gestito, seedare anche il FBM duplicherebbe
                # gli stessi pezzi fisici in due magazzini.
                qty_elsewhere = variant.qty_available - current_qty
                if qty_elsewhere > 0.001:
                    skipped += 1
                    lines.append(
                        f'SALTATO {product.x_amazon_sku or product.default_code}: '
                        f'ha già {qty_elsewhere:g} di giacenza reale in un altro magazzino '
                        f'(possibile doppio conteggio, non toccato)'
                    )
                    continue

                target_qty = float(product.x_amazon_qty or 0)
                if abs(target_qty - current_qty) < 0.001:
                    skipped += 1
                    continue
                self._set_product_stock(variant, fbm_location, target_qty)
                updated += 1
                lines.append(
                    f'AGGIORNATO {product.x_amazon_sku or product.default_code}: '
                    f'{current_qty:g} → {target_qty:g}'
                )
            except Exception as exc:
                errors += 1
                lines.append(f'ERRORE {product.x_amazon_sku or product.default_code}: {exc}')
                _log.error('Errore seed FBM stock %s: %s', product.display_name, exc, exc_info=True)

            if updated % 50 == 0 and updated:
                self.env.cr.commit()

        log_rec.write({
            'state': 'error' if errors and not updated else 'done',
            'products_total': total,
            'products_updated': updated,
            'products_skipped': skipped,
            'products_error': errors,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return updated, skipped, errors

    _TRANSIT_LOCATION_PARAM = 'ke_amazon_sync.transit_location_id'

    def _get_or_create_transit_location(self, fba_location):
        """
        Ritorna la stock.location 'In transito verso Amazon' (creandola se non
        esiste ancora), figlia del magazzino FBA configurato. Ospita la merce
        spedita fisicamente ad Amazon ma non ancora confermata come ricevuta.
        """
        params = self.env['ir.config_parameter'].sudo()
        loc_id = int(params.get_param(self._TRANSIT_LOCATION_PARAM, 0) or 0)
        Location = self.env['stock.location']
        location = Location.browse(loc_id) if loc_id else Location.browse()
        if location.exists():
            return location
        location = Location.create({
            'name': 'In transito verso Amazon',
            'usage': 'internal',
            'location_id': fba_location.location_id.id,
        })
        params.set_param(self._TRANSIT_LOCATION_PARAM, location.id)
        return location

    def _create_and_validate_internal_transfer(self, picking_type_warehouse, source_location,
                                                dest_location, lines, origin, auto_validate=True):
        """
        Crea un trasferimento interno source_location → dest_location per 'lines'
        (iterabile di (product_variant, quantity)), confermato e riservato
        (action_confirm/action_assign). Se auto_validate=True (default, comportamento
        storico) lo valida anche subito (quantità = prevista, stato 'done').
        Se auto_validate=False il picking resta 'assigned': usato dalla spedizione
        verso FBA (vedi ship_to_fba), dove la validazione va rimandata a dopo la
        scansione/etichettatura di ogni unità (wizard ke.amazon.fba.prep.wizard).
        picking_type_warehouse: magazzino di cui usare il tipo operazione 'internal'
        (determina solo la sequenza/numerazione del picking, le location dei
        movimenti sono quelle passate esplicitamente).
        Ritorna il picking creato.
        """
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('warehouse_id', '=', picking_type_warehouse.id),
        ], limit=1)
        if not picking_type:
            raise UserError(
                f'Nessun tipo di trasferimento interno trovato per {picking_type_warehouse.name}.'
            )

        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'origin': origin,
        })
        # Niente 'name': rimosso da stock.move in Odoo 19 (rec_name è ora
        # 'reference', un campo calcolato, non scrivibile) — passarlo fa
        # fallire la create() con "Invalid field 'name' in 'stock.move'".
        # Trovato con un test reale su Odoo 19 in locale, non solo dedotto.
        move_vals = [{
            'product_id': product_variant.id,
            'product_uom_qty': qty,
            'product_uom': product_variant.uom_id.id,
            'picking_id': picking.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
        } for product_variant, qty in lines]
        self.env['stock.move'].create(move_vals)
        picking.action_confirm()
        picking.action_assign()
        if auto_validate:
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
                # Odoo 19: _action_done() scarta i movimenti con picked=False a
                # prescindere dalla quantità impostata — senza questo il picking
                # resta bloccato in stato 'assigned' invece di 'done'. Trovato con
                # un test reale (ship_to_fba) su Odoo 19 in locale.
                move.picked = True
            picking.with_context(skip_immediate=True, skip_backorder=True)._action_done()
        return picking

    def ship_to_fba(self, source_warehouse, lines, marketplace=None):
        """
        Registra una spedizione di rifornimento verso Amazon: trasferimento interno
        dal magazzino sorgente alla location 'In transito verso Amazon'. La merce
        arriva sul magazzino FBA "reale" solo quando sync_fba_quantities rileva che
        Amazon ne ha confermato la ricezione (vedi _consume_transit_stock).
        lines: iterabile di (product_variant, quantity).
        Il picking viene creato confermato e riservato ma NON validato subito
        (auto_validate=False): resta 'assigned' finché l'operatore non ha
        scansionato/etichettato ogni unità (wizard ke.amazon.fba.prep.wizard,
        vedi progetto-odoo-amazon-fba-etichettatura.md) — è quel wizard a
        chiamare infine _action_done(). Comportamento diverso dagli altri
        trasferimenti interni del modulo (receive/allocate/move-to-general),
        che restano validati subito in un solo passaggio.
        'marketplace' (opzionale): se dato, spedisce verso IL SUO magazzino FBA
        dedicato (inventario non pooled, es. UK/US); altrimenti (default, caso
        EU pooled) verso il magazzino FBA condiviso configurato in azienda —
        stesso comportamento di sempre quando non specificato.
        Ritorna il picking creato (stato 'assigned', non ancora 'done').
        """
        fba_location = self._get_fba_warehouse_location(marketplace)
        if not fba_location:
            raise UserError('Magazzino FBA non configurato (Impostazioni → Amazon → Magazzini).')
        transit_location = self._get_or_create_transit_location(fba_location)
        return self._create_and_validate_internal_transfer(
            source_warehouse, source_warehouse.lot_stock_id, transit_location,
            lines, 'Rifornimento Amazon FBA', auto_validate=False,
        )

    def receive_from_fba(self, dest_warehouse, lines, marketplace=None):
        """
        Registra la ricezione fisica di un rientro scorte da Amazon (removal order):
        trasferimento interno dal magazzino FBA configurato al magazzino di
        destinazione, da usare quando il pacco di reso arriva fisicamente.
        Lato Amazon la giacenza è già stata tolta dal fulfillable (e quindi dal
        magazzino FBA Odoo) al prossimo giro di sync_fba_quantities.
        lines: iterabile di (product_variant, quantity).
        'marketplace' (opzionale): stesso significato di ship_to_fba — magazzino
        FBA dedicato se dato, altrimenti il pool EU condiviso (default).
        Ritorna il picking creato e validato.
        """
        fba_location = self._get_fba_warehouse_location(marketplace)
        if not fba_location:
            raise UserError('Magazzino FBA non configurato (Impostazioni → Amazon → Magazzini).')
        return self._create_and_validate_internal_transfer(
            dest_warehouse, fba_location, dest_warehouse.lot_stock_id,
            lines, 'Rientro scorte da Amazon FBA',
        )

    def allocate_fbm_stock(self, source_warehouse, lines):
        """
        Alloca giacenza dal magazzino generale/fisico verso il magazzino 'FBM Amazon'
        (la quota riservata/esposta ad Amazon FBM, quella che patch_quantity spinge
        su Amazon). Trasferimento interno source_warehouse → magazzino FBM.
        lines: iterabile di (product_variant, quantity).
        Ritorna il picking creato e validato.
        """
        fbm_location = self._get_warehouse_location('amazon_fbm_warehouse_id')
        if not fbm_location:
            raise UserError('Magazzino FBM non configurato (Impostazioni → Amazon → Magazzini).')
        return self._create_and_validate_internal_transfer(
            source_warehouse, source_warehouse.lot_stock_id, fbm_location,
            lines, 'Allocazione giacenza Amazon FBM',
        )

    def move_all_stock_to_general(self, source_location):
        """
        Sposta in blocco TUTTA la giacenza (ogni stock.quant con quantità positiva)
        trovata in source_location verso il Magazzino Generale configurato — pensato
        per correggere stock finito in una location sbagliata (es. rettifica
        iniziale senza specificare il magazzino), su centinaia/migliaia di prodotti
        in un solo trasferimento invece che uno alla volta.
        Ritorna (picking_o_None, n_prodotti_spostati).
        """
        dest_warehouse = self.env.company.amazon_default_warehouse_id
        if not dest_warehouse:
            raise UserError('Magazzino Generale non configurato (Impostazioni → Amazon → Magazzini).')

        quants = self.env['stock.quant'].search([
            ('location_id', '=', source_location.id),
            ('quantity', '>', 0),
        ])
        if not quants:
            return None, 0

        lines = [(q.product_id, q.quantity) for q in quants]
        picking = self._create_and_validate_internal_transfer(
            dest_warehouse, source_location, dest_warehouse.lot_stock_id,
            lines, 'Correzione ubicazione: spostamento in blocco verso Magazzino Generale',
        )
        return picking, len(lines)

    def _consume_transit_stock(self, product_variant, transit_location, fba_location, qty_received):
        """
        Amazon ha confermato di aver ricevuto (fino a) qty_received unità: sposta la
        stessa quantità dalla location 'In transito' al magazzino FBA reale, fino
        all'esaurimento di quanto disponibile in transito per quel prodotto.
        Ritorna la quantità effettivamente spostata (0 se non c'era nulla in transito).
        """
        quant = self.env['stock.quant'].search([
            ('product_id', '=', product_variant.id),
            ('location_id', '=', transit_location.id),
        ], limit=1)
        available = quant.quantity if quant else 0.0
        qty_to_move = min(available, qty_received)
        if qty_to_move <= 0:
            return 0.0
        move = self.env['stock.move'].create({
            # Niente 'name': vedi commento in _create_and_validate_internal_transfer.
            'product_id': product_variant.id,
            'product_uom_qty': qty_to_move,
            'product_uom': product_variant.uom_id.id,
            'location_id': transit_location.id,
            'location_dest_id': fba_location.id,
        })
        move._action_confirm()
        move.quantity = qty_to_move
        move.picked = True  # vedi commento in _create_and_validate_internal_transfer
        move._action_done()
        return qty_to_move

    def sync_fba_quantities_page(self, log_rec, page_token=None):
        """
        Scarica e processa UNA pagina di inventario FBA da Amazon, aggiornando i
        quant Odoo nel magazzino FBA configurato. Pensata per essere richiamata
        a ripetizione (una pagina per invocazione del cron) invece di scaricare
        tutte le pagine in un colpo solo: con centinaia di pagine possibili, un
        singolo retry/backoff per throttling potrebbe far superare il limite di
        tempo reale del cron se si tenta di fare tutto in un'unica chiamata.
        Se la giacenza riportata da Amazon per uno SKU è aumentata rispetto
        all'ultima sync, la differenza viene "consumata" dalla location
        'In transito verso Amazon' (rifornimenti registrati con ship_to_fba).

        Fase 3 multi-marketplace (inventario FBA Pan-Europeo, confermato
        dall'utente): "pooled" non è un caso speciale per IT/DE/ES/FR, è
        determinato da ke.amazon.marketplace.is_fba_pooled() — vero se questo
        marketplace condivide il magazzino FBA effettivo con almeno un altro
        marketplace attivo (vedi quel metodo per i dettagli, incluso come
        un marketplace entra/esce dal pool senza toccare codice). Per un
        marketplace pooled, il magazzino FBA in Odoo è condiviso — qui non ha
        senso gating primario/secondario sul campo legacy x_amazon_qty:
        rappresenta lo stesso stock fisico, riconfermato (redundante ma
        innocuo, idempotente) da ognuno dei marketplace pooled che il
        chiamante itera (vedi _thread_sync_fba_quantities).
        Per un marketplace NON pooled (inventario davvero separato, es. UK/US
        fuori dal pool EU), la quantità precedente/nuova si legge/scrive su
        ke.amazon.listing.qty (già scoped per marketplace) invece che sul
        campo condiviso x_amazon_qty, per non farla interferire con la
        contabilità del pool su cui questo marketplace non è più.
        Ritorna il next_token per la pagina successiva, o None se era l'ultima.
        """
        marketplace = self._current_marketplace()
        fba_location = self._get_fba_warehouse_location(marketplace)
        if not fba_location:
            raise UserError('Magazzino FBA non configurato (Impostazioni → Amazon → Magazzini).')
        transit_location = self._get_or_create_transit_location(fba_location)
        mp_tag = f'[{marketplace.code}] ' if marketplace else ''
        dedicated = bool(marketplace) and not marketplace.is_fba_pooled()

        items, new_page_token = self.client.fetch_inventory_page(page_token)

        updated = errors = skipped = 0
        lines = [f'{mp_tag}Pagina ricevuta: {len(items)} SKU.']

        for entry in items:
            sku = entry['sku']
            fba_qty = float(entry['qty'] or 0)

            product_tmpl = self.env['product.template'].search(
                [('x_amazon_sku', '=', sku)], limit=1,
            )
            matched_on = 'template'
            if product_tmpl:
                product_variant = product_tmpl.product_variant_id
            else:
                variant = self.env['product.product'].search(
                    [('x_amazon_sku', '=', sku)], limit=1,
                )
                if not variant:
                    skipped += 1
                    lines.append(f'{mp_tag}SALTATO {sku}: nessun prodotto Odoo con questo x_amazon_sku')
                    continue
                matched_on = 'variante'
                product_variant = variant
                product_tmpl = variant.product_tmpl_id

            try:
                listing = None
                if dedicated:
                    # Inventario non pooled: previous/new qty su ke.amazon.listing,
                    # mai su x_amazon_qty (quel campo resta il valore del pool EU).
                    listing = self.env['ke.amazon.listing'].sudo().search([
                        ('product_id', '=', product_tmpl.id), ('marketplace_id', '=', marketplace.id),
                    ], limit=1)
                    previous_qty = listing.qty if listing else 0
                else:
                    previous_qty = product_tmpl.x_amazon_qty or 0
                increase = fba_qty - previous_qty
                # Prima si sposta (se c'è un aumento) dal transito al magazzino FBA
                # reale — un movimento fisico che incrementa già il quant FBA — e
                # SOLO DOPO si riconcilia al valore assoluto dichiarato da Amazon.
                # Nell'ordine inverso (riconciliazione assoluta, poi movimento in
                # aggiunta) il quant FBA finiva doppiamente incrementato: prima
                # impostato a fba_qty, poi il movimento del transito ci sommava
                # sopra ancora la quantità spostata.
                moved = 0.0
                if increase > 0:
                    moved = self._consume_transit_stock(
                        product_variant, transit_location, fba_location, increase,
                    )
                self._set_product_stock(product_variant, fba_location, fba_qty)
                if dedicated:
                    if listing:
                        listing.write({'qty': int(fba_qty), 'last_sync': datetime.now()})
                else:
                    product_tmpl.write({'x_amazon_qty': int(fba_qty)})
                    # Riga listing del marketplace corrente, se esiste già (creata
                    # da full_import/reconcile): aggiornata per visibilità, mai
                    # creata qui.
                    if marketplace:
                        listing = self.env['ke.amazon.listing'].sudo().search([
                            ('product_id', '=', product_tmpl.id), ('marketplace_id', '=', marketplace.id),
                        ], limit=1)
                        if listing:
                            listing.write({'qty': int(fba_qty), 'last_sync': datetime.now()})
                updated += 1
                msg = (
                    f'{mp_tag}OK {sku} ({matched_on} #{product_tmpl.id}): '
                    f'FBA qty {previous_qty:g} → {fba_qty:g}'
                )
                if moved > 0:
                    msg += f' — {moved:g} spostate da transito verso FBA'
                lines.append(msg)
            except Exception as exc:
                errors += 1
                lines.append(f'{mp_tag}ERRORE {sku}: {exc}')
                _log.error('sync_fba: errore aggiornamento %s: %s', sku, exc)

        if not new_page_token:
            lines.append(
                f'{mp_tag}Riepilogo: {log_rec.products_updated + updated} aggiornati, '
                f'{log_rec.products_skipped + skipped} saltati (nessun match), '
                f'{log_rec.products_error + errors} errori, '
                f'{log_rec.products_total + len(items)} SKU totali da Amazon.'
            )
        new_log = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_total': log_rec.products_total + len(items),
            'products_updated': log_rec.products_updated + updated,
            'products_skipped': log_rec.products_skipped + skipped,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_log,
        })
        self.env.cr.commit()
        return new_page_token

    # ------------------------------------------------------------------
    # Import ordini Amazon → sale.order
    # ------------------------------------------------------------------

    _ORDER_STATUSES = [
        'Pending', 'Unshipped', 'PartiallyShipped', 'Shipped', 'InvoiceUnconfirmed',
    ]
    # Ordini committati a blocco durante l'elaborazione: da quando le righe ordine
    # vengono lette dal report bulk pre-scaricato (items_by_order_id, vedi
    # _thread_import_orders) invece che da una chiamata get_order_items per
    # ordine, questo non serve più a stare sotto un limite di tempo per tick di
    # cron (non esiste più un tick) — resta solo per non perdere lavoro già
    # fatto se il thread venisse interrotto a metà (stesso principio di
    # _ROW_COMMIT_BATCH in amazon_sync_log.py, valore più basso perché qui ogni
    # riga costa di più: partner, ordine, righe, eventuale conferma FBA).
    _ORDERS_CHUNK_SIZE = 15

    def fetch_orders_page(self, created_after, next_token=None):
        """
        Scarica una pagina di ordini Amazon (fino a 100, secondo l'Orders API).
        Ritorna (orders_list, next_token_o_None).
        """
        return self.client.get_orders(
            created_after=created_after,
            next_token=next_token,
            order_statuses=self._ORDER_STATUSES,
        )

    def import_orders_chunk(self, log_rec, orders, offset=0, chunk_size=None, items_by_order_id=None):
        """
        Crea/aggiorna i sale.order per un sotto-blocco di 'orders' (già scaricati
        con fetch_orders_page), a partire da 'offset'.
        Ritorna il nuovo offset per il prossimo chunk, o None se il blocco è esaurito.

        'items_by_order_id', se dato, è il dict {amazon_order_id: [righe report]}
        pre-scaricato una volta sola dal report bulk articoli-ordine (vedi
        _thread_import_orders) — passato giù a _create_amazon_sale_order/
        _fetch_and_create_order_lines per evitare una chiamata get_order_items
        per ordine (vedi progetto-sync-ordini-report.md).
        """
        chunk_size = chunk_size or self._ORDERS_CHUNK_SIZE
        chunk = orders[offset:offset + chunk_size]
        if not chunk:
            return None

        created = updated = skipped = errors = 0
        lines = []

        for order_data in chunk:
            amazon_id = order_data.get('AmazonOrderId', '')
            try:
                existing = self.env['sale.order'].search(
                    [('x_amazon_order_id', '=', amazon_id)], limit=1,
                )
                if existing:
                    new_status = order_data.get('OrderStatus', '')
                    write_vals = {'x_amazon_order_status': new_status}
                    # Corregge la data ordine su record già creati con la data di
                    # importazione invece che con quella reale di acquisto Amazon
                    # (es. import fatti prima del fix del parsing).
                    purchase_date = self._parse_amazon_datetime(
                        order_data.get('PurchaseDate') or order_data.get('LastUpdateDate')
                    )
                    if purchase_date:
                        write_vals['x_amazon_purchase_date'] = purchase_date
                        write_vals['date_order'] = purchase_date
                    existing.write(write_vals)
                    if not existing.order_line:
                        # Ordine creato ma senza righe — probabile: una sync
                        # precedente ha fallito la richiesta righe DOPO aver già
                        # creato l'ordine (un bug ora corretto con un savepoint
                        # in _create_amazon_sale_order, ma un ordine "fantasma"
                        # creato prima del fix resterebbe altrimenti orfano per
                        # sempre, dato che questo ramo prima aggiornava solo
                        # stato/data). Ritenta il recupero righe.
                        self._fetch_and_create_order_lines(existing, amazon_id, items_by_order_id)
                    if (existing.x_amazon_fulfillment_channel == 'AFN'
                            and new_status == 'Shipped'
                            and existing.state == 'draft'):
                        self._confirm_and_validate_fba_order(existing)
                    updated += 1
                    lines.append(f'AGGIORNATO: {amazon_id}')
                else:
                    order = self._create_amazon_sale_order(order_data, items_by_order_id)
                    created += 1
                    lines.append(f'CREATO: {amazon_id} → {order.name}')
            except Exception as exc:
                errors += 1
                lines.append(f'ERRORE {amazon_id}: {exc}')
                _log.error('Errore import ordine %s: %s', amazon_id, exc, exc_info=True)

        new_lines = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_updated': log_rec.products_updated + updated,
            'products_created': (getattr(log_rec, 'products_created', 0) or 0) + created,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_lines,
        })
        self.env.cr.commit()

        new_offset = offset + len(chunk)
        return new_offset if new_offset < len(orders) else None

    @staticmethod
    def _parse_amazon_datetime(date_str):
        """
        Converte un timestamp ISO-8601 Amazon (es. '2026-06-23T01:06:32Z' o con
        offset/frazioni di secondo) nella stringa datetime naive-UTC richiesta da Odoo.
        Ritorna False se il valore è assente o non parsabile.
        """
        if not date_str:
            return False
        try:
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except ValueError:
            return False
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def _create_amazon_sale_order(self, order_data, items_by_order_id=None):
        amazon_id = order_data.get('AmazonOrderId', '')
        partner = self._find_or_create_amazon_partner(order_data)

        purchase_date = self._parse_amazon_datetime(
            order_data.get('PurchaseDate') or order_data.get('LastUpdateDate')
        )

        # Assegna il magazzino corretto in base al canale di fulfillment
        fulfillment_channel = order_data.get('FulfillmentChannel', '')
        company = self.env.company
        marketplace = self._current_marketplace()
        if fulfillment_channel == 'AFN':
            # Magazzino dedicato del marketplace se impostato (inventario FBA
            # non pooled, es. UK/US), altrimenti il pool EU condiviso — stesso
            # fallback usato da sync_fba_quantities_page.
            fba_warehouse = marketplace.get_fba_warehouse() if marketplace else company.amazon_fba_warehouse_id
            wh_id = fba_warehouse.id
        else:
            wh_id = company.amazon_fbm_warehouse_id.id

        order_vals = {
            'partner_id': partner.id,
            'client_order_ref': amazon_id,
            'x_amazon_order_id': amazon_id,
            'x_amazon_order_status': order_data.get('OrderStatus', ''),
            'x_amazon_fulfillment_channel': fulfillment_channel,
            'x_amazon_purchase_date': purchase_date,
            'x_amazon_sales_channel': order_data.get('SalesChannel', ''),
            'x_amazon_marketplace_id': marketplace.id,
        }
        if wh_id:
            order_vals['warehouse_id'] = wh_id
        if purchase_date:
            order_vals['date_order'] = purchase_date

        # Savepoint: se il recupero righe (chiamata Amazon separata, può fallire
        # per errori di rete/throttling) va in errore DOPO che l'ordine è già
        # stato creato, senza questo la create() resterebbe comunque nella
        # transazione e verrebbe commitata a fine chunk (import_orders_chunk fa
        # un commit unico, non per ordine) — un ordine "fantasma" senza righe,
        # che il ramo "ordine già esistente" nei run successivi aggiornava solo
        # stato/data senza mai più ritentare le righe. Con il savepoint, un
        # errore qui annulla la create() e il prossimo import lo ritratta come
        # nuovo (self-healing anche senza il fallback in import_orders_chunk).
        with self.env.cr.savepoint():
            order = self.env['sale.order'].create(order_vals)
            self._fetch_and_create_order_lines(order, amazon_id, items_by_order_id)
            if fulfillment_channel == 'AFN' and order_data.get('OrderStatus') == 'Shipped':
                self._confirm_and_validate_fba_order(order)

        return order

    def _fetch_and_create_order_lines(self, order, amazon_id, items_by_order_id=None):
        """
        Crea le righe ordine su 'order' (già esistente). Se 'items_by_order_id'
        (dict amazon_order_id -> righe del report bulk articoli-ordine,
        pre-scaricato una volta sola per l'intero giro di sync — vedi
        _thread_import_orders) contiene questo ordine, le righe vengono lette
        da lì: zero chiamate Amazon, elimina il pavimento di 2s/ordine di
        get_order_items (vedi progetto-sync-ordini-report.md). Se l'ordine non
        è nel dict (report generato poco prima che questo ordine esistesse —
        raro, il report copre "fino a poco fa") o se items_by_order_id non è
        stato passato affatto, ricade sulla chiamata diretta get_order_items,
        più lenta ma sempre corretta — mai un ordine senza righe per questo.
        """
        if items_by_order_id is not None and amazon_id in items_by_order_id:
            for row in items_by_order_id[amazon_id]:
                self._create_amazon_order_line(order, self._order_report_row_to_item(row))
            return
        items = self.client.get_order_items(amazon_id)
        for item in items:
            self._create_amazon_order_line(order, item)

    @staticmethod
    def _order_report_row_to_item(row):
        """
        Converte una riga del report GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL
        (colonne piatte 'sku'/'item-price'/...) nel formato annidato della
        risposta get_order_items ('SellerSKU'/'ItemPrice.Amount'/...) già
        atteso da _create_amazon_order_line — così quel metodo non deve sapere
        da dove viene la riga.
        """
        return {
            'SellerSKU': row.get('sku', ''),
            'ASIN': row.get('asin', ''),
            'Title': row.get('product-name', ''),
            'QuantityOrdered': row.get('quantity') or 1,
            'ItemPrice': {'Amount': row.get('item-price') or 0},
            'ItemTax': {'Amount': row.get('item-tax') or 0},
            'OrderItemId': row.get('order-item-id', ''),
        }

    def _confirm_and_validate_fba_order(self, order):
        """
        Conferma un ordine FBA e valida immediatamente il picking in uscita.
        Amazon ha già spedito: registriamo il movimento di stock dal magazzino FBA
        e creiamo la fattura bozza pronta per la revisione.
        Salta gli ordini senza righe fatturabili (prodotti non trovati in Odoo).
        """
        billable_lines = order.order_line.filtered(
            lambda l: not l.display_type and l.price_unit > 0
        )
        if not billable_lines:
            msg = (
                f'FBA order {order.x_amazon_order_id} ignorato: nessun prodotto trovato '
                f'in Odoo (solo righe nota).'
            )
            _log.info(msg)
            order.message_post(body=(
                '<b>Ordine non confermato automaticamente</b><br/>'
                'Nessuna riga prodotto abbinata (solo righe "[NON TROVATO]") — '
                'niente da spedire/fatturare finché non risolvi il match SKU/ASIN.'
            ))
            return
        order.action_confirm()
        # action_confirm() tratta date_order come "data di conferma" e la
        # sovrascrive con l'istante corrente: la ripristiniamo alla data di
        # acquisto Amazon reale, già corretta su x_amazon_purchase_date.
        if order.x_amazon_purchase_date:
            order.write({'date_order': order.x_amazon_purchase_date})
        for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
            try:
                picking.with_context(
                    skip_immediate=True,
                    skip_backorder=True,
                )._action_done()
            except Exception as e:
                _log.warning(
                    'Validazione picking FBA %s fallita: %s — ordine confermato, consegna non validata',
                    picking.name, e,
                )
                order.message_post(body=(
                    f'<b>Attenzione: validazione consegna FBA fallita</b><br/>'
                    f'Picking {picking.name} non validato automaticamente: {e}<br/>'
                    f'L\'ordine è confermato ma la consegna va verificata/validata manualmente.'
                ))
        try:
            order._create_invoices()
        except Exception as e:
            _log.warning('Creazione fattura FBA per %s fallita: %s', order.name, e)
            order.message_post(body=(
                f'<b>Attenzione: creazione fattura FBA fallita</b><br/>{e}<br/>'
                f'Va creata manualmente.'
            ))

    def _find_or_create_amazon_partner(self, order_data):
        buyer = order_data.get('BuyerInfo', {})
        email = buyer.get('BuyerEmail', '')
        name = buyer.get('BuyerName', '') or 'Amazon Customer'
        shipping = order_data.get('ShippingAddress', {})

        # Cerca per email se non anonimizzata
        if email and '@marketplace.amazon' not in email:
            partner = self.env['res.partner'].search([('email', '=', email)], limit=1)
            if partner:
                return partner

        # Cerca per nome + città per evitare duplicati ovvi
        city = shipping.get('City', '')
        if name != 'Amazon Customer' and city:
            partner = self.env['res.partner'].search(
                [('name', '=', name), ('city', '=', city)], limit=1,
            )
            if partner:
                return partner

        country_code = shipping.get('CountryCode', 'IT')
        country = self.env['res.country'].search([('code', '=', country_code)], limit=1)

        return self.env['res.partner'].create({
            'name': name,
            'email': email,
            'street': shipping.get('AddressLine1', ''),
            'street2': shipping.get('AddressLine2', ''),
            'city': city,
            'zip': shipping.get('PostalCode', ''),
            'country_id': country.id if country else False,
            'phone': shipping.get('Phone', ''),
            'customer_rank': 1,
            'comment': 'Importato da Amazon',
        })

    def _find_amazon_product_variant(self, sku, asin):
        """
        Trova la variante Amazon (product.product) corrispondente a sku/asin.
        Cerca prima sulla VARIANTE — dove vivono gli SKU dei child di una
        famiglia (es. sync_fba_quantities_page, sync_prices_and_quantities) —
        poi sul TEMPLATE (prodotti semplici, un solo variante).
        Se un template fosse trovato ma avesse PIÙ varianti, non sceglie una a
        caso: product_variant_id ritornerebbe un variante arbitrario, non
        necessariamente quello dell'ordine/riga in questione — meglio restare
        "non trovato" che abbinare silenziosamente al variante sbagliato.
        Ritorna il product.product corretto, o un recordset vuoto.
        """
        Product = self.env['product.product']
        Template = self.env['product.template']

        variant = Product.browse()
        if sku:
            variant = Product.search([('x_amazon_sku', '=', sku)], limit=1)
        if not variant and asin:
            variant = Product.search([('x_amazon_asin', '=', asin)], limit=1)
        if not variant and sku:
            sku_alt = sku[:-1] if sku.endswith('+') else sku + '+'
            variant = Product.search([('x_amazon_sku', '=', sku_alt)], limit=1)
        if variant:
            return variant

        # Cerca prodotto per SKU, poi ASIN (copre i rifornimenti con '+' quando
        # l'ASIN è già tracciato sul record), poi default_code, poi SKU con '+'
        # aggiunto/rimosso come ultima risorsa se l'ASIN non è ancora salvato
        # (prodotto non ancora passato dalla riconciliazione).
        product_tmpl = None
        if sku:
            product_tmpl = Template.search([('x_amazon_sku', '=', sku)], limit=1)
        if not product_tmpl and asin:
            product_tmpl = Template.search([('x_amazon_asin', '=', asin)], limit=1)
        if not product_tmpl and sku:
            product_tmpl = Template.search([('default_code', '=', sku)], limit=1)
        if not product_tmpl and sku:
            sku_alt = sku[:-1] if sku.endswith('+') else sku + '+'
            product_tmpl = Template.search(
                ['|', ('x_amazon_sku', '=', sku_alt), ('default_code', '=', sku_alt)], limit=1,
            )

        if not product_tmpl or len(product_tmpl.product_variant_ids) > 1:
            return Product.browse()
        return product_tmpl.product_variant_id

    def _create_amazon_order_line(self, order, item):
        sku = item.get('SellerSKU', '')
        asin = item.get('ASIN', '')
        title = item.get('Title', '') or sku or asin
        qty = float(item.get('QuantityOrdered', 1) or 1)
        item_price = item.get('ItemPrice', {})
        item_tax = item.get('ItemTax', {})
        total_gross = float(item_price.get('Amount', 0) or 0)
        total_tax_amount = float(item_tax.get('Amount', 0) or 0)
        # Amazon prices are VAT-inclusive in EU; subtract ItemTax to get net price
        # so Odoo can apply product taxes without doubling VAT
        total_net = total_gross - total_tax_amount
        unit_price = total_net / qty if qty else 0.0
        item_id = item.get('OrderItemId', '')

        variant = self._find_amazon_product_variant(sku, asin)

        if variant:
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': variant.id,
                'name': title,
                'product_uom_qty': qty,
                'price_unit': unit_price,
                'x_amazon_sku': sku,
                'x_amazon_asin': asin,
                'x_amazon_order_item_id': item_id,
            })
        else:
            # Prodotto non trovato: riga di testo con dati completi
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'display_type': 'line_note',
                'name': (
                    f'[NON TROVATO] SKU: {sku} | ASIN: {asin} | '
                    f'Qty: {qty} | Prezzo: {total_gross:.2f} (IVA incl.) | {title}'
                ),
            })

    def process_returns_report_rows(self, log_rec, rows):
        """
        Elabora il report resi cliente FBA (GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA).
        Per ogni riga abbina l'ordine Amazon in Odoo (x_amazon_order_id) e registra il
        reso come nota nel chatter dell'ordine — nessuna nota di credito automatica,
        solo visibilità: la valutazione contabile resta manuale.
        Nessun gating primario/secondario necessario qui (a differenza di
        reconcile/prezzi-quantità): l'abbinamento è per x_amazon_order_id, già
        univoco e già taggato con il marketplace giusto fin dall'import ordini
        (fase 2) — non tocca campi Amazon "legacy" del prodotto.
        Non imposta qui log_rec.state='done': con più marketplace in coda
        questo metodo gira una volta per marketplace, chiude il log solo
        _finish_returns in amazon_sync_log.py a coda esaurita. I contatori si
        sommano tra i marketplace (stesso pattern di _do_reconcile).
        Ritorna (matched, not_found, errors).
        """
        matched = not_found = errors = 0
        lines = []

        for row in rows:
            order_id = (row.get('order-id') or '').strip()
            sku = (row.get('sku') or '').strip()
            asin = (row.get('asin') or '').strip()
            qty = (row.get('quantity') or '').strip()
            reason = (row.get('reason') or '').strip()
            disposition = (row.get('detailed-disposition') or '').strip()
            return_date = (row.get('return-date') or '').strip()
            comments = (row.get('customer-comments') or '').strip()

            if not order_id:
                continue

            try:
                order = self.env['sale.order'].search(
                    [('x_amazon_order_id', '=', order_id)], limit=1,
                )
                if not order:
                    not_found += 1
                    lines.append(f'ORDINE NON TROVATO: {order_id} — SKU: {sku} — Qty: {qty}')
                    continue

                body = (
                    f'<b>Reso Amazon ricevuto</b><br/>'
                    f'Data reso: {return_date}<br/>'
                    f'SKU: {sku} — ASIN: {asin} — Quantità: {qty}<br/>'
                    f'Condizione: {disposition or "n/d"} — Motivo: {reason or "n/d"}'
                    + (f'<br/>Commento cliente: {comments}' if comments else '')
                )
                order.message_post(body=body)
                order.write({'x_amazon_has_return': True})
                matched += 1
                lines.append(f'OK {order_id} → {order.name}: SKU {sku}, qty {qty}, {disposition or reason}')
            except Exception as exc:
                errors += 1
                lines.append(f'ERRORE {order_id}: {exc}')
                _log.error('Errore elaborazione reso %s: %s', order_id, exc, exc_info=True)

        new_lines = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_total': log_rec.products_total + len(rows),
            'products_updated': log_rec.products_updated + matched,
            'products_skipped': log_rec.products_skipped + not_found,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_lines,
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return matched, not_found, errors

    def process_fnsku_report_rows(self, log_rec, rows):
        """
        Elabora il report inventario FBA (GET_FBA_MYI_ALL_INVENTORY_DATA) per popolare
        x_amazon_fnsku. Abbina per SKU usando _find_amazon_product_variant (variante
        prima, poi template) — righe senza SKU riconosciuto in Odoo vengono segnalate
        come saltate, non è un errore (es. inventario storico/archiviato non più in
        Odoo). x_amazon_fnsku è oggi un campo di template, condiviso da tutte le
        varianti di una famiglia: se il variante trovato appartiene a un template con
        PIÙ varianti, scriverlo lì sovrascriverebbe l'FNSKU (diverso) delle altre
        varianti — in quel caso la riga viene saltata con un messaggio esplicito
        invece di scrivere un dato sbagliato (limite noto, vedi TASK.md).

        Fase 3 multi-marketplace: "pooled" determinato da
        ke.amazon.marketplace.is_fba_pooled() (stesso meccanismo di
        sync_fba_quantities_page — condivide il magazzino FBA con almeno un
        altro marketplace attivo). Per un marketplace pooled (IT/DE/ES/FR
        oggi, confermato dall'utente), nessun gating primario/secondario —
        l'FNSKU è l'etichetta della stessa unità fisica pooled, riconfermata
        (redundante ma innocua, idempotente) da ogni marketplace pooled che
        il chiamante itera, scritta sul campo condiviso x_amazon_fnsku.
        Per un marketplace NON pooled (inventario davvero separato, es.
        UK/US), l'FNSKU può essere realmente diverso da quello del pool per
        lo stesso prodotto: si scrive invece su ke.amazon.listing.fnsku (già
        scoped per marketplace), mai su x_amazon_fnsku, per non farlo
        sovrascrivere dall'ultimo marketplace
        processato.
        Non imposta qui log_rec.state='done': con più marketplace in coda
        questo metodo gira una volta per marketplace, chiude il log solo
        _finish_fnsku_import in amazon_sync_log.py a coda esaurita. I
        contatori si sommano tra i marketplace (stesso pattern di _do_reconcile).
        Ritorna (matched, not_found, errors).
        """
        marketplace = self._current_marketplace()
        mp_tag = f'[{marketplace.code}] ' if marketplace else ''
        dedicated = bool(marketplace) and not marketplace.is_fba_pooled()
        matched = not_found = errors = 0
        lines = []

        for row in rows:
            sku = (row.get('sku') or '').strip()
            fnsku = (row.get('fnsku') or '').strip()
            if not sku or not fnsku:
                continue

            try:
                variant = self._find_amazon_product_variant(sku, '')
                if not variant:
                    not_found += 1
                    lines.append(f'{mp_tag}SKU NON TROVATO (o ambiguo su più varianti): {sku} — FNSKU: {fnsku}')
                    continue

                product = variant.product_tmpl_id
                if len(product.product_variant_ids) > 1:
                    # x_amazon_fnsku (e, per lo stesso motivo, ke.amazon.listing.fnsku,
                    # anch'esso a livello di template) è condiviso da tutte le
                    # varianti — anche se QUESTA variante è stata identificata con
                    # precisione, scriverlo qui sovrascriverebbe l'FNSKU (diverso)
                    # delle varianti sorelle. Serve un campo per-variante per
                    # gestire correttamente questo caso (non ancora implementato),
                    # a prescindere da pooled/dedicato.
                    not_found += 1
                    lines.append(
                        f'{mp_tag}SALTATO {sku}: appartiene a una famiglia con più varianti, '
                        f"l'FNSKU è condiviso per template — richiede un campo "
                        f'FNSKU per variante non ancora implementato'
                    )
                    continue

                listing = None
                if marketplace:
                    listing = self.env['ke.amazon.listing'].sudo().search([
                        ('product_id', '=', product.id), ('marketplace_id', '=', marketplace.id),
                    ], limit=1)

                if dedicated:
                    if not listing:
                        not_found += 1
                        lines.append(
                            f'{mp_tag}SALTATO {sku}: nessuna riga ke.amazon.listing per questo '
                            f'marketplace dedicato (serve da full import/riconciliazione prima)'
                        )
                        continue
                    if listing.fnsku != fnsku:
                        listing.write({'fnsku': fnsku})
                        lines.append(f'{mp_tag}OK {sku} → {fnsku}')
                    else:
                        lines.append(f'{mp_tag}INVARIATO {sku}: {fnsku}')
                else:
                    if product.x_amazon_fnsku != fnsku:
                        product.write({'x_amazon_fnsku': fnsku})
                        lines.append(f'{mp_tag}OK {sku} → {fnsku}')
                    else:
                        lines.append(f'{mp_tag}INVARIATO {sku}: {fnsku}')
                    # Riga listing del marketplace corrente, se esiste già
                    # (creata da full_import/reconcile): aggiornata per
                    # visibilità, mai creata qui.
                    if listing:
                        listing.write({'fnsku': fnsku})
                matched += 1
            except Exception as exc:
                errors += 1
                lines.append(f'{mp_tag}ERRORE {sku}: {exc}')
                _log.error('Errore elaborazione FNSKU %s: %s', sku, exc, exc_info=True)

        new_lines = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_total': log_rec.products_total + len(rows),
            'products_updated': log_rec.products_updated + matched,
            'products_skipped': log_rec.products_skipped + not_found,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_lines,
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return matched, not_found, errors

    def detect_active_marketplaces(self, log_rec):
        """
        Chiede ad Amazon (Sellers API, getMarketplaceParticipations) su quali
        marketplace il venditore è realmente attivo, e aggiorna ke.amazon.marketplace
        di conseguenza — invece di fidarsi solo del toggle manuale. Attiva/crea solo
        i marketplace CONFERMATI da Amazon; non disattiva mai automaticamente quelli
        già attivi ma non confermati (troppo rischioso farlo da soli, es. durante un
        onboarding in corso su un nuovo marketplace) — li segnala solo nel log,
        decisione manuale dell'utente.
        Ritorna (activated, created, not_confirmed): liste di codici marketplace.
        """
        Marketplace = self.env['ke.amazon.marketplace'].sudo()
        participations = self.client.get_marketplace_participations()

        activated = []
        created = []
        confirmed_ids = set()
        lines = []

        for p in participations:
            if not p['is_participating']:
                lines.append(f"NON PARTECIPA: {p['name'] or p['code']} ({p['marketplace_id']})")
                continue
            confirmed_ids.add(p['marketplace_id'])
            mp = Marketplace.with_context(active_test=False).search(
                [('marketplace_id', '=', p['marketplace_id'])], limit=1,
            )
            if not mp:
                mp = Marketplace.create({
                    'name': p['name'] or p['code'],
                    'code': p['code'],
                    'marketplace_id': p['marketplace_id'],
                })
                created.append(mp.code)
                lines.append(f'CREATO E ATTIVATO: {mp.name} ({mp.code})')
            elif not mp.active:
                mp.write({'active': True})
                activated.append(mp.code)
                lines.append(f'ATTIVATO: {mp.name} ({mp.code})')
            else:
                lines.append(f'GIÀ ATTIVO (confermato): {mp.name} ({mp.code})')

        currently_active = Marketplace.search([('active', '=', True)])
        not_confirmed = [
            mp.code for mp in currently_active if mp.marketplace_id not in confirmed_ids
        ]
        for code in not_confirmed:
            lines.append(
                f'ATTENZIONE: "{code}" è attivo in Odoo ma Amazon non lo conferma — '
                f'non disattivato automaticamente, verifica e disattivalo a mano se necessario.'
            )

        log_rec.write({
            'state': 'done',
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return activated, created, not_confirmed

    def reconcile_from_report_rows(self, log_rec, rows):
        """
        Confronta i prodotti nel report listing Amazon (TSV completo) con quelli in Odoo.
        Usata sia dal cron automatico che dal trigger manuale: essendo basata sul report
        completo (Reports API) non ha limiti sul numero di prodotti, a differenza della
        Listings API (search_listings_items), limitata a ~1000 risultati.
        - SKU in Odoo trovato su Amazon per SKU → aggiorna x_amazon_status e x_amazon_price
        - SKU in Odoo NON trovato per SKU, ma ASIN trovato → aggiorna x_amazon_sku al nuovo valore
          (es. prodotto rifornito: SKU 'PFS-1234' → 'PFS-1234+', ASIN invariato)
        - SKU in Odoo non trovato né per SKU né per ASIN → marca x_amazon_status='inactive'
        - SKU su Amazon senza corrispondente in Odoo → segnalato nel log
        """
        amazon_by_sku = {}
        amazon_by_asin = {}
        for row in rows:
            parsed = self._parse_listing_report(row)
            sku = parsed['sku']
            asin = parsed['asin']
            if sku:
                amazon_by_sku[sku] = parsed
            if asin:
                # Più righe report possono condividere lo stesso ASIN (es. vecchio SKU
                # storico + nuovo SKU '+' di rifornimento): a parità di ASIN vince sempre
                # la versione '+' (quella definitiva), indipendentemente dall'ordine nel report.
                existing_for_asin = amazon_by_asin.get(asin)
                if existing_for_asin is None or (
                    sku.endswith('+') and not existing_for_asin['sku'].endswith('+')
                ):
                    amazon_by_asin[asin] = parsed

        total_amazon = len(amazon_by_sku)
        _log.info('Riconciliazione da report: %d listing su Amazon', total_amazon)
        existing = (log_rec.log_lines or '').rstrip('\n')
        log_rec.write({'log_lines': existing + f'\nStore Amazon: {total_amazon} listing trovati (da report).'})
        self.env.cr.commit()
        return self._do_reconcile(log_rec, amazon_by_sku, amazon_by_asin)

    def _do_reconcile(self, log_rec, amazon_by_sku, amazon_by_asin):
        """Logica di confronto tra Odoo e le mappe Amazon pre-costruite."""
        is_primary = self._is_primary_marketplace()

        # Fase 3 multi-marketplace: un template creato/aggiornato SOLO da un
        # marketplace non primario non ha i campi legacy x_amazon_* popolati
        # (vedi _is_primary_marketplace) e non comparirebbe nella ricerca sotto
        # — verrebbe segnalato "solo Amazon" invece di essere riconciliato.
        # Recuperiamo SKU/ASIN di quei template da ke.amazon.listing (lo SKU
        # è tipicamente lo stesso su tutti i marketplace dello stesso venditore,
        # stessa assunzione già usata da full_import_report_rows).
        listing_sku_by_tmpl = {}
        listing_asin_by_tmpl = {}
        for listing in self.env['ke.amazon.listing'].sudo().search([('sku', '!=', False)]):
            listing_sku_by_tmpl.setdefault(listing.product_id.id, listing.sku)
            if listing.asin:
                listing_asin_by_tmpl.setdefault(listing.product_id.id, listing.asin)

        simple_products = self.env['product.template'].search([
            '|', ('x_amazon_sku', '!=', False), ('id', 'in', list(listing_sku_by_tmpl.keys())),
        ])
        variant_products = self.env['product.product'].search([('x_amazon_sku', '!=', False)])

        matched = updated = sku_updated = only_odoo = only_amazon = errors = 0
        odoo_skus_seen = set()
        lines = []

        for product in simple_products:
            sku = product.x_amazon_sku or listing_sku_by_tmpl.get(product.id)
            if not sku:
                continue
            odoo_skus_seen.add(sku)
            plus_sku = sku + '+'
            plus_data = amazon_by_sku.get(plus_sku)
            amazon_data = amazon_by_sku.get(sku)
            try:
                if plus_data:
                    # Versione '+' disponibile su Amazon: aggiorna SKU (ha precedenza sul match diretto)
                    new_status = 'active' if 'BUYABLE' in plus_data.get('status', '') else 'inactive'
                    new_price = plus_data.get('price') or 0.0
                    if is_primary:
                        product.write({
                            'x_amazon_sku': plus_sku,
                            'default_code': plus_sku,
                            'x_amazon_status': new_status,
                            'x_amazon_price': new_price,
                            'x_amazon_last_sync': datetime.now(),
                            # L'FNSKU è legato alla coppia (SKU, venditore): quando lo
                            # SKU cambia, Amazon ne genera uno nuovo — quello vecchio
                            # non è più valido. Azzerarlo evita di stampare etichette
                            # con un FNSKU sbagliato finché non arriva il prossimo
                            # Import FNSKU (già incatenato al full import).
                            'x_amazon_fnsku': False,
                            # Solo indicazione informativa, non usata per match.
                            'x_amazon_sku_previous': sku,
                        })
                    self._upsert_listing(product, plus_data)
                    odoo_skus_seen.add(plus_sku)
                    sku_updated += 1
                    matched += 1
                    msg = (f'[SKU AGGIORNATO] {product.default_code or product.name} '
                           f'SKU={sku} → {plus_sku} stato={new_status} prezzo={new_price:.2f}€')
                elif amazon_data:
                    matched += 1
                    new_status = 'active' if 'BUYABLE' in amazon_data.get('status', '') else 'inactive'
                    new_price = amazon_data.get('price') or 0.0
                    if is_primary and (
                        product.x_amazon_status != new_status or product.x_amazon_price != new_price
                    ):
                        product.write({'x_amazon_status': new_status, 'x_amazon_price': new_price})
                        updated += 1
                    elif not is_primary:
                        updated += 1
                    self._upsert_listing(product, amazon_data)
                    msg = (f'[OK] {product.default_code or product.name} SKU={sku} '
                           f'stato={new_status} prezzo={new_price:.2f}€')
                else:
                    # Fallback: cerca per ASIN (SKU cambiato in modo non standard, es. non solo '+')
                    product_asin = product.x_amazon_asin or listing_asin_by_tmpl.get(product.id)
                    amazon_data_by_asin = amazon_by_asin.get(product_asin) if product_asin else None
                    if amazon_data_by_asin:
                        new_sku = amazon_data_by_asin['sku']
                        new_status = 'active' if 'BUYABLE' in amazon_data_by_asin.get('status', '') else 'inactive'
                        new_price = amazon_data_by_asin.get('price') or 0.0
                        if is_primary:
                            product.write({
                                'x_amazon_sku': new_sku,
                                'default_code': new_sku,
                                'x_amazon_status': new_status,
                                'x_amazon_price': new_price,
                                'x_amazon_last_sync': datetime.now(),
                                'x_amazon_fnsku': False,  # vedi commento sopra: nuovo SKU → nuovo FNSKU
                                'x_amazon_sku_previous': sku,  # solo indicazione informativa
                            })
                        self._upsert_listing(product, amazon_data_by_asin)
                        odoo_skus_seen.add(new_sku)
                        sku_updated += 1
                        matched += 1
                        msg = (f'[SKU AGGIORNATO] {product.default_code or product.name} '
                               f'ASIN={product_asin} {sku} → {new_sku}')
                    else:
                        only_odoo += 1
                        old_status = product.x_amazon_status
                        asin_label = f' ASIN={product_asin}' if product_asin else ''
                        if is_primary:
                            product.write({'x_amazon_status': 'inactive'})
                        note = f' (era: {old_status})' if old_status and old_status != 'inactive' else ''
                        msg = (f'[SOLO ODOO] {product.default_code or product.name} '
                               f'SKU={sku}{asin_label}{note}')
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
            plus_sku = sku + '+'
            plus_data = amazon_by_sku.get(plus_sku)
            amazon_data = amazon_by_sku.get(sku)
            try:
                if plus_data:
                    # Versione '+' disponibile: aggiorna SKU variante
                    new_status = 'active' if 'BUYABLE' in plus_data.get('status', '') else 'inactive'
                    new_price = plus_data.get('price') or 0.0
                    if is_primary:
                        variant.write({
                            'x_amazon_sku': plus_sku, 'default_code': plus_sku,
                            'x_amazon_sku_previous': sku,  # solo indicazione informativa
                        })
                        tmpl_vals = {
                            'x_amazon_status': new_status,
                            'x_amazon_price': new_price,
                            'x_amazon_last_sync': datetime.now(),
                        }
                        # x_amazon_fnsku è un campo di template condiviso: azzeralo solo
                        # se il template ha un solo variante, altrimenti si perderebbe
                        # l'FNSKU (diverso) delle varianti sorelle non cambiate.
                        if len(tmpl.product_variant_ids) <= 1:
                            tmpl_vals['x_amazon_fnsku'] = False
                        tmpl.write(tmpl_vals)
                    self._upsert_listing(tmpl, plus_data)
                    odoo_skus_seen.add(plus_sku)
                    sku_updated += 1
                    matched += 1
                    msg = (f'[SKU AGGIORNATO] variante {tmpl.default_code or tmpl.name} '
                           f'/ {variant.display_name} SKU={sku} → {plus_sku} stato={new_status}')
                elif amazon_data:
                    matched += 1
                    new_status = 'active' if 'BUYABLE' in amazon_data.get('status', '') else 'inactive'
                    new_price = amazon_data.get('price') or 0.0
                    if is_primary:
                        tmpl.write({'x_amazon_status': new_status, 'x_amazon_price': new_price})
                    self._upsert_listing(tmpl, amazon_data)
                    updated += 1
                    msg = (f'[OK] variante {tmpl.default_code or tmpl.name} '
                           f'/ {variant.display_name} SKU={sku} stato={new_status}')
                else:
                    # Fallback: cerca per ASIN (SKU cambiato in modo non standard)
                    variant_asin = variant.x_amazon_asin
                    amazon_data_by_asin = amazon_by_asin.get(variant_asin) if variant_asin else None
                    if amazon_data_by_asin:
                        new_sku = amazon_data_by_asin['sku']
                        new_status = 'active' if 'BUYABLE' in amazon_data_by_asin.get('status', '') else 'inactive'
                        new_price = amazon_data_by_asin.get('price') or 0.0
                        if is_primary:
                            variant.write({
                                'x_amazon_sku': new_sku, 'default_code': new_sku,
                                'x_amazon_sku_previous': sku,  # solo indicazione informativa
                            })
                            tmpl_vals_asin = {
                                'x_amazon_status': new_status,
                                'x_amazon_price': new_price,
                                'x_amazon_last_sync': datetime.now(),
                            }
                            if len(tmpl.product_variant_ids) <= 1:
                                tmpl_vals_asin['x_amazon_fnsku'] = False
                            tmpl.write(tmpl_vals_asin)
                        self._upsert_listing(tmpl, amazon_data_by_asin)
                        odoo_skus_seen.add(new_sku)
                        sku_updated += 1
                        matched += 1
                        msg = (f'[SKU AGGIORNATO] variante {tmpl.default_code or tmpl.name} '
                               f'/ {variant.display_name} ASIN={variant_asin} {sku} → {new_sku}')
                    else:
                        only_odoo += 1
                        asin_label = f' ASIN={variant_asin}' if variant_asin else ''
                        msg = (f'[SOLO ODOO] variante {tmpl.default_code or tmpl.name} '
                               f'/ {variant.display_name} SKU={sku}{asin_label}')
            except Exception as exc:
                errors += 1
                msg = f'[ERRORE] variante SKU={sku}: {exc}'
                _log.error(msg, exc_info=True)
            lines.append(msg)
            _log.info(msg)

        not_refilled = 0
        for sku, amazon_data in amazon_by_sku.items():
            if sku not in odoo_skus_seen:
                if (sku + '+') in odoo_skus_seen:
                    not_refilled += 1
                    plus_asin = (amazon_by_sku.get(sku + '+') or {}).get('asin', '?')
                    old_asin = amazon_data.get('asin', '?')
                    asin_note = 'ASIN coincide' if old_asin == plus_asin else f'ASIN DIVERSO: {old_asin} ≠ {plus_asin}'
                    msg = (f'[NON REFILLATO] SKU={sku} → {sku}+ già presente in Odoo '
                           f'({asin_note})')
                else:
                    only_amazon += 1
                    msg = (f'[SOLO AMAZON] SKU={sku} ASIN={amazon_data.get("asin", "?")} '
                           f'"{amazon_data.get("name", "")[:60]}"')
                lines.append(msg)
                _log.info(msg)

        summary = (
            f'\nRiepilogo: {matched} trovati su Amazon ({updated} aggiornati, '
            f'{sku_updated} SKU aggiornati per rifornimento), '
            f'{only_odoo} solo in Odoo (non su Amazon), '
            f'{only_amazon} solo su Amazon (non importati), '
            f'{not_refilled} non refillati (versione + già in Odoo), {errors} errori.'
        )
        lines.append(summary)
        _log.info(summary)

        # Non impostiamo qui state='done': con più marketplace in coda (fase 3
        # multi-marketplace) questo metodo gira una volta per marketplace —
        # è l'orchestrazione in amazon_sync_log.py (_finish_reconcile) a
        # chiudere il log solo quando la coda è esaurita. I contatori si
        # sommano tra i marketplace (stesso pattern di full_import_report_rows).
        existing_log = (log_rec.log_lines or '').rstrip('\n')
        log_rec.write({
            'products_total': log_rec.products_total + len(odoo_skus_seen),
            'products_updated': log_rec.products_updated + updated + sku_updated,
            'products_skipped': log_rec.products_skipped + only_odoo,
            'products_created': log_rec.products_created + only_amazon,
            'products_error': log_rec.products_error + errors,
            'log_lines': (existing_log + '\n' + '\n'.join(lines)).lstrip('\n'),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return matched, only_odoo, only_amazon, errors
