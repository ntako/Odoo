import base64
import csv
import io
import logging
import os
import re
import threading
import time

import requests
from PIL import Image

from odoo import _, api, fields, models
from odoo.modules.registry import Registry as OdooRegistry
from odoo.exceptions import UserError

from .field_mapping import (
    FIELD_TYPE_MAP, MAPPING_TARGETS,
    VOLUME_UNIT_FACTORS, VOLUME_UNIT_SELECTION,
    WEIGHT_UNIT_FACTORS, WEIGHT_UNIT_SELECTION,
)

_logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Cancel events keyed by job_id.  Set by action_cancel() to request a graceful
# abort of the background import thread.
_CANCEL_EVENTS: dict = {}
_CANCEL_LOCK = threading.Lock()

# Maps human-readable or alternative names → Odoo type key
_PRODUCT_TYPE_MAP = {
    'consu': 'consu', 'goods': 'consu', 'storable': 'consu', 'consumable': 'consu',
    'service': 'service', 'servizio': 'service',
    'combo': 'combo',
}

# ---------------------------------------------------------------------------
# Hardcoded column → field mappings (used when use_custom_mapping=False)
# ---------------------------------------------------------------------------

# Standard Odoo fields mapped from well-known CSV column names (auto mode).
# All other columns become product attributes via ATTRIBUTE_COLUMNS.
FIELD_MAPPING = [
    ('SAP', 'default_code', 'char'),
    ('SAP Description', 'name', 'char'),
    ('Category', '_category', 'category'),
    ('EAN/UPC', 'barcode', 'barcode'),
    ('Net Weight', 'weight', 'weight'),
]

BATCH_SIZE = 100

# CSV columns treated as product attributes in auto mode.
# In custom mapping mode the user assigns _attribute target manually.
ATTRIBUTE_COLUMNS = [
    # Previously custom fields — now stored as attributes
    'Brand', 'SubCategory', 'Series', 'SubSeries', 'Water Type',
    'Country of origin', 'Trade Pack Qty', 'Package Count', 'Packaging',
    'Color', 'Product Type', 'Net Weight', 'Primary Species',
    'Commodity code', 'Commodity Code Description',
    # Dimensions (informational)
    'Each Dimension - Length', 'Each Dimension - Width', 'Each Dimension - Height',
    'Each Dimension - Gross Weight', 'Each Dimension - Unit of Dimension',
    'Each Dimension - Unit of Weight',
    'Case Quantity', 'UPC for Case Pack',
    'Case Dimension - Length', 'Case Dimension - Width', 'Case Dimension - Height',
    'Case Dimension - Gross Weight', 'Case Dimension - Unit of Dimension',
    'Case Dimension - Unit of Weight',
    'Carton Quantity', 'UPC for Carton',
    'Carton Dimension - Length', 'Carton Dimension - Width', 'Carton Dimension - Height',
    'Carton Dimension - Gross Weight', 'Carton Dimension - Unit of Dimension',
    'Carton Dimension - Unit of Weight',
    # Bait / terminal tackle
    'Bait Buoyancy', 'Bait Scent/Flavor', 'Bait Size',
    'Hook Size', 'Number of Hooks', 'Number of Pieces', 'Number of pieces (nets)',
    'Tackle Size', 'Tackle Style',
    # Rod
    'Rod Action', 'Rod Power', 'Rod Blank Material', 'Rod Handle Material',
    'Guide Type', 'Lure Rating', 'Number of guides With Tip', 'Reel Seat',
    'Rod Handle Type', 'Rod Hook Keeper', 'Rod Butt', 'AFTM Rating',
    # Reel
    'Reel Size', 'Max Drag', 'Drag Type', 'Gear Ratio', 'Reel Spool Material',
    'Bearing Count', 'Mono Capacity m/mm', 'Reel Handle Position', 'Recovery Rate',
    'Drag Material', 'Anti-Reverse Feature', 'Spare Spool Material',
    'Pre-Spooled yd/lb', 'Mono Capacity yd/lb', 'Braid Capacity yd/lb',
    'Braid Capacity m/mm', 'Braking System', 'Fly Line Capacity',
    # Line
    'Break Strength', 'Superline Mono Equiv', 'Line Rating', 'Kind of Material',
    # General
    'Containing Lead?', 'Specialty',
    # Apparel
    'Equipment Size', 'Apparel Size', 'Style 1', 'Style 2', 'Style 3',
    'Gender', 'Chest Size 1/2', 'Sleeve Length From Neck',
    'Waterproofness, gsm', 'Hem Size 1/2', 'Cb Length', 'Inseam',
    'Waist Size 1/2', 'Hip Size 1/2', 'Outseam Size',
]

LANG_COLUMNS = {
    ('UK\nCopy', 'UKBullets'): 'en_GB',
    ('IT\nCopy', 'IT\nBullets'): 'it_IT',
    ('DE\nCopy', 'DE\nBullets'): 'de_DE',
    ('FR\nCopy', 'FR\nBullets'): 'fr_FR',
    ('ES\nCopy', 'ES\nBullets'): 'es_ES',
    ('PL\nCopy', 'PL\nBullets'): 'pl_PL',
    ('CZ\nCopy', 'CZ\nBullets'): 'cs_CZ',
    ('HU Copy', 'HU Bullets'): 'hu_HU',
    ('SE Copy', 'SE Bullets'): 'sv_SE',
}

# ---------------------------------------------------------------------------
# Auto-detection map for action_parse_headers
# Maps CSV header → MAPPING_TARGETS target value
# ---------------------------------------------------------------------------
_AUTO_DETECT_MAP = {}
for _csv_col, _odoo_field, _field_type in FIELD_MAPPING:
    if _csv_col not in _AUTO_DETECT_MAP:
        if _field_type == 'category':
            _AUTO_DETECT_MAP[_csv_col] = '_category'
        else:
            _AUTO_DETECT_MAP[_csv_col] = _odoo_field
for _csv_col in ATTRIBUTE_COLUMNS:
    if _csv_col not in _AUTO_DETECT_MAP:
        _AUTO_DETECT_MAP[_csv_col] = '_attribute'
for (_copy_col, _bullets_col), _lang_code in LANG_COLUMNS.items():
    _AUTO_DETECT_MAP.setdefault(_copy_col, '_skip')
    _AUTO_DETECT_MAP.setdefault(_bullets_col, '_skip')
del _csv_col, _odoo_field, _field_type
_AUTO_DETECT_MAP['Material Image ID URL'] = '_image_url'
_AUTO_DETECT_MAP['Master Image ID URL'] = '_image_url'
_AUTO_DETECT_MAP['Supplier'] = '_supplier'
# Price columns (from engine auto-detect, lines 869-870 and 737)
_AUTO_DETECT_MAP['List Price'] = 'list_price'
_AUTO_DETECT_MAP['Sales Price'] = 'list_price'
_AUTO_DETECT_MAP['Sale Price'] = 'list_price'
_AUTO_DETECT_MAP['Cost'] = 'standard_price'
_AUTO_DETECT_MAP['Standard Price'] = 'standard_price'
_AUTO_DETECT_MAP['Costo'] = 'standard_price'
# Quantity columns (from engine auto-detect, line 738)
_AUTO_DETECT_MAP['Qty'] = '_qty'
_AUTO_DETECT_MAP['Quantity'] = '_qty'
_AUTO_DETECT_MAP['Stock'] = '_qty'
_AUTO_DETECT_MAP['On Hand'] = '_qty'
_AUTO_DETECT_MAP['Quantità'] = '_qty'
# Lot/serial columns (from engine auto-detect, line 739)
_AUTO_DETECT_MAP['Lot'] = '_lot'
_AUTO_DETECT_MAP['Lot Number'] = '_lot'
_AUTO_DETECT_MAP['Serial'] = '_lot'
_AUTO_DETECT_MAP['Serial Number'] = '_lot'
_AUTO_DETECT_MAP['Lotto'] = '_lot'

# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------


