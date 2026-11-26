"""
Regole comuni delle specifiche tecniche (2026-11-17), indipendenti dalla
fonte: elenco delle specifiche con i nomi nelle lingue, vocabolari dei
valori fissi (riconosciuti in qualsiasi lingua, tradotti in it/en/es/fr/de:
vanno bene per i filtri del sito) e scrittura dei valori per lingua.
Le usano gli import di Amazon (ke_amazon_sync/services/specs.py) e dei
cataloghi fornitore (wizard/supplier_catalog.py).

Un valore è una tupla:
- ('num', numero, unità, cifre): stesso valore in ogni lingua, separatore
  decimale della lingua;
- ('vocab', [id], tabella): valore fisso tradotto;
- ('text_all', testo): uguale in ogni lingua (codici, misure, "1+1");
- ('text', testo, lingua): testo libero, solo nella sua lingua.
"""
import re

# Codice → (ordine, {lingua: etichetta}). La prima lingua è la principale.
SPEC_KEYS = {
    'technique': (10, {'it_IT': 'Tecnica', 'en_GB': 'Technique', 'en_US': 'Technique', 'es_ES': 'Técnica',
                       'fr_FR': 'Technique', 'de_DE': 'Angeltechnik'}),
    'species': (12, {'it_IT': 'Specie', 'en_GB': 'Target species', 'en_US': 'Target species',
                     'es_ES': 'Especies', 'fr_FR': 'Espèces', 'de_DE': 'Zielfisch'}),
    'line_type': (20, {'it_IT': 'Tipo di filo', 'en_GB': 'Line type', 'en_US': 'Line type',
                       'es_ES': 'Tipo de hilo', 'fr_FR': 'Type de fil', 'de_DE': 'Schnurtyp'}),
    'diameter': (21, {'it_IT': 'Diametro', 'en_GB': 'Diameter', 'en_US': 'Diameter', 'es_ES': 'Diámetro',
                      'fr_FR': 'Diamètre', 'de_DE': 'Durchmesser'}),
    'breaking_strength': (22, {'it_IT': 'Carico di rottura', 'en_GB': 'Breaking strength',
                               'en_US': 'Breaking strength', 'es_ES': 'Resistencia', 'fr_FR': 'Résistance',
                               'de_DE': 'Tragkraft'}),
    'length': (23, {'it_IT': 'Lunghezza', 'en_GB': 'Length', 'en_US': 'Length', 'es_ES': 'Longitud',
                    'fr_FR': 'Longueur', 'de_DE': 'Länge'}),
    'strands': (24, {'it_IT': 'Capi', 'en_GB': 'Strands', 'en_US': 'Strands', 'es_ES': 'Hebras',
                     'fr_FR': 'Brins', 'de_DE': 'Fasern'}),
    'gear_ratio': (30, {'it_IT': 'Rapporto di recupero', 'en_GB': 'Gear ratio', 'en_US': 'Gear ratio',
                        'es_ES': 'Relación de recogida', 'fr_FR': 'Rapport de récupération',
                        'de_DE': 'Übersetzung'}),
    'bearings': (31, {'it_IT': 'Cuscinetti', 'en_GB': 'Bearings', 'en_US': 'Bearings', 'es_ES': 'Rodamientos',
                      'fr_FR': 'Roulements', 'de_DE': 'Kugellager'}),
    'max_drag': (32, {'it_IT': 'Frizione massima', 'en_GB': 'Max drag', 'en_US': 'Max drag',
                      'es_ES': 'Freno máximo', 'fr_FR': 'Frein maximal', 'de_DE': 'Max. Bremskraft'}),
    'line_capacity': (33, {'it_IT': 'Capacità filo', 'en_GB': 'Line capacity', 'en_US': 'Line capacity',
                           'es_ES': 'Capacidad de hilo', 'fr_FR': 'Contenance', 'de_DE': 'Schnurfassung'}),
    'hand': (34, {'it_IT': 'Mano', 'en_GB': 'Handle side', 'en_US': 'Handle side', 'es_ES': 'Mano',
                  'fr_FR': 'Main', 'de_DE': 'Kurbelseite'}),
    'rod_power': (40, {'it_IT': 'Potenza', 'en_GB': 'Power', 'en_US': 'Power', 'es_ES': 'Potencia',
                       'fr_FR': 'Puissance', 'de_DE': 'Aktion'}),
    'sections': (41, {'it_IT': 'Sezioni', 'en_GB': 'Sections', 'en_US': 'Sections', 'es_ES': 'Tramos',
                      'fr_FR': 'Brins', 'de_DE': 'Teile'}),
    'size': (50, {'it_IT': 'Misura', 'en_GB': 'Size', 'en_US': 'Size', 'es_ES': 'Tamaño', 'fr_FR': 'Taille',
                  'de_DE': 'Größe'}),
    'point': (51, {'it_IT': 'Punta', 'en_GB': 'Point', 'en_US': 'Point', 'es_ES': 'Punta', 'fr_FR': 'Pointe',
                   'de_DE': 'Spitze'}),
    'hook_type': (52, {'it_IT': 'Tipo di amo', 'en_GB': 'Hook type', 'en_US': 'Hook type',
                       'es_ES': 'Tipo de anzuelo', 'fr_FR': "Type d'hameçon", 'de_DE': 'Hakentyp'}),
    'sinker_weight': (55, {'it_IT': 'Grammatura', 'en_GB': 'Weight', 'en_US': 'Weight', 'es_ES': 'Gramaje',
                           'fr_FR': 'Grammage', 'de_DE': 'Gewicht'}),
    'shape': (56, {'it_IT': 'Forma', 'en_GB': 'Shape', 'en_US': 'Shape', 'es_ES': 'Forma', 'fr_FR': 'Forme',
                   'de_DE': 'Form'}),
    'color': (60, {'it_IT': 'Colore', 'en_GB': 'Colour', 'en_US': 'Color', 'es_ES': 'Color',
                   'fr_FR': 'Couleur', 'de_DE': 'Farbe'}),
    'material': (61, {'it_IT': 'Materiale', 'en_GB': 'Material', 'en_US': 'Material', 'es_ES': 'Material',
                      'fr_FR': 'Matière', 'de_DE': 'Material'}),
    'weight': (62, {'it_IT': 'Peso', 'en_GB': 'Weight', 'en_US': 'Weight', 'es_ES': 'Peso', 'fr_FR': 'Poids',
                    'de_DE': 'Gewicht'}),
    'pieces': (63, {'it_IT': 'Pezzi per confezione', 'en_GB': 'Pieces per pack', 'en_US': 'Pieces per pack',
                    'es_ES': 'Piezas por paquete', 'fr_FR': 'Pièces par paquet', 'de_DE': 'Stück pro Packung'}),
}

