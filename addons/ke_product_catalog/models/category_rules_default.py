"""
Regole di classificazione predefinite (2026-11-17), caricate dal pulsante
"Carica regole predefinite": quelle messe a punto su Mulinelli e Canne
(categoria del fornitore, tecnica di Amazon, parole nel nome). Poi si
modificano da Odoo.
"""

REEL_WORDS = r'mulinell|\breel|carrete|rolle\b|rollen|moulinet|bobina di filatura'
ROD_WORDS = r'\bcann[ae]\b|\brods?\b|\bca[ñn]a\b|\brute\b'
MUL, CAN = 'Pesca / Mulinelli', 'Pesca / Canne'

# Sottocategoria del fornitore (fine di "Categoria / Sottocategoria") → categoria Odoo.
SUPPLIER = [
    ('spinning reels', f'{MUL} / Spinning'), ('low profile baitcast reels', f'{MUL} / Casting'),
    ('round baitcast reels', f'{MUL} / Casting'), ('conventional reels', f'{MUL} / Traina, jigging e big game'),
    ('spare spools', f'{MUL} / Ricambi e bobine'),
    ('spinning rods', f'{CAN} / Spinning'), ('casting rods', f'{CAN} / Casting'),
    ('specimen rods', f'{CAN} / Carpfishing e feeder'), ('coarse rods', f'{CAN} / Carpfishing e feeder'),
    ('poles', f'{CAN} / Bolognese, match e fisse'),
    ('superline', 'Pesca / Fili / Trecciati'), ('monofilament', 'Pesca / Fili / Monofilo'),
    ('fluorocarbon', 'Pesca / Fili / Fluorocarbon'), ('leaders', 'Pesca / Fili / Terminali e leader'),
    ('rod bags and sleeves', 'Pesca / Contenitori e borse / Foderi e portacanne'),
    ('reel cases and covers', 'Pesca / Contenitori e borse'),
    ('snaps swivels and rings', 'Pesca / Minuteria / Girelle, moschettoni e anellini'),
    ('hooks', 'Pesca / Minuteria / Ami'), ('weights and sinkers', 'Pesca / Minuteria / Piombi'),
]

# Tecnica (valore italiano della specifica) → sottocategoria; (debole?)
TECH_REEL = [('Spinning|Light game', 'Spinning', True), ('Surfcasting', 'Surfcasting', False),
             ('Carpfishing|Feeder', 'Carpfishing e feeder', False), ('Bolognese|Match', 'Bolognese e match', False),
             ('Traina|Big game|Jigging|Bolentino', 'Traina, jigging e big game', False),
             ('Trota', 'Trota e area', False)]
TECH_ROD = [('Spinning|Light game', 'Spinning', True), ('Surfcasting', 'Surfcasting', False),
            ('Carpfishing|Feeder', 'Carpfishing e feeder', False),
            ('Bolognese|Match', 'Bolognese, match e fisse', False), ('Traina|Big game', 'Traina e big game', False),
            ('Jigging|Bolentino', 'Bolentino e jigging', False), ('Eging', 'Eging', False),
            ('Trota', 'Trota e area', False)]

# Parole nel nome → sottocategoria di mulinelli / canne; (debole?)
NAME_REEL = [
    (r'surf', 'Surfcasting', False),
    (r'carp|feeder|free ?spool|bait ?runner|ledger|karpfen|carpa|freilauf', 'Carpfishing e feeder', False),
    (r'traina|trolling|big ?game|offshore|drifting|jigging|slow ?pitch|curric|bolentino|vertical|elettric',
     'Traina, jigging e big game', False),
    (r'baitcast|bajo perfil|perfil bajo|low ?profile|bait ?caster|moltiplicat|multiplicador|multirolle',
     'Casting', False),
    (r'bolognes|\bmatch\b|inglese', 'Bolognese e match', False),
    (r'trota|trout|forelle|trucha|\barea\b', 'Trota e area', False),
    (r'spinning|rotante|giratorio|spinnroll|frizione (anteriore|posteriore)|front ?drag|filatura (anteriore|posteriore)'
     r'|light ?game|ultra ?light', 'Spinning', True),
]
NAME_ROD = [
    (r'surf|beach', 'Surfcasting', False),
    (r'carp|feeder|karpfen|carpa|quiver|ledger|picker', 'Carpfishing e feeder', False),
    (r'traina|trolling|big ?game|stand ?up|curric|drifting', 'Traina e big game', False),
    (r'bolentino|jigging|slow ?pitch|vertical|\bboat\b|barca', 'Bolentino e jigging', False),
    (r'eging|\begi\b|calamar|squid|totan', 'Eging', False),
    (r'baitcast|casting|\bbc\b', 'Casting', False),
    (r'bolognes|\bmatch\b|inglese|fissa|roubais|\bpole\b|telescop', 'Bolognese, match e fisse', False),
    (r'trota|trout|forelle|trucha|\barea\b', 'Trota e area', False),
    (r'spinning|spinnrute|light ?game|ultra ?light|rock ?fish|ajing|\bspin\b', 'Spinning', True),
]

