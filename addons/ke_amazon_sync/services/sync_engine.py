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
import threading
import time
from datetime import datetime, timezone

import requests

from odoo import fields
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


# Una sessione HTTP per thread, riusata da tutte le immagini: con
# requests.get() ogni immagine apriva una connessione nuova (DNS, TCP,
# handshake TLS e caricamento dei certificati ogni volta) — misurato il
# 2026-11-03 sul Full Import: ~3,5 s ogni 30 prodotti solo di connessioni.
# Per thread perché Full Import e "Scarica immagini mancanti" possono girare
# insieme e requests.Session non è garantita thread-safe.
_http_local = threading.local()


def _http_session():
    session = getattr(_http_local, 'session', None)
    if session is None:
        session = _http_local.session = requests.Session()
    return session


# Ruoli delle immagini del Catalog API in ordine di preferenza per
# l'immagine principale del prodotto: MAIN (principale), poi la prima della
# galleria (PT01), poi il campione colore (SWCH) — per i prodotti che su
# Amazon non hanno una MAIN (verificato il 2026-11-04: esistono, es.
# B0CQ3ZZ3S5 ha solo PT01…PT06).
_IMAGE_ROLE_PREFERENCE = ('MAIN', 'PT01', 'SWCH')


def _best_image_url(catalog):
    """
    Indirizzo dell'immagine principale da usare per un articolo del Catalog
    API: il ruolo preferito disponibile (_IMAGE_ROLE_PREFERENCE) e, di quel
    ruolo, la versione PIÙ GRANDE. Amazon restituisce ogni immagine in più
    dimensioni (es. 75x75, 500x500, 1000x1000) in un ordine non fisso —
    verificato il 2026-11-04: per lo stesso ASIN la prima MAIN era una volta la
    1000x1000, un'ora dopo la 500x500. Prima si prendeva la prima trovata,
    quindi a caso una delle tre, miniatura 75x75 compresa.
    """
    images = [i for block in (catalog or {}).get('images') or [] for i in block.get('images') or []
              if i.get('link')]
    for role in _IMAGE_ROLE_PREFERENCE:
        candidates = [i for i in images if i.get('variant') == role]
        if candidates:
            best = max(candidates, key=lambda i: (i.get('width') or 0) * (i.get('height') or 0))
            return best['link']
    return ''


def _image_role_order(role):
    """Ordine della galleria: MAIN, poi PT01…PTnn, poi SWCH, poi gli altri ruoli."""
    role = role or ''
    if role == 'MAIN':
        return (0, 0, '')
    if role.startswith('PT') and role[2:].isdigit():
        return (1, int(role[2:]), '')
    if role == 'SWCH':
        return (2, 0, '')
    return (3, 0, role)


def _gallery_images(catalog):
    """
    Tutte le immagini di un articolo del Catalog API, una per ruolo (la
    versione più grande, come _best_image_url), in ordine di galleria. Per
    la galleria di ke_product_catalog (ke.product.media): solo indirizzi,
    niente download.
    """
    best_by_role = {}
    for block in (catalog or {}).get('images') or []:
        for image in block.get('images') or []:
            role, link = image.get('variant') or '', image.get('link')
            if not link:
                continue
            size = (image.get('width') or 0) * (image.get('height') or 0)
            current = best_by_role.get(role)
            if current is None or size > (current.get('width') or 0) * (current.get('height') or 0):
                best_by_role[role] = image
    return [
        {'url': i['link'], 'role': role or False, 'width': i.get('width') or 0, 'height': i.get('height') or 0}
        for role, i in sorted(best_by_role.items(), key=lambda item: _image_role_order(item[0]))
    ]


# Separatori in coda al nome comune di una famiglia (es. "Carrete Test 1000, gris"
# e "Carrete Test 2000, rojo" → "Carrete Test").
_NAME_TRAILING = ' ,;:-–—|/(['
_NAME_SEGMENT_SPLIT = re.compile(r'\s*(?:,|\s[-–—|]\s|\()\s*')


# Parole che non possono chiudere il nome di una famiglia: la parte comune ai
# titoli si ferma spesso subito dopo (es. "Abu Garcia Reel Grease by").
_NAME_DANGLING = {
    'by', 'with', 'for', 'and', 'the', 'of', 'di', 'da', 'per', 'con', 'e', 'il', 'la', 'le', 'del', 'della',
    'de', 'para', 'con', 'y', 'el', 'pour', 'avec', 'et', 'du', 'des',
}


def _tidy_family_name(name):
    """
    Toglie in coda le parole sospese (_NAME_DANGLING) e un'etichetta rimasta
    sola dopo l'ultimo separatore ("AKAMI IKEGAMI | COLOR"), se il nome che
    resta ha almeno 2 parole e 12 caratteri (2026-11-06).
    """
    name = name.rstrip(_NAME_TRAILING).strip()
    while True:
        words = name.split()
        if len(words) > 2 and words[-1].casefold() in _NAME_DANGLING:
            name = ' '.join(words[:-1]).rstrip(_NAME_TRAILING).strip()
            continue
        parts = list(_NAME_SEGMENT_SPLIT.finditer(name))
        if parts:
            head, tail = name[:parts[-1].start()].strip(), name[parts[-1].end():].strip()
            if tail and len(tail.split()) == 1 and len(head.split()) >= 2 and len(head) >= 12:
                name = head.rstrip(_NAME_TRAILING).strip()
                continue
        return name


def _family_common_name(titles, variant_values=()):
    """
    Nome di una famiglia dai titoli delle sue varianti in UNA lingua: la parte
    iniziale comune, a parole intere, senza separatori in coda. Con un solo
    titolo (o parte comune troppo corta: < 2 parole o < 12 caratteri) si
    parte dal primo titolo e si tolgono in coda il suffisso tra parentesi e i
    segmenti (separati da virgola, " - ", "(") che contengono i valori di
    variante noti (colore, taglia…). Stesso metodo per tutte le lingue.
    """
    titles = [t.strip() for t in titles if t and t.strip()]
    if not titles:
        return ''
    if len(titles) > 1:
        words = [t.split() for t in titles]
        common = []
        for group in zip(*words):
            if all(w.casefold() == group[0].casefold() for w in group):
                common.append(group[0])
            else:
                break
        name = _tidy_family_name(' '.join(common))
        if len(name.split()) >= 2 and len(name) >= 12:
            return name
    name = _strip_variant_suffix(titles[0])
    values = [v.casefold().strip() for v in variant_values if v and len(v.strip()) >= 2]
    if values:
        bounds, last = [], 0
        for m in _NAME_SEGMENT_SPLIT.finditer(name):
            bounds.append((last, m.start()))
            last = m.end()
        bounds.append((last, len(name)))
        while len(bounds) > 1:
            seg = name[bounds[-1][0]:bounds[-1][1]].strip().casefold()
            if seg and any(v in seg or (len(seg) >= 2 and seg in v) for v in values):
                bounds.pop()
            else:
                break
        name = name[:bounds[-1][1]]
    return _tidy_family_name(name) or titles[0]


def _fetch_image_b64(url):
    """Downloads an image URL and returns base64-encoded bytes, or None on failure."""
    if not url:
        return None
    try:
        resp = _http_session().get(url, timeout=10)
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


def _extract_spec_attrs(attrs, lang=None):
    """Returns {label: value} for known spec attributes found in an Amazon attrs dict."""
    result = {}
    for key, label in _SPEC_ATTRIBUTES.items():
        val = _cat_val(attrs, key, lang)
        if val:
            result[label] = val
    return result


# ---------------------------------------------------------------------------
# Helpers condivisi
# ---------------------------------------------------------------------------

def _parse_listing(listing, lang=None):
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
    bullets = _bullets(attributes, lang)
    return {
        'sku': (listing.get('sku') or '').strip(),
        'asin': (summary.get('asin') or '').strip(),
        'name': summary.get('itemName', '') or _cat_val(attributes, 'item_name', lang),
        'status': ', '.join(summary.get('status', [])),
        'product_type': summary.get('productType', ''),
        'price': price or 0.0,
        'fulfillment': 'fba' if channel == 'AMAZON_NA' else 'fbm',
        'attributes': attributes,
        'description': _cat_val(attributes, 'product_description', lang),
        'bullet_points': bullets,
        'spec_attrs': _extract_spec_attrs(attributes, lang),
    }


def _merge_catalog(product, catalog, lang=None):
    """
    Arricchisce il dict prodotto con i dati del Catalog v2022. 'lang' è il
    locale della chiamata (lingua del marketplace): sceglie descrizione,
    bullet point e specifiche in quella lingua quando Amazon ne manda più.
    """
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
    cat_desc = _cat_val(cat_attrs, 'product_description', lang)
    if cat_desc:
        product['description'] = cat_desc
    cat_bullets = _bullets(cat_attrs, lang)
    if cat_bullets:
        product['bullet_points'] = cat_bullets

    # Specifiche dal catalog (integrano quelle del listing)
    cat_specs = _extract_spec_attrs(cat_attrs, lang)
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

    # Immagine principale: ruolo e dimensione migliori (vedi _best_image_url)
    product['image_url'] = _best_image_url(catalog)
    product['images'] = _gallery_images(catalog)

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


def _norm_lang(tag):
    return (tag or '').replace('-', '_').lower()


def _lang_entries(entries, lang=None):
    """
    Voci di un attributo Amazon nella lingua 'lang' (es. 'es_ES'), se ce ne
    sono; altrimenti tutte. Il Catalog API può restituire lo stesso
    attributo (descrizione, bullet point) in più lingue, ciascuna con il suo
    'language_tag': prima si prendeva sempre la prima voce, che non è
    detto sia quella del marketplace (2026-11-04: descrizioni ES/FR/UK in
    italiano). Le voci senza language_tag (brand, numeri) restano valide.
    """
    if not entries or not isinstance(entries, list):
        return []
    if lang:
        wanted = _norm_lang(lang)
        matching = [e for e in entries if _norm_lang(e.get('language_tag')) == wanted]
        if matching:
            return matching
    return entries


def _cat_val(attrs, key, lang=None):
    val = _lang_entries(attrs.get(key, []), lang)
    if val:
        return str(val[0].get('value', ''))
    return ''


def _bullets(attrs, lang=None):
    return [str(v.get('value', '')) for v in _lang_entries(attrs.get('bullet_point', []), lang) if v.get('value')]


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
    """
    Categoria Odoo di un prodotto nuovo dalla sua categoria Amazon, tramite
    le corrispondenze (services/category_map.py). Dal 2026-11-10 non crea
    più una categoria Odoo per ogni nome Amazon (una per lingua: 440 su
    odoo_2); un nome nuovo va nella categoria predefinita e compare fra le
    corrispondenze da completare.
    """
    from .category_map import CategoryMapper
    return CategoryMapper(env).category_for(name)


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


def _needs_catalog_data(template, lang=None):
    """
    True se a questo product.template manca almeno uno dei campi che il
    Catalog API può fornire — stessa condizione usata da
    apply_category_mapping ("Aggiorna da Catalog API") e, da oggi, anche da
    full_import_report_rows per decidere se vale la pena ri-chiamare il
    Catalog API per un ASIN che corrisponde già a un prodotto esistente
    (vedi lì: niente di nuovo da imparare se è già tutto popolato).
    """
    return (
        not template.x_amazon_category
        or not template.product_length
        or not template.x_amazon_product_type
        or not template.x_amazon_browse_node_id
        or not template.x_amazon_brand
        or not _has_text_in_lang(template, 'description_sale', lang)
    )


def _has_text_in_lang(template, field, lang=None):
    """
    True se 'field' (traducibile) ha un testo proprio nella lingua 'lang'.
    Senza 'lang' basta il valore normale. Con 'lang' serve la colonna JSON
    grezza: letto con with_context(lang=...) Odoo restituisce il testo di
    ripiego di un'altra lingua, e la descrizione italiana bastava a far
    saltare la chiamata Catalog per ES/FR/UK — per questo le traduzioni
    non arrivavano mai (2026-11-04, solo 714 descrizioni es_ES su 2.835).
    """
    if not lang:
        return bool(template[field])
    template.flush_recordset([field])
    template.env.cr.execute(
        f'SELECT ({field}->>%s) FROM product_template WHERE id = %s', (lang, template.id),
    )
    row = template.env.cr.fetchone()
    return bool(row and row[0])


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


# Larghezze standard GTIN usate per confrontare EAN/UPC a prescindere dal
# padding di zeri iniziali — vedi _normalize_ean.
_GTIN_WIDTHS = (8, 12, 13, 14)


def _normalize_ean(code):
    """
    Forma canonica di un EAN/UPC per il confronto (chiavi delle mappe di
    matching): zeri iniziali tolti. Un UPC-A a 12 cifre e un EAN-13 con uno
    zero di padding davanti sono lo stesso codice, solo rappresentati con
    larghezze diverse — senza normalizzare, un confronto di stringa esatta
    non li riconosce mai uguali. Trovato dal vivo il 2026-10-23: un prodotto
    manuale con barcode '028632326992' (12 cifre) non si univa mai alla sua
    variante Amazon reale con barcode '0028632326992' (13 cifre, stesso
    codice) — 577 prodotti con l'esatto stesso pattern nel catalogo.
    Un barcode non puramente numerico (codice interno, non EAN reale) passa
    invariato: non è un GTIN, niente zeri di padding da normalizzare.
    """
    code = (code or '').strip()
    if not code or not code.isdigit():
        return code
    return code.lstrip('0') or '0'


def _ean_search_variants(code):
    """
    Rappresentazioni possibili di un EAN/UPC già memorizzate su un prodotto
    Odoo, per una ricerca live sul DB (_barcode_free) dove non si può usare
    la forma canonica di _normalize_ean su un campo già scritto in formati
    diversi da prodotto a prodotto. Codice as-is + forma nuda (zeri tolti) +
    ripadded alle larghezze GTIN standard.
    """
    code = (code or '').strip()
    if not code:
        return set()
    if not code.isdigit():
        return {code}
    bare = _normalize_ean(code)
    variants = {code, bare}
    for width in _GTIN_WIDTHS:
        if len(bare) <= width:
            variants.add(bare.rjust(width, '0'))
    return variants


# Contesto per le scritture massive dell'import: niente messaggio automatico
# "prodotto creato", niente iscrizione dell'utente ai follower e niente
# tracciamento dei campi nello storico (mail.thread) per ognuno delle
# migliaia di prodotti — misurato il 2026-11-03: fino a ~5 s ogni 30 prodotti
# creati. Le note scritte apposta con message_post (es. le unioni con il
# gestionale) restano: message_post non dipende da queste chiavi (verificato
# su mail/models/mail_thread.py).
_IMPORT_MAIL_CTX = {
    'tracking_disable': True, 'mail_create_nolog': True,
    'mail_create_nosubscribe': True, 'mail_notrack': True,
}

# Errori di concorrenza di PostgreSQL per cui ripetere una riga ha senso
# (deadlock con un altro processo, conflitto di serializzazione): l'import
# del 2026-11-03 è caduto per un deadlock con il cron "Aggiorna Vista
# Inventario" che aggiornava gli stessi prodotti.
_RETRYABLE_PGCODES = {'40P01', '40001'}
_ROW_RETRY_MAX = 2


def _is_grade_and_resell_sku(sku):
    """
    True per uno SKU creato da Amazon con il programma Grade & Resell
    (`amzn.gr.<SKU originale>-<codice>-<LN|VG|…>`): resi rivenduti come usati
    a nome del venditore, sullo STESSO ASIN/EAN del suo SKU. Non sono un
    prodotto del venditore: se entrano nell'abbinamento per ASIN/EAN prendono
    il posto dello SKU vero (SKU legacy, listing del marketplace — una sola
    per variante+marketplace) e lo SKU vero finisce su un prodotto doppione
    senza ASIN. Caso reale verificato su Seller Central il 2026-11-03:
    MGP-KIT42015 attivo in IT con 9 FBA, in Odoo coperto da
    amzn.gr.MGP-KIT42015-lZ9ONo9Bvr79U2ay-LN. Decisione con l'utente:
    ignorarli in tutti i flussi di import (vedi TASK.md).
    """
    return (sku or '').strip().lower().startswith('amzn.gr.')


def _grade_and_resell_log_line(skus, prefix='', tracked=False):
    """
    Una riga di log riassuntiva (non una per SKU) per gli SKU Grade & Resell
    non trattati come prodotti. 'tracked': registrati come offerte usato
    (ke.amazon.resell.offer) invece che solo saltati.
    """
    if not skus:
        return None
    shown = ', '.join(sorted(skus)[:5])
    more = f' e altri {len(skus) - 5}' if len(skus) > 5 else ''
    what = 'registrati come offerte usato' if tracked else 'saltati'
    return (f'{prefix}{len(skus)} SKU Grade & Resell (usato rivenduto da Amazon) {what}, '
            f'non come prodotti: {shown}{more}')


