"""
Specifiche tecniche dagli attributi del Catalog API di Amazon (2026-11-17).

Amazon ha attributi strutturati e tradotti per ogni marketplace (mulinelli:
gearbox_ratio, bearing, fishing_reel_maximum_drag…; fili: line_size,
breaking_strength…), ma sporchi: unità miste (libbre, once, iarde),
valori in altre lingue anche sul catalogo IT ("Spinnfischen"), campi
riempiti a caso. Qui un elenco curato per tipo di prodotto, con:
- numeri in unità uniformi (g/kg, mm, m), scartati se fuori scala, scritti
  in tutte le lingue del sito col separatore decimale della lingua;
- valori fissi (tecnica, mano, tipo di filo, potenza) riconosciuti in
  qualsiasi lingua e tradotti con le tabelle qui sotto: vanno bene per i
  filtri del sito;
- testi liberi (colore, materiale, specie…) presi così come sono, solo
  nella lingua del marketplace che li fornisce.
"""
import re

LB_KG, OZ_G, YD_M, IN_CM, FT_M = 0.453592, 28.3495, 0.9144, 2.54, 0.3048

from odoo.addons.ke_product_catalog.spec_rules import (  # noqa: F401  (riesportati)
    HANDS, LINE_TYPES, POWER, SPEC_KEYS, TECHNIQUES, render, render_own,
)

_REEL_TYPES = ('FISHING_REEL', 'FISHING_ROD_REEL_COMBO')
_ROD_TYPES = ('FISHING_ROD', 'FISHING_ROD_REEL_COMBO')


# ---------------------------------------------------------------------------
# Lettura degli attributi Amazon
# ---------------------------------------------------------------------------

def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value if isinstance(value, dict) else None


def _entry(attrs, key, sub=None):
    e = _first(attrs.get(key))
    if sub and isinstance(e, dict):
        e = _first(e.get(sub))
    return e if isinstance(e, dict) else None


def _text(attrs, key, sub=None):
    e = _entry(attrs, key, sub)
    v = e.get('value') if e else None
    return str(v).strip() if v not in (None, '') else ''


def _number(attrs, key, sub=None):
    e = _entry(attrs, key, sub)
    if not e:
        return None, ''
    try:
        return float(str(e.get('value')).replace(',', '.')), (e.get('unit') or '').lower()
    except (TypeError, ValueError):
        return None, ''


def _to_grams(v, unit):
    return {'grams': v, 'kilograms': v * 1000, 'ounces': v * OZ_G, 'pounds': v * LB_KG * 1000,
            'milligrams': v / 1000}.get(unit)


def _to_kg(v, unit):
    return {'kilograms': v, 'pounds': v * LB_KG, 'grams': v / 1000}.get(unit)


def _to_m(v, unit):
    return {'meters': v, 'yards': v * YD_M, 'feet': v * FT_M, 'centimeters': v / 100,
            'inches': v * IN_CM / 100, 'millimeters': v / 1000}.get(unit)


def _to_mm(v, unit):
    return {'millimeters': v, 'centimeters': v * 10, 'inches': v * 25.4}.get(unit)


def _vocab(value, table):
    from odoo.addons.ke_product_catalog.spec_rules import vocab
    return vocab(value, table)


# ---------------------------------------------------------------------------
# Valori: ('num', numero, unità, cifre) | ('vocab', [id], tabella) | ('text', testo)
# ---------------------------------------------------------------------------