# Prodotti finiti in Mulinelli/Canne che non lo sono (nome senza parole da mulinello/canna).
WRONG = [
    (r'trecc|braid|geflochten|trenzad|superline', 'Pesca / Fili / Trecciati'),
    (r'fluoro', 'Pesca / Fili / Fluorocarbon'),
    (r'leader|terminal|\bfinali\b|shock', 'Pesca / Fili / Terminali e leader'),
    (r'monofil|nylon|copolim', 'Pesca / Fili / Monofilo'),
    (r'\bfilo\b|\blenza\b|\bline\b|schnur|sedal|\d+ ?mt\b|\b0[.,]\d{2,3} ?mm', 'Pesca / Fili'),
    (r'\bami\b|\bhooks?\b|anzuel|haken', 'Pesca / Minuteria / Ami'),
    (r'piomb|\bsinker|roccotop', 'Pesca / Minuteria / Piombi'),
    (r'girell|\bsnap|moschetton|swivel|anellin', 'Pesca / Minuteria / Girelle, moschettoni e anellini'),
    (r'slamator|\baghi\b|pinza|forbic|coltell', 'Pesca / Accessori / Pinze, coltelli e attrezzi'),
    (r'artificial|\bmaggot|\besca\b|\blure', 'Pesca / Esche artificiali'),
    (r'\bborsa|\bbag\b|zaino|marsupio|custodia|valigett', 'Pesca / Contenitori e borse'),
    (r'acquari|aquarium|backdrop', 'Altro / Acquariologia'),
]
# Accessori PER mulinelli/canne: valgono anche se il nome cita mulinelli o canne.
HOLDERS = [
    (r'fodero|portacann|porta ?canne|rod ?bag|rod ?holder|rutentasche|funda', 'Pesca / Contenitori e borse / Foderi e portacanne'),
    (r'\bolio\b|grasso|\boil\b|grease|lubric', 'Pesca / Accessori / Manutenzione mulinelli'),
]


def rules():
    """(priorità, tipo, testo, si applica a, categoria, debole, tranne se il nome contiene)"""
    out = []
    for sub, target in SUPPLIER:
        out.append((10, 'supplier', rf'/\s*{sub}\s*$', [], target, False, None))
    for pattern, target in HOLDERS:
        out.append((15, 'name', pattern, [MUL, CAN], target, False, None))
    for pattern, target in WRONG:
        out.append((20, 'name', pattern, [MUL], target, False, REEL_WORDS))
        out.append((20, 'name', pattern, [CAN], target, False, ROD_WORDS))
    out.append((25, 'name', r'^\s*(bobina|spool|manovella|knob|pomello)\b', [MUL], f'{MUL} / Ricambi e bobine',
                False, r'mulinell|\breel|carrete|filatura'))
    for pattern, sub, weak in TECH_REEL:
        out.append((30, 'technique', pattern, [MUL], f'{MUL} / {sub}', weak, None))
    for pattern, sub, weak in TECH_ROD:
        out.append((30, 'technique', pattern, [CAN], f'{CAN} / {sub}', weak, None))
    for pattern, sub, weak in NAME_REEL:
        out.append((40, 'name', pattern, [MUL], f'{MUL} / {sub}', weak, None))
    for pattern, sub, weak in NAME_ROD:
        out.append((40, 'name', pattern, [CAN], f'{CAN} / {sub}', weak, None))
    return out