# Valori fissi: (id, espressione che lo riconosce in qualsiasi lingua, {lingua: testo}).
_T = lambda it, en, es, fr, de: {'it_IT': it, 'en_GB': en, 'en_US': en, 'es_ES': es, 'fr_FR': fr, 'de_DE': de}
TECHNIQUES = [
    ('spinning', r'spinn|lancio leggero', _T('Spinning', 'Spinning', 'Spinning', 'Spinning', 'Spinnfischen')),
    ('casting', r'\bcasting\b|baitcast', _T('Casting', 'Casting', 'Casting', 'Casting', 'Baitcasting')),
    ('surf', r'surf', _T('Surfcasting', 'Surfcasting', 'Surfcasting', 'Surfcasting', 'Brandungsangeln')),
    ('carp', r'carp|karpfen|carpa', _T('Carpfishing', 'Carp fishing', 'Carpfishing', 'Pêche de la carpe',
                                        'Karpfenangeln')),
    ('feeder', r'feeder|ledger', _T('Feeder', 'Feeder', 'Feeder', 'Feeder', 'Feederangeln')),
    ('bolognese', r'bolognes', _T('Bolognese', 'Bolognese', 'Boloñesa', 'Bolognaise', 'Bologneser')),
    ('match', r'\bmatch\b|inglese', _T('Match/inglese', 'Match', 'Match/inglesa', 'Match/anglaise', 'Match')),
    ('trolling', r'traina|trolling|curric|schlepp|traîne', _T('Traina', 'Trolling', 'Curricán', 'Traîne',
                                                              'Schleppangeln')),
    ('big_game', r'big ?game|offshore', _T('Big game', 'Big game', 'Big game', 'Big game', 'Big Game')),
    ('jigging', r'jigging|slow ?pitch|\bjig\b', _T('Jigging', 'Jigging', 'Jigging', 'Jigging', 'Jigging')),
    ('bottom', r'bolentino|vertical|bottom', _T('Bolentino', 'Bottom fishing', 'Fondeo', 'Pêche au fond',
                                                'Grundangeln vom Boot')),
    ('eging', r'eging|\begi\b|calamar|totanar|squid|seiche', _T('Eging', 'Eging', 'Eging', 'Eging', 'Eging')),
    ('trout', r'trota|trout|forelle|trucha|truite|tremarella|\barea\b', _T('Trota/area', 'Trout/area',
                                                                         'Trucha/area', 'Truite/area',
                                                                         'Forelle/Area')),
    ('fly', r'\bmosca\b|\bfly\b|fliegen|mouche', _T('Mosca', 'Fly fishing', 'Mosca', 'Mouche', 'Fliegenfischen')),
    ('light_game', r'light ?game|rock ?fish|ajing|ultra ?light', _T('Light game', 'Light game', 'Light game',
                                                                     'Light game', 'Light Game')),
    ('ledgering', r'fondo|ground|grund', _T('Pesca a fondo', 'Ledgering', 'Pesca a fondo', 'Pêche au posé',
                                            'Grundangeln')),
    ('shore', r'riva|shore|spiaggia|beach|costa|bord\b', _T('Pesca da riva', 'Shore fishing', 'Pesca desde costa',
                                                           'Pêche du bord', 'Uferangeln')),
    ('boat', r'barca|boat|bateau|barco', _T('Pesca in barca', 'Boat fishing', 'Pesca desde barco',
                                            'Pêche en bateau', 'Bootsangeln')),
    ('spearfishing', r'subacque|apnea|spearfish|underwater', _T('Pesca subacquea', 'Spearfishing',
                                                                'Pesca submarina', 'Chasse sous-marine',
                                                                'Speerfischen')),
]
HANDS = [
    ('both', r'ambid|beidh|both|entramb|destri e manc|droitier et gauch|reversib|intercamb|interchang',
     _T('Ambidestro', 'Ambidextrous', 'Ambidiestro', 'Ambidextre', 'Beidhändig')),
    ('left', r'sinistr|left|links|gauche|izquier', _T('Mano sinistra', 'Left hand', 'Mano izquierda',
                                                      'Main gauche', 'Linkshand')),
    ('right', r'destr|right|rechts|droit|derech', _T('Mano destra', 'Right hand', 'Mano derecha', 'Main droite',
                                                     'Rechtshand')),
]
LINE_TYPES = [
    ('braid', r'trecc|intrecc|braid|superline|geflocht|tress|trenz', _T('Trecciato', 'Braided', 'Trenzado', 'Tresse',
                                                              'Geflochten')),
    ('fluoro', r'fluoro', _T('Fluorocarbon', 'Fluorocarbon', 'Fluorocarbono', 'Fluorocarbone', 'Fluorocarbon')),
    ('mono', r'mono|nylon', _T('Monofilo', 'Monofilament', 'Monofilamento', 'Monofilament', 'Monofil')),
    ('steel', r'acciaio|steel|stahl|wire|cavetto|acier|acero', _T('Acciaio', 'Steel wire', 'Acero', 'Acier',
                                                                  'Stahl')),
]
POWER = {'ultra_light': 'Ultra light', 'light': 'Light', 'medium_light': 'Medium light', 'medium': 'Medium',
         'medium_heavy': 'Medium heavy', 'heavy': 'Heavy', 'extra_heavy': 'Extra heavy'}