def _parse_open_date(value):
    """
    open-date del report annunci ("08/10/2026 15:56:15 MEST", in altri
    marketplace "2026-10-08 15:56:15 PDT"): data e ora così come sono, fuso
    orario ignorato (serve solo a confrontare gli SKU dello stesso report).
    None se vuota o non leggibile.
    """
    parts = (value or '').split()
    if not parts:
        return None
    for text, fmt in ((' '.join(parts[:2]), '%d/%m/%Y %H:%M:%S'), (' '.join(parts[:2]), '%Y-%m-%d %H:%M:%S'),
                      (parts[0], '%d/%m/%Y'), (parts[0], '%Y-%m-%d')):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class _MatchMaps:
    """
    Raggruppa tutte le mappe di matching SKU/ASIN/EAN usate da full_import/
    full_import_report_rows/full_import_chunk e da _process_variant_listing,
    con un punto unico per leggerle (find_*) e aggiornarle (register_*).

    Nata dal nono/decimo/undicesimo/dodicesimo bug (2026-10-09/12/13, vedi
    TASK.md): tutti causati dalla stessa dinamica — una mappa costruita una
    volta a inizio chunk, tenuta sincronizzata a mano con righe sparse nei
    due rami (prodotto semplice / variante), e puntualmente dimenticata in
    uno dei due quando se ne aggiungeva una nuova o se ne cambiava la
    semantica. Centralizzare qui lettura e scrittura non elimina la
    possibilità di un bug, ma lo confina a UN punto invece che a N punti
    sincronizzati manualmente.
    """

    def __init__(self, env, by_barcode):
        self.by_sku = {
            p.x_amazon_sku: p for p in
            env['product.template'].search([('x_amazon_sku', '!=', False)])
        }
        self.by_asin = {
            p.x_amazon_asin: p for p in
            env['product.template'].search([('x_amazon_asin', '!=', False)])
        }
        self.by_default_code = {
            p.default_code: p for p in
            env['product.template'].search([('default_code', '!=', False)])
            if p.default_code
        }
        # Costruita a parte (SyncEngine._build_barcode_map(), serve self)
        # e passata qui invece di ricostruita, per non duplicare la query.
        self.by_barcode = by_barcode
        self.by_parent_asin = {
            p.x_amazon_parent_asin: p for p in
            env['product.template'].search([('x_amazon_parent_asin', '!=', False)])
        }
        self.variant_by_asin = {
            v.x_amazon_asin: v for v in
            env['product.product'].search([('x_amazon_asin', '!=', False)])
        }
        # Varianti di famiglia indicizzate per SKU (non solo per ASIN): serve
        # a riconoscere uno SKU già appartenente a una famiglia anche quando
        # la riga del report ha l'ASIN vuoto (quirk noto del report bulk
        # Amazon — verificato dal vivo via Listings API che il listing
        # l'ASIN ce l'ha davvero, il report a volte no). x_amazon_sku su
        # product.product è scritto solo per le varianti di famiglia, mai
        # per prodotti semplici — nessun falso positivo possibile qui.
        self.variant_tmpl_by_sku = {
            v.x_amazon_sku: v.product_tmpl_id for v in
            env['product.product'].search([('x_amazon_sku', '!=', False)])
        }
        # Anche il codice interno delle varianti di famiglia: Amazon può
        # avere due SKU sullo stesso ASIN ("TUB-20104" e "TUB-20104-in
        # comune", "SAV-64565" e "SAV-64565."), x_amazon_sku tiene l'ultimo
        # letto, default_code resta il primo. Senza questo la riga FR senza
        # ASIN con l'altro SKU non trovava la famiglia e creava un prodotto
        # doppio (25 casi nell'import di arundo_prod del 2026-11-04).
        for v in env['product.product'].search([
            ('default_code', '!=', False), ('product_tmpl_id.x_amazon_parent_asin', '!=', False),
        ]):
            self.variant_tmpl_by_sku.setdefault(v.default_code, v.product_tmpl_id)

    def add_listing_fallback(self, listings):
        """
        Fase 3 multi-marketplace: un prodotto creato/aggiornato SOLO da un
        marketplace non primario non ha i campi legacy x_amazon_* popolati
        (vedi SyncEngine._is_primary_marketplace) — senza questo, un secondo
        marketplace per lo stesso prodotto non lo troverebbe in by_sku/
        by_asin e ne creerebbe un duplicato. ke.amazon.listing copre tutti i
        marketplace, a prescindere da quale sia primario.
        """
        for listing in listings:
            # Una listing Grade & Resell rimasta da un import precedente
            # guiderebbe ancora l'abbinamento verso il prodotto sbagliato.
            if _is_grade_and_resell_sku(listing.sku):
                continue
            # listing.product_id è la VARIANTE (ke.amazon.listing è scoped
            # per variante, vedi amazon_listing.py) — qui serve il template,
            # come tutti gli altri valori di by_sku/by_asin.
            tmpl = listing.product_id.product_tmpl_id
            if listing.sku and listing.sku not in self.by_sku:
                self.by_sku[listing.sku] = tmpl
            if listing.asin and listing.asin not in self.by_asin:
                self.by_asin[listing.asin] = tmpl
            # L'ASIN è dell'annuncio (dal 2026-11-05): una variante di
            # famiglia con annunci su più ASIN (stesso prodotto fisico) si
            # ritrova da ognuno. Solo famiglie: per un prodotto semplice
            # variant_by_asin lo farebbe trattare come variante.
            if listing.asin and listing.asin not in self.variant_by_asin and tmpl.x_amazon_parent_asin:
                self.variant_by_asin[listing.asin] = listing.product_id

    def find_simple(self, sku, asin, ean):
        """
        Prodotto semplice esistente per sku/asin/ean/default_code, o None.
        Ritorna None anche quando il match trovato è in realtà un template
        famiglia (visibile qui solo perché Odoo espone default_code/
        x_amazon_sku del template quando la famiglia ha momentaneamente una
        sola variante) — _update_product non va MAI chiamato su una
        famiglia, le scriverebbe sopra campi "semplice" corrompendola
        (decimo bug, verificato dal vivo con un test isolato).
        """
        match = (
            self.by_sku.get(sku)
            or self.by_asin.get(asin)
            or (ean and self.by_barcode.get(_normalize_ean(ean)))
            or self.by_default_code.get(sku)
        )
        if match and match.x_amazon_parent_asin:
            return None
        return match

    def find_family_conflict(self, sku):
        """
        Stesso match di find_simple, letto al contrario: un template
        famiglia trovato per questo SKU tramite le mappe 'semplice'. Usato
        da _process_variant_listing per sapere se questo SKU appartiene GIA'
        a una famiglia (eventualmente diversa da quella indicata dalla riga
        corrente — dodicesimo bug, causa A: ASIN non più aggiornato nel
        report, due famiglie diverse trovate per lo stesso SKU in momenti
        diversi dello stesso import).
        """
        match = self.by_sku.get(sku) or self.by_default_code.get(sku)
        if match and match.x_amazon_parent_asin:
            return match
        return None

    def find_known_family(self, sku):
        """
        Ritorna il template famiglia già noto per questo SKU, usato dal ramo
        prodotto-semplice per evitare un duplicato quando la riga ha
        l'ASIN vuoto. Cerca prima tra le varianti (variant_tmpl_by_sku),
        poi come riserva tra i template stessi (stessa ricerca di
        find_family_conflict) — alcune famiglie più vecchie hanno la
        variante senza x_amazon_sku scritto (debito di una generazione di
        bug precedente, mai sanato retroattivamente), quindi il solo
        controllo su variant_tmpl_by_sku non basta: senza questa riserva,
        una riga con ASIN vuoto per quello SKU non trova nulla e crea un
        duplicato. Trovato e verificato dal vivo il 2026-10-15 (129 casi
        su un Full Import reale, tredicesimo bug).
        """
        return self.variant_tmpl_by_sku.get(sku) or self.find_family_conflict(sku)

    def find_family_by_variant_asin(self, asin):
        """
        Template famiglia che ha già una variante con questo ASIN, o None.
        Serve al ramo prodotto-semplice quando la riga non è stata
        riconosciuta come variante (chiamata Catalog API fallita, o
        relationships assenti per quel marketplace): senza questo controllo
        l'ASIN di una variante esistente diventava un prodotto semplice
        duplicato — 290 casi trovati il 2026-10-22 tra i prodotti creati dai
        marketplace secondari.
        """
        variant = self.variant_by_asin.get(asin) if asin else None
        if variant and variant.product_tmpl_id.x_amazon_parent_asin:
            return variant.product_tmpl_id
        return None

    def knows_sku(self, sku):
        """True se questo SKU porta già a un prodotto in Odoo (prodotto, variante,
        codice interno o annuncio su un marketplace)."""
        return bool(
            self.by_sku.get(sku) or self.by_default_code.get(sku)
            or self.variant_tmpl_by_sku.get(sku)
        )

    def forget(self, tmpl):
        """Toglie 'tmpl' da tutte le mappe del ramo semplice — usato dopo
        averlo archiviato (SyncEngine._merge_manual_into_variant), perché
        una riga successiva dello stesso import non lo ritrovi."""
        for mapping in (self.by_sku, self.by_asin, self.by_default_code, self.by_barcode):
            for key in [k for k, v in mapping.items() if v.id == tmpl.id]:
                del mapping[key]

    def register_simple(self, sku, asin, ean, tmpl):
        """Un solo punto per aggiornare tutte le mappe del ramo prodotto-semplice."""
        self.by_sku[sku] = tmpl
        if asin:
            self.by_asin[asin] = tmpl
        if ean:
            self.by_barcode[_normalize_ean(ean)] = tmpl
        self.by_default_code[sku] = tmpl

    def register_family(self, parent_asin, tmpl):
        self.by_parent_asin[parent_asin] = tmpl

    def register_variant(self, asin, sku, variant, tmpl):
        """Un solo punto per aggiornare le mappe del ramo variante — causa
        diretta dell'undicesimo bug quando mancava per variant_tmpl_by_sku."""
        if asin:
            self.variant_by_asin[asin] = variant
        if sku:
            self.variant_tmpl_by_sku[sku] = tmpl