def _prepare_product_vals(row, mappings, category_cache, CategoryModel,
                          weight_unit='g', volume_unit='cm'):
    vals = {}
    for mapping in mappings:
        csv_header = mapping[0]
        odoo_field = mapping[1]
        field_type = mapping[2]
        default_value = mapping[3] if len(mapping) > 3 else ''
        raw_value = (row.get(csv_header) or '').strip() if csv_header else ''
        if not raw_value:
            # Use explicit `!= ''` so that default_value='0' (or any zero-like string) is kept
            raw_value = default_value.strip() if default_value != '' else ''
        if not raw_value:
            continue
        if field_type == 'char':
            vals[odoo_field] = raw_value
        elif field_type == 'int':
            try:
                vals[odoo_field] = int(raw_value)
            except ValueError:
                pass
        elif field_type == 'float':
            vals[odoo_field] = _parse_float(raw_value)
        elif field_type == 'barcode':
            vals[odoo_field] = raw_value
        elif field_type == 'weight':
            vals[odoo_field] = _parse_weight(raw_value, default_unit=weight_unit)
        elif field_type == 'dimension':
            # Returns m³ directly (unit detected from cell, fallback to volume_unit)
            v = _parse_volume_to_m3(raw_value, default_unit=volume_unit)
            if v:
                vals[odoo_field] = v
        elif field_type == 'category':
            vals['categ_id'] = _get_or_create_category(raw_value, category_cache, CategoryModel)
    return vals


def _get_attribute_lines(row, available_attr_cols, attr_cache, val_cache,
                         AttrModel, ValModel):
    lines = []
    for col in available_attr_cols:
        value = (row.get(col) or '').strip()
        if not value:
            continue
        if col not in attr_cache:
            attr = AttrModel.search([('name', '=', col)], limit=1)
            if not attr:
                attr = AttrModel.create({'name': col, 'create_variant': 'no_variant'})
            attr_cache[col] = attr.id
        attr_id = attr_cache[col]
        key = (attr_id, value)
        if key not in val_cache:
            val = ValModel.search(
                [('attribute_id', '=', attr_id), ('name', '=', value)], limit=1
            )
            if not val:
                val = ValModel.create({'attribute_id': attr_id, 'name': value})
            val_cache[key] = val.id
        lines.append((attr_id, val_cache[key]))
    return lines


def _sync_attribute_lines(product, new_attr_lines):
    """Update or create attribute lines on an existing product template.

    For each (attr_id, val_id) in new_attr_lines:
    - If a line for attr_id already exists: update value_ids in-place via (1, id, values).
    - If no line exists: create a new one via (0, 0, ...).
    Attributes absent from new_attr_lines are left unchanged.

    Uses in-place updates instead of delete+recreate to avoid Odoo constraints
    on attribute_line_ids and to preserve existing record IDs.
    """
    if not new_attr_lines:
        return

    existing_by_attr = {
        line.attribute_id.id: line
        for line in product.attribute_line_ids
    }

    cmds = []
    for attr_id, val_id in new_attr_lines:
        existing_line = existing_by_attr.get(attr_id)
        if existing_line:
            existing_val_ids = existing_line.value_ids.ids
            if existing_val_ids == [val_id]:
                continue  # already correct, skip
            remove = [(3, v) for v in existing_val_ids if v != val_id]
            add = [(4, val_id)] if val_id not in existing_val_ids else []
            if remove or add:
                cmds.append((1, existing_line.id, {'value_ids': remove + add}))
        else:
            cmds.append((0, 0, {'attribute_id': attr_id, 'value_ids': [(4, val_id)]}))

    if cmds:
        product.write({'attribute_line_ids': cmds})


def _write_lang_descriptions(product, row, available_lang_cols, installed_langs):
    for (copy_col, bullets_col), lang_code in LANG_COLUMNS.items():
        if lang_code not in installed_langs:
            continue
        copy = (row.get(copy_col) or '').strip() if copy_col in available_lang_cols else ''
        bullets_raw = (row.get(bullets_col) or '').strip() if bullets_col in available_lang_cols else ''
        parts = []
        if copy:
            parts.append('<p>%s</p>' % copy)
        if bullets_raw:
            items = [b.strip() for b in bullets_raw.split('_') if b.strip()]
            if items:
                lis = ''.join('<li>%s</li>' % item for item in items)
                parts.append('<ul>%s</ul>' % lis)
        if parts:
            product.with_context(lang=lang_code).write(
                {'description_sale': ''.join(parts)}
            )


def _parse_float(value):
    try:
        return float(value.strip().replace(',', '.'))
    except ValueError:
        return 0.0


def _parse_weight(value, default_unit='g'):
    """Parse weight to kg. Handles formats:
    - '9.2 oz|261 g'  → takes part after |, interprets unit explicitly
    - '500 g' / '500g' → grams → kg
    - '1.5 kg'         → kg as-is
    - '2 lb' / '3 oz'  → converts to kg
    - '1.5'            → no unit: uses default_unit (from job/preset weight_unit field)
    """
    _UNIT_ALIASES = {
        'kg': 'kg',
        'g': 'g', 'gr': 'g', 'gram': 'g', 'grams': 'g',
        'oz': 'oz',
        'lb': 'lb', 'lbs': 'lb', 'pound': 'lb', 'pounds': 'lb',
    }
    value = value.strip()
    if not value:
        return 0.0
    # Pipe-separated (e.g. '9.2 oz|261 g'): take the last segment (metric)
    part = value.split('|')[-1].strip() if '|' in value else value
    m = re.match(r'^([\d.,]+)\s*([a-zA-Z]*)', part)
    if not m:
        return 0.0
    try:
        num = float(m.group(1).replace(',', '.'))
    except ValueError:
        return 0.0
    detected = _UNIT_ALIASES.get(m.group(2).lower())
    unit = detected if detected else default_unit
    factor = WEIGHT_UNIT_FACTORS.get(unit, 1e-3)
    return round(num * factor, 6)


def _extract_number(value):
    """Extract the first numeric value from a string, stripping any unit text.

    Examples: '25', '25 cm', '25.5cm', '1.234,5 mm', '3/4"' → 25.0, 25.5, 1234.5, 0.75
    Returns 0.0 if no number found.
    """
    import re
    value = str(value).strip()
    # Handle simple fractions like 3/4
    frac = re.match(r'^(\d+)\s*/\s*(\d+)$', value)
    if frac:
        try:
            return float(frac.group(1)) / float(frac.group(2))
        except ZeroDivisionError:
            return 0.0
    # Strip leading sign then grab digits/separators
    m = re.search(r'[-+]?\d[\d\s,\.]*', value)
    if not m:
        return 0.0
    num_str = m.group(0).strip()
    # Normalise: if both ',' and '.' present, the last one is decimal separator
    if ',' in num_str and '.' in num_str:
        # e.g. '1.234,5' → '1234.5'  or  '1,234.5' → '1234.5'
        if num_str.rfind(',') > num_str.rfind('.'):
            num_str = num_str.replace('.', '').replace(',', '.')
        else:
            num_str = num_str.replace(',', '')
    elif ',' in num_str:
        num_str = num_str.replace(',', '.')
    num_str = num_str.replace(' ', '')
    try:
        return float(num_str)
    except ValueError:
        return 0.0


def _parse_linear_to_m(value, default_unit='cm'):
    """Parse a linear dimension string to metres.

    Detects unit from the cell value; falls back to default_unit when absent.
    Supports: mm, cm, dm, m, in/inch/", ft/foot/feet/'
    Examples: '25 cm' → 0.25, '10 mm' → 0.01, '5' (default cm) → 0.05
    """
    _LINEAR_TO_M = {
        'mm': 1e-3, 'cm': 1e-2, 'dm': 1e-1, 'm': 1.0,
        'in': 0.0254, 'inch': 0.0254, '"': 0.0254,
        'ft': 0.3048, 'foot': 0.3048, 'feet': 0.3048, "'": 0.3048,
    }
    value = str(value).strip()
    if not value:
        return 0.0
    # Fraction like "3/4"
    frac = re.match(r'^(\d+)\s*/\s*(\d+)\s*([^\d\s,./]*)', value)
    if frac:
        try:
            num = float(frac.group(1)) / float(frac.group(2))
        except ZeroDivisionError:
            return 0.0
        unit_str = frac.group(3).strip().lower()
    else:
        m = re.match(r'^([\d.,]+)\s*([^\d\s,./]*)', value)
        if not m:
            return 0.0
        try:
            num = float(m.group(1).replace(',', '.'))
        except ValueError:
            return 0.0
        unit_str = m.group(2).strip().lower()
    factor = _LINEAR_TO_M.get(unit_str) or _LINEAR_TO_M.get(default_unit, 1e-2)
    return num * factor