SPEC_KEYS.update({
    'water': (5, {'it_IT': 'Acqua', 'en_GB': 'Water type', 'en_US': 'Water type', 'es_ES': 'Agua',
                  'fr_FR': 'Eau', 'de_DE': 'Gewässer'}),
    'rod_action': (42, {'it_IT': 'Azione', 'en_GB': 'Action', 'en_US': 'Action', 'es_ES': 'Acción',
                        'fr_FR': 'Action', 'de_DE': 'Aktion'}),
    'casting_weight': (43, {'it_IT': 'Grammatura di lancio', 'en_GB': 'Casting weight', 'en_US': 'Casting weight',
                            'es_ES': 'Peso de lance', 'fr_FR': 'Puissance de lancer', 'de_DE': 'Wurfgewicht'}),
    'line_rating': (44, {'it_IT': 'Libbraggio', 'en_GB': 'Line rating', 'en_US': 'Line rating',
                         'es_ES': 'Línea recomendada', 'fr_FR': 'Fil conseillé', 'de_DE': 'Schnurklasse'}),
    'transport_length': (45, {'it_IT': 'Lunghezza da trasporto', 'en_GB': 'Transport length',
                              'en_US': 'Transport length', 'es_ES': 'Longitud de transporte',
                              'fr_FR': 'Longueur de transport', 'de_DE': 'Transportlänge'}),
    'guides': (46, {'it_IT': 'Anelli', 'en_GB': 'Guides', 'en_US': 'Guides', 'es_ES': 'Anillas',
                    'fr_FR': 'Anneaux', 'de_DE': 'Ringe'}),
    'mono_capacity': (35, {'it_IT': 'Capacità monofilo', 'en_GB': 'Mono capacity', 'en_US': 'Mono capacity',
                           'es_ES': 'Capacidad monofilamento', 'fr_FR': 'Contenance nylon',
                           'de_DE': 'Schnurfassung Mono'}),
    'braid_capacity': (36, {'it_IT': 'Capacità trecciato', 'en_GB': 'Braid capacity', 'en_US': 'Braid capacity',
                            'es_ES': 'Capacidad trenzado', 'fr_FR': 'Contenance tresse',
                            'de_DE': 'Schnurfassung geflochten'}),
    'recovery': (37, {'it_IT': 'Recupero per giro', 'en_GB': 'Line retrieve', 'en_US': 'Line retrieve',
                      'es_ES': 'Recogida por vuelta', 'fr_FR': 'Récupération par tour',
                      'de_DE': 'Einzug pro Kurbelumdrehung'}),
    'drag_type': (38, {'it_IT': 'Tipo di frizione', 'en_GB': 'Drag type', 'en_US': 'Drag type',
                       'es_ES': 'Tipo de freno', 'fr_FR': 'Type de frein', 'de_DE': 'Bremsart'}),
    'buoyancy': (53, {'it_IT': 'Galleggiamento', 'en_GB': 'Buoyancy', 'en_US': 'Buoyancy',
                      'es_ES': 'Flotabilidad', 'fr_FR': 'Flottabilité', 'de_DE': 'Schwimmverhalten'}),
    'depth': (54, {'it_IT': 'Profondità di nuoto', 'en_GB': 'Running depth', 'en_US': 'Running depth',
                   'es_ES': 'Profundidad de nado', 'fr_FR': 'Profondeur de nage', 'de_DE': 'Lauftiefe'}),
    'hook_size': (57, {'it_IT': 'Misura amo', 'en_GB': 'Hook size', 'en_US': 'Hook size',
                       'es_ES': 'Tamaño de anzuelo', 'fr_FR': "Taille d'hameçon", 'de_DE': 'Hakengröße'}),
    'hooks': (58, {'it_IT': 'Numero di ami', 'en_GB': 'Number of hooks', 'en_US': 'Number of hooks',
                   'es_ES': 'Número de anzuelos', 'fr_FR': "Nombre d'hameçons", 'de_DE': 'Anzahl Haken'}),
    'scent': (59, {'it_IT': 'Aroma', 'en_GB': 'Scent', 'en_US': 'Scent', 'es_ES': 'Aroma', 'fr_FR': 'Arôme',
                   'de_DE': 'Duft'}),
})