class SyncEngine:
    def __init__(self, env, client):
        self.env = env
        self.client = client

    def _price_currency_name(self):
        marketplace = self._current_marketplace()
        return (marketplace.currency_id or self.env.company.currency_id).name

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

    def _upsert_listing(self, variant, data):
        """
        Crea o aggiorna l'annuncio (ke.amazon.listing) del marketplace
        corrente con lo SKU di 'data', sulla variante 'variant'. Un annuncio
        è marketplace + SKU: una variante può averne più d'uno sullo stesso
        marketplace (Amazon ammette più SKU sullo stesso ASIN), e il secondo
        SKU non sostituisce più il primo. Se lo SKU era registrato su
        un'altra variante, l'annuncio passa a quella abbinata ora. Un
        annuncio senza SKU della variante (creato dalla sync giacenze FBA)
        viene completato invece di crearne un altro. Non fa nulla se il
        marketplace corrente non è censito in ke.amazon.marketplace.

        'fnsku' non viene mai scritto da qui: è dell'annuncio (SKU +
        venditore), lo scrive solo process_fnsku_report_rows. Prima del
        2026-11-05 l'annuncio era unico per variante e cambiava SKU, e
        l'FNSKU andava invalidato a ogni cambio: ora uno SKU nuovo è un
        annuncio nuovo, quello vecchio tiene il suo FNSKU.
        """
        marketplace = self._current_marketplace()
        if not marketplace:
            return
        Listing = self.env['ke.amazon.listing'].sudo()
        sku = data.get('sku') or False
        vals = {
            'sku': sku,
            'product_type': data.get('product_type', ''),
            'status': 'active' if 'BUYABLE' in (data.get('status') or '') else 'inactive',
            'fulfillment': data.get('fulfillment'),
            'price': data.get('price') or 0.0,
            'qty': data.get('qty_available') or 0,
            'category': data.get('category', ''),
            'browse_node_id': data.get('browse_node_id', ''),
            'parent_asin': data.get('parent_asin') or False,
        }
        if data.get('brand'):
            vals['brand'] = data['brand']
        if data.get('open_date'):
            vals['open_date'] = data['open_date']
        # Una riga senza ASIN (report FR) non cancella quello già noto dell'annuncio.
        if data.get('asin'):
            vals['asin'] = data['asin']
        # Titolo: solo se presente (le righe senza titolo non cancellano quello noto).
        if data.get('name'):
            vals['item_name'] = data['name']
        listing = Listing.search([('marketplace_id', '=', marketplace.id), ('sku', '=', sku)], limit=1) if sku else Listing
        if not listing:
            listing = Listing.search([
                ('product_id', '=', variant.id), ('marketplace_id', '=', marketplace.id), ('sku', '=', False),
            ], limit=1)
        # Riga senza ASIN su un annuncio che non lo conosce ancora: quello
        # dello stesso SKU su un altro marketplace (prima restava vuoto se il
        # prodotto era trovato per SKU: 1.577 annunci FR al 2026-11-05).
        if 'asin' not in vals and not listing.asin:
            known = self._known_asin_for_sku(sku)
            if known:
                vals['asin'] = known
        if listing:
            # Annuncio abbinato a mano a un altro prodotto: resta lì (vedi
            # ke.amazon.listing.manual_product), si aggiornano solo i dati.
            if listing.product_id != variant and not listing.manual_product:
                vals['product_id'] = variant.id
            listing.with_context(ke_import_listing=True).write(vals)
        else:
            vals.update({'product_id': variant.id, 'marketplace_id': marketplace.id})
            Listing.create(vals)
        self._assign_brand(variant.product_tmpl_id, data.get('brand'))

    def _assign_brand(self, template, amazon_brand):
        """
        Marca di catalogo (ke_product_catalog) dalla marca Amazon: riconosciuta
        per nome o nome alternativo (confronto normalizzato), creata "Da
        verificare" se sconosciuta, nessuna se il nome "non è una marca". Non
        cambia una marca già confermata sul prodotto (scelta a mano o
        verificata); sostituisce solo una marca vuota o ancora "Da verificare".
        """
        if not amazon_brand or not template:
            return
        current = template.ke_brand_id
        if current and not current.to_review:
            return
        brand = self.env['ke.product.brand'].sudo().find_or_create(amazon_brand)
        if brand != current:
            template.ke_brand_id = brand

    def _attach_listing_to_existing_variant(self, product, maps):
        """
        Registra l'annuncio del marketplace corrente sulla variante che in
        Odoo ha già questo ASIN (o, in mancanza, questo SKU), senza toccare
        la struttura della famiglia. Per le righe che prima venivano solo
        saltate perché Amazon, su quel marketplace, raggruppa le famiglie in
        modo diverso (conflitto di parent_asin, o riga non riconosciuta come
        variante): la variante c'era già, ma il suo annuncio su quel
        marketplace non veniva mai registrato — import del 2026-11-03,
        ~2.700 righe ES su 8.263, 0 su IT (il primo marketplace crea le
        famiglie). Ritorna la variante, o None se non ce n'è una sicura.
        """
        if not self._current_marketplace():
            return None
        sku, asin = product.get('sku'), product.get('asin')
        variant = maps.variant_by_asin.get(asin) if asin else None
        if not variant and sku:
            Product = self.env['product.product']
            variant = (
                Product.search([('x_amazon_sku', '=', sku)], limit=1)
                or Product.search([('default_code', '=', sku)], limit=1)
                or self.env['ke.amazon.listing'].sudo().search([('sku', '=', sku)], limit=1).product_id
            )
        if not variant:
            return None
        if not product.get('asin'):
            product['asin'] = self._known_asin_for_sku(sku)
        self._upsert_listing(variant, product)
        self._maybe_switch_variant_sku(variant, product)
        self._write_family_text(variant.product_tmpl_id, product)
        return variant

    def _maybe_switch_variant_sku(self, variant, data):
        """
        Righe collegate a una variante esistente (famiglia diversa, riga
        senza ASIN): lo SKU principale della variante segue la stessa regola
        dell'aggiornamento normale (_switch_main_sku), es. sul marketplace
        principale arriva la versione "+" di una variante creata da UK con
        il vecchio SKU. Solo varianti di famiglia (per i prodotti semplici lo
        SKU è sul template e lo gestisce _update_product). L'FNSKU della
        variante si azzera: era del vecchio SKU, l'Import FNSKU (incatenato
        al Full Import) mette quello nuovo.
        """
        sku = data.get('sku')
        current = variant.x_amazon_sku
        if not sku or sku == current or not variant.product_tmpl_id.x_amazon_parent_asin:
            return
        if not self._switch_main_sku(current, sku, data.get('status')):
            return
        vals = {'x_amazon_sku': sku, 'x_amazon_fnsku': False}
        if not variant.default_code or variant.default_code == current:
            vals['default_code'] = sku
        variant.write(vals)

    def _attach_listing_by_sku(self, product, family_tmpl):
        """
        Riga del report SENZA ASIN il cui SKU è esattamente quello di una
        variante della famiglia 'family_tmpl': registra l'annuncio del
        marketplace corrente su quella variante (ASIN preso dalla variante),
        senza toccare il template — prima la riga veniva solo saltata. Nel
        report FR del 2026-11-04 l'ASIN mancava per 4.051 righe su 8.254,
        quasi tutte varianti di famiglia: senza questo l'annuncio FR non
        veniva mai registrato. Ritorna la variante, o None.
        """
        sku = product.get('sku')
        if not sku or not family_tmpl or not self._current_marketplace():
            return None
        variants = family_tmpl.product_variant_ids
        variant = (
            variants.filtered(lambda v: v.x_amazon_sku == sku)[:1]
            or variants.filtered(lambda v: v.default_code == sku)[:1]
            or self.env['ke.amazon.listing'].sudo().search([
                ('sku', '=', sku), ('product_id', 'in', variants.ids),
            ], limit=1).product_id
        )
        if not variant:
            return None
        if not product.get('asin'):
            product['asin'] = self._known_asin_for_sku(sku)
        self._upsert_listing(variant, product)
        self._maybe_switch_variant_sku(variant, product)
        return variant

    def _known_asin_for_sku(self, sku):
        """
        ASIN di una riga senza ASIN: quello dello stesso SKU su un altro
        marketplace (lo stesso annuncio del venditore), altrimenti vuoto.
        Fino al 2026-11-05 si copiava l'ASIN del prodotto: falso quando il
        prodotto ha annunci su ASIN diversi (FR "DAIWA-22CFAILT2000" risultava
        sull'ASIN della versione "+").
        """
        if not sku:
            return ''
        return self.env['ke.amazon.listing'].sudo().search([
            ('sku', '=', sku), ('asin', '!=', False), ('asin', '!=', ''),
        ], limit=1).asin or ''

    def _attached_listing_msg(self, product, variant, reason):
        marketplace = self._current_marketplace()
        return (
            f'COLLEGATO (famiglia diversa su {marketplace.code}): {product.get("sku")} / '
            f'{product.get("asin")} → variante id={variant.id} della famiglia '
            f'"{variant.product_tmpl_id.name[:40]}" — annuncio registrato, struttura non '
            f'modificata ({reason})'
        )

    # -----------------------------------------------------------------------
    # Funzione 1: Import completo (solo manuale)
    # -----------------------------------------------------------------------

    def full_import(self, log_rec, limit=None, update_existing=True, log_cb=None):
        """
        Scarica tutti i listing Amazon e crea/aggiorna product.template in Odoo.
        log_cb: callable(str) per messaggi di progresso in tempo reale.
        """
        self.env = self.env(context=dict(self.env.context, **_IMPORT_MAIL_CTX))
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
        maps = _MatchMaps(self.env, self._build_barcode_map())

        created = updated = skipped = errors = 0
        lines = []
        grade_resell_skus = set()

        for listing in self.client.iter_seller_skus(max_products=limit):
            product = _parse_listing(listing, self._catalog_lang())
            sku = product['sku']
            asin = product['asin']
            if _is_grade_and_resell_sku(sku):
                skipped += 1
                grade_resell_skus.add(sku)
                resell_marketplace = self._current_marketplace()
                if resell_marketplace:
                    self._upsert_resell_offer(resell_marketplace, sku, self._resell_vals_from_listing(product))
                continue

            # Arricchisci con dati catalogo (include relationships)
            if asin:
                catalog = self.client.get_catalog_item(asin)
                _merge_catalog(product, catalog, self._catalog_lang())

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
                        product, parent_asin, product_type_odoo, maps, update_existing,
                    )
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    else:
                        skipped += 1
                else:
                    # --- Prodotto semplice ---
                    # find_simple ritorna None anche quando il match trovato è
                    # in realtà un template famiglia (vedi _MatchMaps.find_simple).
                    existing = maps.find_simple(sku, asin, product['ean'])
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
                    elif maps.find_known_family(sku):
                        # Vedi commento su _MatchMaps.find_known_family: SKU
                        # già di una famiglia esistente, riga senza ASIN nel
                        # report — salta invece di creare un duplicato o
                        # corrompere il template famiglia con campi "semplice".
                        variant_tmpl = maps.find_known_family(sku)
                        attached = self._attach_listing_by_sku(product, variant_tmpl)
                        if attached:
                            updated += 1
                            msg = (
                                f'COLLEGATO (riga senza ASIN nel report, per SKU): {sku} → variante '
                                f'id={attached.id} della famiglia "{variant_tmpl.name[:40]}"'
                            )
                        else:
                            skipped += 1
                            msg = (
                                f'SALTATO (SKU già di una famiglia esistente "{variant_tmpl.name[:40]}" '
                                f'id={variant_tmpl.id} — riga senza ASIN nel report): {sku}'
                            )
                        result_tmpl = None
                    else:
                        result_tmpl = self._create_product(product, product_type_odoo)
                        created += 1
                        msg = f'CREATO: {sku} / {asin} — {product["name"][:50]}'

                    if result_tmpl:
                        maps.register_simple(sku, asin, product['ean'], result_tmpl)

            except Exception as exc:
                errors += 1
                msg = f'ERRORE {sku}: {exc}'
                _log.error(msg, exc_info=True)

            lines.append(msg)
            _log_line(msg)

            # Commit ogni 50 prodotti per non perdere tutto in caso di errore
            if (created + updated) % 50 == 0:
                self.env.cr.commit()

        gr_line = _grade_and_resell_log_line(grade_resell_skus, tracked=True)
        if gr_line:
            lines.append(gr_line)
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

    def _fill_missing_asins(self, chunk, maps):
        """
        Righe del report senza ASIN il cui SKU Odoo non conosce (nessun
        prodotto, codice interno, annuncio o versione '+'): chiede l'ASIN ad
        Amazon per quello SKU (getListingsItem) e lo scrive nella riga, che
        poi segue il percorso normale (Catalog API, abbinamento per ASIN).
        Senza, la riga diventava un prodotto nuovo: 12 casi nell'import di
        arundo_prod del 2026-11-05 (es. "PFS-73032}", "FRM-AMZ29PG-150.").
        Uno SKU già noto non viene chiesto ad Amazon: prende l'ASIN dello
        stesso SKU su un altro marketplace, se c'è (2026-11-06: nel report FR
        quasi tutte le righe sono senza ASIN, il Catalog API non veniva mai
        chiesto e le descrizioni francesi mancavano: 8 prodotti contro 2.446
        in spagnolo). Un errore nella richiesta lascia la riga com'era.
        Ritorna (chiesti, trovati, presi dallo stesso SKU).
        """
        asked = found = known = 0
        can_ask = hasattr(self.client, 'get_listing_asin')
        for row in chunk:
            sku = (row.get('seller-sku') or '').strip()
            if not sku or (row.get('asin1') or '').strip() or _is_grade_and_resell_sku(sku):
                continue
            if maps.knows_sku(sku):
                asin = self._known_asin_for_sku(sku)
                if asin:
                    row['asin1'] = asin
                    known += 1
                continue
            if not can_ask:
                continue
            asked += 1
            try:
                asin = self.client.get_listing_asin(sku)
            except Exception as exc:
                _log.warning('ASIN per lo SKU %s non ottenuto da Amazon: %s', sku, exc)
                continue
            if asin:
                row['asin1'] = asin
                found += 1
        return asked, found, known

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
            'open_date': _parse_open_date(row.get('open-date')),
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

    # Batch searchCatalogItems per l'arricchimento durante l'import a report —
    # stesso valore di _CAT_MAP_BATCH_SIZE e stesso motivo (la risposta tronca
    # 'items' a 10 anche se ne chiedi di più, vedi commento lì).
    _FULL_IMPORT_CATALOG_BATCH_SIZE = 10

    def full_import_report_rows(self, log_rec, rows, row_offset, chunk_size, update_existing):
        """
        Processa un chunk di righe del report listing, arricchendo ogni ASIN
        con una chiamata batch al Catalog API (10 ASIN/chiamata, stesso
        meccanismo di apply_category_mapping) PRIMA di creare/aggiornare i
        prodotti — a differenza del comportamento originale (nessuna chiamata
        catalog qui, dimensioni/categoria/brand/descrizione/relazioni-varianti
        sempre lasciate vuote fino a un giro separato di "Aggiorna da Catalog
        API", che in più non ha mai potuto costruire famiglie di varianti
        perché quel giro tocca solo campi su prodotti già creati come semplici).
        Con questa chiamata in più, un ASIN che risulta 'relationships'
        type=VARIATION viene instradato su _process_variant_listing (stessa
        funzione già usata dal percorso legacy full_import/full_import_chunk
        per cataloghi piccoli) invece di diventare un prodotto standalone.
        Se la chiamata batch per un gruppo fallisce, quei prodotti vengono
        comunque creati (come semplici, senza arricchimento) invece di bloccare
        l'intero import — recuperabili in seguito da un giro di "Aggiorna da
        Catalog API" (che però non potrà mai retroattivamente trasformarli in
        varianti collegate, solo backfillare categoria/dimensioni/brand/testo).
        Ritorna il prossimo row_offset, oppure None se tutte le righe sono state elaborate.

        Ogni riga gira in un proprio savepoint: un errore annulla solo quella
        riga invece di lasciare la transazione dell'intero blocco in stato
        "aborted" (prima ogni riga successiva falliva a cascata e il blocco
        andava perso — import del 2026-11-03); un deadlock o un conflitto di
        serializzazione ripete la riga fino a _ROW_RETRY_MAX volte.
        """
        self.env = self.env(context=dict(self.env.context, **_IMPORT_MAIL_CTX))
        product_type_odoo = _resolve_product_type(
            self.env,
            self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.odoo_product_type', 'consu'
            )
        )

        maps = _MatchMaps(self.env, self._build_barcode_map())
        # Fase 3 multi-marketplace: un prodotto creato/aggiornato SOLO da un
        # marketplace non primario non ha i campi legacy x_amazon_* popolati
        # (vedi _is_primary_marketplace) — senza questo, un secondo marketplace
        # per lo stesso prodotto non lo troverebbe nelle mappe sopra e ne
        # creerebbe un duplicato. ke.amazon.listing copre tutti i marketplace,
        # a prescindere da quale sia primario.
        maps.add_listing_fallback(self.env['ke.amazon.listing'].sudo().search([]))


        # Stato e data di apertura di ogni SKU nel report di questo
        # marketplace: servono a _switch_main_sku per scegliere lo SKU
        # principale quando lo stesso prodotto ha più SKU.
        self._report_info_by_sku = {
            (r.get('seller-sku') or '').strip(): (
                (r.get('status') or '').strip().upper() == 'ACTIVE',
                _parse_open_date(r.get('open-date')),
            )
            for r in rows if (r.get('seller-sku') or '').strip()
        }

        chunk = rows[row_offset:row_offset + chunk_size]
        if not chunk:
            return None
        asin_asked, asin_found, _asin_known = self._fill_missing_asins(chunk, maps)

        # Arricchimento Catalog API a batch (10 ASIN/chiamata) PRIMA di creare
        # i prodotti — vedi docstring. Un fallimento di UNA chiamata batch non
        # blocca l'intero chunk: quei prodotti restano senza arricchimento
        # (creati comunque, come semplici), recuperabili in seguito.
        #
        # Salta gli ASIN che corrispondono già a un prodotto SEMPLICE
        # esistente con tutti i campi Catalog già popolati (_needs_catalog_
        # data, stessa condizione di apply_category_mapping): per quelli non
        # c'è nulla di nuovo da imparare, la chiamata è pura ripetizione —
        # l'aggiornamento di prezzo/quantità/stato arriva comunque da
        # _update_product più sotto (match su by_asin, indipendente
        # dall'arricchimento). NON si applica a famiglie, prodotti con dati
        # ancora incompleti, o ASIN mai visti prima: per quelli il Catalog
        # API resta l'unico modo di sapere se una riga è una variante
        # (relationships) — saltarlo rischierebbe di instradarla sul ramo
        # sbagliato. Richiesta esplicita dell'utente dopo aver notato che un
        # Full Import su un catalogo già in gran parte arricchito rifà le
        # stesse migliaia di chiamate ad ogni giro senza risparmiare nulla.
        chunk_asins = sorted({
            (row.get('asin1') or '').strip() for row in chunk
            if (row.get('asin1') or '').strip()
        })
        skipped_catalog_calls = 0
        asins_to_fetch = []
        for asin in chunk_asins:
            existing = maps.by_asin.get(asin)
            if existing and not existing.x_amazon_parent_asin and not _needs_catalog_data(existing, self._marketplace_lang_context().get('lang')):
                skipped_catalog_calls += 1
                continue
            asins_to_fetch.append(asin)
        chunk_asins = asins_to_fetch
        if skipped_catalog_calls:
            _log.warning(
                'full_import_report_rows: %d ASIN già noti e completi, chiamata Catalog API saltata '
                '(%d da richiedere)', skipped_catalog_calls, len(chunk_asins),
            )
        catalog_by_asin = {}
        n_batches = (len(chunk_asins) + self._FULL_IMPORT_CATALOG_BATCH_SIZE - 1) // self._FULL_IMPORT_CATALOG_BATCH_SIZE
        for i in range(0, len(chunk_asins), self._FULL_IMPORT_CATALOG_BATCH_SIZE):
            batch = chunk_asins[i:i + self._FULL_IMPORT_CATALOG_BATCH_SIZE]
            batch_num = i // self._FULL_IMPORT_CATALOG_BATCH_SIZE + 1
            # Strumentazione minima aggiunta dopo uno stallo reale non
            # diagnosticato (import fermo per ore, nessun errore in log,
            # nessun lock DB, Amazon raggiungibile subito dopo) — WARNING (non
            # INFO) perché su questa istanza il livello log filtra INFO. Se si
            # blocca di nuovo, l'ultima riga "richiedo" senza il "ricevuto"
            # corrispondente isola con precisione la chiamata in corso, invece
            # di dover indagare alla cieca come la prima volta.
            _log.warning('full_import_report_rows: richiedo batch Catalog API %d/%d (%d ASIN)',
                         batch_num, n_batches, len(batch))
            try:
                catalog_by_asin.update(self.client.get_catalog_items_batch(batch))
            except Exception as exc:
                _log.warning(
                    'full_import_report_rows: chiamata batch Catalog API fallita per %d ASIN '
                    '(creati comunque senza arricchimento, recuperabili da Aggiorna da Catalog '
                    'API in seguito): %s', len(batch), exc,
                )
            else:
                _log.warning('full_import_report_rows: ricevuto batch Catalog API %d/%d', batch_num, n_batches)

        created = updated = skipped = errors = 0
        lines = []
        if asin_asked:
            lines.append(f'ASIN chiesto ad Amazon per {asin_asked} SKU senza ASIN nel report e sconosciuti '
                         f'in Odoo: trovato per {asin_found}.')

        grade_resell_skus = set()
        resell_marketplace = self._current_marketplace()
        pending = list(chunk)
        attempts = {}
        while pending:
            row = pending.pop(0)
            product = self._parse_listing_report(row)
            sku = product['sku']
            asin = product['asin']
            if not sku:
                continue
            if _is_grade_and_resell_sku(sku):
                skipped += 1
                grade_resell_skus.add(sku)
                if resell_marketplace:
                    self._upsert_resell_offer(resell_marketplace, sku, self._resell_vals_from_listing(product))
                continue

            if asin and asin in catalog_by_asin:
                _merge_catalog(product, catalog_by_asin[asin], self._catalog_lang())

            try:
                with self.env.cr.savepoint():
                    # Contenitore della famiglia (non un'offerta vendibile a sé) — salta.
                    if product.get('is_parent_listing'):
                        skipped += 1
                        lines.append(f'SALTATO (parent listing): {sku} / {asin}')
                        continue

                    parent_asin = product.get('parent_asin')
                    if parent_asin:
                        # --- Variante di una famiglia (relationships dal Catalog API) ---
                        res, msg = self._process_variant_listing(
                            product, parent_asin, product_type_odoo, maps, update_existing,
                        )
                        if res == 'created':
                            created += 1
                        elif res == 'updated':
                            updated += 1
                        else:
                            skipped += 1
                        lines.append(msg)
                        continue

                    # --- Prodotto semplice ---
                    # Più SKU sullo stesso ASIN (vecchio e "+", ".", ecc.): la
                    # riga trova il prodotto per ASIN o SKU come le altre e
                    # diventa un annuncio in più; quale SKU porta il prodotto
                    # lo decide _switch_main_sku. Fino al 2026-11-05 la
                    # versione senza "+" veniva saltata qui (best_sku_by_asin,
                    # find_superseding).
                    # ASIN di una variante di famiglia già esistente, ma riga non
                    # riconosciuta come variante: saltare invece di creare un
                    # semplice duplicato (vedi _MatchMaps.find_family_by_variant_asin).
                    family = maps.find_family_by_variant_asin(asin)
                    if family:
                        attached = self._attach_listing_to_existing_variant(product, maps)
                        if attached:
                            updated += 1
                            lines.append(self._attached_listing_msg(
                                product, attached, 'riga non riconosciuta come variante'))
                            continue
                        skipped += 1
                        lines.append(
                            f'SALTATO (ASIN di una variante della famiglia "{family.name[:40]}" '
                            f'id={family.id}, riga non riconosciuta come variante): {sku} / {asin}'
                        )
                        continue

                    # find_simple ritorna None anche quando il match trovato è in
                    # realtà un template famiglia (vedi _MatchMaps.find_simple) —
                    # ricade sul controllo variant_tmpl_by_sku sotto invece di
                    # corrompere la famiglia con _update_product.
                    existing = maps.find_simple(sku, asin, product['ean'])
                    if existing:
                        if update_existing:
                            self._update_product(existing, product)
                            updated += 1
                            msg = f'AGGIORNATO: {sku} / {product["asin"]} — {product["name"][:50]}'
                        else:
                            skipped += 1
                            msg = f'SALTATO (già esiste): {sku}'
                        result_tmpl = existing
                    elif maps.find_known_family(sku):
                        # Vedi commento su _MatchMaps.find_known_family: SKU già
                        # di una famiglia esistente, riga senza ASIN nel report
                        # (quirk noto del report bulk Amazon: a volte omette
                        # l'ASIN anche se il listing ce l'ha, verificato dal vivo
                        # via Listings API in tempo reale sullo stesso SKU) —
                        # senza ASIN non possiamo instradare su
                        # _process_variant_listing, e chiamare _update_product qui
                        # scriverebbe campi "semplice" su un template famiglia
                        # (corrompendolo). Meglio saltare che creare un duplicato o
                        # corrompere la famiglia.
                        variant_tmpl = maps.find_known_family(sku)
                        attached = self._attach_listing_by_sku(product, variant_tmpl)
                        if attached:
                            updated += 1
                            msg = (
                                f'COLLEGATO (riga senza ASIN nel report, per SKU): {sku} → variante '
                                f'id={attached.id} della famiglia "{variant_tmpl.name[:40]}"'
                            )
                        else:
                            skipped += 1
                            msg = (
                                f'SALTATO (SKU già di una famiglia esistente "{variant_tmpl.name[:40]}" '
                                f'id={variant_tmpl.id} — riga senza ASIN nel report): {sku}'
                            )
                        result_tmpl = None
                    else:
                        result_tmpl = self._create_product(product, product_type_odoo)
                        created += 1
                        msg = f'CREATO: {sku} / {product["asin"]} — {product["name"][:50]}'

                    # Aggiorna le mappe in-memory: senza questo, un ASIN che compare più
                    # volte nello stesso chunk sotto SKU diversi (listing raggruppati
                    # 'amzn.gr.*', varianti 'stickerless', typo nello SKU) non verrebbe
                    # riconosciuto come lo stesso prodotto e finirebbe duplicato.
                    if result_tmpl:
                        maps.register_simple(sku, asin, product['ean'], result_tmpl)
                    lines.append(msg)
            except Exception as exc:
                # Il savepoint ha già annullato solo questa riga; la cache
                # dell'ambiente può però ancora contenere valori scritti
                # prima dell'annullamento.
                self.env.invalidate_all()
                key = id(row)
                if getattr(exc, 'pgcode', None) in _RETRYABLE_PGCODES and attempts.get(key, 0) < _ROW_RETRY_MAX:
                    attempts[key] = attempts.get(key, 0) + 1
                    _log.warning('Full import: %s su %s, ripeto la riga (tentativo %d/%d)',
                                 exc.pgcode, sku, attempts[key], _ROW_RETRY_MAX)
                    time.sleep(attempts[key])
                    pending.insert(0, row)
                    continue
                errors += 1
                msg = f'ERRORE {sku}: {exc}'
                _log.error(msg, exc_info=True)
                lines.append(msg)

        gr_line = _grade_and_resell_log_line(grade_resell_skus, tracked=True)
        if gr_line:
            lines.append(gr_line)
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
        self.env = self.env(context=dict(self.env.context, **_IMPORT_MAIL_CTX))
        items, next_token = self.client.fetch_listings_page(page_token)
        if not items:
            return None

        product_type_odoo = _resolve_product_type(
            self.env,
            self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.odoo_product_type', 'consu'
            )
        )

        maps = _MatchMaps(self.env, self._build_barcode_map())
        # Fase 3 multi-marketplace (stesso fix già fatto in full_import_report_rows,
        # mancava qui — vedi TASK.md): un prodotto creato/aggiornato SOLO da un
        # marketplace non primario non ha i campi legacy x_amazon_* popolati
        # (vedi _is_primary_marketplace), quindi non comparirebbe nelle mappe
        # sopra e un secondo marketplace lo duplicherebbe invece di aggiornarlo.
        # ke.amazon.listing copre tutti i marketplace, a prescindere da quale
        # sia primario. Non serve estendere by_parent_asin/variant_by_asin: le
        # famiglie con varianti non passano mai da _upsert_listing (vedi
        # _process_variant_listing), quindi non hanno comunque righe
        # ke.amazon.listing da cui attingere.
        maps.add_listing_fallback(self.env['ke.amazon.listing'].sudo().search([]))

        created = updated = skipped = errors = 0
        lines = []
        grade_resell_skus = set()

        for listing in items:
            product = _parse_listing(listing, self._catalog_lang())
            sku = product['sku']
            asin = product['asin']
            if _is_grade_and_resell_sku(sku):
                skipped += 1
                grade_resell_skus.add(sku)
                resell_marketplace = self._current_marketplace()
                if resell_marketplace:
                    self._upsert_resell_offer(resell_marketplace, sku, self._resell_vals_from_listing(product))
                continue

            if asin:
                catalog = self.client.get_catalog_item(asin)
                _merge_catalog(product, catalog, self._catalog_lang())

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
                        product, parent_asin, product_type_odoo, maps, update_existing,
                    )
                    if res == 'created':
                        created += 1
                    elif res == 'updated':
                        updated += 1
                    else:
                        skipped += 1
                else:
                    # find_simple ritorna None anche quando il match trovato è
                    # in realtà un template famiglia (vedi _MatchMaps.find_simple).
                    existing = maps.find_simple(sku, asin, product['ean'])
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
                    elif maps.find_known_family(sku):
                        # Vedi commento su _MatchMaps.find_known_family: SKU
                        # già di una famiglia esistente, riga senza ASIN nel
                        # report — salta invece di creare un duplicato o
                        # corrompere il template famiglia con campi "semplice".
                        variant_tmpl = maps.find_known_family(sku)
                        attached = self._attach_listing_by_sku(product, variant_tmpl)
                        if attached:
                            updated += 1
                            msg = (
                                f'COLLEGATO (riga senza ASIN nel report, per SKU): {sku} → variante '
                                f'id={attached.id} della famiglia "{variant_tmpl.name[:40]}"'
                            )
                        else:
                            skipped += 1
                            msg = (
                                f'SALTATO (SKU già di una famiglia esistente "{variant_tmpl.name[:40]}" '
                                f'id={variant_tmpl.id} — riga senza ASIN nel report): {sku}'
                            )
                        result_tmpl = None
                    else:
                        result_tmpl = self._create_product(product, product_type_odoo)
                        created += 1
                        msg = f'CREATO: {sku} / {asin} — {product["name"][:50]}'

                    # Aggiorna le mappe in-memory: senza questo, un ASIN che compare più
                    # volte nello stesso chunk sotto SKU diversi (listing raggruppati,
                    # varianti 'stickerless', typo nello SKU) non verrebbe riconosciuto
                    # come lo stesso prodotto e finirebbe duplicato.
                    if result_tmpl:
                        maps.register_simple(sku, asin, product['ean'], result_tmpl)
            except Exception as exc:
                errors += 1
                msg = f'ERRORE {sku}: {exc}'
                _log.error(msg, exc_info=True)

            lines.append(msg)
            _log.info(msg)

        gr_line = _grade_and_resell_log_line(grade_resell_skus, tracked=True)
        if gr_line:
            lines.append(gr_line)
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
        """
        Returns True if ean is not already assigned to another product.product.
        Confronta contro tutte le rappresentazioni equivalenti dell'EAN (vedi
        _ean_search_variants) — senza questo, un barcode già presente su un
        altro prodotto solo con un diverso padding di zeri (es. UPC-A 12 cifre
        vs EAN-13) risulterebbe "libero" per errore e verrebbe assegnato due
        volte allo stesso codice.
        """
        if not ean:
            return False
        domain = [('barcode', 'in', list(_ean_search_variants(ean)))]
        if exclude_product_ids:
            domain.append(('id', 'not in', list(exclude_product_ids)))
        return not self.env['product.product'].sudo().search(domain, limit=1)

    def report_similar_skus_on_different_products(self, log_rec):
        """
        Elenca (non unisce) gli SKU quasi uguali — uguali a meno di "+", ".",
        "-", "}" o "/" in fondo, es. "X" e "X+", "X+" e "X++" — finiti su
        prodotti diversi perché Amazon li ha su ASIN diversi. Dal 2026-11-05
        l'abbinamento segue solo l'ASIN (prima "X+" si univa a "X" per nome,
        e sbagliava: SPRO-4621-104+ è sull'ASIN di SPRO-4621-105). Se sono
        davvero lo stesso articolo: spostare a mano l'annuncio sul prodotto
        giusto (resta "Abbinato a mano") o unire i prodotti. Ritorna il
        numero di gruppi.
        """
        groups = {}
        for listing in self.env['ke.amazon.listing'].sudo().search([('sku', '!=', False)]):
            base = listing.sku.rstrip('+.-}/ ')
            if base and not _is_grade_and_resell_sku(listing.sku):
                groups.setdefault(base, []).append(listing)
        lines = []
        for base, listings in sorted(groups.items()):
            products = {l.product_id for l in listings}
            if len(products) < 2:
                continue
            parts = []
            for product in sorted(products, key=lambda p: p.id):
                mine = [l for l in listings if l.product_id == product]
                skus = sorted({l.sku for l in mine})
                asins = sorted({l.asin for l in mine if l.asin})
                parts.append(f'id={product.id} {product.default_code or ""}: SKU {", ".join(skus)} '
                             f'(ASIN {", ".join(asins) or "-"})')
            lines.append(f'"{base}": ' + ' | '.join(parts))
        if lines:
            header = (f'=== SKU QUASI UGUALI SU PRODOTTI DIVERSI (ASIN diversi su Amazon): {len(lines)} — '
                      f'verifica manuale, nessuna unione automatica ===')
            log_rec.write({'log_lines': ((log_rec.log_lines or '') + '\n' + '\n'.join([header] + lines)).strip()})
        return len(lines)

    def detect_duplicate_skus(self, log_rec):
        """
        Rileva (non risolve) SKU che risultano su più di un product.template
        tra i prodotti legati ad Amazon — causa principale verificata dal
        vivo: il report bulk Amazon contiene a volte un ASIN storico/non
        aggiornato per alcune righe (dodicesimo bug, vedi TASK.md), oppure
        marketplace diversi creano template indipendenti per lo stesso SKU
        testuale prima che il matching li riconosca come lo stesso prodotto.

        Scrive un riepilogo nel log della sync con abbastanza dettaglio (id,
        tipo semplice/famiglia, ASIN/parent_asin, marketplace coinvolti) per
        una verifica manuale — NON fonde né cancella nulla: fondere
        automaticamente due template rischierebbe di spostare stock/ordini
        sul prodotto sbagliato (stesso principio già applicato al conflitto
        rilevato durante l'import, vedi _process_variant_listing).

        Filtra su 'active' (su entrambi i lati, nella CTE e nel join finale):
        un template con lo stesso default_code ma già archiviato (tipicamente
        l'originale di una fusione EAN/SKU completata nello stesso Full
        Import — vedi _merge_manual_into_variant, 'originale archiviato') non
        richiede nessuna verifica, è rumore puro. Trovato dal vivo il
        2026-10-27: 12 dei 14 gruppi segnalati su un Full Import reale erano
        di questo tipo, solo 2 genuini (entrambi i lati ancora attivi).

        Chiamato una volta a fine Full Import (tutti i marketplace
        esauriti), sull'intero catalogo — non solo sui prodotti toccati da
        questo giro, così resta uno strumento di verifica anche per
        duplicati residui da giri precedenti. Ritorna il numero di gruppi
        di SKU duplicati trovati (0 se nessuno).
        """
        self.env.cr.execute("""
            WITH dups AS (
                SELECT t.default_code
                FROM product_template t
                WHERE t.active
                  AND t.default_code IS NOT NULL AND t.default_code != ''
                  AND (
                        (t.x_amazon_asin IS NOT NULL AND t.x_amazon_asin != '')
                     OR (t.x_amazon_parent_asin IS NOT NULL AND t.x_amazon_parent_asin != '')
                     OR EXISTS (
                            SELECT 1 FROM ke_amazon_listing l
                            JOIN product_product pp ON pp.id = l.product_id
                            WHERE pp.product_tmpl_id = t.id
                        )
                  )
                GROUP BY t.default_code
                HAVING count(*) > 1
            )
            SELECT t.id, t.default_code, t.x_amazon_asin, t.x_amazon_parent_asin
            FROM product_template t
            JOIN dups d ON d.default_code = t.default_code
            WHERE t.active
            ORDER BY t.default_code, t.id
        """)
        rows = self.env.cr.dictfetchall()
        if not rows:
            return 0

        by_code = {}
        for r in rows:
            by_code.setdefault(r['default_code'], []).append(r)

        lines = [
            f'=== SKU DUPLICATI RILEVATI: {len(by_code)} — verifica manuale, '
            f'nessuna fusione automatica ==='
        ]
        for code, templates in by_code.items():
            parts = []
            for t in templates:
                tmpl_id = t['id']
                variant_ids = self.env['product.product'].sudo().search(
                    [('product_tmpl_id', '=', tmpl_id)]
                ).ids
                listings = self.env['ke.amazon.listing'].sudo().search(
                    [('product_id', 'in', variant_ids)]
                )
                marketplaces = ', '.join(listings.mapped('marketplace_id.code')) or '-'
                kind = 'famiglia' if t['x_amazon_parent_asin'] else 'semplice'
                parts.append(
                    f'id={tmpl_id} ({kind}, asin={t["x_amazon_asin"] or "-"}, '
                    f'parent_asin={t["x_amazon_parent_asin"] or "-"}, marketplace=[{marketplaces}])'
                )
            lines.append(f'SKU "{code}": ' + ' | '.join(parts))

        log_rec.write({
            'log_lines': ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip(),
        })
        self.env.cr.commit()
        return len(by_code)

    # Messaggio scritto in _process_variant_listing quando una riga indica
    # un parent_asin diverso da quello già registrato per lo stesso SKU —
    # se cambi quel testo, aggiorna anche questa regex (vedi
    # recover_renamed_family_parent_asin sotto, che lo rilegge dal log).
    _RENAMED_FAMILY_LOG_RE = re.compile(
        r'risulta già nella famiglia "[^"]*" \(id=(\d+), parent_asin=([A-Z0-9]+)\) '
        r'ma questa riga indica un parent_asin diverso \(([A-Z0-9]+)\)'
    )

    def recover_renamed_family_parent_asin(self, log_rec, source_log_id):
        """
        Operazione di recupero: Amazon a volte riassegna in blocco un nuovo
        parent ASIN a famiglie di varianti già esistenti (pulizia periodica
        delle relazioni di variazione sul suo catalogo, non un bug nostro) —
        verificato dal vivo il 2026-10-29 su un Full Import reale: 668
        famiglie coinvolte, loggate come "conflitto" e saltate (comportamento
        corretto: _process_variant_listing non deve indovinare). Di queste,
        solo quelle dove OGNI riga coinvolta della stessa famiglia indica lo
        STESSO unico nuovo parent_asin (nessuno split su più ASIN, nessun
        altro parent_asin storico già puntato allo stesso nuovo valore) sono
        una semplice rinomina: stesso gruppo di figli, nessun dato da
        spostare. Qui si ripara SOLO quel caso, aggiornando x_amazon_parent_asin
        sul template esistente — famiglie divise o unite restano segnalate,
        serve una decisione reale su come ristrutturare i dati, non
        automatizzabile in sicurezza qui.

        Rilegge il testo già salvato di un Full Import precedente
        (source_log_id, tipicamente l'ultimo) — nessuna chiamata Amazon,
        nessuna nuova analisi dal vivo: i dati vengono dal log di
        quell'import, non da uno stato più recente del catalogo. Se il
        catalogo è cambiato nel frattempo (altro import, altra riconciliazione),
        la famiglia viene saltata invece di sovrascrivere un dato più
        recente (vedi il controllo su x_amazon_parent_asin invariato sotto).

        Ritorna (riparate, saltate_split_o_unione, saltate_collisione).
        """
        source_log = self.env['ke.amazon.sync.log'].browse(source_log_id)
        if not source_log.exists() or not source_log.log_lines:
            msg = f'Log Full Import #{source_log_id} non trovato o senza righe.'
            log_rec.write({'state': 'error', 'error_summary': msg, 'end_date': datetime.now()})
            self.env.cr.commit()
            return 0, 0, 0

        fam_to_news = {}
        fam_old_asin = {}
        for line in source_log.log_lines.splitlines():
            m = self._RENAMED_FAMILY_LOG_RE.search(line)
            if not m:
                continue
            fam_id, old_asin, new_asin = m.groups()
            fam_to_news.setdefault(fam_id, set()).add(new_asin)
            fam_old_asin[fam_id] = old_asin

        new_to_fams = {}
        for fam_id, news in fam_to_news.items():
            for new_asin in news:
                new_to_fams.setdefault(new_asin, set()).add(fam_id)

        Template = self.env['product.template']
        fixed = skipped_split = skipped_collision = 0
        lines = [f'Famiglie candidate trovate nel log #{source_log_id}: {len(fam_to_news)}']
        for fam_id, news in fam_to_news.items():
            if len(news) > 1:
                # La famiglia è stata DIVISA su più nuovi parent_asin diversi
                # — non una semplice rinomina, serve decidere come separare
                # le varianti in Odoo. Non automatizzabile qui.
                skipped_split += 1
                lines.append(
                    f'SALTATO (divisa) id={fam_id}: {len(news)} nuovi parent_asin diversi '
                    f'({", ".join(sorted(news))}) — serve una decisione su come separare le varianti'
                )
                continue
            new_asin = next(iter(news))
            if len(new_to_fams[new_asin]) > 1:
                # Lo stesso nuovo parent_asin è il bersaglio di PIÙ famiglie
                # vecchie diverse — famiglie UNITE da Amazon, non una
                # rinomina 1:1. Non automatizzabile qui.
                skipped_split += 1
                lines.append(
                    f'SALTATO (unione con altra famiglia) id={fam_id}: nuovo parent_asin {new_asin} '
                    f'conteso anche da altre famiglie — serve una decisione su come unire le varianti'
                )
                continue
            tmpl = Template.browse(int(fam_id))
            if not tmpl.exists() or not tmpl.active:
                lines.append(f'SALTATO id={fam_id}: template non trovato o archiviato nel frattempo')
                continue
            if tmpl.x_amazon_parent_asin != fam_old_asin[fam_id]:
                # Il parent_asin è già cambiato da quando questo log è stato
                # scritto (altro import/riconciliazione, o un giro precedente
                # di questo stesso recupero) — non sovrascrivere un dato più
                # recente di quello che questo log descriveva.
                lines.append(
                    f'SALTATO "{tmpl.name[:40]}" (id={tmpl.id}): parent_asin già cambiato dal log, '
                    f'non più {fam_old_asin[fam_id]}'
                )
                continue
            collision = Template.search([
                ('x_amazon_parent_asin', '=', new_asin), ('id', '!=', tmpl.id), ('active', '=', True),
            ], limit=1)
            if collision:
                # Probabile famiglia duplicata creata per errore durante
                # l'import stesso (una riga di quella famiglia non è stata
                # riconosciuta via SKU, ne ha creata una seconda sotto il
                # nuovo parent_asin) — fondere due template è un'operazione
                # diversa e più rischiosa (spostare stock/ordini), non la
                # faccio qui.
                skipped_collision += 1
                lines.append(
                    f'SALTATO (collisione) "{tmpl.name[:40]}" (id={tmpl.id}): esiste già un altro '
                    f'template (id={collision.id}) con il nuovo parent_asin {new_asin} — probabile '
                    f'duplicato creato durante l\'import, serve fusione manuale'
                )
                continue
            tmpl.write({'x_amazon_parent_asin': new_asin})
            fixed += 1
            lines.append(
                f'RIPARATO "{tmpl.name[:40]}" (id={tmpl.id}): parent_asin {fam_old_asin[fam_id]} → {new_asin}'
            )

        log_rec.write({
            'state': 'done',
            'products_total': len(fam_to_news),
            'products_updated': fixed,
            'products_skipped': skipped_split + skipped_collision,
            'log_lines': '\n'.join(lines),
            'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return fixed, skipped_split, skipped_collision

    def _build_barcode_map(self):
        """Mappa barcode (forma canonica, vedi _normalize_ean) -> product.template.
        Logga (senza bloccare) i barcode duplicati: in caso di duplicato l'ultimo
        prodotto trovato sovrascrive il precedente nella mappa, con effetto sul
        matching a valle (import via EAN). Chiave normalizzata: un UPC-A a 12
        cifre e un EAN-13 con uno zero di padding davanti sono lo stesso codice,
        senza normalizzare non si riconoscerebbero mai uguali (577 prodotti col
        pattern esatto trovati dal vivo il 2026-10-23)."""
        by_barcode = {}
        for product in self.env['product.template'].search([('barcode', '!=', False)]):
            if not product.barcode:
                continue
            key = _normalize_ean(product.barcode)
            previous = by_barcode.get(key)
            if previous is not None:
                _log.warning(
                    'Barcode duplicato (equivalente a %s) %s: %s (id=%d) sovrascrive %s (id=%d) nella mappa di matching import',
                    key, product.barcode, product.display_name, product.id,
                    previous.display_name, previous.id,
                )
            by_barcode[key] = product
        return by_barcode

    def _net_price(self, gross, product=None):
        """
        Toglie l'IVA da un prezzo lordo Amazon. Con 'product' dato (già
        creato, con taxes_id risolto da Odoo) usa la SUA aliquota di
        vendita reale — stessa fonte di verità di compute_amazon_sale_price
        (product_template.py, usata nel verso opposto Odoo→Amazon), invece
        dell'unica aliquota di default configurata. Senza 'product' (i
        chiamanti di creazione: il prodotto non esiste ancora, Odoo non gli
        ha ancora potuto assegnare una tassa) ricade sull'aliquota di
        default — un'approssimazione nota per categorie con IVA ridotta
        (4%/10%), corretta subito dopo la creazione da
        _correct_seeded_list_price. Trovato in revisione (TASK.md
        "Prezzi"): prima questa funzione usava SEMPRE l'aliquota di default,
        anche quando il prodotto aveva già una tassa reale diversa — a
        differenza del calcolo inverso, che la legge correttamente.
        """
        if product:
            from odoo.addons.ke_amazon_sync.models.product_template import _get_amazon_vat_info
            rate, price_include = _get_amazon_vat_info(product)
            if price_include:
                # list_price per questo prodotto è già nella convenzione
                # lorda (stessa lettura di compute_amazon_sale_price: con
                # price_include=True il valore del campo include l'IVA) —
                # 'gross' va scritto così com'è, nessuno scorporo.
                return gross
        else:
            rate = float(self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.default_vat_rate', '22'
            ))
        if rate <= 0:
            return gross
        return gross / (1.0 + rate / 100.0)

    def _correct_seeded_list_price(self, template, gross):
        """
        list_price viene seedato alla creazione con _net_price(gross) usando
        l'aliquota di DEFAULT (il prodotto non esisteva ancora — vedi
        _net_price). Appena creato, Odoo gli assegna una tassa di vendita
        reale (dalla categoria/azienda): se è diversa da quella di default,
        ricalcola subito list_price con l'aliquota vera, invece di lasciare
        un'approssimazione nota per sempre. Nessuna scrittura se il prezzo
        risultante è comunque lo stesso (nessuna tassa reale trovata, o
        stessa aliquota del default).
        """
        corrected = self._base_price_from_amazon(gross, template)
        if abs(corrected - template.list_price) > 0.001:
            template.write({'list_price': corrected})

    def _base_price_from_amazon(self, gross, product, marketplace=None):
        """
        Prezzo base di Odoo (prezzo di vendita) ricavato da un prezzo Amazon
        IVA inclusa del marketplace corrente: il calcolo di
        compute_amazon_sale_price fatto all'indietro, così che accendendo
        "Sync prezzo" il prezzo su Amazon resti quello di oggi. Deciso con
        l'utente il 2026-11-05: il prezzo di vendita in Odoo è il prezzo base,
        quello Amazon ne deriva; prima qui si toglieva solo l'IVA e la sync
        avrebbe alzato tutti i prezzi del ricarico del listino (+15%).

        Toglie l'IVA del paese (o del prodotto), il ricarico della regola in
        percentuale del listino del marketplace (o "Amazon") applicabile al
        prodotto, riporta nella valuta aziendale se c'è il tasso di cambio, e
        rimette l'IVA del prodotto se il suo prezzo è IVA inclusa. Regole a
        prezzo fisso e supplementi non vengono invertiti (approssimazione).
        'marketplace': quello del prezzo (default: il marketplace di questa
        sync), usato da "Ricalcola prezzi base da Amazon".
        """
        from odoo.addons.ke_amazon_sync.models.product_template import (
            _amazon_pricelist_items_by_priority, _get_amazon_vat_info,
        )
        if not gross:
            return 0.0
        if marketplace is None:
            marketplace = self._current_marketplace()
        rate, price_include = _get_amazon_vat_info(product)
        country_rate = marketplace.vat_rate if marketplace and marketplace.vat_rate else rate
        net = gross / (1.0 + country_rate / 100.0)
        pricelist = (marketplace.pricelist_id if marketplace else False) or self.env.ref(
            'ke_amazon_sync.pricelist_amazon', raise_if_not_found=False)
        if pricelist:
            rule = next((i for i in _amazon_pricelist_items_by_priority(pricelist, product)
                         if i.compute_price in ('formula', 'percentage')), None)
            if rule:
                discount = rule.price_discount if rule.compute_price == 'formula' else rule.percent_price
                factor = 1.0 - (discount or 0.0) / 100.0
                if factor > 0:
                    net = net / factor
        company = self.env.company
        currency = marketplace.currency_id if marketplace else False
        if currency and currency != company.currency_id and currency.sudo().rate_ids:
            net = currency._convert(net, company.currency_id, company, fields.Date.today())
        base = net * (1.0 + rate / 100.0) if price_include else net
        return round(base, 2)

    def _marketplace_lang_context(self):
        """
        Contesto {'lang': codice} per il marketplace corrente di questa sync
        (ke.amazon.marketplace.lang_id), se configurato — usato per scrivere
        nome/descrizione nello slot di traduzione giusto invece di
        sovrascrivere sempre la lingua di default dell'ambiente (vedi
        TASK.md, "Testi/traduzioni multi-marketplace": name/description_sale
        sono già campi traducibili in Odoo, il problema era solo che le
        nostre write() non specificavano mai la lingua). Dizionario vuoto
        se non c'è un marketplace corrente o non ha lingua configurata — il
        chiamante scrive allora nella lingua di default come sempre
        (comportamento invariato per chi non ha ancora configurato lang_id).

        Controlla anche che la lingua sia ATTIVA in Odoo (Impostazioni →
        Traduzioni → Lingue): un `with_context(lang=...)` con un codice non
        attivo fa sollevare a Odoo "Invalid language code", un'eccezione
        non catturata da nessuna parte nel loop di import — trovato dal
        vivo il 2026-10-16: un Full Import intero è caduto dopo 45 minuti
        di lavoro per un marketplace con lang_id configurato ma la lingua
        corrispondente non ancora attivata. Meglio ignorare silenziosamente
        (scrivere nella lingua di default, come se lang_id non fosse
        configurato) che perdere un intero giro per una svista di
        configurazione.
        """
        marketplace = self._current_marketplace()
        lang = marketplace.lang_id if marketplace else self.env['res.lang']
        if not lang:
            return {}
        if not lang.active:
            _log.warning(
                "Marketplace '%s': lang_id='%s' configurato ma non attivo in Odoo "
                "(Impostazioni → Traduzioni → Lingue) — ignorato per questo giro, "
                "scritto nella lingua di default invece di far fallire l'import.",
                marketplace.code, lang.code,
            )
            return {}
        return {'lang': lang.code}

    def _catalog_lang(self):
        """Locale con cui il client interroga il Catalog API (vedi SpApiClient.locale)."""
        return getattr(self.client, 'locale', None) or None

    def _has_translation(self, template, field, lang):
        """
        True se 'template' ha già un valore esplicito per 'field' nella
        lingua 'lang'. Non si può controllare leggendo il campo con
        with_context(lang=...): se quella lingua non ha ancora una
        traduzione propria, Odoo ritorna il valore di FALLBACK (un'altra
        lingua), non una stringa vuota — un controllo 'if not
        localized.field' darebbe sempre falso positivo (verificato dal vivo
        con un test che falliva per questo motivo esatto). La colonna JSON
        grezza invece non applica nessun fallback: una chiave assente resta
        assente.
        """
        template.flush_recordset([field])
        self.env.cr.execute(
            f'SELECT ({field}->>%s) FROM product_template WHERE id = %s',
            (lang, template.id),
        )
        row = self.env.cr.fetchone()
        return bool(row and row[0])

    def _marketplace_langs(self):
        if not hasattr(self, '_mp_langs_cache'):
            self._mp_langs_cache = set(self.env['ke.amazon.marketplace'].search([
                ('active', '=', True), ('lang_id', '!=', False)]).mapped('lang_id.code'))
        return self._mp_langs_cache

    def _has_own_translation(self, template, field, lang):
        """
        Come _has_translation, ma un testo che è la copia esatta di quello di
        un'altra lingua dei marketplace non conta come proprio: è il testo
        con cui la famiglia è nata su un altro marketplace (2026-11-06: la
        famiglia Berkley "Power Hawg" creata da ES aveva in italiano il nome
        spagnolo, e l'import non lo sostituiva mai col titolo italiano).
        """
        template.flush_recordset([field])
        self.env.cr.execute(f'SELECT {field} FROM product_template WHERE id = %s', (template.id,))
        row = self.env.cr.fetchone()
        values = (row and row[0]) or {}
        own = values.get(lang)
        if not own:
            return False
        return not any(values.get(other) == own for other in self._marketplace_langs() if other != lang)

    def _write_localized_text(self, template, name=None, description_sale=None):
        """
        Scrive nome/descrizione sul template, nello slot di traduzione del
        marketplace corrente, solo se quello slot è attualmente vuoto — mai
        sovrascritto un testo già presente in quella lingua (stesso
        principio già usato per brand/descrizione nel backfill di
        apply_category_mapping). Non fa nulla se il marketplace corrente
        non ha una lingua configurata.
        """
        ctx = self._marketplace_lang_context()
        if not ctx:
            return
        lang = ctx['lang']
        vals = {}
        if name and not self._has_own_translation(template, 'name', lang):
            vals['name'] = name
        if description_sale and not self._has_own_translation(template, 'description_sale', lang):
            vals['description_sale'] = description_sale
        if vals:
            template.with_context(**ctx).write(vals)

    def _write_family_text(self, template, product):
        """
        Nome/descrizione della FAMIGLIA nella lingua del marketplace corrente,
        dalla riga di una sua variante (nome senza il suffisso di variante,
        come in _create_configurable_template). Prima solo i prodotti
        semplici ricevevano il testo tradotto (_update_product): le righe
        ES/FR/UK delle varianti di famiglie già create da IT lasciavano il
        template solo in italiano — trovato il 2026-11-04 durante l'import
        da capo (10 famiglie su 885 con testo ES, a ES già avviato). Stesse
        regole di _write_localized_text: solo slot vuoto, nessun effetto
        senza lingua del marketplace.
        """
        name = product.get('name') or ''
        self._write_localized_text(
            template,
            name=_strip_variant_suffix(name) or name or None,
            description_sale=self._format_description(product) or None,
        )

    @staticmethod
    def _is_import_family_name(current, titles):
        """
        True se il nome attuale della famiglia è uno scritto dall'import (uguale
        o l'inizio di uno dei titoli Amazon, anche senza il suffisso tra
        parentesi) e non un nome scelto a mano: solo quelli vengono ricalcolati.
        """
        cur = (current or '').strip().casefold()
        if not cur:
            return True
        for title in titles:
            t = (title or '').strip().casefold()
            if t.startswith(cur) or cur == (_strip_variant_suffix(title) or '').strip().casefold():
                return True
        return False

    def assign_brands_from_amazon(self):
        """
        Marca di catalogo per tutti i prodotti dalla loro marca Amazon
        (x_amazon_brand), stesse regole di _assign_brand; poi elimina le marche
        "Da verificare" rimaste senza prodotti e propone il "Nome su Amazon"
        delle marche che non l'hanno. A fine Full Import e dal pulsante in
        Impostazioni (es. dopo aver importato le corrispondenze). Nessuna
        chiamata Amazon. Ritorna il numero di prodotti aggiornati.
        """
        Tmpl = self.env['product.template'].with_context(active_test=False)
        Brand = self.env['ke.product.brand'].sudo().with_context(active_test=False)
        updated = 0
        for tmpl in Tmpl.search([('x_amazon_brand', '!=', False)]):
            before = tmpl.ke_brand_id
            self._assign_brand(tmpl, tmpl.x_amazon_brand)
            if tmpl.ke_brand_id != before:
                updated += 1
        Brand.search([('to_review', '=', True)]).filtered(lambda b: not b.product_count).unlink()
        Brand.search([('amazon_name', '=', False)])._propose_amazon_name()
        return updated

    def recompute_family_names(self, templates=None):
        """
        Nome delle famiglie in ogni lingua dei marketplace attivi: la parte
        comune ai titoli Amazon delle varianti su quel marketplace
        (ke.amazon.listing.item_name, vedi _family_common_name). Stesso metodo
        per tutte le lingue, italiano compreso — prima il nome era il titolo
        della prima variante senza il suffisso tra parentesi, e su ES/FR/UK
        (attributi in coda dopo la virgola) restava la variante nel nome della
        famiglia (es. "…, gris oscuro, XL"). Sostituisce solo un nome vuoto o
        scritto dall'import (_is_import_family_name), mai uno scelto a mano.
        Nessuna chiamata Amazon. Ritorna il numero di nomi aggiornati.
        """
        Tmpl = self.env['product.template'].with_context(active_test=False)
        if templates is None:
            templates = Tmpl.search([('x_amazon_parent_asin', '!=', False)])
        if not templates:
            return 0
        marketplaces = self.env['ke.amazon.marketplace'].search([
            ('active', '=', True), ('lang_id', '!=', False), ('lang_id.active', '=', True),
        ], order='sequence, id')
        lang_by_marketplace = {}
        for marketplace in marketplaces:
            if marketplace.lang_id.code not in lang_by_marketplace.values():
                lang_by_marketplace[marketplace.id] = marketplace.lang_id.code
        if not lang_by_marketplace:
            return 0
        self.env.flush_all()
        self.env.cr.execute(
            """SELECT p.product_tmpl_id, l.marketplace_id, l.item_name
                 FROM ke_amazon_listing l JOIN product_product p ON p.id = l.product_id
                WHERE p.product_tmpl_id = ANY(%s) AND l.marketplace_id = ANY(%s)
                  AND l.item_name IS NOT NULL AND l.item_name <> ''
                ORDER BY p.id""",
            (list(templates.ids), list(lang_by_marketplace)),
        )
        titles = {}
        for tmpl_id, marketplace_id, title in self.env.cr.fetchall():
            titles.setdefault((tmpl_id, lang_by_marketplace[marketplace_id]), []).append(title)
        self.env.cr.execute('SELECT id, name FROM product_template WHERE id = ANY(%s)', (list(templates.ids),))
        current_names = {tid: (name or {}) for tid, name in self.env.cr.fetchall()}
        # Titoli di tutti i marketplace della famiglia: un nome nato dal
        # titolo di un altro marketplace (famiglia creata da ES/FR/UK) è
        # dell'import, non scelto a mano (2026-11-06).
        all_titles = {}
        for (tmpl_id, _lang), tmpl_titles in titles.items():
            all_titles.setdefault(tmpl_id, []).extend(tmpl_titles)
        updated = 0
        for (tmpl_id, lang), tmpl_titles in titles.items():
            tmpl = Tmpl.browse(tmpl_id)
            values = tmpl.product_variant_ids.product_template_attribute_value_ids.mapped('name')
            new_name = _family_common_name(tmpl_titles, values)
            current = current_names.get(tmpl_id, {}).get(lang) or ''
            if not new_name or new_name == current:
                continue
            if not self._is_import_family_name(current, all_titles[tmpl_id]):
                continue
            tmpl.with_context(lang=lang).write({'name': new_name})
            updated += 1
        return updated

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
                vals['x_amazon_brand'] = data['brand']
                vals['x_amazon_found_by_manufacturer'] = False
        # Immagine: solo l'indirizzo, scaricata dopo da download_missing_images
        # (vedi x_amazon_image_url) — scaricarla qui era circa metà del lavoro
        # di creazione di un prodotto (misurato il 2026-11-03).
        # Crea direttamente nello slot di traduzione del marketplace
        # corrente (vedi _marketplace_lang_context) così name/description_
        # sale nascono già nella lingua giusta, non solo in quella di
        # default dell'ambiente.
        template = self.env['product.template'].with_context(
            **self._marketplace_lang_context()
        ).create(vals)
        self._correct_seeded_list_price(template, data['price'])
        if data.get('spec_attrs'):
            self._set_product_attributes(template, data['spec_attrs'])
        self._init_product_warehouse_quant(
            template.product_variant_id, data['fulfillment'], data.get('qty_available') or 0,
        )
        self._upsert_listing(template.product_variant_id, data)
        self._remember_image_url(template.product_variant_id, data.get('image_url'))
        self._remember_gallery(template.product_variant_id, data)
        return template

    @staticmethod
    def _remember_image_url(variant, url):
        """Salva l'indirizzo dell'immagine principale da scaricare dopo (vedi x_amazon_image_url)."""
        if url and variant.x_amazon_image_url != url:
            variant.x_amazon_image_url = url

    def _remember_gallery(self, variant, data):
        """
        Annota sulla variante le immagini Amazon (x_amazon_gallery_pending)
        da salvare nella galleria (ke.product.media) — il salvataggio vero lo
        fa save_pending_galleries, all'inizio di "Scarica immagini mancanti"
        a fine catena: chiesto dall'utente il 2026-11-04 per alleggerire il
        Full Import (misurato: ~9 ms per variante con 6 immagini contro
        ~0,5 ms per questa sola scrittura). Nessuna chiamata Amazon in più:
        le immagini arrivano già con il Catalog API dell'import. Il
        marketplace principale aggiorna sempre; i secondari solo se la
        variante non ha ancora immagini Amazon, così la galleria non cambia
        a ogni marketplace importato (stessa regola dei campi "legacy").
        """
        images = data.get('images')
        if not images or not variant:
            return
        if not self._is_primary_marketplace() and (
            variant.x_amazon_gallery_pending
            or self.env['ke.product.media'].sudo().search_count([
                ('product_id', '=', variant.id), ('source', '=', 'amazon'),
            ], limit=1)
        ):
            return
        marketplace = self._current_marketplace()
        pending = {'ref': f'Amazon {marketplace.code}' if marketplace else 'Amazon', 'images': images}
        if variant.x_amazon_gallery_pending != pending:
            variant.x_amazon_gallery_pending = pending

    _GALLERY_SAVE_BATCH = 500

    def save_pending_galleries(self, commit=True):
        """
        Trasferisce nella galleria (ke.product.media, fonte Amazon) le
        immagini annotate dal Full Import (x_amazon_gallery_pending) e svuota
        l'annotazione. A blocchi, con salvataggio dopo ogni blocco. Ritorna
        il numero di varianti trattate.
        """
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT id FROM product_product WHERE x_amazon_gallery_pending IS NOT NULL ORDER BY id'
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        Media = self.env['ke.product.media'].sudo()
        for start in range(0, len(ids), self._GALLERY_SAVE_BATCH):
            variants = self.env['product.product'].with_context(active_test=False).browse(
                ids[start:start + self._GALLERY_SAVE_BATCH])
            for variant in variants:
                pending = variant.x_amazon_gallery_pending or {}
                Media._set_source_images(
                    variant.product_tmpl_id, variant, 'amazon', pending.get('images'),
                    source_ref=pending.get('ref') or 'Amazon',
                )
            variants.write({'x_amazon_gallery_pending': False})
            if commit:
                self.env.cr.commit()
                self.env.invalidate_all()
        return len(ids)

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

    def _switch_main_sku(self, current, new, new_status):
        """
        True se lo SKU principale del prodotto (x_amazon_sku, default_code)
        passa da 'current' a 'new'. Amazon può avere più SKU sullo stesso
        prodotto: gli altri sono solo annunci in più (_upsert_listing). Lo
        decide solo il marketplace principale (gli altri lo farebbero
        rimbalzare a seconda dell'ordine dei marketplace), con il report di
        quel marketplace:
        - entrambi nel report: attivo batte inattivo; a parità di stato
          vince l'annuncio aperto più di recente (open-date: nei report del
          2026-11-05 la versione "+" è la più recente in 6.735 coppie su
          6.743);
        - lo SKU attuale non è nel report: decide la data salvata sui suoi
          annunci (altri marketplace, import precedenti); senza data, lo SKU
          nuovo vince solo se attivo (è quello in vendita sul marketplace
          principale);
        - entrambi nel report con date assenti o uguali → resta l'attuale.
        Fino al 2026-11-05 decideva il "+" nel nome: con ASIN diversi
        sbagliava (SPRO-4621-104/105, "+" sull'ASIN dell'altro).
        Fuori dal Full Import a report (nessun report noto) cambia come
        prima.
        """
        if not current or current == new:
            return True
        if not self._is_primary_marketplace():
            return False
        report = getattr(self, '_report_info_by_sku', None)
        if report is None:
            return True
        new_active = 'BUYABLE' in (new_status or '')
        new_info = report.get(new, (new_active, None))
        current_info = report.get(current)
        if current_info is None:
            current_date = self._stored_open_date(current)
            if current_date and new_info[1]:
                return new_info[1] > current_date
            return new_info[0]
        if current_info[0] != new_info[0]:
            return new_info[0]
        if current_info[1] and new_info[1]:
            return new_info[1] > current_info[1]
        return False

    def _stored_open_date(self, sku):
        """Data di apertura più recente salvata sugli annunci con questo SKU (tutti i marketplace)."""
        listings = self.env['ke.amazon.listing'].sudo().search([('sku', '=', sku), ('open_date', '!=', False)])
        return max(listings.mapped('open_date'), default=None)

    def _update_product(self, product, data):
        new_sku = data['sku']
        # Confrontato con lo SKU legacy (primario): usato SOLO per
        # invalidare x_amazon_fnsku più sotto (stesso campo condiviso,
        # scritto solo sul marketplace primario — vedi _is_primary_
        # marketplace). NON va riusato per l'FNSKU della listing di
        # QUESTO marketplace: su un marketplace secondario x_amazon_sku
        # riflette l'ultimo marketplace primario, non lo SKU precedente di
        # QUESTA listing, che può differire in entrambe le direzioni
        # (conserverebbe un FNSKU ormai sbagliato, o ne invaliderebbe uno
        # ancora valido) — vedi listing_sku_changed più sotto, calcolato a
        # parte dalla listing stessa. Trovato in revisione, vedi TASK.md
        # "FNSKU".
        sku_changed = new_sku != (product.x_amazon_sku or '')
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
        # Un secondo SKU sullo stesso ASIN (vedi _switch_main_sku) aggiorna
        # solo il suo annuncio: i campi del prodotto restano quelli dello
        # SKU principale.
        if self._is_primary_marketplace() and self._switch_main_sku(
                product.x_amazon_sku, new_sku, data.get('status')):
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
            old_default_code = product.default_code or ''
            if sku_changed:
                # L'FNSKU è legato alla coppia (SKU, venditore): quando lo SKU
                # cambia, Amazon ne genera uno nuovo — quello vecchio non è
                # più valido. Prima questo ramo (prodotto semplice) non lo
                # invalidava mai, a differenza di _do_reconcile che lo fa già
                # — trovato in revisione, vedi TASK.md "FNSKU". Azzerarlo
                # evita di stampare un'etichetta sbagliata finché non arriva
                # il prossimo Import FNSKU (già incatenato al Full Import).
                vals['x_amazon_fnsku'] = False
            if new_sku != old_sku and (not old_sku or product.default_code == old_sku):
                vals['default_code'] = new_sku
                # Indicazione informativa (non usata per nessun match): tiene
                # visibile l'ultimo codice sostituito, per capire a colpo
                # d'occhio quali prodotti sono stati riassegnati (es.
                # rifornimento con '+') senza dover cercare nel log
                # sincronizzazioni. Tracciava solo 'old_sku' — quindi SOLO
                # quando c'era già uno SKU Amazon da sostituire: un prodotto
                # puramente manuale (x_amazon_sku mai impostato, default_code
                # = riferimento interno originale) abbinato per la prima volta
                # via EAN/barcode finiva nel ramo 'not old_sku' sopra, il suo
                # default_code originale veniva sovrascritto e perso senza
                # traccia da nessuna parte (trovato verificando 176 prodotti
                # rinominati così dopo un Full Import reale). Tracciare invece
                # 'old_default_code' copre entrambi i casi con lo stesso
                # campo: quando c'era già un old_sku coincide con prima
                # (default_code era allineato a x_amazon_sku), quando non
                # c'era cattura il codice manuale originale.
                if old_default_code and old_default_code != new_sku:
                    vals['x_amazon_sku_previous'] = old_default_code

        if vals:
            product.write(vals)
        # Nome/descrizione nello slot di traduzione del marketplace corrente
        # (vedi _write_localized_text) — a differenza dei campi "legacy"
        # sopra, NON è gated su _is_primary_marketplace(): ogni marketplace,
        # primario o no, ha un suo testo localizzato da catturare.
        self._write_localized_text(
            product, name=data.get('name'), description_sale=self._format_description(data),
        )
        if len(product.product_variant_ids) == 1:
            self._remember_gallery(product.product_variant_id, data)
        if not product.barcode and data.get('ean'):
            if self._barcode_free(data['ean'], exclude_product_ids=product.product_variant_ids.ids):
                try:
                    product.write({'barcode': data['ean']})
                except Exception as exc:
                    _log.warning('Impossibile assegnare barcode %s a %s: %s',
                                 data['ean'], data['sku'], exc)
        self._upsert_listing(product.product_variant_id, data)

    # -----------------------------------------------------------------------
    # Gestione prodotti con varianti (famiglia Amazon)
    # -----------------------------------------------------------------------

    def _process_variant_listing(self, product, parent_asin, product_type_odoo, maps, update_existing):
        """
        Processa un child ASIN Amazon come variante Odoo.
        Crea o aggiorna il product.template della famiglia e aggiunge la variante.
        Ritorna (stato, msg) dove stato è 'created'|'updated'|'skipped'.

        `maps` (_MatchMaps, condivisa col ramo prodotto-semplice nei tre
        chiamanti) rileva il caso in cui questo SKU è già stato importato
        come prodotto semplice in un passaggio precedente (es. l'arricchimento
        Catalog API era fallito quella volta e non aveva ancora rivelato
        'relationships') — senza questo controllo si creerebbe un secondo
        template per lo stesso SKU reale. Trovato e verificato dopo il Full
        Import del 2026-10-09: 127 casi, tutti con famiglia ancora senza
        altri fratelli — per quelli si promuove sul posto il template
        semplice esistente (sotto). Il caso "famiglia già creata da un
        fratello" non può fondersi in sicurezza qui, viene solo segnalato.

        `maps.variant_tmpl_by_sku` va aggiornata anche qui (via
        maps.register_variant) ogni volta che una variante viene creata/
        confermata, altrimenti resta ferma allo stato dell'inizio del chunk
        e una riga più avanti nello STESSO chunk per lo stesso SKU non la
        trova ancora, duplicando. Trovato e verificato dopo il Full Import
        del 2026-10-12: 22 casi, tutti creati nella stessa finestra di un
        chunk (stesso `create_date` al microsecondo — il `now()` di Postgres
        è fisso per transazione, conferma che sono nati nello stesso commit).
        """
        sku = product['sku']
        asin = product['asin']
        attr_info = product.get('variation_attr')

        conflict = maps.by_sku.get(sku) or maps.by_default_code.get(sku)

        # Prodotto manuale con lo stesso EAN, da unire nella variante (vedi
        # _find_manual_by_ean). Se coincide con 'conflict' lo gestisce già la
        # promozione sul posto qui sotto. Se c'è ma non si può unire e la
        # variante non esiste ancora: salta PRIMA di creare la famiglia, che
        # altrimenti resterebbe vuota.
        #
        # Stessa unione anche per un prodotto semplice con lo stesso SKU
        # quando la famiglia esiste già (prima: "esiste già come prodotto
        # semplice … serve fusione manuale", 410 righe nel Full Import del
        # 2026-10-22 — estensione chiesta dall'utente). Se la famiglia non
        # esiste ancora, il semplice con lo stesso SKU viene invece promosso
        # sul posto a famiglia (sotto), come prima.
        family = maps.by_parent_asin.get(parent_asin)
        merge_by = 'EAN'
        manual, manual_reason = self._find_manual_by_ean(maps, product.get('ean'))
        if manual and conflict and manual.id == conflict.id and not family:
            manual = manual_reason = None
        if (not manual and family and conflict and conflict.id != family.id
                and not conflict.x_amazon_parent_asin):
            manual, manual_reason, merge_by = conflict, self._merge_blocker(conflict), 'SKU'
        if manual and manual_reason and asin not in maps.variant_by_asin:
            msg = (
                f'SALTATO {sku} / {asin}: stesso {merge_by} del prodotto "{manual.name[:40]}" '
                f'(id={manual.id}) che {manual_reason} — serve fusione manuale'
            )
            _log.warning(msg)
            return 'skipped', msg

        # Trova o crea il template della famiglia
        tmpl = maps.by_parent_asin.get(parent_asin)
        if not tmpl:
            if conflict and conflict.x_amazon_parent_asin == parent_asin:
                # Il conflitto trovato è già la famiglia giusta —
                # maps.by_parent_asin non l'aveva ancora (mappa non
                # sincronizzata in qualche punto): riusala invece di
                # crearne una terza.
                tmpl = conflict
            elif conflict and not conflict.x_amazon_parent_asin:
                # Prodotto semplice genuino: promuove sul posto a famiglia
                # — stesso id, stessa product.product, nessun quant da
                # spostare (verificato dal vivo: aggiungere la prima riga
                # attributo a un template con una sola variante riusa quella
                # stessa variante, non ne crea una seconda; _init_product_
                # warehouse_quant più sotto è idempotente, non raddoppia la
                # giacenza già seedata quando questo template era semplice).
                tmpl = conflict
                promo_vals = {'x_amazon_parent_asin': parent_asin}
                if not tmpl.x_amazon_status:
                    promo_vals['x_amazon_status'] = 'active'
                if not tmpl.x_amazon_fulfillment:
                    promo_vals['x_amazon_fulfillment'] = product.get('fulfillment', 'fbm')
                if not tmpl.x_amazon_product_type:
                    promo_vals['x_amazon_product_type'] = product.get('product_type', '')
                stripped_name = _strip_variant_suffix(product['name']) or product['name']
                if stripped_name and stripped_name != tmpl.name:
                    promo_vals['name'] = stripped_name
                tmpl.write(promo_vals)
            elif conflict:
                # conflict è GIA' una famiglia ma con un parent_asin DIVERSO
                # da quello di questa riga — non è il caso "promuovi un
                # semplice", è un conflitto genuino: lo stesso SKU risulta
                # appartenere a due famiglie diverse secondo Amazon in
                # momenti diversi dello stesso import (verificato dal vivo:
                # ASIN del report non più aggiornato, lo SKU non esiste
                # nemmeno più sul marketplace che l'aveva riportato così).
                # Creare una terza famiglia peggiorerebbe le cose: segnala
                # e basta, stesso trattamento del caso sotto. Bug trovato
                # perché la vecchia versione di questo controllo nullificava
                # 'conflict' ogni volta che era già una famiglia, nascondendo
                # anche questo caso genuino invece di segnalarlo.
                attached = self._attach_listing_to_existing_variant(product, maps)
                if attached:
                    return 'updated', self._attached_listing_msg(
                        product, attached, f'parent_asin {parent_asin} invece di {conflict.x_amazon_parent_asin}')
                msg = (
                    f'SALTATO (conflitto): {sku} / {asin} risulta già nella famiglia '
                    f'"{conflict.name[:40]}" (id={conflict.id}, parent_asin={conflict.x_amazon_parent_asin}) '
                    f'ma questa riga indica un parent_asin diverso ({parent_asin}) — '
                    f'probabile ASIN non aggiornato nel report Amazon, serve verifica manuale'
                )
                _log.warning(msg)
                return 'skipped', msg
            else:
                # L'ASIN è già una variante di un'altra famiglia (su questo
                # marketplace Amazon la raggruppa in modo diverso, e lo SKU
                # non ha trovato conflitti, es. SKU diverso fra marketplace):
                # collega l'annuncio a quella variante SENZA creare la
                # famiglia — prima la famiglia nasceva comunque e restava
                # vuota (325 famiglie fantasma su ES nell'import del
                # 2026-11-04, una variante segnaposto senza codice né annunci).
                existing_elsewhere = maps.variant_by_asin.get(asin)
                if existing_elsewhere:
                    attached = self._attach_listing_to_existing_variant(product, maps)
                    if attached:
                        return 'updated', self._attached_listing_msg(
                            product, attached, f'su Amazon appartiene alla famiglia {parent_asin}')
                tmpl = self._create_configurable_template(product, parent_asin, product_type_odoo)
            maps.register_family(parent_asin, tmpl)
        elif conflict and conflict.id != tmpl.id and not (
            manual and manual.id == conflict.id and not manual_reason
        ):
            # La famiglia esiste già (trovata via parent_asin) E lo stesso
            # SKU punta ANCHE a un template diverso (semplice separato, o
            # un'altra famiglia con parent_asin diverso — stesso genere di
            # conflitto del ramo sopra): fondere qui richiederebbe spostare
            # stock.quant/righe ordine, troppo rischioso in automatico
            # dentro il loop di import. Segnala, non creare un'altra
            # variante duplicata.
            if conflict.x_amazon_parent_asin:
                attached = self._attach_listing_to_existing_variant(product, maps)
                if attached:
                    return 'updated', self._attached_listing_msg(
                        product, attached, f'su Amazon appartiene alla famiglia {parent_asin}')
            msg = (
                f'SALTATO (conflitto): {sku} / {asin} esiste già come '
                f'{"prodotto semplice" if not conflict.x_amazon_parent_asin else "famiglia diversa"} '
                f'(id={conflict.id}) separato dalla famiglia "{tmpl.name[:40]}" (id={tmpl.id}) '
                f'— serve fusione manuale'
            )
            _log.warning(msg)
            return 'skipped', msg

        # Controlla se questa variante (child ASIN) esiste già
        existing_variant = maps.variant_by_asin.get(asin)
        if existing_variant:
            # Un altro SKU sulla stessa variante (es. il vecchio senza "+"):
            # aggiornamento normale, l'annuncio si aggiunge e lo SKU della
            # variante lo decide _switch_main_sku — solo il marketplace
            # principale, quindi niente più "rimbalzo" fra le due forme a
            # seconda dell'ordine dei marketplace (motivo della guardia "non
            # sovrascrivere uno SKU con '+'" tolta il 2026-11-05).
            if update_existing:
                # L'FNSKU è legato alla coppia (SKU, venditore): quando lo
                # SKU della variante cambia, quello vecchio non è più
                # valido. Prima questo ramo (variante già esistente) non lo
                # invalidava mai, a differenza di _do_reconcile che lo fa
                # già per lo stesso caso — trovato in revisione, vedi
                # TASK.md "FNSKU". x_amazon_sku qui viene scritto da
                # QUALUNQUE marketplace che tocca questa variante (non solo
                # quello primario, a differenza del ramo prodotto-semplice)
                # — "cambiato" è quindi rispetto all'ultimo marketplace che
                # l'ha scritto, non necessariamente quello corrente: per
                # l'FNSKU della LISTING di questo marketplace serve un
                # confronto separato, vedi listing_sku_changed più sotto.
                # Un secondo SKU sullo stesso ASIN resta solo un annuncio
                # in più: la variante tiene il suo SKU (_switch_main_sku).
                switch_sku = self._switch_main_sku(existing_variant.x_amazon_sku, sku, product.get('status'))
                legacy_sku_changed = switch_sku and (existing_variant.x_amazon_sku or '') != sku
                variant_vals = {'x_amazon_sku': sku, 'default_code': sku} if switch_sku else {}
                if legacy_sku_changed:
                    variant_vals['x_amazon_fnsku'] = False
                if variant_vals:
                    existing_variant.write(variant_vals)
                if legacy_sku_changed and len(tmpl.product_variant_ids) <= 1:
                    # Famiglia con UNA sola variante: _get_amazon_fnsku()
                    # (product_variant.py) ricade sul campo del TEMPLATE
                    # quando quello della variante è vuoto — lasciarlo
                    # stantio qui vanificherebbe l'azzeramento appena fatto
                    # sopra, l'FNSKU vecchio resterebbe comunque recuperabile
                    # e stampabile. Stesso principio già applicato in
                    # _do_reconcile per lo stesso caso. Trovato in revisione.
                    tmpl.write({'x_amazon_fnsku': False})
                # Doppione già creato da un import precedente (variante senza
                # EAN perché occupato da un prodotto manuale): unione ora.
                # Se la variante ha già un barcode, blocca l'unione SOLO se è
                # un EAN davvero diverso da quello del manuale — non se è lo
                # STESSO EAN scritto in un formato diverso (es. un giro
                # precedente a questo fix aveva assegnato l'EAN com'era,
                # senza normalizzare: la variante aveva già 13 cifre con zero
                # di padding, il manuale 12 — stesso codice, confronto
                # bloccava l'unione per sempre anche dopo aver corretto
                # _normalize_ean, perché "la variante ha già un barcode" era
                # vero a prescindere da cosa rappresentasse davvero. Trovato
                # dal vivo il 2026-10-23 rilanciando il Full Import dopo il
                # fix: il prodotto rimaneva ancora doppione).
                manual_ean = manual.product_variant_id.barcode if manual else ''
                barcode_conflicts = (
                    existing_variant.barcode and manual_ean
                    and _normalize_ean(existing_variant.barcode) != _normalize_ean(manual_ean)
                )
                merged_note = ''
                if (not barcode_conflicts or merge_by == 'SKU') and manual and manual.id != tmpl.id:
                    if manual_reason:
                        merged_note = (f' — stesso {merge_by} del prodotto id={manual.id} che '
                                       f'{manual_reason}, serve fusione manuale')
                    else:
                        moved = self._merge_manual_into_variant(maps, manual, existing_variant, merge_by)
                        merged_note = (f' — unito il prodotto {manual.default_code or manual.name} '
                                       f'(id={manual.id}), giacenza spostata: {moved:g}, originale archiviato')
                if not existing_variant.barcode and product.get('ean'):
                    if self._barcode_free(product['ean'], exclude_product_ids=[existing_variant.id]):
                        try:
                            existing_variant.write({'barcode': product['ean']})
                        except Exception as exc:
                            _log.warning('Impossibile assegnare barcode %s a variante %s: %s',
                                         product['ean'], sku, exc)
                # Backfill per varianti create prima del fix (2026-10-27) che
                # ha introdotto image_variant_1920 in _add_variant_to_template
                # — solo se manca ancora, non riscarica a ogni giro.
                if not existing_variant.image_variant_1920:
                    self._remember_image_url(existing_variant, product.get('image_url'))
                self._remember_gallery(existing_variant, product)
                self._write_family_text(tmpl, product)
                tmpl.write({'x_amazon_last_sync': datetime.now()})
                maps.register_variant(asin, sku, existing_variant, tmpl)
                self._upsert_listing(existing_variant, product)
                msg = f'AGGIORNATO variante: {sku} / {asin} → {tmpl.name[:40]}{merged_note}'
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
                    # (es. famiglia con "Variante"="Taglia – Colore" e questa riga
                    # ha solo "Taglia" risolta: la completiamo con il colore che
                    # già conosciamo per il resto della famiglia).
                    attr_value = f'{existing_val} – {attr_value}'
                    attr_label = existing_label
                    _log.info(
                        'Composito variante %s / %s: variation_theme incoerente, '
                        'uso "%s=%s"', sku, asin, attr_label, attr_value
                    )
                else:
                    # Nessun valore per la dimensione esistente nei dati di
                    # QUESTA riga (es. "Variante" è il composito Taglia+Colore
                    # di famiglia, qui Amazon ha risolto solo "Taglia"): prima
                    # si saltava del tutto, lasciando il prodotto scollegato
                    # in attesa che un'altra riga (magari da un altro
                    # marketplace, elaborato in una chiamata separata con
                    # mappe di matching ricostruite da zero) portasse dati
                    # migliori — non garantito, e nel frattempo l'ASIN restava
                    # non scritto da nessuna parte (trovato dal vivo il
                    # 2026-10-26 indagando l'inventario FBA: SASA-D2800261+ /
                    # B07FPKVNPK mai collegato nonostante giacenza reale).
                    # Il valore che QUESTA riga porta (es. "0,130 mm X 50 mt")
                    # è già di per sé un valore distintivo valido — non serve
                    # inventare nulla, basta rietichettarlo sulla dimensione
                    # esistente della famiglia e lasciarlo com'è. Se per caso
                    # collide con un valore già usato da un ASIN diverso, il
                    # controllo _attr_value_taken subito sotto se ne accorge e
                    # aggiunge lo SKU solo in quel caso, non sempre.
                    attr_label = existing_label
                    _log.info(
                        'Variante %s / %s: variation_theme incoerente, nessun valore "%s" '
                        'nei dati di questa riga — riuso "%s=%s" così com\'è',
                        sku, asin, existing_label, attr_label, attr_value,
                    )

        # Valore attributo già usato da una variante con un ASIN diverso
        # (es. due misure della stessa famiglia con lo stesso "colore: Lemon
        # Back"): _add_variant_to_template restituirebbe quella variante e le
        # riscriverebbe SKU/ASIN, "rubandola" al fratello — 85 figli persi
        # così nel Full Import del 2026-10-22, 2 dei quali con due prodotti
        # manuali uniti nella stessa variante. Lo SKU nel valore la rende
        # distinta.
        if self._attr_value_taken(tmpl, attr_label, attr_value, asin):
            attr_value = f'{attr_value} ({sku})'

        if manual and manual.id == tmpl.id:
            manual = None
        variant = self._add_variant_to_template(tmpl, product, attr_label, attr_value)
        if variant:
            maps.register_variant(asin, sku, variant, tmpl)
            self._upsert_listing(variant, product)
            if manual:
                # Niente giacenza iniziale da Amazon: quella vera è sul
                # manuale e viene spostata qui, seedarla la raddoppierebbe.
                moved = self._merge_manual_into_variant(maps, manual, variant, merge_by)
                msg = (
                    f'CREATO variante: {sku} / {asin} [{attr_label}={attr_value}] → {tmpl.name[:40]} '
                    f'— unito il prodotto {manual.default_code or manual.name} (id={manual.id}, '
                    f'stesso {merge_by}), giacenza spostata: {moved:g}, originale archiviato'
                )
                return 'created', msg
            self._init_product_warehouse_quant(
                variant, product.get('fulfillment', 'fbm'), product.get('qty_available') or 0,
            )
            msg = f'CREATO variante: {sku} / {asin} [{attr_label}={attr_value}] → {tmpl.name[:40]}'
            return 'created', msg

        msg = f'ERRORE creazione variante: {sku} / {asin}'
        return 'skipped', msg

    @staticmethod
    def _attr_value_taken(tmpl, attr_label, attr_value, asin):
        """True se una variante di 'tmpl' con un ASIN diverso da 'asin' usa
        già il valore attributo attr_label=attr_value (vedi chiamante)."""
        return any(
            v.x_amazon_asin and v.x_amazon_asin != asin and any(
                ptav.attribute_id.name == attr_label and ptav.name == attr_value
                for ptav in v.product_template_attribute_value_ids
            )
            for v in tmpl.product_variant_ids
        )

    def _find_manual_by_ean(self, maps, ean):
        """
        Prodotto semplice già in Odoo con questo EAN (tipicamente del
        catalogo manuale pre-Amazon), da unire nella variante Amazon invece
        di lasciarlo come doppione. Ritorna (template, None) se unibile,
        (template, motivo) se c'è ma non si può unire in automatico, o
        (None, None) se non c'è.

        Trovato il 2026-10-22: il ramo varianti non cercava mai per EAN, solo
        per SKU/codice — un prodotto manuale con codice diverso dallo SKU
        Amazon (es. 'SVS77954' vs 'SAV-77954', stesso EAN) non veniva
        trovato, e la variante nasceva doppione (senza EAN, già occupato dal
        manuale) con la giacenza vera rimasta sul manuale: 1.403 varianti su
        5.360 senza EAN dopo il Full Import. Decisione dell'utente: creare la
        variante nella famiglia, spostarci la giacenza del manuale e
        archiviare il manuale (vedi _merge_manual_into_variant).
        """
        manual = maps.by_barcode.get(_normalize_ean(ean)) if ean else None
        if not manual or manual.x_amazon_parent_asin:
            return None, None
        return manual, self._merge_blocker(manual)

    def _merge_blocker(self, manual):
        """Motivo per cui il prodotto semplice 'manual' non si può unire in
        automatico in una variante (vedi _merge_manual_into_variant), o None."""
        if len(manual.product_variant_ids) != 1:
            return 'ha più varianti'
        mv = manual.product_variant_id
        if self.env['stock.quant'].sudo().search_count([
            ('product_id', '=', mv.id), ('reserved_quantity', '!=', 0),
        ]):
            return 'ha giacenza riservata'
        if self.env['stock.move'].sudo().search_count([
            ('product_id', '=', mv.id), ('state', 'not in', ('done', 'cancel')),
        ]):
            return 'ha movimenti di magazzino aperti'
        return None

    def _merge_manual_into_variant(self, maps, manual, variant, merge_by='EAN'):
        """
        Unisce il prodotto semplice 'manual' nella variante Amazon 'variant'
        (vedi _find_manual_by_ean): sposta la giacenza interna con lo stesso
        meccanismo di _set_product_stock (_update_available_quantity, nessun
        movimento — come la giacenza iniziale seedata alla creazione), passa
        l'EAN alla variante, sposta sulla variante gli annunci del manuale e
        lo archivia. In un savepoint: se qualcosa fallisce non resta mai una
        giacenza spostata a metà. Ritorna i pezzi spostati.

        Annunci spostati, non eliminati (dal 2026-11-05): sono SKU Amazon
        dello stesso prodotto fisico (es. "PFS-1366190" e "PFS-1366190.",
        stesso EAN, due ASIN). Eliminandoli lo SKU del manuale non restava da
        nessuna parte e la riga FR senza ASIN con quello SKU creava un
        prodotto nuovo. Lo SKU del manuale resta riconoscibile anche nelle
        mappe di questo import (variant_tmpl_by_sku).
        """
        Quant = self.env['stock.quant'].sudo()
        mv = manual.product_variant_id
        ean = mv.barcode
        with self.env.cr.savepoint():
            to_move = [
                (q.location_id, q.quantity) for q in Quant.search([
                    ('product_id', '=', mv.id),
                    ('location_id.usage', '=', 'internal'),
                    ('quantity', '!=', 0),
                ])
            ]
            for location, qty in to_move:
                Quant._update_available_quantity(variant, location, qty)
                Quant._update_available_quantity(mv, location, -qty)
            mv.write({'barcode': False})
            # Mai sovrascrivere un EAN che la variante ha già (caso SKU).
            if ean and not variant.barcode:
                variant.write({'barcode': ean})
            # Costo d'acquisto: prima restava sul prodotto archiviato (485 su
            # 1.013 unioni nell'import del 2026-11-03, e nel resto del
            # catalogo il costo c'è solo su 151 prodotti su 18.628). Copiato
            # solo se la variante non ne ha già uno.
            cost = mv.standard_price
            cost_copied = bool(cost and not variant.standard_price)
            if cost_copied:
                variant.write({'standard_price': cost})
            manual_listings = self.env['ke.amazon.listing'].sudo().search([('product_id', '=', mv.id)])
            # Spostati dall'import: non diventano "Abbinato a mano".
            manual_listings.with_context(ke_import_listing=True).write({'product_id': variant.id})
            moved = sum(qty for _loc, qty in to_move)
            manual.message_post(body=(
                f'Archiviato dal Full Import Amazon: unito nella variante '
                f'{variant.default_code or variant.display_name} della famiglia '
                f'"{variant.product_tmpl_id.name}" (stesso {merge_by}; EAN {ean or "-"}). '
                f'Giacenza spostata sulla variante: {moved:g}.'
                + (f' Costo d\'acquisto copiato: {cost:g}.' if cost_copied else '')
            ))
            variant.product_tmpl_id.message_post(body=(
                f'Variante {variant.default_code or variant.display_name}: unito il prodotto '
                f'{manual.default_code or manual.name} (EAN {ean or "-"}), giacenza spostata: {moved:g}. '
                f'Il prodotto originale è archiviato.'
            ))
            manual.write({'active': False})
        maps.forget(manual)
        if ean:
            maps.by_barcode.pop(_normalize_ean(ean), None)
        if variant.product_tmpl_id.x_amazon_parent_asin:
            for code in {manual.default_code, manual.x_amazon_sku, *manual_listings.mapped('sku')} - {False, ''}:
                maps.variant_tmpl_by_sku.setdefault(code, variant.product_tmpl_id)
        return moved

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
            # Dimensioni/categoria-Amazon/brand mancavano qui — scritti solo
            # da _create_product (prodotti semplici), mai da questa funzione
            # (famiglie varianti). Trovato verificando un Full Import reale:
            # 0 famiglie su 2.166 con x_amazon_category/x_amazon_brand, solo
            # 76 con dimensioni (quelle arrivate da un giro successivo di
            # apply_category_mapping, non dalla creazione). Il dato era già
            # disponibile in first_child (_merge_catalog lo popola), solo mai
            # copiato in vals.
            'product_length': first_child.get('length_cm') or 0.0,
            'product_width': first_child.get('width_cm') or 0.0,
            'product_height': first_child.get('height_cm') or 0.0,
            'description_sale': description,
            'x_amazon_parent_asin': parent_asin,
            'x_amazon_product_type': first_child.get('product_type', ''),
            'x_amazon_status': 'active',
            'x_amazon_fulfillment': first_child.get('fulfillment', 'fbm'),
            'x_amazon_last_sync': datetime.now(),
        }
        if first_child.get('category'):
            vals['x_amazon_category'] = first_child['category']
        if first_child.get('browse_node_id'):
            vals['x_amazon_browse_node_id'] = first_child['browse_node_id']
        if first_child.get('brand'):
            vals['x_amazon_brand'] = first_child['brand']
        # Immagine del template: la imposta download_missing_images da quella
        # della prima variante (x_amazon_image_url), non scaricata qui.
        template = self.env['product.template'].with_context(
            **self._marketplace_lang_context()
        ).create(vals)
        self._correct_seeded_list_price(template, first_child['price'])
        return template

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
        # image_variant_1920 (immagine PROPRIA della variante, non quella
        # condivisa del template — vedi _create_configurable_template, che
        # seeda image_1920 del template solo dal primo figlio processato):
        # senza questo, ogni variante successiva alla prima di una famiglia
        # restava senza foto propria, mostrando sempre e solo la foto del
        # primo figlio anche per colori/taglie visibilmente diversi.
        # Trovato dal vivo il 2026-10-27, segnalato dall'utente.
        self._remember_image_url(variant, product.get('image_url'))
        self._remember_gallery(variant, product)
        self._write_family_text(tmpl, product)
        self._seed_variant_price(variant, product)
        return variant

    def _seed_variant_price(self, variant, product):
        """
        Prezzo proprio della variante (ke_list_price, ke_product_catalog) alla
        creazione, se il suo prezzo Amazon dà un prezzo base diverso da quello
        della famiglia (seedato dal primo figlio): 276 famiglie Amazon IT con
        varianti a prezzi diversi al 2026-11-05, prima tutte al prezzo del
        primo figlio. Non tocca un prezzo variante già scelto.
        """
        if variant.ke_list_price or not product.get('price'):
            return
        base = self._base_price_from_amazon(product['price'], variant.product_tmpl_id)
        if base and abs(base - variant.product_tmpl_id.list_price) > 0.005:
            variant.ke_list_price = base

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

    # Immagini scaricate in parallelo (solo HTTP: la scrittura su Odoo resta
    # in questo thread, un cursore non si condivide): il collo di bottiglia è
    # l'attesa della rete, non Odoo.
    _IMAGE_DOWNLOAD_WORKERS = 6
    _IMAGE_BATCH = 30

    def _catalog_main_image_url(self, asin):
        """Indirizzo dell'immagine MAIN dal Catalog API — solo ripiego, se il Full Import non l'ha salvato."""
        catalog = self.client.get_catalog_item(asin) if (self.client and asin) else {}
        return _best_image_url(catalog)

    _FEES_BATCH = 20

    def update_listing_fees(self, log_rec, marketplaces, client_factory):
        """
        Commissioni Amazon stimate (Product Fees API) per gli annunci attivi
        con prezzo, marketplace per marketplace, 20 per chiamata: commissione
        di vendita reale per categoria e, per FBA, tariffa di logistica
        (2026-11-06, per l'Analisi prezzi: prima si ipotizzava un 15% fisso).
        Salta gli annunci già stimati allo stesso prezzo senza errori, così un
        secondo giro (o la ripresa dopo un riavvio) chiede solo il resto.
        Solo letture su Amazon. Ritorna (stimati, saltati, errori).
        """
        Listing = self.env['ke.amazon.listing'].sudo()
        done = skipped = errors = 0
        lines = []
        for marketplace in marketplaces:
            listings = Listing.search([
                ('marketplace_id', '=', marketplace.id), ('status', '=', 'active'),
                ('price', '>', 0), ('sku', '!=', False),
            ])
            todo = listings.filtered(lambda l: not (l.fee_date and not l.fee_error
                                                    and abs(l.fee_price - l.price) < 0.005))
            skipped += len(listings) - len(todo)
            if not todo:
                continue
            client = client_factory(marketplace)
            currency = (marketplace.currency_id or self.env.company.currency_id).name
            mp_done = mp_err = 0
            for start in range(0, len(todo), self._FEES_BATCH):
                batch = todo[start:start + self._FEES_BATCH]
                items = [{'id': l.id, 'sku': l.sku, 'price': l.price, 'currency': currency,
                          'is_fba': l.fulfillment == 'fba'} for l in batch]
                try:
                    results = client.get_fees_estimates(items)
                except Exception as exc:
                    _log.warning('Commissioni %s: lotto fallito: %s', marketplace.code, exc)
                    results = {str(l.id): {'error': str(exc)[:250]} for l in batch}
                now = datetime.now()
                for listing in batch:
                    res = results.get(str(listing.id)) or {'error': 'nessuna stima restituita'}
                    if res.get('error'):
                        listing.write({'fee_error': res['error'], 'fee_date': now, 'fee_price': listing.price})
                        mp_err += 1
                        continue
                    listing.write({
                        'fee_total': res['total'], 'fee_referral': res['referral'],
                        'fee_fulfillment': res['fulfillment'], 'fee_other': res['other'],
                        'fee_price': listing.price, 'fee_date': now, 'fee_error': False,
                    })
                    mp_done += 1
                log_rec.write({'products_total': done + mp_done + errors + mp_err + skipped,
                               'products_updated': done + mp_done, 'products_error': errors + mp_err,
                               'products_skipped': skipped,
                               'log_lines': '\n'.join(lines + [f'[{marketplace.code}] {start + len(batch)}/{len(todo)}'])})
                self.env.cr.commit()
            done += mp_done
            errors += mp_err
            lines.append(f'[{marketplace.code}] commissioni stimate: {mp_done}, errori: {mp_err}')
        lines.append(f'Già stimati allo stesso prezzo (saltati): {skipped}.')
        log_rec.write({
            'state': 'done' if done or not errors else 'error',
            'products_total': done + errors + skipped, 'products_updated': done,
            'products_skipped': skipped, 'products_error': errors,
            'log_lines': '\n'.join(lines), 'end_date': datetime.now(),
        })
        self.env.cr.commit()
        return done, skipped, errors

    def download_missing_images(self, log_rec):
        """
        Scarica l'immagine mancante per: prodotti semplici (product.template,
        image_1920), varianti di famiglia (product.product, image_variant_1920
        — la foto PROPRIA della variante) e template di famiglia senza
        immagine (image_1920, da quella della sua prima variante). Prima
        copriva solo i semplici: trovato dal vivo il 2026-10-27, varianti
        senza immagine dopo l'import.

        Dal 2026-11-03 il Full Import non scarica più le immagini: salva solo
        l'indirizzo (product.product.x_amazon_image_url) e a fine catena
        (dopo l'Import FNSKU) fa partire questo. Qui si usa quell'indirizzo
        senza chiamare Amazon; il Catalog API resta solo un ripiego per i
        prodotti che non l'hanno (creati prima di questa modifica). I download
        girano in parallelo a lotti, le scritture in sequenza.
        """
        from concurrent.futures import ThreadPoolExecutor

        # Prima la galleria (solo indirizzi, nessun download): le immagini
        # raccolte dal Full Import (vedi _remember_gallery).
        gallery_count = self.save_pending_galleries()

        Template = self.env['product.template']
        # Semplici anche senza ASIN sul prodotto: chi esiste solo su ES/FR/UK
        # ha ASIN e SKU solo sugli annunci (i campi del prodotto sono del
        # marketplace principale) — 302 prodotti senza immagine così su
        # arundo_prod il 2026-11-05 (es. PFS-1530586), pur con l'indirizzo.
        simple_products = Template.search([
            ('x_amazon_parent_asin', '=', False),
            '|', '|', ('x_amazon_asin', '!=', False),
            ('product_variant_ids.x_amazon_image_url', '!=', False),
            ('product_variant_ids.x_amazon_listing_ids.asin', '!=', False),
            ('image_1920', '=', False),
        ])
        family_variants = self.env['product.product'].search([
            '|', ('x_amazon_asin', '!=', False), ('x_amazon_image_url', '!=', False),
            ('image_variant_1920', '=', False),
            # product_variant_count non è stored (niente domain diretto) —
            # x_amazon_parent_asin sul template sì, ed è vero per ogni
            # famiglia (vedi _create_configurable_template), mai per un
            # prodotto semplice: stesso filtro, campo diverso.
            ('product_tmpl_id.x_amazon_parent_asin', '!=', False),
        ])
        family_templates = Template.search([
            ('x_amazon_parent_asin', '!=', False),
            ('image_1920', '=', False),
        ])

        def simple_url(tmpl):
            variant = tmpl.product_variant_id
            asin = tmpl.x_amazon_asin or variant.x_amazon_listing_ids.filtered('asin')[:1].asin
            return variant.x_amazon_image_url, asin

        def variant_url(variant):
            return variant.x_amazon_image_url, variant.x_amazon_asin

        def family_url(tmpl):
            with_url = tmpl.product_variant_ids.filtered('x_amazon_image_url')[:1]
            return with_url.x_amazon_image_url, ''

        # (record, campo immagine, funzione che dà (url salvato, asin di ripiego))
        targets = [(p, 'image_1920', simple_url) for p in simple_products] \
            + [(v, 'image_variant_1920', variant_url) for v in family_variants] \
            + [(t, 'image_1920', family_url) for t in family_templates]
        total = len(targets)
        ok = errors = skipped = catalog_calls = 0
        lines = [f'Galleria immagini salvata per {gallery_count} varianti.'] if gallery_count else []

        log_rec.write({'products_total': total})
        self.env.cr.commit()

        with ThreadPoolExecutor(max_workers=self._IMAGE_DOWNLOAD_WORKERS) as pool:
            for start in range(0, total, self._IMAGE_BATCH):
                batch = targets[start:start + self._IMAGE_BATCH]
                urls = []
                for record, _field, url_of in batch:
                    url, asin = url_of(record)
                    if not url and asin:
                        try:
                            url = self._catalog_main_image_url(asin)
                            catalog_calls += 1
                            if url and record._name == 'product.product':
                                record.x_amazon_image_url = url
                            elif url:
                                record.product_variant_id.x_amazon_image_url = url
                        except Exception as exc:
                            _log.warning('Catalog API per immagine di %s fallito: %s', asin, exc)
                            url = ''
                    urls.append(url)
                images = list(pool.map(lambda u: _fetch_image_b64(u) if u else None, urls))
                for (record, image_field, _url_of), url, img in zip(batch, urls, images):
                    code = record.default_code or record.x_amazon_asin or record.id
                    try:
                        if img:
                            record.write({image_field: img})
                            ok += 1
                            msg = f'OK {code}: immagine scaricata'
                        else:
                            skipped += 1
                            msg = f'SKIP {code}: nessuna immagine disponibile'
                    except Exception as exc:
                        errors += 1
                        msg = f'ERRORE {code}: {exc}'
                        _log.error(msg, exc_info=True)
                    lines.append(msg)
                # Progresso a ogni lotto: con migliaia di immagini, senza
                # l'utente vedrebbe i contatori a zero per molto tempo.
                log_rec.write({
                    'products_updated': ok,
                    'products_skipped': skipped,
                    'products_error': errors,
                    'log_lines': '\n'.join(lines),
                })
                self.env.cr.commit()

        lines.append(f'Chiamate al Catalog API per indirizzi mancanti: {catalog_calls}.')
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
                    amazon_price = self.env['ke.amazon.listing'].sudo()._price_for(
                        product, self._current_marketplace(), sku)
                    self.client.patch_price(sku, product_type, amazon_price)
                    ops_done.append(f'prezzo={amazon_price:.2f} {self._price_currency_name()}')
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
                        amazon_price = self.env['ke.amazon.listing'].sudo()._price_for(
                            variant, self._current_marketplace(), sku)
                        self.client.patch_price(sku, product_type, amazon_price)
                        ops_done.append(f'prezzo={amazon_price:.2f} {self._price_currency_name()}')
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
            # listing.product_id è già la variante (ke.amazon.listing è
            # scoped per variante) — prezzo/quantità letti/scritti su di
            # essa direttamente, non più sul template (fix di un bug latente:
            # ignorava eventuali regole di pricelist specifiche per variante).
            product = listing.product_id
            sku = listing.sku
            product_type = listing.product_type or 'PRODUCT'
            ops_done = []
            error_msg = ''

            if listing.sync_price:
                try:
                    amazon_price = listing._price_to_send()
                    self.client.patch_price(sku, product_type, amazon_price)
                    ops_done.append(f'prezzo={amazon_price:.2f} {self._price_currency_name()}')
                except Exception as exc:
                    error_msg += f'prezzo: {exc}; '
                    _log.error('Errore patch_price %s (%s): %s', sku, marketplace.code, exc)

            if listing.sync_qty:
                if listing.fulfillment == 'fbm':
                    try:
                        qty = int(self._qty_in_warehouse(product, fbm_warehouse))
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

    # NON 20 nonostante la doc SP-API dica "fino a 20 identifiers": verificato
    # dal vivo che la risposta di searchCatalogItems tronca silenziosamente
    # 'items' a un massimo di 10, anche quando 'numberOfResults' nella stessa
    # risposta conferma che tutti i richiesti esistono e anche senza
    # 'pagination'/nextToken per recuperare il resto — nessun errore, nessun
    # segnale di troncamento, i dati mancanti finivano scambiati per "Amazon
    # non ce li ha". Riprodotto in modo consistente con richieste da 12/15/20
    # identifiers (sempre e solo 10 item tornati), anche con pause extra tra
    # le chiamate (non è throttling). 10 richiesti -> 10 tornati, sempre.
    _CAT_MAP_BATCH_SIZE = 10

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
        Copre anche i TEMPLATE di famiglie di varianti (x_amazon_parent_asin
        impostato), non solo i prodotti semplici: per una famiglia l'ASIN non
        vive sul template (resta sempre vuoto), ma su una delle sue varianti
        — qui si usa la prima variante come rappresentante per interrogare il
        Catalog API, scrivendo comunque sul template (categoria/dimensioni/
        brand sono dati di famiglia, condivisi da tutte le varianti). Aggiunto
        dopo aver scoperto che _create_configurable_template non scriveva
        affatto questi campi alla creazione (0 su 2.166 famiglie in un Full
        Import reale) — quel bug è stato corretto a parte, ma le famiglie già
        create prima del fix restavano comunque scoperte, perché questa
        funzione non le considerava proprio (dominio filtrato solo su
        x_amazon_asin, sempre vuoto per una famiglia).
        Ritorna (next_offset, total) o (None, total) se completato.
        """
        domain = ['|', ('x_amazon_asin', '!=', False), ('x_amazon_parent_asin', '!=', False)]
        if max_id:
            domain = ['&'] + domain + [('id', '<=', max_id)]
        products = self.env['product.template'].search(domain, order='id asc')
        total = len(products)
        chunk = products[offset:offset + chunk_size]
        if not chunk:
            return None, total

        updated = skipped = errors = 0
        msg_by_product_id = {}
        to_fetch = []  # lista di (product, query_asin)

        for product in chunk:
            if product.x_amazon_parent_asin:
                representative = product.product_variant_ids[:1]
                query_asin = representative.x_amazon_asin if representative else ''
            else:
                query_asin = product.x_amazon_asin
            ref = product.default_code or query_asin or f'#{product.id}'
            if not query_asin:
                skipped += 1
                msg_by_product_id[product.id] = f'SKIP {ref}: famiglia senza varianti con ASIN, impossibile interrogare Amazon'
                continue
            if _needs_catalog_data(product, self._marketplace_lang_context().get('lang')):
                to_fetch.append((product, query_asin))
            else:
                skipped += 1
                msg_by_product_id[product.id] = f'SKIP {ref}: dati già presenti'

        for i in range(0, len(to_fetch), self._CAT_MAP_BATCH_SIZE):
            batch = to_fetch[i:i + self._CAT_MAP_BATCH_SIZE]
            try:
                catalog_by_asin = self.client.get_catalog_items_batch([qa for _, qa in batch])
            except Exception as exc:
                # La chiamata batch è fallita per TUTTO il gruppo (throttling
                # dopo i retry, errore di rete, ecc.) — non è "Amazon non ha
                # questi dati", è "non siamo riusciti a chiederglieli". Segnare
                # ERRORE (non SKIP) fa sì che needs_catalog resti vero per
                # questi prodotti e vengano ritentati al prossimo giro,
                # invece di essere scambiati per genuinamente privi di dati.
                errors += len(batch)
                for product, query_asin in batch:
                    ref = product.default_code or query_asin
                    msg = f'ERRORE {ref}: chiamata batch fallita ({exc}), da ritentare'
                    _log.error(msg)
                    msg_by_product_id[product.id] = msg
                continue

            for product, asin in batch:
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
                    description = _cat_val(cat_attrs, 'product_description', self._catalog_lang())
                    bullets = _bullets(cat_attrs, self._catalog_lang())

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
                    # Nella lingua del marketplace (stesso slot controllato da
                    # _needs_catalog_data), altrimenti la descrizione ES/FR/UK
                    # non arriverebbe mai e il prodotto verrebbe richiesto a
                    # ogni giro.
                    lang_ctx = self._marketplace_lang_context()
                    if (description or bullets) and not _has_text_in_lang(
                            product, 'description_sale', lang_ctx.get('lang')):
                        formatted = self._format_description({'description': description, 'bullet_points': bullets})
                        if formatted:
                            vals['description_sale'] = formatted

                    if vals:
                        product.with_context(**lang_ctx).write(vals)
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

    def sync_fba_quantities_page(self, log_rec, page_token=None, asin_totals=None):
        """
        Scarica UNA pagina di inventario FBA e la accumula in 'asin_totals'
        (dict mutato in place, {asin: quantità totale}); riconcilia i quant
        Odoo solo sull'ULTIMA pagina (quando Amazon non restituisce più un
        nextToken). Il chiamante (_thread_sync_fba_quantities) possiede
        'asin_totals' per tutta la durata di un marketplace e lo passa a ogni
        chiamata — deve azzerarlo se riparte da capo per un token scaduto,
        altrimenti le pagine già viste verrebbero sommate due volte.

        Aggrega per ASIN, non per SKU: durante una transizione di
        etichettatura Amazon (FNSKU) lo stesso ASIN può comparire sotto due
        SKU contemporaneamente attivi (il vecchio, in esaurimento, e il nuovo
        con '+', su cui arrivano i rifornimenti) — Odoo tratta l'ASIN come
        identità primaria del prodotto (deciso in sessione), quindi la
        giacenza reale è la SOMMA di entrambi gli SKU, non quella di uno solo.
        Prima di questo fix, riconciliando riga per riga per SKU, il vecchio
        SKU (che punta allo stesso prodotto Odoo rinominato al nuovo '+' — vedi
        _update_product/best_sku_by_asin) semplicemente non trovava più nessun
        prodotto con quello x_amazon_sku esatto e la sua giacenza spariva dalla
        vista di Odoo, silenziosamente. Aggregare per pagina non basterebbe:
        se il vecchio e il nuovo SKU finiscono su pagine diverse (probabile,
        l'inventario è paginato ~50 righe a volta), sommarli pagina per pagina
        sovrascriverebbe invece di sommare (_set_product_stock imposta un
        valore ASSOLUTO) — da qui la necessità di accumulare su TUTTE le
        pagine prima di scrivere qualunque quant.

        Se la giacenza aggregata per un ASIN è aumentata rispetto all'ultima
        sync, la differenza viene "consumata" dalla location 'In transito
        verso Amazon' (rifornimenti registrati con ship_to_fba) prima di
        riconciliare al valore assoluto — stesso ordine (transito prima,
        assoluto dopo) del fix P1 doppio-incremento originale, per lo stesso
        motivo: al contrario il quant finirebbe doppiamente incrementato.

        Fase 3 multi-marketplace (inventario FBA Pan-Europeo, confermato
        dall'utente): "pooled" non è un caso speciale per IT/DE/ES/FR, è
        determinato da ke.amazon.marketplace.is_fba_pooled() — vero se questo
        marketplace condivide il magazzino FBA effettivo con almeno un altro
        marketplace attivo. Per un marketplace pooled, il magazzino FBA in
        Odoo è condiviso e x_amazon_qty del template resta scritto a ogni
        giro come riepilogo visibile (stesso stock fisico, riconfermato —
        ridondante ma innocuo — da ognuno dei marketplace pooled che il
        chiamante itera). Per un marketplace NON pooled (inventario davvero
        separato, es. UK/US fuori dal pool EU) la quantità nuova si scrive
        su ke.amazon.listing.qty, x_amazon_qty non viene toccato.

        La quantità PRECEDENTE usata per decidere quanto consumare dal
        transito NON si legge più da questi stessi campi di visualizzazione
        (qty/x_amazon_qty) — un Full Import o una riconciliazione nel
        frattempo li sovrascrivono con un dato di fonte e momento diversi
        dall'inventario FBA reale, falsando il "precedente" del giro
        successivo (bug trovato in revisione, vedi TASK.md "Giacenze").
        Si legge invece da un campo dedicato, scritto SOLO da questa
        funzione: ke.amazon.listing.fba_qty_snapshot per VARIANTE+
        MARKETPLACE sul ramo dedicato (inventario davvero separato, un
        proprio precedente corretto); product.product.x_amazon_
        fba_qty_snapshot per VARIANTE sul ramo pooled, condiviso fra TUTTI
        i marketplace dello stesso pool fisico — non per singolo
        marketplace, altrimenti due marketplace pooled sincronizzati nello
        stesso giro leggerebbero ciascuno un proprio "precedente" mai
        aggiornato dall'altro e consumerebbero entrambi lo stesso
        incremento reale dal transito (bug reale trovato in revisione,
        distinto da quello — già corretto — del clobbering fra sorelle
        sullo stesso marketplace).
        Ritorna il next_token per la pagina successiva, o None se era l'ultima
        (e la riconciliazione è già stata fatta).
        """
        marketplace = self._current_marketplace()
        fba_location = self._get_fba_warehouse_location(marketplace)
        if not fba_location:
            raise UserError('Magazzino FBA non configurato (Impostazioni → Amazon → Magazzini).')
        mp_tag = f'[{marketplace.code}] ' if marketplace else ''

        items, new_page_token = self.client.fetch_inventory_page(page_token)
        # Le unità Grade & Resell sono usate: non vanno sommate alla giacenza
        # del prodotto nuovo con lo stesso ASIN, né scriverne la condizione.
        # Le unità Grade & Resell vanno nella giacenza "FBA – Usato" del
        # prodotto originale (_sync_resell_inventory, sull'ultima pagina),
        # mai in quella del nuovo. Accumulo azzerato sulla prima pagina: un
        # riavvio da capo per token scaduto riparte da page_token=None.
        if not page_token:
            self._resell_inventory = {}
        for item in items:
            if _is_grade_and_resell_sku(item.get('sku')):
                self._resell_inventory[item['sku'].strip()] = item
        items = [i for i in items if not _is_grade_and_resell_sku(i.get('sku'))]
        # Condizione per SKU, pagina per pagina: indipendente dalle quantità
        # (che si aggregano per ASIN e si scrivono solo sull'ultima pagina).
        self._store_item_conditions(items, marketplace)

        if asin_totals is None:
            asin_totals = {}
        for entry in items:
            asin = entry.get('asin') or ''
            if not asin:
                continue
            asin_totals[asin] = asin_totals.get(asin, 0.0) + float(entry.get('qty') or 0)

        if new_page_token:
            # Non ultima pagina: solo accumulo, nessuna scrittura su Odoo qui.
            return new_page_token

        # --- Ultima pagina: riconcilia tutto quello accumulato per il marketplace ---
        transit_location = self._get_or_create_transit_location(fba_location)
        dedicated = bool(marketplace) and not marketplace.is_fba_pooled()

        updated = errors = skipped = 0
        lines = [f'{mp_tag}Inventario scaricato: {len(asin_totals)} ASIN distinti.']
        lines.extend(self._condition_log_lines(mp_tag))
        lines.extend(self._sync_resell_inventory(marketplace, mp_tag))

        for asin, fba_qty in asin_totals.items():
            product_tmpl = self.env['product.template'].search(
                [('x_amazon_asin', '=', asin)], limit=1,
            )
            matched_on = 'template'
            if product_tmpl:
                product_variant = product_tmpl.product_variant_id
            else:
                variant = self.env['product.product'].search(
                    [('x_amazon_asin', '=', asin)], limit=1,
                )
                if not variant:
                    skipped += 1
                    lines.append(f'{mp_tag}SALTATO {asin}: nessun prodotto Odoo con questo x_amazon_asin')
                    continue
                matched_on = 'variante'
                product_variant = variant
                product_tmpl = variant.product_tmpl_id

            try:
                # ke.amazon.listing è scoped per VARIANTE — la lookup usa
                # sempre product_variant.id (la variante risolta per questa
                # riga), mai product_tmpl.id, altrimenti due sorelle della
                # stessa famiglia condividerebbero la stessa riga/quantità
                # (stesso bug di clobbering già risolto altrove in questo
                # refactor). Un'unica ricerca per entrambi i rami
                # dedicato/pooled: prima il ramo pooled ne faceva una seconda
                # identica solo per scrivere, qui serve anche in lettura.
                # Con più SKU sullo stesso ASIN la giacenza (sommata per
                # ASIN) va sull'annuncio principale (_main_for).
                listing = self.env['ke.amazon.listing'].sudo()._main_for(product_variant, marketplace)

                # La giacenza precedente per il confronto NON va letta da
                # 'listing.qty'/'x_amazon_qty' (i campi di VISUALIZZAZIONE,
                # scritti anche da Full Import/riconciliazione con un dato
                # di fonte e momento diversi dall'inventario FBA reale) ma
                # da un campo dedicato, scritto solo da questa funzione —
                # altrimenti un Full Import nel frattempo falsa il
                # "precedente" del giro successivo. Bug reale trovato in
                # revisione, vedi TASK.md "Giacenze".
                if dedicated:
                    # Inventario davvero separato per questo marketplace:
                    # snapshot per VARIANTE+MARKETPLACE, su listing.
                    previous_qty = listing.fba_qty_snapshot if listing else 0
                else:
                    # Pool fisico unico condiviso da più marketplace (es.
                    # IT/ES/FR Pan-EU): snapshot per VARIANTE, non per
                    # singolo marketplace, su un campo della variante
                    # stessa — altrimenti due marketplace pooled
                    # sincronizzati nello stesso giro (stesso ASIN, stessa
                    # giacenza fisica reale) leggono ciascuno un proprio
                    # "precedente" mai aggiornato dall'altro e consumano
                    # ENTRAMBI lo stesso incremento reale dal transito
                    # (doppio consumo per una spedizione ricevuta una volta
                    # sola). Bug reale trovato in revisione, vedi TASK.md
                    # "Giacenze".
                    previous_qty = product_variant.x_amazon_fba_qty_snapshot
                increase = fba_qty - previous_qty
                moved = 0.0
                if increase > 0:
                    moved = self._consume_transit_stock(
                        product_variant, transit_location, fba_location, increase,
                    )
                self._set_product_stock(product_variant, fba_location, fba_qty)

                # Scritture di VISUALIZZAZIONE: invariate, possono restare
                # ridondanti con lo snapshot interno sopra senza alcun
                # rischio — non sono più la fonte di 'previous_qty'.
                if listing:
                    listing.write({'qty': int(fba_qty), 'last_sync': datetime.now()})
                elif marketplace:
                    listing = self.env['ke.amazon.listing'].sudo().create({
                        'product_id': product_variant.id, 'marketplace_id': marketplace.id,
                        'asin': asin, 'qty': int(fba_qty), 'last_sync': datetime.now(),
                    })
                if dedicated:
                    if listing:
                        listing.write({'fba_qty_snapshot': int(fba_qty)})
                else:
                    product_tmpl.write({'x_amazon_qty': int(fba_qty)})
                    product_variant.write({'x_amazon_fba_qty_snapshot': int(fba_qty)})
                updated += 1
                msg = (
                    f'{mp_tag}OK {asin} ({matched_on} #{product_tmpl.id}): '
                    f'FBA qty {previous_qty:g} → {fba_qty:g}'
                )
                if moved > 0:
                    msg += f' — {moved:g} spostate da transito verso FBA'
                lines.append(msg)
            except Exception as exc:
                errors += 1
                lines.append(f'{mp_tag}ERRORE {asin}: {exc}')
                _log.error('sync_fba: errore aggiornamento %s: %s', asin, exc)

        lines.append(
            f'{mp_tag}Riepilogo: {log_rec.products_updated + updated} aggiornati, '
            f'{log_rec.products_skipped + skipped} saltati (nessun match), '
            f'{log_rec.products_error + errors} errori, '
            f'{len(asin_totals)} ASIN totali da Amazon.'
        )
        new_log = ((log_rec.log_lines or '') + '\n' + '\n'.join(lines)).strip()
        log_rec.write({
            'products_total': log_rec.products_total + len(asin_totals),
            'products_updated': log_rec.products_updated + updated,
            'products_skipped': log_rec.products_skipped + skipped,
            'products_error': log_rec.products_error + errors,
            'log_lines': new_log,
        })
        self.env.cr.commit()
        return None

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
        if not variant and sku:
            # Annuncio con questo SKU (un secondo SKU dello stesso prodotto,
            # es. il vecchio senza "+"): dal 2026-11-05 ognuno ha il suo.
            marketplace = self._current_marketplace()
            Listing = self.env['ke.amazon.listing'].sudo()
            domain = [('sku', '=', sku)] + ([('marketplace_id', '=', marketplace.id)] if marketplace else [])
            listing = Listing.search(domain, limit=1) or Listing.search([('sku', '=', sku)], limit=1)
            variant = listing.product_id
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

    # InventorySummary.condition (testo libero, es. 'NewItem' — modello
    # ufficiale FBA Inventory: "for example, New Item") → condizione Odoo.
    # Chiave normalizzata: minuscolo, senza spazi/trattini/underscore. Tutte
    # le varianti 'New…' (con garanzia, OEM, confezione aperta) stampano
    # 'Nuovo'; un valore non riconosciuto non sovrascrive niente.
    _CONDITION_MAP = {
        'new': 'new_new', 'newitem': 'new_new', 'newwithwarranty': 'new_new',
        'newoem': 'new_new', 'newopenbox': 'new_new',
        'usedlikenew': 'used_like_new', 'usedverygood': 'used_very_good',
        'usedgood': 'used_good', 'usedacceptable': 'used_acceptable',
    }

    def _store_item_conditions(self, items, marketplace):
        """
        Salva la condizione Amazon di ogni SKU dell'inventario FBA, per
        stamparla sull'etichetta FNSKU come fa Amazon ('Nuovo'), richiesto
        dall'utente il 2026-10-30. Sull'inserzione della variante per questo
        marketplace e, per un prodotto a variante singola, anche sul
        template (x_amazon_condition, la stessa usata per pubblicare).
        Abbinamento SKU → variante con la stessa regola severa dell'Import
        FNSKU (_find_fnsku_variant): uno SKU ambiguo si salta. Scrive solo
        se cambia. Contatori sull'istanza, riportati nel log a fine
        marketplace da _condition_log_lines.
        """
        self._conditions_updated = getattr(self, '_conditions_updated', 0)
        self._conditions_unknown = getattr(self, '_conditions_unknown', set())
        page_skus = {(i.get('sku') or '').strip() for i in items} - {''}
        for item in items:
            sku = (item.get('sku') or '').strip()
            raw = (item.get('condition') or '').strip()
            if not sku or not raw:
                continue
            key = raw.lower().replace(' ', '').replace('_', '').replace('-', '')
            condition = self._CONDITION_MAP.get(key)
            if not condition:
                self._conditions_unknown.add(raw)
                continue
            variant, _reason, listing = self._find_report_sku_variant(sku, page_skus)
            if not variant:
                continue
            changed = False
            if marketplace:
                listing = listing or self.env['ke.amazon.listing'].sudo()._main_for(variant, marketplace)
                if listing and listing.condition != condition:
                    listing.write({'condition': condition})
                    changed = True
            template = variant.product_tmpl_id
            if len(template.product_variant_ids) == 1 and template.x_amazon_condition != condition:
                template.write({'x_amazon_condition': condition})
                changed = True
            if changed:
                self._conditions_updated += 1

    def _condition_log_lines(self, mp_tag):
        lines = []
        if getattr(self, '_conditions_updated', 0):
            lines.append(f'{mp_tag}Condizione Amazon aggiornata per {self._conditions_updated} SKU.')
        if getattr(self, '_conditions_unknown', None):
            lines.append(
                f'{mp_tag}Condizioni Amazon non riconosciute (non salvate): '
                f'{", ".join(sorted(self._conditions_unknown))}'
            )
        self._conditions_updated = 0
        self._conditions_unknown = set()
        return lines

    # ------------------------------------------------------------------
    # Usato Amazon (Grade & Resell) — offerte collegate al prodotto
    # originale e giacenza nell'ubicazione "FBA – Usato" (2026-11-03)
    # ------------------------------------------------------------------

    # Suffisso condizione degli SKU Grade & Resell (amzn.gr.…-LN). LN, VG e
    # GD osservati sui dati reali (2026-11-03), AC è la stessa convenzione
    # presunta: è solo un ripiego, la fonte affidabile resta
    # InventorySummary.condition della sync giacenze.
    _RESELL_SUFFIX_CONDITION = {
        'LN': 'used_like_new', 'VG': 'used_very_good', 'GD': 'used_good', 'AC': 'used_acceptable',
    }

    @staticmethod
    def _resell_vals_from_listing(product):
        """Valori dell'offerta usato da una riga listing già interpretata (report o Listings API)."""
        raw = (product.get('status') or '').strip().upper()
        return {
            'asin': product.get('asin') or None,
            'status': 'active' if raw in ('BUYABLE', 'ACTIVE') else (raw.lower() or None),
            'price': product.get('price') or None,
            'fulfillment': product.get('fulfillment') or None,
        }

    def _resell_original_variant(self, sku, asin):
        """
        Prodotto originale di uno SKU Grade & Resell. Prima l'ASIN (è lo
        stesso del prodotto): da una listing di uno SKU vero, poi dai campi
        del prodotto. Poi lo SKU originale contenuto nello SKU di Amazon: è
        troncato a 40 caratteri e seguito da una parte casuale di lunghezza
        variabile, che può contenere '-' e '_' (es.
        'amzn.gr.C4C-0MAC4PCOBK_-2-z2FzG0vyUks-LN' → 'C4C-0MAC4PCOBK'), quindi
        si provano i tagli possibili dal più lungo, finché uno corrisponde a
        un prodotto. Mai un prodotto che abbia esso stesso uno SKU Grade &
        Resell (residuo di un import precedente alla correzione).
        """
        Product = self.env['product.product']

        def usable(variant):
            return variant and not _is_grade_and_resell_sku(variant.x_amazon_sku or variant.default_code)

        if asin:
            listings = self.env['ke.amazon.listing'].sudo().search([('asin', '=', asin)]).filtered(
                lambda l: l.product_id and not _is_grade_and_resell_sku(l.sku)
            )
            if listings and usable(listings[0].product_id):
                return listings[0].product_id
            variant = self._find_amazon_product_variant('', asin)
            if usable(variant):
                return variant
        body = (sku or '').strip()[len('amzn.gr.'):]
        body = re.sub(r'-[A-Za-z]{1,3}$', '', body)
        cuts = sorted({body[:i] for i, ch in enumerate(body) if ch in '-_' and i > 0}, key=len, reverse=True)
        for candidate in cuts:
            variant = self._find_amazon_product_variant(candidate, '')
            if usable(variant):
                return variant
        return Product.browse()

    def _upsert_resell_offer(self, marketplace, sku, vals):
        """Crea/aggiorna l'offerta usato (sku+marketplace) e la collega al prodotto originale."""
        Offer = self.env['ke.amazon.resell.offer'].sudo()
        sku = sku.strip()
        offer = Offer.search([('sku', '=', sku), ('marketplace_id', '=', marketplace.id)], limit=1)
        vals = {key: value for key, value in vals.items() if value is not None}
        if 'condition' not in vals and not offer.condition:
            suffix = sku.rsplit('-', 1)[-1].upper()
            if suffix in self._RESELL_SUFFIX_CONDITION:
                vals['condition'] = self._RESELL_SUFFIX_CONDITION[suffix]
        if not offer.product_id:
            variant = self._resell_original_variant(sku, vals.get('asin') or offer.asin)
            if variant:
                vals['product_id'] = variant.id
        if offer:
            offer.write(vals)
            return offer
        return Offer.create({'sku': sku, 'marketplace_id': marketplace.id, **vals})

    def _get_fba_used_location(self, marketplace=None):
        """
        Ubicazione "FBA – Usato" del magazzino FBA del marketplace, configurata
        sul magazzino stesso (stock.warehouse.amazon_fba_used_location_id): chi
        condivide il magazzino (il pool IT/ES/FR) condivide l'ubicazione, chi
        ne ha uno proprio (UK) ha la propria. Se manca, la crea accanto allo
        stock del magazzino (stesso livello del transito), così non entra
        nella giacenza del nuovo, scritta sull'ubicazione esatta dello stock
        (_set_product_stock), né nei prelievi da quello stock.
        """
        warehouse = (
            marketplace.get_fba_warehouse() if marketplace else self.env.company.amazon_fba_warehouse_id
        )
        if not warehouse:
            return None
        if warehouse.amazon_fba_used_location_id:
            return warehouse.amazon_fba_used_location_id
        location = self.env['stock.location'].sudo().create({
            'name': 'Usato', 'usage': 'internal', 'location_id': warehouse.view_location_id.id,
        })
        warehouse.sudo().amazon_fba_used_location_id = location
        return location

    def _sync_resell_inventory(self, marketplace, mp_tag=''):
        """
        Ultima pagina della sync giacenze: aggiorna quantità/condizione delle
        offerte usato del marketplace con l'inventario appena letto, azzera
        quelle non più presenti, poi ricalcola la giacenza "FBA – Usato".
        """
        items = getattr(self, '_resell_inventory', {}) or {}
        self._resell_inventory = {}
        if not marketplace:
            return []
        Offer = self.env['ke.amazon.resell.offer'].sudo()
        now = datetime.now()
        seen = Offer.browse()
        for sku, item in items.items():
            key = (item.get('condition') or '').lower().replace(' ', '').replace('_', '').replace('-', '')
            condition = self._CONDITION_MAP.get(key)
            vals = {'asin': item.get('asin') or None, 'qty': int(item.get('qty') or 0), 'last_sync': now}
            if condition and condition != 'new_new':
                vals['condition'] = condition
            seen |= self._upsert_resell_offer(marketplace, sku, vals)
        Offer.search([
            ('marketplace_id', '=', marketplace.id), ('id', 'not in', seen.ids), ('qty', '!=', 0),
        ]).write({'qty': 0, 'last_sync': now})
        location = self._get_fba_used_location(marketplace)
        if not location:
            return [f'{mp_tag}Usato Amazon: nessun magazzino FBA configurato, giacenza usato non aggiornata.']
        units, unlinked = self._recompute_resell_stock(marketplace, location)
        line = (f'{mp_tag}Usato Amazon: {len(items)} offerte nell\'inventario, {units} unità '
                f'in "{location.display_name}"')
        if unlinked:
            line += f'; {unlinked} offerte con unità ma senza prodotto originale in Odoo (non in giacenza)'
        return [line]

    def _recompute_resell_stock(self, marketplace, location):
        """
        Giacenza "FBA – Usato" = somma delle quantità delle offerte usato di
        TUTTI i marketplace che condividono quel magazzino, in valore
        assoluto: lo stesso SKU visto da più marketplace del pool conta una
        volta sola (massimo), così ripetere la sync da IT, ES e FR dà sempre
        lo stesso risultato. Un prodotto senza più offerte con unità torna a 0.
        Ritorna (unità totali, offerte con unità ma senza prodotto originale).
        """
        warehouse = marketplace.get_fba_warehouse()
        pool = self.env['ke.amazon.marketplace'].sudo().search([('active', '=', True)]).filtered(
            lambda m: m.get_fba_warehouse() == warehouse
        ) | marketplace
        offers = self.env['ke.amazon.resell.offer'].sudo().search([('marketplace_id', 'in', pool.ids)])
        qty_by_sku, product_by_sku = {}, {}
        for offer in offers:
            qty_by_sku[offer.sku] = max(qty_by_sku.get(offer.sku, 0), offer.qty)
            if offer.product_id:
                product_by_sku[offer.sku] = offer.product_id
        totals = {}
        unlinked = 0
        for sku, qty in qty_by_sku.items():
            product = product_by_sku.get(sku)
            if product:
                totals[product] = totals.get(product, 0) + qty
            elif qty:
                unlinked += 1
        for product, qty in totals.items():
            self._set_product_stock(product, location, qty)
        for quant in self.env['stock.quant'].sudo().search([
            ('location_id', '=', location.id), ('quantity', '!=', 0),
        ]):
            if quant.product_id not in totals:
                self._set_product_stock(quant.product_id, location, 0)
        return sum(totals.values()), unlinked

    def _find_report_sku_variant(self, sku, report_skus):
        """
        Variante per uno SKU di un report FBA (Import FNSKU, condizione):
        prima l'annuncio con quello SKU sul marketplace corrente (abbinamento
        esatto fatto dal Full Import, copre anche i secondi SKU sullo stesso
        ASIN), poi _find_fnsku_variant sui campi del prodotto. Ritorna
        (variante, motivo, annuncio trovato per SKU o vuoto).
        """
        Listing = self.env['ke.amazon.listing'].sudo()
        marketplace = self._current_marketplace()
        listing = Listing.search([
            ('marketplace_id', '=', marketplace.id), ('sku', '=', sku),
        ], limit=1) if marketplace and sku else Listing
        if listing and listing.product_id:
            return listing.product_id, '', listing
        variant, reason = self._find_fnsku_variant(sku, report_skus)
        return variant, reason, Listing

    def _find_fnsku_variant(self, sku, report_skus):
        """
        Abbinamento SKU → variante dedicato all'Import FNSKU. Più severo di
        _find_amazon_product_variant: un FNSKU sbagliato finisce stampato
        sull'etichetta, quindi nel dubbio si salta invece di scegliere.

        - Match esatto (SKU variante, poi SKU/default_code template); se lo
          stesso valore è su più record → ambiguo.
        - Template con più varianti → 'multi_variante' (come
          _find_amazon_product_variant: non si sceglie una variante a caso).
        - Nessun ripiego "SKU con/senza +" (tolto il 2026-11-05): ogni SKU
          ha il suo annuncio dopo il Full Import e _find_report_sku_variant
          lo trova da lì; uno SKU sconosciuto resta "non trovato", la scelta
          prudente per un'etichetta. 'report_skus' non è più usato, resta
          nella firma per i chiamanti.

        Ritorna (variante, motivo): motivo '' se trovato, altrimenti
        'non_trovato', 'multi_variante' o 'ambiguo'.
        """
        Product = self.env['product.product']
        Template = self.env['product.template']

        def _resolve(sku_value):
            variants = Product.search([('x_amazon_sku', '=', sku_value)], limit=2)
            if len(variants) > 1:
                return Product.browse(), 'ambiguo'
            if variants:
                return variants, ''
            for field in ('x_amazon_sku', 'default_code'):
                tmpls = Template.search([(field, '=', sku_value)], limit=2)
                if len(tmpls) > 1:
                    return Product.browse(), 'ambiguo'
                if tmpls:
                    if len(tmpls.product_variant_ids) > 1:
                        return Product.browse(), 'multi_variante'
                    return tmpls.product_variant_id, ''
            return Product.browse(), 'non_trovato'

        return _resolve(sku)

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
        x_amazon_fnsku. Abbina per SKU usando _find_fnsku_variant (variante
        prima, poi template; fallback '+' solo se univoco e non conteso da
        un'altra riga del report) — righe senza SKU riconosciuto in Odoo vengono segnalate
        come saltate, non è un errore (es. inventario storico/archiviato non più in
        Odoo). Se la variante trovata appartiene a un template con PIÙ varianti,
        l'FNSKU va sulla variante (product.product.x_amazon_fnsku), non sul
        template, dove sovrascriverebbe quello delle varianti sorelle.

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
        # Tutti gli SKU del report: serve a _find_fnsku_variant per non
        # applicare il fallback con/senza '+' quando lo SKU alternativo ha
        # una propria riga (storica e nuova sullo stesso prodotto).
        report_skus = {
            (row.get('sku') or '').strip() for row in rows
            if not _is_grade_and_resell_sku(row.get('sku'))
        } - {''}

        grade_resell_skus = set()
        for row in rows:
            sku = (row.get('sku') or '').strip()
            fnsku = (row.get('fnsku') or '').strip()
            if not sku or not fnsku:
                continue
            if _is_grade_and_resell_sku(sku):
                grade_resell_skus.add(sku)
                continue

            try:
                variant, reason, sku_listing = self._find_report_sku_variant(sku, report_skus)
                if not variant:
                    not_found += 1
                    if reason == 'ambiguo':
                        lines.append(
                            f'{mp_tag}SALTATO {sku} (abbinamento ambiguo: SKU su più prodotti, '
                            f'o prodotto già abbinato dalla riga dello SKU con/senza "+") — FNSKU: {fnsku}'
                        )
                    else:
                        lines.append(f'{mp_tag}SKU NON TROVATO (o ambiguo su più varianti): {sku} — FNSKU: {fnsku}')
                    continue

                # L'FNSKU è dell'annuncio (SKU): sempre sul suo annuncio. Il
                # campo condiviso della variante/template (etichette,
                # spedizioni) solo per lo SKU principale del prodotto — un
                # secondo SKU sullo stesso ASIN ha un FNSKU suo che non deve
                # sostituire quello stampato sulle unità.
                listing = sku_listing or self.env['ke.amazon.listing'].sudo()._main_for(variant, marketplace)
                is_main = not sku_listing or sku == variant._amazon_main_sku()
                product = variant.product_tmpl_id

                if dedicated or not is_main:
                    if not listing:
                        not_found += 1
                        lines.append(
                            f'{mp_tag}SALTATO {sku}: nessuna riga ke.amazon.listing per questo '
                            f'marketplace dedicato (serve da full import/riconciliazione prima)'
                        )
                        continue
                    if listing.fnsku != fnsku:
                        listing.write({'fnsku': fnsku})
                        lines.append(f'{mp_tag}OK {sku} → {fnsku}' + ('' if is_main else ' (annuncio secondario)'))
                    else:
                        lines.append(f'{mp_tag}INVARIATO {sku}: {fnsku}')
                elif len(product.product_variant_ids) > 1:
                    # Famiglia su marketplace POOLED: x_amazon_fnsku del
                    # template è condiviso da tutte le varianti, si scrive
                    # quello della variante — l'FNSKU è lo stesso per tutto
                    # il pool.
                    if variant.x_amazon_fnsku != fnsku:
                        variant.write({'x_amazon_fnsku': fnsku})
                        lines.append(f'{mp_tag}OK {sku} → {fnsku} (variante)')
                    else:
                        lines.append(f'{mp_tag}INVARIATO {sku}: {fnsku} (variante)')
                    if listing and listing.fnsku != fnsku:
                        listing.write({'fnsku': fnsku})
                else:
                    if product.x_amazon_fnsku != fnsku:
                        product.write({'x_amazon_fnsku': fnsku})
                        lines.append(f'{mp_tag}OK {sku} → {fnsku}')
                    else:
                        lines.append(f'{mp_tag}INVARIATO {sku}: {fnsku}')
                    # Annuncio del marketplace corrente, se esiste già
                    # (creato da full_import/reconcile): aggiornato per
                    # visibilità, mai creato qui.
                    if listing and listing.fnsku != fnsku:
                        listing.write({'fnsku': fnsku})
                matched += 1
            except Exception as exc:
                errors += 1
                lines.append(f'{mp_tag}ERRORE {sku}: {exc}')
                _log.error('Errore elaborazione FNSKU %s: %s', sku, exc, exc_info=True)

        gr_line = _grade_and_resell_log_line(grade_resell_skus, mp_tag)
        if gr_line:
            lines.append(gr_line)
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
        grade_resell_skus = set()
        for row in rows:
            parsed = self._parse_listing_report(row)
            sku = parsed['sku']
            asin = parsed['asin']
            if _is_grade_and_resell_sku(sku):
                grade_resell_skus.add(sku)
                continue
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
        gr_line = _grade_and_resell_log_line(grade_resell_skus)
        log_rec.write({'log_lines': existing + f'\nStore Amazon: {total_amazon} listing trovati (da report).'
                       + (f'\n{gr_line}' if gr_line else '')})
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
            # listing.product_id è la VARIANTE (ke.amazon.listing è scoped
            # per variante) — qui serve l'id template, come il resto di
            # queste mappe/la ricerca simple_products sotto.
            tmpl_id = listing.product_id.product_tmpl_id.id
            listing_sku_by_tmpl.setdefault(tmpl_id, listing.sku)
            if listing.asin:
                listing_asin_by_tmpl.setdefault(tmpl_id, listing.asin)

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
                    # invalidate_fnsku=True anche qui: prima questa chiamata
                    # riscriveva comunque fnsku=None perché _upsert_listing
                    # lo leggeva (sempre assente) da 'data' — ora che non lo
                    # fa più, va invalidato esplicitamente per coerenza con
                    # x_amazon_fnsku sopra, vedi TASK.md "FNSKU".
                    self._upsert_listing(product.product_variant_id, plus_data)
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
                    self._upsert_listing(product.product_variant_id, amazon_data)
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
                        self._upsert_listing(product.product_variant_id, amazon_data_by_asin)
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
                            'x_amazon_fnsku': False,  # nuovo SKU → nuovo FNSKU, vedi sopra
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
                    self._upsert_listing(variant, plus_data)
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
                    self._upsert_listing(variant, amazon_data)
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
                                'x_amazon_fnsku': False,  # nuovo SKU → nuovo FNSKU, vedi sopra
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
                        self._upsert_listing(variant, amazon_data_by_asin)
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