def _parse_volume_to_m3(value, default_unit='cm'):
    """Parse a direct-volume cell value to m³.

    Detects unit from the cell value; falls back to VOLUME_UNIT_FACTORS[default_unit].
    Linear unit suffixes (cm, mm, …) are treated as cubic (cm³, mm³, …).
    Also supports: L/litre/liter, mL, cl, dl.
    Examples: '25 cm³' → 2.5e-5, '1.5 L' → 1.5e-3, '25' (default cm) → 2.5e-5
    """
    _VOL_TO_M3 = {
        # explicit cubic
        'mm3': 1e-9, 'mm³': 1e-9,
        'cm3': 1e-6, 'cm³': 1e-6,
        'dm3': 1e-3, 'dm³': 1e-3,
        'm3':  1.0,  'm³':  1.0,
        'in3': 1.6387064e-5, 'in³': 1.6387064e-5,
        'ft3': 2.8316847e-2, 'ft³': 2.8316847e-2,
        # linear suffix → treated as cubic
        'mm': 1e-9, 'cm': 1e-6, 'dm': 1e-3, 'm': 1.0,
        'in': 1.6387064e-5, 'ft': 2.8316847e-2,
        # liquid
        'l': 1e-3, 'litre': 1e-3, 'liter': 1e-3, 'litres': 1e-3, 'liters': 1e-3,
        'ml': 1e-6, 'cl': 1e-5, 'dl': 1e-4,
    }
    value = str(value).strip()
    if not value:
        return 0.0
    m = re.match(r'^([\d.,]+)\s*(.*)', value)
    if not m:
        return 0.0
    try:
        num = float(m.group(1).replace(',', '.'))
    except ValueError:
        return 0.0
    unit_raw = m.group(2).strip()
    factor = (_VOL_TO_M3.get(unit_raw)
              or _VOL_TO_M3.get(unit_raw.lower())
              or VOLUME_UNIT_FACTORS.get(default_unit, 1e-6))
    return num * factor


def _download_image(url, session, current_etag=None):
    """Download and optionally resize an image from a URL.

    Returns (img_data, etag, error_msg):
      img_data  = base64 bytes on success, None on 304, False on failure
      etag      = ETag string or None
      error_msg = error description string on failure, None otherwise
    """
    IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
    _MAX_PIXELS = 50_000_000
    _, ext = os.path.splitext(url.split('?')[0])
    if ext.lower() not in IMAGE_EXTENSIONS:
        url = url + '.jpeg'
    try:
        headers = {}
        if current_etag:
            headers['If-None-Match'] = current_etag
        resp = session.get(url, timeout=15, headers=headers)
        if resp.status_code == 304:
            return None, None, None
        resp.raise_for_status()
        etag = resp.headers.get('ETag')
        img_bytes = resp.content
        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size
        if w * h > _MAX_PIXELS:
            scale = (_MAX_PIXELS / (w * h)) ** 0.5
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = img.format or 'JPEG'
            if fmt == 'JPEG' and img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(buf, format=fmt)
            img_bytes = buf.getvalue()
            _logger.info('Resized image %s: %dx%d -> %dx%d', url, w, h, new_w, new_h)
        return base64.b64encode(img_bytes), etag, None
    except Exception as e:
        _logger.warning('Failed to download image from %s: %s', url, e)
        return False, None, str(e)


def _get_or_create_category(name, cache, CategoryModel):
    if name in cache:
        return cache[name]
    category = CategoryModel.search([('name', '=', name)], limit=1)
    if not category:
        category = CategoryModel.create({'name': name})
    cache[name] = category.id
    return category.id



def _apply_post_import(env, qty_queue, existing_products,
                       import_qty, location_id, import_tracking, cr):
    """Apply standard_price and/or stock.quant adjustments after all products are saved.

    qty_queue: list of (sap_code, cost_str, qty_str, lot_name)
    Quant adjustments are batched: all quants are collected, then
    action_apply_inventory() is called once on the full recordset.
    """
    if not qty_queue:
        return

    company_id = env.company.id
    LotModel = env['stock.lot'].sudo() if (import_tracking and import_qty) else None
    QuantModel = env['stock.quant'].sudo() if import_qty else None
    quants_to_apply = env['stock.quant'].sudo()

    for sap_code, cost_str, qty_str, lot_name in qty_queue:
        tmpl = existing_products.get(sap_code)
        if not tmpl:
            continue
        try:
            if cost_str:
                cost = _parse_float(cost_str)
                if cost >= 0:
                    tmpl.sudo().standard_price = cost

            if import_qty and qty_str and location_id:
                qty = _parse_float(qty_str)
                product = tmpl.product_variant_ids[:1]
                if not product:
                    continue

                lot_id = False
                if import_tracking and lot_name and LotModel:
                    lot = LotModel.search([
                        ('name', '=', lot_name),
                        ('product_id', '=', product.id),
                    ], limit=1)
                    if not lot:
                        lot = LotModel.create({
                            'name': lot_name,
                            'product_id': product.id,
                            'company_id': company_id,
                        })
                    lot_id = lot.id

                domain = [
                    ('product_id', '=', product.id),
                    ('location_id', '=', location_id),
                    ('lot_id', '=', lot_id or False),
                ]
                quant = QuantModel.search(domain, limit=1)
                if quant:
                    quant.with_context(inventory_mode=True).write(
                        {'inventory_quantity': qty}
                    )
                else:
                    quant = QuantModel.with_context(inventory_mode=True).create({
                        'product_id': product.id,
                        'location_id': location_id,
                        'lot_id': lot_id or False,
                        'inventory_quantity': qty,
                    })
                quants_to_apply |= quant

        except Exception as e:
            _logger.warning('Post-import error for SAP %s: %s', sap_code, e)

    if quants_to_apply:
        quants_to_apply.action_apply_inventory()
        cr.commit()
        _logger.info(
            'Post-import: applied inventory adjustments for %d quants', len(quants_to_apply)
        )


def _apply_supplier_info(env, supplier_queue, existing_products, cr):
    """Create or update product.supplierinfo records from the supplier_queue.

    supplier_queue: list of (sap_code, supplier_name)
    For each product, ensures at least one supplierinfo record for the named partner.
    Does not create duplicates if the partner is already a vendor.
    """
    if not supplier_queue:
        return

    SupplierInfo = env['product.supplierinfo'].sudo()
    PartnerModel = env['res.partner'].sudo()
    partner_cache = {}

    for sap_code, supplier_name in supplier_queue:
        tmpl = existing_products.get(sap_code)
        if not tmpl:
            continue

        if supplier_name not in partner_cache:
            partner = PartnerModel.search(
                [('name', '=', supplier_name), ('is_company', '=', True)], limit=1
            )
            if not partner:
                partner = PartnerModel.create({'name': supplier_name, 'is_company': True})
            partner_cache[supplier_name] = partner.id
        partner_id = partner_cache[supplier_name]

        # Check if this partner is already a vendor for this product
        existing_info = SupplierInfo.search([
            ('product_tmpl_id', '=', tmpl.id),
            ('partner_id', '=', partner_id),
        ], limit=1)
        if not existing_info:
            SupplierInfo.create({
                'product_tmpl_id': tmpl.id,
                'partner_id': partner_id,
                'min_qty': 0.0,
                'price': 0.0,
            })

    cr.commit()
    _logger.info('Post-import: applied supplier info for %d entries', len(supplier_queue))