WATER = [
    ('salt', r'salt|mare|marin|sea\b|salad|meer|mer\b', _T('Mare', 'Saltwater', 'Agua salada', 'Mer', 'Salzwasser')),
    ('fresh', r'fresh|dolce|dulce|douce|süß|suss', _T('Acqua dolce', 'Freshwater', 'Agua dulce', 'Eau douce',
                                                    'Süßwasser')),
]
BUOYANCY = [
    ('suspending', r'suspend|sospens', _T('Sospensione', 'Suspending', 'Suspending', 'Suspending', 'Schwebend')),
    ('slow_sinking', r'slow ?sink|affondamento lento', _T('Affondamento lento', 'Slow sinking', 'Hundimiento lento',
                                                          'Coulant lent', 'Langsam sinkend')),
    ('sinking', r'sink|affond|coulant|hundi', _T('Affondante', 'Sinking', 'Hundimiento', 'Coulant', 'Sinkend')),
    ('floating', r'float|galleg|flott|flot', _T('Galleggiante', 'Floating', 'Flotante', 'Flottant', 'Schwimmend')),
]


def vocab(value, table):
    """Id dei valori fissi riconosciuti nel testo (in qualsiasi lingua)."""
    return [ident for ident, rx, _labels in table if re.search(rx, value or '', re.I)]