def extract(product_type, attrs, name=''):
    """{codice specifica: valore} da un dizionario 'attributes' del Catalog API."""
    pt = product_type or ''
    out = {}
    tech = _vocab(_text(attrs, 'fishing_technique'), TECHNIQUES)
    if tech:
        out['technique'] = ('vocab', tech, TECHNIQUES)
    species = _text(attrs, 'target_species')
    if species:
        out['species'] = ('text', species[:60])
    color = _text(attrs, 'color')
    if color and color.lower() not in ('assortito', 'multicolore', 'vari', 'varie'):
        out['color'] = ('text', color[:40])
    material = _text(attrs, 'material')
    if material and len(material) <= 30:
        out['material'] = ('text', material)

    w, unit = _number(attrs, 'item_weight')
    grams = _to_grams(w, unit) if w else None
    min_g = 30 if pt in _REEL_TYPES + _ROD_TYPES else 0.1
    # Piombi: il peso Amazon è spesso quello della confezione, vale la grammatura.
    if grams and min_g <= grams <= 20000 and pt != 'FISHING_WEIGHT':
        out['weight'] = ('num', grams, 'g', 1) if grams < 1000 else ('num', grams / 1000, 'kg', 2)
    pieces, _u = _number(attrs, 'number_of_pieces')
    if not pieces:
        pieces, _u = _number(attrs, 'number_of_items')
    if pieces and pieces > 1:
        out['pieces'] = ('num', int(pieces), '', 0)

    if pt in _REEL_TYPES:
        ratio, _u = _number(attrs, 'gearbox_ratio')
        if ratio and 2 <= ratio <= 10:
            out['gear_ratio'] = ('num', ratio, ':1', 1)
        bearings, _u = _number(attrs, 'bearing', 'number')
        if bearings and 1 <= bearings <= 20:
            out['bearings'] = ('num', int(bearings), '', 0)
        drag, unit = _number(attrs, 'fishing_reel_maximum_drag')
        kg = _to_kg(drag, unit) if drag else None
        if kg and 0.5 <= kg <= 60:
            out['max_drag'] = ('num', kg, 'kg', 1)
        capacity = re.sub(r'\[[^\]]*\]|\([^)]*\)|"', '', _text(attrs, 'line_capacity')).strip(' -,;')
        if capacity and re.search(r'\d', capacity) and '?' not in capacity:
            out['line_capacity'] = ('text', capacity[:60])
        hand = _vocab(_text(attrs, 'hand_orientation'), HANDS)
        if hand:
            out['hand'] = ('vocab', hand[:1], HANDS)

    if pt == 'FISHING_LINE':
        line_type = _vocab(_text(attrs, 'fishing_line_type') + ' ' + _text(attrs, 'material'), LINE_TYPES)
        if line_type:
            out['line_type'] = ('vocab', line_type[:1], LINE_TYPES)
        mm, unit = _number(attrs, 'line_size')
        mm = _to_mm(mm, unit) if mm else None
        if not mm:
            m = re.search(r'\b0[.,](\d{2,3})\s*mm', _text(attrs, 'size'), re.I)
            mm = float('0.' + m.group(1)) if m else None
        if mm and 0.03 <= mm <= 3:
            out['diameter'] = ('num', mm, 'mm', 3)
        strength, unit = _number(attrs, 'breaking_strength')
        kg = _to_kg(strength, unit) if strength else None
        if kg and 0.3 <= kg <= 300:
            out['breaking_strength'] = ('num', kg, 'kg', 1)
        length, unit = _number(attrs, 'item_length')
        meters = _to_m(length, unit) if length else None
        # "1.000" col punto delle migliaia arriva come 1,0: vale la lunghezza nel nome.
        named = re.search(r'\b(\d{2,4})\s?(?:m|mt|metri|meters|metros|mètres)\b', name or '', re.I)
        if named and (not meters or meters < 10):
            meters = float(named.group(1))
        if meters and 1 <= meters <= 5000:
            out['length'] = ('num', meters, 'm', 0)
        strands = re.search(r'(\d+)', _text(attrs, 'strand', 'type'))
        if strands and 2 <= int(strands.group(1)) <= 16:
            out['strands'] = ('num', int(strands.group(1)), '', 0)

    if pt in _ROD_TYPES:
        power = _text(attrs, 'fishing_rod_power')
        if power in POWER:
            out['rod_power'] = ('text_all', POWER[power])
        length, unit = _number(attrs, 'item_length')
        meters = _to_m(length, unit) if length else None
        if meters and 1 <= meters <= 15:
            out['length'] = ('num', meters, 'm', 2)
        if pt == 'FISHING_ROD' and pieces and 1 <= pieces <= 10:
            out['sections'] = ('num', int(pieces), '', 0)
            out.pop('pieces', None)

    if pt == 'FISHING_HOOK':
        point = _text(attrs, 'point', 'style')
        if point:
            out['point'] = ('text', point[:40])
        hook_type = _text(attrs, 'fishing_hook_type')
        if hook_type:
            out['hook_type'] = ('text', hook_type[:40])
        size = _text(attrs, 'size')
        if size and re.search(r'\d', size):
            out['size'] = ('text', size[:40])

    if pt == 'FISHING_WEIGHT':
        m = re.search(r'(\d+[.,]?\d*)\s*(gr|g)\b', _text(attrs, 'size'), re.I)
        if m:
            out['sinker_weight'] = ('num', float(m.group(1).replace(',', '.')), 'g', 1)
        shape = _text(attrs, 'item_shape')
        if shape:
            out['shape'] = ('text', shape[:30])
    return out