def _build_custom_mapping_config(custom_mapping, csv_headers):
    """Parse the custom_mapping list [(csv_col, target, default_val), ...] into runtime structures.

    csv_col may be empty string to represent a fixed default value applied to all rows.

    Returns a dict with:
      available_mappings      – list of (csv_col, odoo_field, field_type, default_val)
      available_attr_cols     – list of csv_col to treat as product attributes
      supplier_col            – csv_col for supplier or None
      supplier_default        – default supplier name or ''
      cost_col                – csv_col for standard_price or None
      cost_default            – default cost string or ''
      qty_col                 – csv_col for stock quantity or None
      qty_default             – default quantity string or ''
      lot_col                 – csv_col for lot number or None
      lot_default             – default lot string or ''
      image_url_col           – csv_col for image URL or None
      image_url_default       – default image URL or ''
    """
    available_mappings = []
    available_attr_cols = []
    supplier_col = None
    supplier_default = ''
    cost_col = None
    cost_default = ''
    qty_col = None
    qty_default = ''
    lot_col = None
    lot_default = ''
    image_url_col = None
    image_url_default = ''
    product_type_col = None
    product_type_default = ''
    volume_l_col = None
    volume_l_default = ''
    volume_w_col = None
    volume_w_default = ''
    volume_h_col = None
    volume_h_default = ''
    weight_unit_col = None
    dim_unit_col = None
    volume_unit_col = None

    for entry in custom_mapping:
        csv_col = entry[0] if entry[0] else ''
        target = entry[1]
        default_val = entry[2] if len(entry) > 2 else ''

        if target == '_skip':
            continue
        # A non-empty csv_col must exist in the CSV headers to be used for reading
        col_in_csv = csv_col and csv_col in csv_headers

        if target == '_attribute':
            if col_in_csv:
                available_attr_cols.append(csv_col)
        elif target == '_category':
            if col_in_csv:
                available_mappings.append((csv_col, 'categ_id', 'category', default_val))
            elif default_val:
                available_mappings.append(('', 'categ_id', 'category', default_val))
        elif target == '_supplier':
            if col_in_csv:
                supplier_col = csv_col
            supplier_default = default_val
        elif target == '_qty':
            if col_in_csv:
                qty_col = csv_col
            qty_default = default_val
        elif target == '_lot':
            if col_in_csv:
                lot_col = csv_col
            lot_default = default_val
        elif target == '_image_url':
            if col_in_csv:
                image_url_col = csv_col
            image_url_default = default_val
        elif target == '_product_type':
            if col_in_csv:
                product_type_col = csv_col
            product_type_default = default_val
        elif target == '_volume_l':
            if col_in_csv:
                volume_l_col = csv_col
            volume_l_default = default_val
        elif target == '_volume_w':
            if col_in_csv:
                volume_w_col = csv_col
            volume_w_default = default_val
        elif target == '_volume_h':
            if col_in_csv:
                volume_h_col = csv_col
            volume_h_default = default_val
        elif target == '_weight_unit':
            if col_in_csv:
                weight_unit_col = csv_col
        elif target == '_dim_unit':
            if col_in_csv:
                dim_unit_col = csv_col
        elif target == '_volume_unit':
            if col_in_csv:
                volume_unit_col = csv_col
        elif target == 'standard_price':
            if col_in_csv:
                cost_col = csv_col
            cost_default = default_val
        else:
            field_type = FIELD_TYPE_MAP.get(target, 'char')
            # Use `!= ''` instead of truthiness so that default_val='0' is preserved
            if col_in_csv or default_val != '':
                available_mappings.append((csv_col, target, field_type, default_val))

    # All CSV columns not explicitly assigned to any field → attributes automatically
    assigned_cols = set(filter(None, [
        supplier_col, cost_col, qty_col, lot_col, image_url_col, product_type_col,
        volume_l_col, volume_w_col, volume_h_col,
        weight_unit_col, dim_unit_col, volume_unit_col,
    ]))
    for m in available_mappings:
        if m[0]:
            assigned_cols.add(m[0])
    for col in list(available_attr_cols):
        assigned_cols.add(col)
    for col in csv_headers:
        if col and col not in assigned_cols:
            available_attr_cols.append(col)
            assigned_cols.add(col)

    return dict(
        available_mappings=available_mappings,
        available_attr_cols=available_attr_cols,
        supplier_col=supplier_col,
        supplier_default=supplier_default,
        cost_col=cost_col,
        cost_default=cost_default,
        qty_col=qty_col,
        qty_default=qty_default,
        lot_col=lot_col,
        lot_default=lot_default,
        image_url_col=image_url_col,
        image_url_default=image_url_default,
        product_type_col=product_type_col,
        product_type_default=product_type_default,
        volume_l_col=volume_l_col,
        volume_l_default=volume_l_default,
        volume_w_col=volume_w_col,
        volume_w_default=volume_w_default,
        volume_h_col=volume_h_col,
        volume_h_default=volume_h_default,
        weight_unit_col=weight_unit_col,
        dim_unit_col=dim_unit_col,
        volume_unit_col=volume_unit_col,
    )


def _run_in_background(db_name, uid, job_id, file_string, options, cancel_event):
    """Background thread entry point."""
    _logger.info('Import job %d: background thread started', job_id)
    db_reg = OdooRegistry(db_name)
    try:
        with db_reg.cursor() as cr:
            env = api.Environment(cr, uid, {})
            job = env['inventory.import.job'].browse(job_id)
            _do_import(job, env, file_string, options, cr, cancel_event)
    except Exception as exc:
        _logger.exception('Import job %d: unhandled error: %s', job_id, exc)
        try:
            with db_reg.cursor() as cr:
                env = api.Environment(cr, uid, {})
                env['inventory.import.job'].browse(job_id).write({
                    'state': 'error',
                    'completed_at': fields.Datetime.now(),
                    'error_log': str(exc),
                })
        except Exception:
            pass
    finally:
        with _CANCEL_LOCK:
            _CANCEL_EVENTS.pop(job_id, None)


def _parallel_write_products(db_name, uid, write_chunk, do_descriptions,
                              available_lang_cols, installed_langs):
    """Write vals to existing products using a dedicated cursor (called from a thread).

    write_chunk: list of (rec_id, vals, attr_lines, row, product_code, row_idx)
    Returns (updated_count, errors_list) where errors_list items are (product_code, row_idx, msg).
    """
    db_reg = OdooRegistry(db_name)
    updated = 0
    errors = []
    with db_reg.cursor() as cr:
        env = api.Environment(cr, uid, {})
        PT = env['product.template'].sudo()
        _ZERO_SENSITIVE_FLOATS = {'list_price', 'standard_price', 'weight', 'volume'}
        for rec_id, vals, attr_lines, row, product_code, row_idx in write_chunk:
            try:
                rec = PT.browse(rec_id)
                rec.write(vals)
                # Re-apply explicit zero float values: Odoo's write() may ignore
                # 0.0 for fields that have a non-zero default or computed inverse.
                zero_overrides = {
                    f: v for f, v in vals.items()
                    if f in _ZERO_SENSITIVE_FLOATS and v == 0.0
                }
                if zero_overrides:
                    rec.write(zero_overrides)
                if attr_lines:
                    _sync_attribute_lines(rec, attr_lines)
                if do_descriptions and available_lang_cols:
                    _write_lang_descriptions(rec, row, available_lang_cols, installed_langs)
                updated += 1
            except Exception as e:
                errors.append((product_code, row_idx, str(e)))
        cr.commit()
    return updated, errors