def _fmt_number(value, digits, lang):
    s = f'{value:.{digits}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s if lang.startswith('en') else s.replace('.', ',')


def render(spec, lang):
    """Testo del valore nella lingua; None se il valore non va scritto in quella lingua."""
    kind = spec[0]
    if kind == 'num':
        _kind, value, unit, digits = spec
        number = _fmt_number(value, digits, lang)
        if unit == ':1':
            return f'{number}:1'
        return f'{number} {unit}'.strip()
    if kind == 'vocab':
        _kind, idents, table = spec
        labels = {ident: lbls for ident, _rx, lbls in table}
        return ', '.join(labels[i].get(lang) or labels[i]['it_IT'] for i in idents)
    if kind == 'text_all':
        return spec[1]
    if kind == 'text':
        # Testo libero: solo nella sua lingua (en_GB vale anche per en_US).
        own = spec[2] if len(spec) > 2 else None
        if own and (lang == own or {lang, own} == {'en_GB', 'en_US'}):
            return spec[1]
    return None


def render_own(spec):
    """Valore di un testo libero nella lingua di chi l'ha fornito (es. il marketplace)."""
    return spec[1] if spec[0] == 'text' else None


# ---------------------------------------------------------------------------
# Lettura di valori da testo (cataloghi fornitore, 2026-11-17)
# ---------------------------------------------------------------------------

# Numeri: codice → (unità di arrivo, cifre, minimo, massimo). 'auto_g' e
# 'auto_m' scelgono g/kg e cm/m secondo la grandezza.
NUMBER_SPECS = {
    'max_drag': ('kg', 1, 0.5, 60), 'breaking_strength': ('kg', 1, 0.3, 300),
    'diameter': ('mm', 3, 0.03, 3), 'length': ('auto_m', 2, 0.005, 5000),
    'transport_length': ('m', 2, 0.2, 5), 'weight': ('auto_g', 1, 0.1, 50000),
    'recovery': ('cm', 0, 20, 200), 'sinker_weight': ('g', 1, 0.1, 5000),
}
RATIO_SPECS = {'gear_ratio'}
# Valori fissi: codice → (tabella, quanti valori tenere; None = tutti).
VOCAB_SPECS = {'technique': (TECHNIQUES, None), 'hand': (HANDS, 1), 'water': (WATER, None),
               'buoyancy': (BUOYANCY, 1), 'line_type': (LINE_TYPES, 1)}
# Testi uguali in ogni lingua (misure, codici, capacità).
SAME_TEXT_SPECS = {'bearings', 'mono_capacity', 'braid_capacity', 'line_capacity', 'rod_power', 'rod_action',
                   'sections', 'casting_weight', 'line_rating', 'guides', 'depth', 'hook_size', 'hooks',
                   'strands', 'pieces', 'size'}
_EMPTY = {'', '-', 'n/a', 'na', 'varied', 'various', 'none', 'tbc', 'tba', 'not applicable'}

# unità trovata nel testo → (grandezza, fattore verso g / mm)
_UNITS = {
    'mg': ('mass', 0.001), 'g': ('mass', 1), 'gr': ('mass', 1), 'kg': ('mass', 1000),
    'oz': ('mass', 28.3495), 'lb': ('mass', 453.592), 'lbs': ('mass', 453.592),
    'mm': ('len', 1), 'cm': ('len', 10), 'm': ('len', 1000), 'mt': ('len', 1000),
    'in': ('len', 25.4), '"': ('len', 25.4), 'ft': ('len', 304.8), 'yd': ('len', 914.4), 'yds': ('len', 914.4),
}
_METRIC = {'mg', 'g', 'gr', 'kg', 'mm', 'cm', 'm', 'mt'}
_OUT = {'kg': ('mass', 1000), 'g': ('mass', 1), 'mm': ('len', 1), 'cm': ('len', 10), 'm': ('len', 1000)}


def clean(value):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    return '' if value.lower() in _EMPTY else value


def _quantities(text):
    """[(numero, unità)] trovati nel testo, le unità metriche per prime."""
    found = []
    for num, unit in re.findall(r'(\d+(?:[.,]\d+)?)\s*(mg|kg|gr|g|oz|lbs|lb|mm|cm|mt|m|in|ft|yds|yd|")(?![a-z])',
                                text or '', re.I):
        found.append((float(num.replace(',', '.')), unit.lower()))
    return sorted(found, key=lambda q: q[1] not in _METRIC)


def _number_spec(code, text):
    out_unit, digits, low, high = NUMBER_SPECS[code]
    kind = 'mass' if out_unit in ('kg', 'g', 'auto_g') else 'len'
    for value, unit in _quantities(text):
        dim, factor = _UNITS[unit]
        if dim != kind:
            continue
        base = value * factor                      # g oppure mm
        if out_unit == 'auto_g':
            unit_out = 'g' if base < 1000 else 'kg'
        elif out_unit == 'auto_m':
            unit_out = 'cm' if base < 1000 else 'm'
        else:
            unit_out = out_unit
        number = base / _OUT[unit_out][1]
        # Limiti in m (auto_m), in g (auto_g) o nell'unità di arrivo.
        check = base / 1000 if out_unit == 'auto_m' else base if out_unit == 'auto_g' else number
        if low <= check <= high:
            return ('num', number, unit_out, 1 if unit_out in ('cm', 'g') else digits)
        return None
    # Numero senza unità: si intende già nell'unità di arrivo.
    bare = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*', text or '')
    if bare and out_unit not in ('auto_g', 'auto_m'):
        number = float(bare.group(1).replace(',', '.'))
        if low <= number <= high:
            return ('num', number, out_unit, digits)
    return None


_ZERO = re.compile(r'0+(?:[.,]0+)?')


def parse_spec(code, values, text_lang='en_GB'):
    """
    Valore di una specifica da uno o più testi (es. colonne di un catalogo
    fornitore, nell'ordine di preferenza). Valori fissi: si uniscono tutti i
    testi (es. categoria e sottocategoria per la tecnica); altrimenti vale il
    primo testo che dà un valore valido. None se niente di utile.
    """
    # Uno zero da solo non è un valore (2026-11-24): il file Pure Fishing di novembre 2026 scrive "0" per
    # "nessun dato" (Bearing Count 18.896 righe, Number of guides 18.065, Number of Hooks 15.548).
    texts = [t for t in (clean(v) for v in values) if t and not _ZERO.fullmatch(t)]
    if not texts:
        return None
    if code in VOCAB_SPECS:
        table, keep = VOCAB_SPECS[code]
        found = vocab(' | '.join(texts), table)
        return ('vocab', found[:keep] if keep else found, table) if found else None
    for text in texts:
        if code in NUMBER_SPECS:
            spec = _number_spec(code, text)
        elif code in RATIO_SPECS:
            m = re.search(r'(\d+(?:[.,]\d+)?)\s*:\s*1', text)
            ratio = float(m.group(1).replace(',', '.')) if m else None
            spec = ('num', ratio, ':1', 1) if ratio and 2 <= ratio <= 10 else None
        elif code in SAME_TEXT_SPECS:
            spec = ('text_all', text[:80])
        else:
            spec = ('text', text[:80], text_lang)
        if spec:
            return spec
    return None