def _download_images_parallel(image_tasks, existing_products, image_delay, max_workers, report_rows, job, cr):
    """Download images in parallel using a thread pool and apply them to products.

    Each worker thread has its own requests.Session.
    Returns the number of products successfully updated with an image.
    """
    from concurrent.futures import ThreadPoolExecutor

    _local = threading.local()

    def _get_session():
        if not hasattr(_local, 'session'):
            s = requests.Session()
            s.headers.update(_BROWSER_HEADERS)
            _local.session = s
        return _local.session

    def _download_one(task):
        code, url, row_idx = task
        if image_delay:
            time.sleep(image_delay)
        img_data, _etag, img_err = _download_image(url, _get_session())
        return code, row_idx, img_data, img_err

    total = len(image_tasks)
    _logger.info('Import job %d: downloading %d images with %d workers', job.id, total, max_workers)

    img_updated = 0
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for code, row_idx, img_data, img_err in pool.map(_download_one, image_tasks):
            done += 1
            existing = existing_products.get(code)
            if existing and img_data:
                existing.write({'image_1920': img_data})
                img_updated += 1
            elif img_err:
                report_rows.append({
                    'riga': row_idx, 'sap': code,
                    'tipo': 'Errore immagine',
                    'messaggio': 'Download fallito: %s' % img_err,
                })
            if done % 50 == 0:
                cr.commit()
                _logger.info('Import job %d: %d/%d immagini elaborate', job.id, done, total)

    cr.commit()
    _logger.info('Import job %d: %d immagini applicate', job.id, img_updated)
    return img_updated