# ---------------------------------------------------------------------------
# Tipo di prodotto per il nome sul sito (2026-11-17)
# ---------------------------------------------------------------------------

# Sottocategoria del catalogo fornitore (minuscolo) → tipo nelle lingue del sito.
PRODUCT_KINDS = {
    'hard bait': _T('Esca artificiale', 'Hard lure', 'Señuelo duro', 'Leurre dur', 'Wobbler'),
    'soft bait': _T('Esca siliconica', 'Soft lure', 'Vinilo', 'Leurre souple', 'Gummiköder'),
    'jigs': _T('Jig', 'Jig', 'Jig', 'Jig', 'Jig'),
    'dough': _T('Pasta per trota', 'Trout dough', 'Pasta para truchas', 'Pâte à truite', 'Forellenteig'),
    'attractant': _T('Attrattivo', 'Attractant', 'Atrayente', 'Attractant', 'Lockstoff'),
    'kits': _T('Kit di esche', 'Lure kit', 'Kit de señuelos', 'Kit de leurres', 'Köder-Set'),
    'spinning rods': _T('Canna da spinning', 'Spinning rod', 'Caña de spinning', 'Canne spinning', 'Spinnrute'),
    'casting rods': _T('Canna da casting', 'Casting rod', 'Caña de casting', 'Canne casting', 'Baitcastrute'),
    'fly rods': _T('Canna da mosca', 'Fly rod', 'Caña de mosca', 'Canne mouche', 'Fliegenrute'),
    'specimen rods': _T('Canna da carpfishing', 'Specimen rod', 'Caña de carpfishing', 'Canne carpe',
                        'Karpfenrute'),
    'coarse rods': _T('Canna da feeder', 'Coarse rod', 'Caña feeder', 'Canne coup', 'Friedfischrute'),
    'poles': _T('Canna fissa', 'Pole', 'Caña enchufable', 'Canne au coup', 'Kopfrute'),
    'superline': _T('Filo trecciato', 'Braided line', 'Línea trenzada', 'Tresse', 'Geflochtene Schnur'),
    'monofilament': _T('Filo monofilo', 'Monofilament line', 'Hilo monofilamento', 'Fil nylon', 'Monofile Schnur'),
    'fluorocarbon': _T('Filo fluorocarbon', 'Fluorocarbon line', 'Hilo fluorocarbono', 'Fil fluorocarbone',
                       'Fluorocarbonschnur'),
    'fly line': _T('Coda di topo', 'Fly line', 'Línea de mosca', 'Soie mouche', 'Fliegenschnur'),
    'leaders': _T('Terminale', 'Leader', 'Bajo de línea', 'Bas de ligne', 'Vorfach'),
    'spinning reels': _T('Mulinello da spinning', 'Spinning reel', 'Carrete de spinning', 'Moulinet spinning',
                         'Stationärrolle'),
    'low profile baitcast reels': _T('Mulinello da casting', 'Baitcasting reel', 'Carrete de casting',
                                     'Moulinet casting', 'Baitcastrolle'),
    'round baitcast reels': _T('Mulinello rotante', 'Round baitcasting reel', 'Carrete redondo',
                               'Moulinet rond', 'Rundrolle'),
    'conventional reels': _T('Mulinello da traina', 'Conventional reel', 'Carrete de curricán',
                             'Moulinet traîne', 'Multirolle'),
    'fly reels': _T('Mulinello da mosca', 'Fly reel', 'Carrete de mosca', 'Moulinet mouche', 'Fliegenrolle'),
    'spare spools': _T('Bobina di ricambio', 'Spare spool', 'Bobina de recambio', 'Bobine de rechange',
                       'Ersatzspule'),
    'spinning combos': _T('Combo canna e mulinello', 'Rod and reel combo', 'Combo caña y carrete',
                          'Combo canne et moulinet', 'Rute-Rolle-Kombo'),
    'baitcast combos': _T('Combo casting', 'Baitcast combo', 'Combo casting', 'Combo casting', 'Baitcast-Kombo'),
    'jigheads': _T('Testina piombata', 'Jig head', 'Cabeza plomada', 'Tête plombée', 'Jigkopf'),
    'rigs': _T('Montatura', 'Rig', 'Montaje', 'Montage', 'Montage'),
    'hooks': _T('Ami', 'Hooks', 'Anzuelos', 'Hameçons', 'Haken'),
    'snaps swivels and rings': _T('Girelle e moschettoni', 'Snaps and swivels', 'Emerillones y mosquetones',
                                  'Émerillons et agrafes', 'Wirbel und Karabiner'),
    'weights and sinkers': _T('Piombi', 'Weights', 'Plomos', 'Plombs', 'Bleie'),
    'floats': _T('Galleggiante', 'Float', 'Flotador', 'Flotteur', 'Pose'),
    'landing nets': _T('Guadino', 'Landing net', 'Sacadera', 'Épuisette', 'Kescher'),
    'bite alarms': _T('Segnalatore di abboccata', 'Bite alarm', 'Avisador de picada', 'Détecteur de touche',
                      'Bissanzeiger'),
    'pliers and hook removers': _T('Pinza', 'Pliers', 'Alicates', 'Pince', 'Zange'),
    'tackle bags': _T('Borsa porta artificiali', 'Tackle bag', 'Bolsa de pesca', 'Sac de pêche', 'Angeltasche'),
    'rod bags and sleeves': _T('Fodero portacanne', 'Rod bag', 'Funda para cañas', 'Fourreau', 'Rutenfutteral'),
    'utility boxes': _T('Scatola porta minuteria', 'Tackle box', 'Caja de pesca', 'Boîte de rangement',
                        'Angelbox'),
    'waders': _T('Waders', 'Waders', 'Vadeador', 'Waders', 'Wathose'),
    'sunglasses': _T('Occhiali polarizzati', 'Sunglasses', 'Gafas de sol', 'Lunettes de soleil', 'Sonnenbrille'),
    'gloves': _T('Guanti', 'Gloves', 'Guantes', 'Gants', 'Handschuhe'),
    'headwear': _T('Cappello', 'Hat', 'Gorra', 'Casquette', 'Mütze'),
}


def product_kind(supplier_category):
    """Tipo del prodotto dalla categoria del fornitore ("Line / Superline"), o None."""
    parts = [p.strip().lower() for p in (supplier_category or '').split('/') if p.strip()]
    for part in reversed(parts):
        if part in PRODUCT_KINDS:
            return PRODUCT_KINDS[part]
    return None


def site_name(brand, supplier_name, kind, lang):
    """
    Nome sul sito (2026-11-17, formato scelto dall'utente): marca + nome
    commerciale del fornitore + tipo nella lingua ("Berkley Sick Braid –
    Filo trecciato"). Il tipo non si aggiunge se il nome lo contiene già
    (es. "… Low Profile Reel", "… Spin Combo").
    """
    name = re.sub(r'\s+', ' ', supplier_name or '').strip()
    brand = (brand or '').strip()
    if not name:
        return ''
    if brand and not name.lower().startswith(brand.lower()):
        name = f'{brand} {name}'
    label = (kind or {}).get(lang) or (kind or {}).get('en_GB')
    if not label:
        return name
    words = {w.lower() for l in (label, (kind or {}).get('en_GB') or '') for w in re.findall(r'\w{4,}', l)}
    words |= {'reel', 'rod', 'combo', 'braid', 'line', 'lure', 'hook', 'hooks', 'net', 'bag'} & {
        w.lower().rstrip('s') for w in re.findall(r'\w+', (kind or {}).get('en_GB') or '')}
    if any(re.search(rf'\b{re.escape(w)}s?\b', name, re.I) for w in words):
        return name
    return f'{name} – {label}'