def _do_import(job, env, file_string, options, cr, cancel_event=None):
    """Run the full CSV import. Called from background thread."""
    reader = csv.DictReader(io.StringIO(file_string), delimiter=options['delimiter'])
    csv_headers = set(reader.fieldnames or [])
    rows = list(reader)
    total = len(rows)
    job.write({'total_count': total})
    cr.commit()

    db_name = cr.dbname
    uid = env.uid

    imported = updated = skipped = 0
    errors = []
    report_rows = []  # list of dicts: {riga, sap, tipo, messaggio}

    ProductTemplate = env['product.template']
    ProductCategory = env['product.category'].sudo()
    category_cache = {}

    http_session = requests.Session()
    http_session.headers.update(_BROWSER_HEADERS)

    image_delay = options['image_delay']
    max_workers = max(1, min(int(options.get('max_workers', 4)), 32))
    image_workers = max(1, min(int(options.get('image_workers', 1)), 64))
    do_attrs = options['import_attributes']
    do_descriptions = options['import_descriptions']
    images_only = options['images_only']
    import_tracking = options['import_tracking']
    import_qty = options.get('import_qty', False)
    location_id = options.get('location_id', False)
    use_custom_mapping = options.get('use_custom_mapping', False)
    custom_mapping = options.get('custom_mapping', [])
    volume_unit = options.get('volume_unit', 'cm')
    weight_unit = options.get('weight_unit', 'g')

    qty_queue = []       # (sap_code, cost_str, qty_str, lot_name) for post-processing
    supplier_queue = []  # (sap_code, supplier_name) for post-processing
    image_tasks = []     # (product_code, image_url, row_idx) for parallel download
    write_queue = []     # (rec_id, vals, attr_lines, row, product_code, row_idx) for parallel write

    # --- Build mapping structures ---
    if use_custom_mapping and custom_mapping:
        cfg = _build_custom_mapping_config(custom_mapping, csv_headers)
        available_mappings = cfg['available_mappings']
        available_attr_cols = cfg['available_attr_cols'] if do_attrs else []
        supplier_col = cfg['supplier_col']
        supplier_default = cfg['supplier_default']
        cost_col = cfg['cost_col']
        cost_default = cfg['cost_default']
        qty_col = cfg['qty_col']
        qty_default = cfg['qty_default']
        lot_col = cfg['lot_col']
        lot_default = cfg['lot_default']
        image_url_col = cfg['image_url_col']
        image_url_default = cfg['image_url_default']
        product_type_col = cfg['product_type_col']
        product_type_default = cfg['product_type_default']
        volume_l_col = cfg['volume_l_col']
        volume_l_default = cfg['volume_l_default']
        volume_w_col = cfg['volume_w_col']
        volume_w_default = cfg['volume_w_default']
        volume_h_col = cfg['volume_h_col']
        volume_h_default = cfg['volume_h_default']
        weight_unit_col = cfg['weight_unit_col']
        dim_unit_col = cfg['dim_unit_col']
        volume_unit_col = cfg['volume_unit_col']
        _logger.info(
            'Import job %d: custom mapping active – %d field mappings, %d attribute cols, image_url_col=%r',
            job.id, len(available_mappings), len(available_attr_cols), image_url_col,
        )
    else:
        # Original hardcoded mappings
        available_mappings = [(h, f, t) for h, f, t in FIELD_MAPPING if h in csv_headers]
        available_attr_cols = [c for c in ATTRIBUTE_COLUMNS if c in csv_headers] if do_attrs else []
        supplier_col = None
        supplier_default = ''
        cost_col = None
        cost_default = ''
        qty_col = None
        qty_default = ''
        lot_col = None
        lot_default = ''
        image_url_col = None
        image_url_default = ''
        product_type_col = None
        product_type_default = ''
        volume_l_col = None
        volume_l_default = ''
        volume_w_col = None
        volume_w_default = ''
        volume_h_col = None
        volume_h_default = ''
        weight_unit_col = None
        dim_unit_col = None
        volume_unit_col = None

    volume_unit_factor = VOLUME_UNIT_FACTORS.get(volume_unit, 1e-6)

    # Supplier column: custom mapping takes priority, else auto-detect 'Supplier'
    effective_supplier_col = supplier_col or ('Supplier' if 'Supplier' in csv_headers else None)

    # Column name lists for cost/qty/lot
    if use_custom_mapping:
        cost_cols = [cost_col] if cost_col else []
        qty_cols = [qty_col] if qty_col else []
        lot_cols = [lot_col] if lot_col else []
    else:
        cost_cols = ('Cost', 'Standard Price', 'Costo')
        qty_cols = ('Qty', 'Quantity', 'Stock', 'On Hand', 'Quantità')
        lot_cols = ('Lot', 'Lot Number', 'Serial', 'Serial Number', 'Lotto')

    # import_cost: True if any cost column is mapped/available (no manual checkbox needed)
    if use_custom_mapping:
        import_cost = bool(cost_col or cost_default)
    else:
        import_cost = any(c in csv_headers for c in cost_cols)

    # do_images: True if image URL column is mapped/available (no manual checkbox needed)
    if images_only:
        do_images = True
    elif use_custom_mapping:
        do_images = bool(image_url_col or image_url_default)
    else:
        do_images = any(c in csv_headers for c in ('Material Image ID URL', 'Master Image ID URL'))

    # import_qty: True if qty column is mapped/available (no manual checkbox needed)
    if use_custom_mapping:
        import_qty = bool(qty_col or qty_default)
    else:
        import_qty = any(c in csv_headers for c in qty_cols)

    # Auto mode: detect which price column to use for the whole import
    # (precomputed here so per-row logic is a simple lookup)
    _auto_price_col = None
    if not use_custom_mapping:
        _auto_price_col = next(
            (c for c in ('List Price', 'Sales Price', 'Sale Price') if c in csv_headers), None
        )
    # Custom mode: check whether list_price is explicitly mapped
    _has_list_price_mapping = (
        any(m[1] == 'list_price' for m in available_mappings)
        if use_custom_mapping else bool(_auto_price_col)
    )

    _logger.info(
        'Import job %d: %d attribute columns: %s',
        job.id, len(available_attr_cols), available_attr_cols[:5],
    )

    attr_cache = {}
    val_cache = {}
    ProductAttribute = env['product.attribute'].sudo()
    ProductAttributeValue = env['product.attribute.value'].sudo()

    available_lang_cols = {col for pair in LANG_COLUMNS for col in pair if col in csv_headers}
    installed_langs = set()
    if do_descriptions and available_lang_cols:
        installed_langs = set(
            env['res.lang'].search([('active', '=', True)]).mapped('code')
        )

    # Determine which CSV column holds default_code (product identifier)
    if use_custom_mapping:
        code_col = next(
            (entry[0] for entry in custom_mapping if entry[1] == 'default_code' and entry[0]),
            'SAP',
        )
    else:
        code_col = 'SAP'

    # Pre-load existing products by default_code for fast lookup
    all_codes = list({
        (r.get(code_col) or '').strip() for r in rows if (r.get(code_col) or '').strip()
    })
    existing_products = {}
    for i in range(0, len(all_codes), 1000):
        for rec in ProductTemplate.search([('default_code', 'in', all_codes[i:i + 1000])]):
            existing_products[rec.default_code] = rec

    create_batch = []
    create_rows = []

    # Float fields where Odoo's ORM may silently replace explicit 0.0 with the
    # field default (e.g. list_price has default=1.0).  We write them separately
    # after create() so the zero value is honoured.
    _ZERO_SENSITIVE_FLOATS = {'list_price', 'standard_price', 'weight', 'volume'}

    def _flush_batch(batch, batch_rows):
        # Collect explicit zero values before create (0.0 is falsy in Python and
        # some Odoo Float inverse/compute paths may fall back to field.default)
        zero_overrides = [
            {f: v for f, v in vals.items() if f in _ZERO_SENSITIVE_FLOATS and v == 0.0}
            for vals in batch
        ]
        new_records = ProductTemplate.create(batch)
        # Re-apply explicit zeros that create() may have silently dropped
        for rec, overrides in zip(new_records, zero_overrides):
            if overrides:
                rec.write(overrides)
        if do_descriptions and available_lang_cols:
            by_code = {r.default_code: r for r in new_records}
            for r_row in batch_rows:
                r_code = (r_row.get(code_col) or '').strip()
                if r_code in by_code:
                    _write_lang_descriptions(
                        by_code[r_code], r_row, available_lang_cols, installed_langs,
                    )
        for rec in new_records:
            existing_products[rec.default_code] = rec
        return len(new_records)

    for row_idx, row in enumerate(rows, start=2):
        product_code = (row.get(code_col) or '').strip()

        if not product_code:
            skipped += 1
            msg = 'Row %d: Codice prodotto mancante (%r vuoto), riga saltata.' % (row_idx, code_col)
            errors.append(msg)
            report_rows.append({'riga': row_idx, 'sap': '', 'tipo': 'Errore', 'messaggio': msg})
            continue

        try:
            if images_only:
                existing = existing_products.get(product_code)
                if not existing:
                    skipped += 1
                    continue
                if image_url_col:
                    image_url = (row.get(image_url_col) or '').strip()
                else:
                    image_url = (
                        (row.get('Material Image ID URL') or '').strip()
                        or (row.get('Master Image ID URL') or '').strip()
                    )
                if image_url:
                    image_tasks.append((product_code, image_url, row_idx))
                else:
                    report_rows.append({
                        'riga': row_idx, 'sap': product_code,
                        'tipo': 'Avvertimento immagine',
                        'messaggio': 'Nessuna URL immagine per questo prodotto',
                    })
            else:
                # Per-row unit overrides: read from dedicated unit columns if mapped,
                # falling back to the global job-level unit selectors.
                row_weight_unit = (
                    (row.get(weight_unit_col) or '').strip().lower()
                    if weight_unit_col else ''
                ) or weight_unit
                row_volume_unit = (
                    (row.get(volume_unit_col) or '').strip().lower()
                    if volume_unit_col else ''
                ) or volume_unit
                row_dim_unit = (
                    (row.get(dim_unit_col) or '').strip().lower()
                    if dim_unit_col else ''
                ) or volume_unit

                vals = _prepare_product_vals(
                    row, available_mappings, category_cache, ProductCategory,
                    weight_unit=row_weight_unit,
                    volume_unit=row_volume_unit,
                )
                # If list_price is mapped but the cell was empty, default to 0.0
                if _has_list_price_mapping and 'list_price' not in vals:
                    vals['list_price'] = 0.0

                # Volume calculation ------------------------------------------------
                # L×W×H mode: each dimension is parsed to metres (unit from cell or
                # row_dim_unit fallback), then multiplied to get m³ directly.
                if volume_l_col or volume_w_col or volume_h_col or \
                        volume_l_default or volume_w_default or volume_h_default:
                    l_str = (row.get(volume_l_col) or '').strip() if volume_l_col else ''
                    w_str = (row.get(volume_w_col) or '').strip() if volume_w_col else ''
                    h_str = (row.get(volume_h_col) or '').strip() if volume_h_col else ''
                    l = _parse_linear_to_m(l_str or volume_l_default, default_unit=row_dim_unit)
                    w = _parse_linear_to_m(w_str or volume_w_default, default_unit=row_dim_unit)
                    h = _parse_linear_to_m(h_str or volume_h_default, default_unit=row_dim_unit)
                    if l > 0 and w > 0 and h > 0:
                        vals['volume'] = l * w * h
                # Direct volume column: already in m³ from _parse_volume_to_m3 in
                # _prepare_product_vals — no further multiplication needed.
                # ------------------------------------------------------------------

                # Supplier (collect for post-processing after product creation)
                if effective_supplier_col or supplier_default:
                    supplier_name = (row.get(effective_supplier_col) or '').strip() \
                        if effective_supplier_col else ''
                    if not supplier_name:
                        supplier_name = supplier_default
                    if supplier_name:
                        supplier_queue.append((product_code, supplier_name))

                # Sell price (auto mode: read precomputed col, default to 0 if empty)
                if _auto_price_col:
                    price_str = (row.get(_auto_price_col) or '').strip()
                    vals['list_price'] = _parse_float(price_str) if price_str else 0.0

                # Lot tracking
                if import_tracking:
                    vals['tracking'] = 'lot'

                # Collect cost/qty for post-processing
                if import_qty or import_cost:
                    cost_str = ''
                    qty_str = ''
                    lot_str = ''
                    if import_cost:
                        for col in cost_cols:
                            cost_str = (row.get(col) or '').strip()
                            if cost_str:
                                break
                        if not cost_str:
                            cost_str = cost_default
                    if import_qty:
                        for col in qty_cols:
                            qty_str = (row.get(col) or '').strip()
                            if qty_str:
                                break
                        if not qty_str:
                            qty_str = qty_default
                    if import_tracking:
                        for col in lot_cols:
                            lot_str = (row.get(col) or '').strip()
                            if lot_str:
                                break
                        if not lot_str:
                            lot_str = lot_default
                    if cost_str or qty_str:
                        qty_queue.append((product_code, cost_str, qty_str, lot_str))

                if do_images:
                    if image_url_col:
                        image_url = (row.get(image_url_col) or '').strip() or image_url_default
                    elif image_url_default:
                        image_url = image_url_default
                    else:
                        image_url = (
                            (row.get('Material Image ID URL') or '').strip()
                            or (row.get('Master Image ID URL') or '').strip()
                        )
                    if image_url:
                        image_tasks.append((product_code, image_url, row_idx))
                    else:
                        report_rows.append({
                            'riga': row_idx, 'sap': product_code,
                            'tipo': 'Avvertimento immagine',
                            'messaggio': 'Nessuna URL immagine per questo prodotto',
                        })

                attr_lines = []
                if do_attrs and available_attr_cols:
                    attr_lines = _get_attribute_lines(
                        row, available_attr_cols, attr_cache, val_cache,
                        ProductAttribute, ProductAttributeValue,
                    )

                existing = existing_products.get(product_code)
                if existing:
                    if options['update_existing']:
                        # Defer write to parallel phase
                        write_queue.append((
                            existing.id, vals, attr_lines, row, product_code, row_idx,
                        ))
                    else:
                        skipped += 1
                else:
                    ptype = (
                        (row.get(product_type_col) or '').strip()
                        if product_type_col else ''
                    ) or product_type_default or 'consu'
                    ptype = _PRODUCT_TYPE_MAP.get(ptype.lower(), ptype)
                    if ptype not in ('consu', 'service', 'combo'):
                        ptype = 'consu'
                    vals['type'] = ptype
                    vals['is_storable'] = (ptype == 'consu')
                    if attr_lines:
                        vals['attribute_line_ids'] = [
                            (0, 0, {'attribute_id': a, 'value_ids': [(4, v)]})
                            for a, v in attr_lines
                        ]
                    create_batch.append(vals)
                    create_rows.append(row)

        except Exception as e:
            skipped += 1
            msg = 'Row %d (%s): %s' % (row_idx, product_code, str(e))
            errors.append(msg)
            report_rows.append({'riga': row_idx, 'sap': product_code, 'tipo': 'Errore', 'messaggio': str(e)})
            _logger.warning('Import job %d: error row %d: %s', job.id, row_idx, e)

        if len(create_batch) >= BATCH_SIZE:
            imported += _flush_batch(create_batch, create_rows)
            create_batch = []
            create_rows = []

        if (row_idx - 1) % 200 == 0:
            job.write({
                'processed_count': row_idx - 1,
                'import_count': imported,
                'updated_count': len(write_queue),  # queued (actual writes happen after loop)
                'skipped_count': skipped,
            })
            cr.commit()
            _logger.info(
                'Import job %d: %d/%d rows processed', job.id, row_idx - 1, total
            )
            if cancel_event and cancel_event.is_set():
                _logger.info('Import job %d: cancellation detected, aborting', job.id)
                return

    if create_batch:
        imported += _flush_batch(create_batch, create_rows)

    http_session.close()

    if cancel_event and cancel_event.is_set():
        _logger.info('Import job %d: cancellation detected before parallel writes, aborting', job.id)
        return

    # --- Parallel write of existing products ---
    if write_queue:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n_chunks = min(max_workers, len(write_queue))
        # Round-robin split keeps each chunk roughly equal in size
        chunks = [write_queue[i::n_chunks] for i in range(n_chunks)]
        chunks = [c for c in chunks if c]
        _logger.info(
            'Import job %d: writing %d existing products with %d workers',
            job.id, len(write_queue), len(chunks),
        )
        if len(chunks) == 1:
            n_upd, write_errors = _parallel_write_products(
                db_name, uid, chunks[0], do_descriptions, available_lang_cols, installed_langs,
            )
            updated += n_upd
            for p_code, r_idx, msg in write_errors:
                errors.append(msg)
                report_rows.append({'riga': r_idx, 'sap': p_code, 'tipo': 'Errore', 'messaggio': msg})
        else:
            with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
                futures = [
                    pool.submit(
                        _parallel_write_products,
                        db_name, uid, chunk,
                        do_descriptions, available_lang_cols, installed_langs,
                    )
                    for chunk in chunks
                ]
                for fut in as_completed(futures):
                    n_upd, write_errors = fut.result()
                    updated += n_upd
                    for p_code, r_idx, msg in write_errors:
                        errors.append(msg)
                        report_rows.append({'riga': r_idx, 'sap': p_code, 'tipo': 'Errore', 'messaggio': msg})
        # Invalidate main env cache so subsequent reads see committed data
        env['product.template'].invalidate_model()
        cr.commit()

    if cancel_event and cancel_event.is_set():
        _logger.info('Import job %d: cancellation detected before image downloads, aborting', job.id)
        return

    # --- Parallel image download (after all products exist in existing_products) ---
    if image_tasks:
        img_updated = _download_images_parallel(
            image_tasks, existing_products, image_delay, image_workers, report_rows, job, cr,
        )
        if images_only:
            updated += img_updated
        else:
            # For normal imports, images are applied on top of already-counted records
            pass

    if qty_queue:
        _apply_post_import(
            env, qty_queue, existing_products,
            import_qty, location_id, import_tracking, cr
        )

    if supplier_queue:
        _apply_supplier_info(env, supplier_queue, existing_products, cr)

    # --- Generate CSV report ---
    report_buf = io.StringIO()
    writer = csv.DictWriter(
        report_buf,
        fieldnames=['Riga', 'Codice SAP', 'Tipo', 'Messaggio'],
        delimiter=';',
    )
    writer.writeheader()
    writer.writerow({
        'Riga': '',
        'Codice SAP': '',
        'Tipo': 'Riepilogo',
        'Messaggio': 'Create: %d | Aggiornate: %d | Saltate: %d | Totale righe: %d' % (
            imported, updated, skipped, total,
        ),
    })
    for r in report_rows:
        writer.writerow({
            'Riga': r['riga'],
            'Codice SAP': r['sap'],
            'Tipo': r['tipo'],
            'Messaggio': r['messaggio'],
        })
    report_bytes = report_buf.getvalue().encode('utf-8-sig')
    report_b64 = base64.b64encode(report_bytes).decode()
    report_name = 'import_report_%d.csv' % job.id

    attachment = env['ir.attachment'].sudo().create({
        'name': report_name,
        'datas': report_b64,
        'res_model': 'inventory.import.job',
        'res_id': job.id,
        'mimetype': 'text/csv',
    })

    job.write({
        'state': 'done',
        'total_count': total,
        'processed_count': total,
        'import_count': imported,
        'updated_count': updated,
        'skipped_count': skipped,
        'error_log': '\n'.join(errors) if errors else 'Nessun errore.',
        'completed_at': fields.Datetime.now(),
        'report_attachment_id': attachment.id,
    })
    _logger.info(
        'Import job %d complete: %d created, %d updated, %d skipped',
        job.id, imported, updated, skipped,
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class InventoryImportJob(models.Model):
    _name = 'inventory.import.job'
    _description = 'Inventory Import Job'
    _order = 'create_date desc'

    csv_file_id = fields.Many2one(
        'inventory.csv.file',
        string='Libreria CSV',
        ondelete='set null',
        help='Seleziona un file dalla libreria per pre-caricare il CSV',
    )
    file_name = fields.Char('File')
    file_data = fields.Binary('CSV', attachment=True)
    delimiter = fields.Selection([
        (';', 'Semicolon (;)'),
        (',', 'Comma (,)'),
        ('\t', 'Tab'),
    ], default=';')
    update_existing = fields.Boolean('Update existing', default=True)
    images_only = fields.Boolean('Images only', default=False)
    image_delay = fields.Float('Delay between images (s)', default=0.5)
    max_workers = fields.Integer(
        'Worker paralleli scrittura DB',
        default=4,
        help='Thread paralleli per aggiornare prodotti esistenti nel DB (consigliato: 2–6, dipende dai core disponibili)',
    )
    image_workers = fields.Integer(
        'Worker paralleli immagini',
        default=1,
        help='Thread paralleli per il download delle immagini (1 = sequenziale con delay rispettato; >1 = parallelo)',
    )
    import_attributes = fields.Boolean('Import attributes', default=True)
    import_descriptions = fields.Boolean('Import descriptions', default=True)
    import_tracking = fields.Boolean(
        'Tracking per lotto',
        default=False,
        help='Abilita il tracking per lotto su tutti i prodotti importati',
    )
    import_qty = fields.Boolean(
        'Importa quantità',
        default=False,
        help='Aggiorna le giacenze tramite rettifica inventario (stock.quant)',
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Location magazzino',
        domain=[('usage', '=', 'internal')],
        default=lambda self: self.env['stock.warehouse'].search([], limit=1).lot_stock_id,
        help='Location in cui aggiornare le giacenze (es. WH/Stock)',
    )

    # --- Weight unit (fallback when no unit is embedded in the CSV value) ---
    weight_unit = fields.Selection(
        WEIGHT_UNIT_SELECTION,
        string='Unità peso',
        default='g',
        help='Unità di misura del peso nel CSV. Usata quando il valore non contiene '
             'un\'unità esplicita (es. "261" → grammi se selezioni g). '
             'Se il valore contiene già l\'unità (es. "261 g" o "1.5 kg") questa impostazione '
             'viene ignorata.',
    )

    # --- Volume unit (applies to volume/dimension columns in custom mapping) ---
    volume_unit = fields.Selection(
        VOLUME_UNIT_SELECTION,
        string='Unità dimensioni volume',
        default='cm',
        help='Unità di misura usata nel CSV per le dimensioni (L×W×H) o il volume diretto. '
             'Il valore viene convertito in m³ prima di essere salvato in Odoo.',
    )

    # --- Custom column mapping ---
    use_custom_mapping = fields.Boolean(
        'Usa mapping personalizzato',
        default=False,
        help='Se attivo, usa la tabella di mapping qui sotto invece del rilevamento automatico',
    )
    mapping_line_ids = fields.One2many(
        'inventory.field.mapping.line', 'job_id',
        string='Mapping colonne',
    )
    preset_id = fields.Many2one(
        'inventory.mapping.preset',
        string='Carica preset',
        ondelete='set null',
        help='Seleziona un preset per caricare automaticamente le associazioni colonne',
    )

    state = fields.Selection([
        ('queued', 'In Coda'),
        ('running', 'In Esecuzione'),
        ('done', 'Completato'),
        ('error', 'Errore'),
        ('cancelled', 'Annullato'),
    ], default='queued', string='Stato')

    total_count = fields.Integer('Totale righe', readonly=True)
    processed_count = fields.Integer('Elaborate', readonly=True)
    import_count = fields.Integer('Create', readonly=True)
    updated_count = fields.Integer('Aggiornate', readonly=True)
    skipped_count = fields.Integer('Saltate', readonly=True)
    error_log = fields.Text('Log Errori', readonly=True)
    started_at = fields.Datetime('Avviato il', readonly=True)
    completed_at = fields.Datetime('Completato il', readonly=True)
    report_attachment_id = fields.Many2one(
        'ir.attachment', string='Report', readonly=True, ondelete='set null',
    )

    @api.onchange('csv_file_id')
    def _onchange_csv_file_id(self):
        if self.csv_file_id:
            self.file_name = self.csv_file_id.file_name or self.csv_file_id.name
            self.delimiter = self.csv_file_id.delimiter

    def _get_file_data(self):
        """Return base64 file data from file_data field or from the library file."""
        self.ensure_one()
        if self.file_data:
            return self.file_data
        if self.csv_file_id and self.csv_file_id.file_data:
            return self.csv_file_id.file_data
        return None

    @api.onchange('preset_id')
    def _onchange_preset_id(self):
        if not self.preset_id:
            return
        self.mapping_line_ids = [(5, 0, 0)]
        lines = []
        for i, pline in enumerate(self.preset_id.line_ids):
            lines.append((0, 0, {
                'target': pline.target,
                'csv_column': pline.csv_column or '',
                'default_value': pline.default_value or '',
                'sequence': pline.sequence or (i * 10),
            }))
        self.mapping_line_ids = lines
        self.use_custom_mapping = True
        if self.preset_id.volume_unit:
            self.volume_unit = self.preset_id.volume_unit
        if self.preset_id.weight_unit:
            self.weight_unit = self.preset_id.weight_unit

    def action_save_as_preset(self):
        """Save current mapping lines as a new preset (opens a name dialog)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'inventory.mapping.preset.save.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_job_id': self.id},
        }

    def action_refresh(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_download_report(self):
        self.ensure_one()
        if not self.report_attachment_id:
            return
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%d?download=true' % self.report_attachment_id.id,
            'target': 'self',
        }

    def action_parse_headers(self):
        """Read CSV headers and populate mapping_line_ids with auto-detected targets."""
        self.ensure_one()
        raw = self._get_file_data()
        if not raw:
            raise UserError('Seleziona prima un file CSV dalla libreria.')

        file_content = base64.b64decode(raw)
        try:
            file_string = file_content.decode('utf-8-sig')
        except UnicodeDecodeError:
            file_string = file_content.decode('latin-1')

        # Read only the first logical CSV row via csv.reader, which correctly
        # handles quoted fields that contain embedded newlines (e.g. "UK\nCopy").
        reader = csv.reader(io.StringIO(file_string), delimiter=self.delimiter)
        headers = next(reader, [])
        if not headers:
            raise UserError('Nessuna intestazione trovata nel CSV.')

        # Drop existing lines and recreate: one row per CSV column found in the file.
        self.mapping_line_ids.unlink()

        lines = []
        for i, csv_col in enumerate(headers):
            tgt = _AUTO_DETECT_MAP.get(csv_col, '_attribute')
            if tgt == '_skip':
                tgt = '_attribute'
            lines.append({
                'job_id': self.id,
                'target': tgt,
                'csv_column': csv_col,
                'sequence': i * 10,
            })

        self.env['inventory.field.mapping.line'].create(lines)
        self.use_custom_mapping = True

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_run(self):
        """Decode CSV, commit job to DB, then start background thread."""
        self.ensure_one()

        self._ensure_volume_precision()

        raw = self._get_file_data()
        if not raw:
            raise UserError('Seleziona un file CSV dalla libreria prima di avviare l\'import.')
        file_content = base64.b64decode(raw)
        try:
            file_string = file_content.decode('utf-8-sig')
        except UnicodeDecodeError:
            file_string = file_content.decode('latin-1')

        custom_mapping = []
        if self.use_custom_mapping:
            custom_mapping = [
                (line.csv_column or '', line.target, line.default_value or '')
                for line in self.mapping_line_ids
            ]
            if not custom_mapping:
                raise UserError(
                    'Il mapping personalizzato è attivo ma non contiene righe. '
                    'Aggiungi almeno una riga di mapping o disattiva "Mapping personalizzato".'
                )
            # At least one line must map to 'name' or 'default_code' (or provide a default)
            # so that products can be identified/created
            has_identifier = any(
                t in ('name', 'default_code') and (col or dflt)
                for col, t, dflt in custom_mapping
                if t not in ('_skip',)
            )
            if not has_identifier:
                raise UserError(
                    'Il mapping personalizzato deve contenere almeno una colonna mappata a '
                    '"Nome prodotto [name]" o "Codice interno [default_code]" '
                    'per poter identificare i prodotti.'
                )

        options = dict(
            delimiter=self.delimiter,
            update_existing=self.update_existing,
            images_only=self.images_only,
            image_delay=max(self.image_delay, 0),
            max_workers=max(1, self.max_workers or 4),
            image_workers=max(1, self.image_workers or 1),
            import_attributes=self.import_attributes,
            import_descriptions=self.import_descriptions,
            import_tracking=self.import_tracking,
            import_qty=self.import_qty,
            location_id=self.location_id.id if self.location_id else False,
            use_custom_mapping=self.use_custom_mapping,
            custom_mapping=custom_mapping,
            volume_unit=self.volume_unit or 'cm',
            weight_unit=self.weight_unit or 'g',
        )

        self.write({'state': 'running', 'started_at': fields.Datetime.now()})
        self.env.cr.commit()

        cancel_event = threading.Event()
        with _CANCEL_LOCK:
            _CANCEL_EVENTS[self.id] = cancel_event

        thread = threading.Thread(
            target=_run_in_background,
            args=(self.env.cr.dbname, self.env.uid, self.id, file_string, options, cancel_event),
            daemon=True,
        )
        thread.start()

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_cancel(self):
        """Request graceful cancellation of the running import job.

        Sets the threading.Event so the background thread exits at its next
        checkpoint, then immediately marks the job as cancelled so the UI
        reflects the change without waiting for the thread to reach a checkpoint.
        """
        self.ensure_one()
        with _CANCEL_LOCK:
            ev = _CANCEL_EVENTS.get(self.id)
        if ev:
            ev.set()
            _logger.info('Import job %d: cancellation requested by user', self.id)
        self.write({
            'state': 'cancelled',
            'completed_at': fields.Datetime.now(),
            'error_log': 'Import annullato dall\'utente.',
        })

    def _register_hook(self):
        """Recover jobs stuck in 'running' state after a server restart."""
        super()._register_hook()
        stuck = self.search([('state', '=', 'running')])
        if stuck:
            stuck.write({
                'state': 'error',
                'completed_at': fields.Datetime.now(),
                'error_log': 'Job interrotto: il server è stato riavviato durante l\'elaborazione.',
            })
            _logger.warning('Recovered %d stuck import job(s) after server restart.', len(stuck))

    def _ensure_volume_precision(self):
        """Ensure the 'Volume' decimal precision is at least 6 digits."""
        DecimalPrecision = self.env['decimal.precision']
        dp = DecimalPrecision.search([('name', '=', 'Volume')], limit=1)
        if dp:
            if dp.digits < 6:
                dp.write({'digits': 6})
                _logger.info('Updated Volume decimal precision to 6 digits')
        else:
            DecimalPrecision.create({'name': 'Volume', 'digits': 6})
            _logger.info('Created Volume decimal precision with 6 digits')
