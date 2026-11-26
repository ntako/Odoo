"""
Regole di classificazione predefinite (2026-11-17), caricate dal pulsante
"Carica regole predefinite": quelle messe a punto su Mulinelli e Canne
(categoria del fornitore, tecnica di Amazon, parole nel nome). Poi si
modificano da Odoo. Dal 2026-11-24 anche le sottocategorie degli ami
(come Nencini Sport).
"""

REEL_WORDS = r'mulinell|\breel|carrete|rolle\b|rollen|moulinet|bobina di filatura'
ROD_WORDS = r'\bcann[ae]\b|\brods?\b|\bca[ñn]a\b|\brute\b'
MUL, CAN = 'Pesca / Mulinelli', 'Pesca / Canne'

# Sottocategoria del fornitore (fine di "Categoria / Sottocategoria") → categoria Odoo.
SUPPLIER = [
    ('hooks', 'Pesca / Ami'), ('weights and sinkers', 'Pesca / Piombi'),
]

# ----------------------------------------------------------------------
# Canne (2026-11-26): le 22 sottocategorie di Nencini Sport. Il fornitore dice quasi sempre il tipo
# (Trabucco 012/BOLO, 015/FEEDER, 016/SURF…; Tubertini "Canne Bolognesi"…); Pure Fishing solo "Spinning /
# Casting Rods" (deboli: il nome precisa surf, eging, trota area…).
# ----------------------------------------------------------------------
C = {k: f'{CAN} / {v}' for k, v in {
    'all': 'All round', 'bolen': 'Bolentino', 'bolo': 'Bolognesi', 'carp': 'Carpfishing', 'cast': 'Casting',
    'travel': 'Da viaggio', 'eging': 'Eging', 'feeder': 'Feeder e ledgering', 'fisse': 'Fisse',
    'match': 'Inglesi e match', 'jig': 'Jigging e slow pitch', 'pike': 'Luccio e siluro', 'fly': 'Mosca',
    'ele': 'Per mulinelli elettrici', 'spin': 'Spinning', 'roub': 'Spondini e roubaisienne',
    'surf': 'Surfcasting e beach ledgering', 'traina': 'Traina e drifting', 'lago': 'Trota lago',
    'torr': 'Trota torrente', 'area': 'Trout area', 'vette': 'Vette'}.items()}

# Categoria del fornitore (per tutti): (testo, categoria, debole).
ROD_SUPPLIER = [
    (r'/\s*spinning rods\s*$|^\s*012/SPINN\s*$|^\s*canne spinning\s*$|^\s*(042/)?020/210\s*$', C['spin'], True),
    (r'/\s*casting rods\s*$', C['cast'], True),
    (r'/\s*carp rods\s*$|^\s*015/CARP\s*$|^\s*019/KK-010\s*$', C['carp'], False),
    (r'/\s*specimen rods\s*$', C['carp'], True),
    (r'/\s*coarse rods\s*$', C['feeder'], True),
    (r'^\s*(042/)?015/(FEEDER|FDR-T)\s*$|^\s*canne feeder\s*$', C['feeder'], False),
    (r'/\s*poles\s*$', C['fisse'], True),
    (r'^\s*013/T-POLE\s*$|^\s*canne fisse\s*$', C['fisse'], False),
    (r'/\s*fly rods\s*$|^\s*(042/)?020/200\s*$', C['fly'], False),
    (r'/\s*rod tips\s*$|^\s*vettini\s*$', C['vette'], False),
    (r'^\s*rods / spare parts\s*$|^\s*ricambi canne\s*$', C['vette'], True),
    (r'^\s*(042/)?012/(BOLO|SHORT)\s*$|^\s*canne bolognesi\s*$', C['bolo'], False),
    (r'^\s*012/TROTAT\s*$|^\s*canne trota torrente\s*$', C['torr'], False),
    (r'^\s*021/ROD_LK\s*$|^\s*canne trota lago\s*$', C['lago'], False),
    (r'^\s*013/ROUB\s*$|^\s*canne roubasienne\s*$', C['roub'], True),    # anche cimini e ricambi
    (r'^\s*015/(MAT-T|MATCH)\s*$|^\s*canne inglesi\s*$', C['match'], False),
    (r'^\s*015/SPEC\s*$', C['pike'], False),
    (r'^\s*016/BOAT(-T)?\s*$|^\s*canne bolentino\s*$', C['bolen'], True),
    (r'^\s*(042/)?016/SURF(-T)?\s*$|^\s*canne (surf casting|beach ledgering)\s*$', C['surf'], False),
    (r'^\s*016/TROLL\s*$|^\s*canne (traina|drifting)\s*$', C['traina'], False),
    (r'^\s*canne vertical\s*$', C['jig'], False),
    (r'^\s*canne travel\s*$', C['travel'], False),
    (r'^\s*canne all round\s*$', C['all'], True),
]
# Tecnica (specifiche) → sottocategoria; (debole?).
TECH_ROD = [('Spinning|Light game', C['spin'], True), ('Surfcasting', C['surf'], False),
            ('Carpfishing', C['carp'], False), ('Feeder', C['feeder'], False), ('Bolognese', C['bolo'], False),
            ('Match', C['match'], False), ('Traina|Big game', C['traina'], False), ('Jigging', C['jig'], False),
            ('Bolentino', C['bolen'], False), ('Eging', C['eging'], False), ('Trota', C['lago'], True),
            ('Mosca', C['fly'], False)]
# Parole nel nome (solo canne): (priorità, testo, categoria, debole).
NAME_ROD = [
    (25, r'^\s*(vett|cimin|tips?\b|puntal|spitze)', C['vette'], False),
    (35, r'elettric|electric(?!\s*(blue|green|purple|pink|lime|chicken|orange|shad))', C['ele'], False),
    # Serie che Nencini mette tutte in una sottocategoria diversa da "Spinning Rods" del fornitore.
    (38, r'\btidal\b', C['surf'], False),
    (38, r'\boverseas\b', C['travel'], False),
    # "Carp Pole" / "Carpodrome Pole": canna fissa per la carpa da laghetto, non carpfishing.
    (38, r'carp\w* pole|carp\w* tip set', C['fisse'], False),
    (40, r'surf|beach', C['surf'], False),
    (40, r'carp|karpfen|carpa|specimen|stalker|\bspod\b|marker', C['carp'], False),
    (40, r'feeder|quiver|picker|method|ledger', C['feeder'], False),
    (40, r'traina|trolling|big ?game|stand ?up|curric|drifting', C['traina'], False),
    (40, r'jigging|slow ?pitch|vertical|inchiku|tenya', C['jig'], False),
    (40, r'bolentino|\bboat\b|barca', C['bolen'], False),
    (40, r'eging|\begi\b|calamar|squid|totan|tataki', C['eging'], False),
    # Casting dice solo il mulinello: cede all'uso (Boat → Bolentino, Trolling → Traina, Vertical → Jigging).
    (44, r'baitcast|casting|\bbc\b', C['cast'], False),
    (40, r'bolognes|\bbolo\b|\bbls\b', C['bolo'], False),
    (40, r'\bmatch\b|inglese|waggler', C['match'], False),
    (40, r'\bfiss[ae]\b|\bpoles?\b', C['fisse'], False),
    (40, r'roubais|spondin', C['roub'], False),
    (40, r'trout ?area|\barea\b', C['area'], False),
    (40, r'trota lago|lake trout|\blago\b|\blake\b', C['lago'], False),
    (40, r'torrente|\bstream\b|\bcreek\b|torrent', C['torr'], False),
    (40, r'luccio|silur|\bpike\b|\bwels|catfish|madcat|storion|sturgeon', C['pike'], False),
    (40, r'\bmosca\b|\bfly\b|fliegen|mouche|#\d', C['fly'], False),
    (40, r'viaggio|travel|reise', C['travel'], False),
    (42, r'\b[4-8] ?(pc|pcs|pz|pezzi|sez|sezioni|sections?|pieces?|tlg)\b', C['travel'], True),
    (45, r'trota|trout|forelle|trucha', C['lago'], True),
    (45, r'all ?round', C['all'], True),
    (45, r'spinning|spinnrute|light ?game|ultra ?light|rock ?fish|ajing|\blrf\b|drop ?shot|\bjerk|\bspin\b',
     C['spin'], True),
]
# Regole predefinite delle canne di prima (2026-11-17): quelle che non ci sono più si disattivano.
OLD_ROD = [('supplier', rf'/\s*{sub}\s*$') for sub in ('spinning rods', 'casting rods', 'specimen rods',
                                                        'coarse rods', 'poles')] + [
    ('technique', p) for p in ('Spinning|Light game', 'Surfcasting', 'Carpfishing|Feeder', 'Bolognese|Match',
                               'Traina|Big game', 'Jigging|Bolentino', 'Eging', 'Trota')] + [
    ('name', p) for p in (r'surf|beach', r'carp|feeder|karpfen|carpa|quiver|ledger|picker',
                          r'traina|trolling|big ?game|stand ?up|curric|drifting',
                          r'bolentino|jigging|slow ?pitch|vertical|\bboat\b|barca', r'eging|\begi\b|calamar|squid|totan',
                          r'baitcast|casting|\bbc\b', r'bolognes|\bmatch\b|inglese|fissa|roubais|\bpole\b|telescop',
                          r'trota|trout|forelle|trucha|\barea\b',
                          r'spinning|spinnrute|light ?game|ultra ?light|rock ?fish|ajing|\bspin\b')]
ROD_TREE = [(f'{CAN} / Surfcasting', C['surf']), (f'{CAN} / Carpfishing e feeder', C['carp']),
            (f'{CAN} / Traina e big game', C['traina']), (f'{CAN} / Bolentino e jigging', C['bolen']),
            (f'{CAN} / Bolognese, match e fisse', C['bolo']), (f'{CAN} / Trota e area', C['lago'])]

# ----------------------------------------------------------------------
# Fili (2026-11-26): le 8 sottocategorie di Nencini Sport. Il fornitore dice quasi sempre il tipo
# (Pure Fishing Superline/Monofilament/Fluorocarbon/Fly Line; Trabucco 005/BRAID, 005/MONO/…; Tubertini
# Dyneema, Fluorocarbon, Shock Leaders); i Trabucco "MONO" comprendono fluorocarbon e big game (deboli).
# ----------------------------------------------------------------------
FIL = 'Pesca / Fili'
F = {k: f'{FIL} / {v}' for k, v in {
    'big': 'Big game', 'wire': 'Cavetti in acciaio', 'fly': 'Code', 'fc': 'Fluorocarbon',
    'fcc': 'Fluorocoated', 'mono': 'Nylon', 'shock': 'Shock leader', 'braid': 'Trecciati'}.items()}
LINE_SUPPLIER = [
    (r'/\s*(superline|uni-filament)\s*$|^\s*005/BRAID\s*$|^\s*dyneema\s*$', F['braid'], False),
    # Esclusi i codici e i gruppi che hanno una regola loro (fluorocarbon, big game, Momoi).
    (r'/\s*monofilament\s*$|^\s*005/MONO(/(?!010|080|110)\d+)?\s*$|^\s*monofili\b(?! momoi)', F['mono'], True),
    (r'/\s*fluorocarbon\s*$|^\s*fluorocarbon\s*$', F['fc'], False),
    (r'^\s*005/MONO/(010|110)\s*$|^\s*monofili momoi\s*$', F['fc'], True),
    (r'^\s*005/MONO/080\s*$', F['big'], True),
    (r'/\s*leaders\s*$|^\s*shock leaders\s*$', F['shock'], True),
    (r'/\s*fly line\s*$|^\s*020/240\s*$', F['fly'], False),
]
# Parole nel nome (solo fili): (priorità, testo, categoria, debole).
NAME_LINE = [
    (35, r'\bcod[ae]\b|fly ?line|flyline|\b(wf|dt|st)-?\d|\btaper', F['fly'], False),
    (35, r'cavett|\bwire|acciaio|\bsteel|titani|nitinol|\b(7x7|1x7|1x19|7x19)\b|49 ?fili', F['wire'], False),
    (38, r'fluoro ?coat|fluorocoated|\bfc ?coat', F['fcc'], False),
    (40, r'fluoro|fluorin', F['fc'], False),
    # Prima di fluoro: un trecciato con rivestimento fluoro è un trecciato.
    (37, r'trecc|braid|geflochten|trenzad|superline|dyneema|dynema|\b(x4|x8|x9|x12|4x|8x|9x|12x)\b|\bpe ?\d',
     F['braid'], False),
    (40, r'shock|leader|\bfinal[ei]\b|terminal', F['shock'], False),
    (42, r'big ?game|trolling|traina|\bibf\b', F['big'], False),
    (45, r'nylon|monofil|copolim|\bmono\b', F['mono'], False),
]
OLD_LINE = [('supplier', rf'/\s*{sub}\s*$') for sub in ('superline', 'monofilament', 'fluorocarbon', 'leaders')]
LINE_TREE = [(f'{FIL} / Monofilo', F['mono']), (f'{FIL} / Terminali e leader', F['shock'])]

# ----------------------------------------------------------------------
# Esche artificiali (2026-11-26): le 5 sottocategorie di Nencini Sport (20.852 articoli letti il 26/11).
# Wire baits = cucchiaini (ondulanti, trout area), spinner, spinnerbait, chatter/buzz bait; Jigs = metal
# jig, inchiku, tenya, kabura, slow jig; Egi = egi, totanare, polpare, tataki; Soft = siliconiche;
# Hard = il resto. Pure Fishing dice solo "Hard Bait" anche per spinner e ondulanti: decide il nome,
# con le serie che il nome non dice (Droppen, Reflex = spinner; Seeker, Sillen = jig) come le mette Nencini.
# ----------------------------------------------------------------------
ESC = 'Pesca / Esche artificiali'
L = {k: f'{ESC} / {v}' for k, v in {'egi': 'Egi', 'jig': 'Jigs', 'hard': 'Hard baits', 'soft': 'Soft baits',
                                     'wire': 'Wire baits'}.items()}
LURE_SUPPLIER = [
    (r'/\s*hard bait\s*$|^\s*artificiali (light trolling|ryobi|seika (mare|acqua dolce))\s*$', L['hard'], True),
    (r'/\s*soft bait\s*$', L['soft'], True),
    (r'^\s*018/SOFT\s*$|^\s*artificiali seika soft\s*$', L['soft'], False),
    (r'/\s*jigs\s*$', L['jig'], True),
    (r'^\s*artificiali (seika (jig|inchiku)|lanquanfishing jig)\s*$', L['jig'], False),
    (r'^\s*artificiali seika squid\s*$', L['egi'], False),
    (r'/\s*spinnerbaits\s*$', L['wire'], False),
]
NAME_LURE = [
    # Serie che il nome generico porterebbe altrove.
    (28, r'vibrax|blue fox|slobberknocker|salmon seeker|crazy swim jig|blade[ds]? jig|chatter|\bbuzz ?bait', L['wire'], False),
    (28, r'squid dealer', L['egi'], False),
    (29, r'inchiku|tenya|kabura|madai|\btai\b', L['jig'], False),
    (30, r'\begi\b|totanar|polpar|tataki|calamar|seppi|squid ?jig|\bsquid\b|cefalopod', L['egi'], False),
    (32, r'spinner|cucchiain|ondulant|\bspoons?\b|rotant|reflex|droppen|fast attack|\btoby\b|\bmepps\b',
     L['wire'], False),
    (33, r'metal ?jig|slow ?jig|jigging|\bjigs?\b|seeker|zerling|cast hacker|fast cast|sillen|\blucas\b|clonk',
     L['jig'], False),
    (34, r'\bsoft\b|silicon|gommos|\bvinil|slurp|hybrid pike|mutant rat|\bfrog|\bduck', L['soft'], False),
    (35, r'\bhybrid\b|crank|popper|stick ?bait|pencil|\bwtd\b|lipless|jerk ?bait|glide|topwater|\bplug\b', L['hard'], False),
    (36, r'\bshad|\bworms?\b|\bgrubs?\b|\bcraws?\b|creature|paddle|\btails?\b|\bnymph|\bleech', L['soft'], False),
    (45, r'minnow|\bvib\b|vibration|hard|swimbait|wobbler', L['hard'], True),
]
LURE_TREE = [(f'{ESC} / Hard bait e altre', L['hard']), (f'{ESC} / Calamaro (egi e totanare)', L['egi']),
             (f'{ESC} / Cucchiaini e spinner', L['wire']), (f'{ESC} / Jig e metal', L['jig']),
             (f'{ESC} / Siliconiche (soft bait)', L['soft']), (f'{ESC} / Superficie (topwater)', L['hard'])]

# ----------------------------------------------------------------------
# Esche e pasture (2026-11-26): le 7 sottocategorie di Nencini Sport (597 articoli). La nostra "Esche
# naturali, aromi e pasturazione" contiene anche mulinelli, ami, fili… arrivati dalla categoria Amazon
# "Aromi attrattivi": le regole di WRONG li portano fuori (vedi rules()).
# ----------------------------------------------------------------------
EP = 'Pesca / Esche e pasture'
B = {k: f'{EP} / {v}' for k, v in {'liq': 'Pasture liquide', 'boil': 'Boiles', 'glue': 'Colla', 'corn': 'Mais',
                                    'hook': 'Inneschi pronti', 'pel': 'Pellet', 'gb': 'Sfarinati'}.items()}
BAIT_SUPPLIER = [
    (r'/\s*attractant\s*$', B['liq'], False),
    (r'/\s*dough\s*$', B['hook'], False),
    (r'^\s*boiles e pellet\s*$', B['pel'], True),
    (r'^\s*(pastura acqua (dolce|salata)|pasture van den eynde)\s*$', B['gb'], False),
]
NAME_BAIT = [
    (30, r'attratt|attract|liquid|\bgel\b|spray|\bdips?\b|\baroma|sciropp|syrup|booster|\bslime\b', B['liq'], False),
    (31, r'power ?bait|trout ?bait|gulp|dough', B['hook'], False),   # anche se il nome dice pellet/pastura
    (32, r'boil|pop-?ups?\b|wafter', B['boil'], False),
    (32, r'\bcolla\b|gomma arabica|collant|stabilix', B['glue'], False),
    (32, r'\bmais\b|sweet ?corn|\bcorn\b|canapa|hemp|granagli', B['corn'], False),
    (32, r'pellet', B['pel'], False),
    (32, r'pastur(?!ator)|groundbait|method mix|sfarinat|farina|destrina', B['gb'], False),
    (35, r'power ?bait|trout ?bait|\bpast[ae]\b|paste|dough|camol|\buova\b|\beggs?\b|waxies|bigatt|nugget|innesc'
         r'|isome|ragworm|gulp', B['hook'], False),
    (45, r'compress|effervescent', B['liq'], True),
]
# Non sono esche: escono (le parole delle esche nel nome le tengono qui).
BAIT_WORDS = r'pastur(?!ator)|\besc[ah]e?\b|aroma|attratt|attract|boil|pellet|\bcolla\b|\bmais\b|trout ?bait|power ?bait|gulp|dough|bait'
BAIT_OUT = [(r'mulinell|\breels?\b', MUL), (r'\bcann[ae]\b|\brods?\b', CAN)]
BAIT_TREE = [('Pesca / Esche naturali, aromi e pasturazione', EP)]

# ----------------------------------------------------------------------
# Piombi (2026-11-26): categoria principale come da Nencini (prima Pesca / Minuteria / Piombi), con le
# sue 21 sottocategorie (781 articoli): quasi sempre una parola del nome le distingue.
# ----------------------------------------------------------------------
PB = 'Pesca / Piombi'
SINKER_SUPPLIER = [
    (r'^\s*019/KK-120\s*$', f'{PB} / Carpfishing', False),
    (r'^\s*014/FEEDER\s*$', f'{PB} / Pasturatori', True),
]
NAME_SINKER = [(seq, pattern, f'{PB} / {sub}', False) for seq, pattern, sub in [
    (29, r'pasturator|feeder|method|\bcage\b|basket|open end', 'Pasturatori'),
    (30, r'\bsond[ae]\b|plummet', 'Sonde'),
    (30, r'mascott?e?\b|mascot\b', 'Mascotte'),
    (30, r'affondator|downrigger', 'Per affondatore'),
    (30, r'filo (di )?piombo', 'Filo piombo'),
    (30, r'temolin', 'Temolino'),
    (30, r'guardian', 'Guardiani'),
    (30, r'sgancio|speedy|agua.?j', 'Sgancio rapido'),
    (30, r'cheburashka|\bcheb\b|bottom weights?\b', 'Cheburashka'),
    (30, r'\bnail\b', 'Nail'),
    (29, r'screw|punch rig head', 'Screw-in'),
    (30, r'bullet|texas', 'Bullet'),
    (30, r'drop ?shot|klip ?lok|bottom bouncer|lure specialist sinker', 'Drop shot'),
    (32, r'trigger|football|sticker|adesiv', 'Bottom weights'),
    (34, r'\bcarp|k-?karp|\bk-k\b|back lead|trilobe|distance lead|square bomb|pear lead|\bhang-on', 'Carpfishing'),
    (35, r'pallin(?!i da caccia)|spaccat|split ?shot|morbid|\bssg\b|piombo soft', 'Pallini spaccati'),
    (35, r'torpill|\bsfere\b|olivett|tube drop|tronchett', 'Torpille e sfere'),
    (36, r'plastific|plastgum|fosfo', 'Plastificati'),
    (37, r'anello inox|con anello|girella|sportix|aguglia|arrows', 'Con anello inox'),
    (37, r'foro passante|\boliva\b|spirale|siluro intercambiabile|inline|in-line|roccotop|roccorush',
     'Foro passante'),
    (38, r'bolentino|candela|blue deep', 'Bolentino'),
]]

# ----------------------------------------------------------------------
# Galleggianti (2026-11-26): categoria principale come da Nencini (prima Pesca / Minuteria /
# Galleggianti), 8 sottocategorie (836 articoli). Trabucco 001/… e Tubertini dicono quasi sempre il tipo.
# ----------------------------------------------------------------------
GA = 'Pesca / Galleggianti'
G = {k: f'{GA} / {v}' for k, v in {'acc': 'Accessori', 'ai': 'Acque interne', 'bomb': 'Bombarde',
                                    'feed': 'Galleggianti pasturatori', 'ing': 'Inglese', 'mare': 'Mare',
                                    'scor': 'Scorrevoli', 'trota': 'Trota lago'}.items()}
FLOAT_SUPPLIER = [
    (r'^\s*001/ACCESS\s*$|^\s*antenne galleggianti\s*$', G['acc'], False),
    (r'^\s*001/(COMP|LAGO|ANT/30)\s*$|^\s*galleggianti pro acqua (ferma|lenta|veloce)\s*$', G['ai'], False),
    (r'^\s*001/HOBBY\s*$|^\s*galleggianti vari\s*$', G['ai'], True),
    (r'^\s*001/(PIOM45|ANT/45)\s*$|^\s*galleggianti pro mare\s*$', G['mare'], False),
    (r'^\s*001/SLID45\s*$', G['scor'], False),
    (r'^\s*001/(WAG|MATCH)\s*$|^\s*galleggianti inglesi\s*$', G['ing'], False),
    (r'^\s*001/VELA\s*$|^\s*galleggianti e articoli trota\s*$', G['trota'], False),
    (r'^\s*bombarde\s*$', G['bomb'], False),
    (r'/\s*floats\s*$', GA, True),
]
NAME_FLOAT = [
    (28, r'pasturator|feeder', G['feed'], False),
    (30, r'bombard|maracas|speedy|\bbomb\b|calimero', G['bomb'], False),
    (30, r'antenn|^\W*(starli(ght|te)|vetrin)|scoubidou|anellini|gommini|glass weight|infila|accessori|micro tub', G['acc'], False),
    (29, r'torpill|olivett', f'{PB} / Torpille e sfere', False),   # sono piombi
    (32, r'scorrev|slider|subfloat', G['scor'], False),
    (33, r'\btrot|trout|\bvela\b|\bsail\b', G['trota'], False),
    (34, r'ingles|waggler|pavone|\bstrale\b', G['ing'], False),
    (35, r'\bmare\b|piombat|\bsea\b|spigol|\bsw\b', G['mare'], False),
    (40, r'carp|bolognes|fiume|river|acqua (ferma|lenta|veloce)|roubais|\bpole\b|\blago\b|competiz', G['ai'], True),
]

# ----------------------------------------------------------------------
# Minuteria (2026-11-26): le 11 sottocategorie di Nencini Sport (1.054 articoli). Ami, piombi e
# galleggianti sono ora categorie principali.
# ----------------------------------------------------------------------
MN = 'Pesca / Minuteria'
OLD_TACKLE = [('supplier', r'/\s*snaps swivels and rings\s*$')]
TACKLE_SUPPLIER = [
    (r'/\s*snaps swivels and rings\s*$|^\s*010/SWIVEL\s*$|^\s*021/SWIV\s*$|^\s*girelle\s*$', f'{MN} / Girelle', True),
    (r'/\s*components\s*$', f'{MN} / Componenti per il montaggio', True),
    (r'/\s*rigs\s*$', f'{MN} / Terminali', True),
    (r'^\s*moschettoni e attacchi\s*$', f'{MN} / Moschettoni', True),
    (r'^\s*rings\s*$', f'{MN} / Split ring', True),
]
NAME_TACKLE = [(seq, pattern, f'{MN} / {sub}', False) for seq, pattern, sub in [
    (28, r'crimp|manicott', 'Crimps'),
    (28, r'solid ?rings?|assist ring|metal ring', 'Solid ring'),
    (28, r'split ?rings?|splitring|anellin', 'Split ring'),
    (28, r'heat shrink', 'Anti-tangle'),
    (29, r'fast snaps?|corkscrew snap|fermagallegg', 'Attacchi'),
    (30, r'anti ?-?tangle|\bbooms?\b|cross ?line|feeder links?|sliding link', 'Anti-tangle'),
    (30, r'stopper|\bstops?\b|push ?lock', 'Stopper'),
    (30, r'perlin|\bperle\b|beads|pearls|helicopter|salvanodo', 'Perline'),
    (31, r'\btrave\b|terminal[ei]|chod|\brigs?\b|leaders?\b|\bcore\b', 'Terminali'),
    (32, r'girell|swivel|rolling|barrel|crane', 'Girelle'),
    (33, r'snap|moschett|\bclips?\b|quick ?link', 'Moschettoni'),
    (36, r'attacc|connector|connettor|fast ?link|stinger screw|triplo', 'Attacchi'),
    (38, r'\beyes\b|floss|polymultifil|streamer|piumett|bait (stop|needle)|\bago\b|aghi', 'Componenti per il montaggio'),
]]
TACKLE_TREE = [(f'{MN} / Girelle, moschettoni e anellini', f'{MN} / Girelle'),
               (f'{MN} / Montature e altro', f'{MN} / Terminali')]

# ----------------------------------------------------------------------
# Reggicanne e supporti (2026-11-26): nuova categoria principale come da Nencini (150 articoli, 5
# sottocategorie). Oggi questi prodotti sono soprattutto in "Accessori / Carpfishing e postazione": le
# regole sul nome valgono anche lì. I reggicanna da carpa (Prologic Snatch, One Way…) Nencini li mette
# in Rod pod: la parola "rod rest" è debole e cede al fornitore.
# ----------------------------------------------------------------------
RG = 'Pesca / Reggicanne e supporti'
RG_SUPPLIER = [
    (r'/\s*(rod pods|buzzerbars|banksticks)\s*$', f'{RG} / Rod pod', False),
    (r'/\s*bankware accessories\s*$|^\s*019/KK-050\s*$', f'{RG} / Rod pod', True),
    (r'/\s*tripods\s*$', f'{RG} / Tripodi e treppiedi', False),
]
NAME_RG = [(seq, pattern, f'{RG} / {sub}', weak) for seq, pattern, sub, weak in [
    (30, r'tripod|treppied', 'Tripodi e treppiedi', False),
    (30, r'rod ?pod|\btri ?pod|\bpod\b|buzz ?bar|buzzerbar|goal ?post|bank ?stick|stabili[sz]er', 'Rod pod', False),
    (31, r'kits? ?rest|appoggia ?kit|(?<!per )\broller\b|(?<!per )\brullo\b', 'Poggia kit', False),
    (32, r'picchett|(sand|rod) spike|puntal|butt grip|beach rest|rod ?rest beach|reggi ?canna surf', 'Picchetti e puntali', False),
    (33, r'rod ?rests?|reggi ?cann|appoggia ?cann|side (pole )?arm|butt rest|feeder rest', 'Reggicanne da sedia', True),
]]
RG_SCOPE = [RG, 'Pesca / Accessori']

# ----------------------------------------------------------------------
# Panieri (2026-11-26): nuova categoria principale come da Nencini (228 articoli, 9 sottocategorie). Oggi
# sono in Contenitori e borse (seat box Pure Fishing), Accessori e Campeggio / Sedie (accessori SKP).
# Trabucco 011/SEAT-3 = accessori dei panieri ("GENIUS * PIATTO LATERALE": la famiglia è "GENIUS").
# ----------------------------------------------------------------------
PN = 'Pesca / Panieri'
PN_SUPPLIER = [
    (r'^\s*011/SEAT-3\s*$', f'{PN} / Accessori', True),
    (r'^\s*011/(SEAT-1|CHAIR)\s*$|/\s*tackle seat boxes\s*$', f'{PN} / Panchetti', True),
    (r'^\s*011/SEAT-2\s*$', f'{PN} / Moduli', False),
]
NAME_PN = [(seq, pattern, f'{PN} / {sub}', False) for seq, pattern, sub in [
    (28, r'trolley|wheel ?(set|kit)|carrell|transporter|handle set|kit traino', 'Carrelli e kit trasporto'),
    (29, r'umbrella arm|porta ?ombrellon', 'Staffe per ombrelloni'),
    (30, r'foot ?plate|pedana(?! laterale)|piattaforma', 'Pedane'),
    (30, r'side (tray|plate|platform)|piatt[oi] lateral|shield tray|vassoio lateral|pedana laterale', 'Piatti laterali'),
    (31, r'cassone|shakespeare seat ?box|surf box|saltwater seat ?box|sherpa|sportsman.?s trunk|d-vec seat|spallaccio', 'Cassoni surfcasting'),
    (32, r'\bmodul[oei]\b|(front|side) drawers?|cassett[oi] frontal|cushion', 'Moduli'),
    (33, r'feeder arms?|cross arm|keepnet (arm|support|rest)|braccio telescop|barra antivento|ripple bar'
         r'|side rod arms?', 'Bracci'),
    (35, r'panchett|seat ?box|seatbox|gnt station|feeder chair|flexy ?chair|panier', 'Panchetti'),
]]
PN_SCOPE = [PN, 'Pesca / Borse e foderi', 'Pesca / Accessori', 'Campeggio e outdoor / Sedie e arredo']

# ----------------------------------------------------------------------
# Valigette e scatole (2026-11-26): nuova categoria principale come da Nencini (351 articoli, 6
# sottocategorie); la nostra "Contenitori e borse / Cassette e scatole" si sposta qui.
# ----------------------------------------------------------------------
VA = 'Pesca / Valigette e scatole'
VA_SUPPLIER = [
    (r'/\s*(tray tackle boxes|rack systems)\s*$', f'{VA} / Valigette', False),
    (r'^\s*fishing storage / tackle organizers\s*$', f'{VA} / Porta artificiali', True),
    (r'^\s*fishing storage / utility boxes\s*$|^\s*011/BOX\s*$', f'{VA} / Porta minuteria e varie', True),
    (r'/\s*fly boxes\s*$', f'{VA} / Porta mosche', False),
]
NAME_VA = [(seq, pattern, f'{VA} / {sub}', weak) for seq, pattern, sub, weak in [
    (28, r'fly (box|patch)|porta ?mosche', 'Porta mosche', False),
    (29, r'porta ?ami', 'Porta ami', False),
    (29, r'hook ?length|porta ?terminali', 'Porta terminali', False),
    (30, r'tray tackle box|drawer tackle box|tackle system|cantilever|rack system|valigett|hip roof|'
         r'\b(one|two|three|four)-(tray|drawer)', 'Valigette', False),
    (31, r'bait ?box|lure ?box|lurebox|crankbait|jerkbait|swimbait|spinner ?bait|soft plastic|jig box|porta ?artificial'
         r'|porta ?esche|bait bucket', 'Porta artificiali', False),
    (33, r'utility box|stowaway|terminal|accessor[iy] box|scatol|\bbox\b|cassett', 'Porta minuteria e varie', True),
]]
VA_SCOPE = [VA, 'Pesca / Borse e foderi']
VA_TREE = [('Pesca / Contenitori e borse / Cassette e scatole', VA)]

# ----------------------------------------------------------------------
# Guadini, nasse e bilance (2026-11-26): categoria principale come da Nencini (472 articoli, 4
# sottocategorie; le bilance Nencini le mette in Accessori). "Accessori / Guadini e reti" si sposta qui.
# ----------------------------------------------------------------------
GU = 'Pesca / Guadini, nasse e bilance'
GU_SUPPLIER = [
    (r'/\s*landing nets\s*$|^\s*guadini\s*$', f'{GU} / Guadini', True),
    (r'/\s*keepnets\s*$', f'{GU} / Nasse', False),
    (r'/\s*bait nets\s*$', f'{GU} / Nasse', True),
    (r'/\s*replacement nets\s*$|^\s*008/NET\s*$', f'{GU} / Teste di guadino', True),
    (r'^\s*008/HAN(/\w+)?\s*$', f'{GU} / Pali per guadino', False),
]
NAME_GU = [(seq, pattern, f'{GU} / {sub}', False) for seq, pattern, sub in [
    (28, r'nass[ae]|keep ?nets?|bait station', 'Nasse'),
    (29, r'testa (di |per )?guadino|net ?heads?|landing net head|replacement net|rete di ricambio|alu-?head', 'Teste di guadino'),
    (30, r'(net|guadino) (handle|pole)|manico (di |per )?guadino|\bpalo\b|\d[.,]\d{1,2} ?mt\b|\bmt\.? ?\d[.,]\d', 'Pali per guadino'),
    (32, r'guadin|landing net|boat net|\bnet\b', 'Guadini'),
]]
GU_SCOPE = [GU, 'Pesca / Accessori']
GU_TREE = [('Pesca / Accessori / Guadini e reti', GU)]

# ----------------------------------------------------------------------
# Borse e foderi (2026-11-26): "Contenitori e borse" diventa la categoria di Nencini (773 articoli, 7
# sottocategorie). Le borse Prologic e JRC (Avenger, Defender, Element…) Nencini le mette in Borse
# carpfishing.
# ----------------------------------------------------------------------
BF = 'Pesca / Borse e foderi'
OLD_BAG = [('supplier', r'/\s*rod bags and sleeves\s*$'), ('supplier', r'/\s*reel cases and covers\s*$')]
BF_SUPPLIER = [
    (r'/\s*(rod bags and sleeves|rod cases and tubes|holdalls)\s*$|^\s*foderi portacanne\s*$', f'{BF} / Portacanne', False),
    (r'/\s*rod racks\s*$', f'{BF} / Portacanne', True),
    (r'/\s*(reel cases and covers|carryalls|tackle bags|rucksacks|satchels|tackle backpacks and slings'
     r'|specialty bags and boxes)\s*$|^\s*borse (varie|seika e ethnic)\s*$', f'{BF} / Borse generiche', True),
    (r'/\s*bait bags wallets and wraps\s*$', f'{BF} / Borse porta artificiali', True),
    (r'/\s*(soft|hard)-sided coolers\s*$', f'{BF} / Borse frigo', False),
]
NAME_BF = [(seq, pattern, f'{BF} / {sub}', weak) for seq, pattern, sub, weak in [
    (28, r'portanass|keep ?net (bag|storage)', 'Portanasse', False),
    (29, r'frigo|termic|thermic|cool(er)?\b|cool bag', 'Borse frigo', False),
    (31, r'\bcarp|avenger|defender|element storm|cocoon|\brova\b|bedchair|cruzade|c-series', 'Borse carpfishing', False),
    (30, r'foder[oi]|portacann|porta ?canne|rod ?(bag|sleeve|tube|case|quiver|holdall)|holdall|sleeve|quiver'
         r'|\bcase \d{3}\b',
     'Portacanne', False),
    (32, r'artificial|lure ?(bag|wallet|case)|jig wallet|\begi\b|squid case|porta ?esche|bait (bucket|bag)|secchi|hip bag|sling'
         r'|bucket', 'Borse porta artificiali', False),
    (33, r'\beva\b|bakkan|insert pouch', 'Borse EVA', False),
    (36, r'borsa|\bbags?\b|zaino|rucksack|backpack|marsup|waist|carryall|\bcase\b|pouch|tasca', 'Borse generiche',
     True),
]]
BF_TREE = [(f'Pesca / Contenitori e borse / Borse e zaini', f'{BF} / Borse generiche'),
           (f'Pesca / Contenitori e borse / Foderi e portacanne', f'{BF} / Portacanne'),
           (f'Pesca / Contenitori e borse / Borse e secchi per esche', f'{BF} / Borse porta artificiali'),
           ('Pesca / Contenitori e borse', BF)]

# ----------------------------------------------------------------------
# Kit pesca (2026-11-26): "Combo canna e mulinello" diventa la categoria di Nencini (11 sottocategorie per
# tecnica). Pure Fishing dice il tipo di combo; il nome precisa la tecnica (Carp, Feeder, Surf, Trota…).
# ----------------------------------------------------------------------
KIT = 'Pesca / Kit pesca'
K = {k: f'{KIT} / {v}' for k, v in {
    'bolen': 'Bolentino', 'bolo': 'Bolognesi', 'carp': 'Carpfishing', 'cast': 'Casting',
    'feeder': 'Feeder e ledgering', 'pike': 'Luccio e siluro', 'fly': 'Mosca', 'spin': 'Spinning',
    'surf': 'Surfcasting', 'traina': 'Traina', 'trota': 'Trota'}.items()}
KIT_SUPPLIER = [
    (r'/\s*(spinning|spincast) combos\s*$', K['spin'], True),
    (r'/\s*baitcast combos\s*$', K['cast'], True),
    (r'/\s*fly combos\s*$', K['fly'], False),
    (r'/\s*conventional combos\s*$', K['traina'], True),
    (r'/\s*(specimen|carp) combos\s*$', K['carp'], False),
    (r'/\s*coarse combos\s*$', K['feeder'], True),
    (r'^\s*combos / poles\s*$', K['bolo'], True),
]
NAME_KIT = [(seq, pattern, K[key], weak) for seq, pattern, key, weak in [
    (30, r'\bfly\b|mosca|nymph', 'fly', False),
    (30, r'\bcarp|specimen|karpfen', 'carp', False),
    (30, r'feeder|ledger|picker|coarse|\bmatch\b', 'feeder', False),
    (30, r'\bsurf(?!ace)|beach', 'surf', False),
    (30, r'traina|trolling|downrigger', 'traina', False),
    # Nencini divide i kit per mulinello: "Boat", "Trout", "Pike" con mulinello spinning stanno in Spinning.
    (30, r'\bboat\b|bolentino|barca|level wind', 'bolen', True),
    (30, r'\bpike\b|luccio|silur|\bwels|catfish|madcat', 'pike', True),
    (30, r'\btrot|trout|forelle', 'trota', True),
    (30, r'bolognes|\bbolo\b|\bpole\b', 'bolo', False),
    (32, r'casting|baitcast|low profile|\bbc\b', 'cast', False),
    (35, r'spinning|spin\b|allround|all round', 'spin', True),
]]
KIT_TREE = [('Pesca / Combo canna e mulinello', KIT)]

# ----------------------------------------------------------------------
# Abbigliamento (2026-11-26): Pesca / Abbigliamento come da Nencini (1.451 articoli, 13 sottocategorie).
# Scelta dell'utente: ci va solo l'abbigliamento da pesca riconosciuto dal fornitore (Pure Fishing
# Apparel ed Eyewear, Tubertini, Trabucco 004/ABB, K-Karp); quello generico resta in "Abbigliamento e
# calzature" (fuori da Pesca), per ora.
# ----------------------------------------------------------------------
AB = 'Pesca / Abbigliamento'
A = lambda sub: f'{AB} / {sub}'
AB_SUPPLIER = [
    (r'^\s*apparel / outerwear\s*$|^\s*giacche\s*$', A('Giacche e pantaloni'), True),
    (r'^\s*apparel / headwear\s*$|^\s*guanti, cappelli e cuffie\s*$', A('Cappelli'), True),
    (r'^\s*apparel / shirts\s*$', A('T-shirt'), True),
    (r'^\s*apparel / gloves\s*$', A('Guanti'), False),
    (r'^\s*apparel / (pants|shorts)\s*$|^\s*pantaloni e salopette\s*$', A('Pantaloni e salopette'), False),
    (r'^\s*apparel / (underwear|socks)\s*$', A('Intimo'), False),
    (r'^\s*apparel / |^\s*004/ABB\s*$|^\s*019/KK-130\s*$', AB, True),
    (r'^\s*eyewear / |^\s*occhiali\s*$', A('Occhiali'), False),
]
NAME_AB = [(seq, pattern, A(sub), False) for seq, pattern, sub in [
    (25, r'occhial|sunglass|polariz', 'Occhiali'),
    (26, r'guant|glove|mitten|muffol', 'Guanti'),
    (27, r'\bcalz[ae]\b|socks|scaldacollo|neck ?(gaiter|warmer|tube)|intimo|underwear|base ?layer|\bbuff\b',
     'Intimo'),
    (28, r'cappell|\bcaps?\b|beanie|berrett|visor|visiera|\bhats?\b|bandana', 'Cappelli'),
    (29, r'complet[oi]\b|\bsuits?\b|\btuta\b', 'Completi'),
    (30, r'gilet|\bvests?\b|smanicat', 'Gilet'),
    (31, r'\bpile\b|fleece|soft ?shell', 'Pile e softshell'),
    (32, r'felp[ae]|hood(ie|y)|sweat|crew ?neck|zip ?neck', 'Felpe'),
    (33, r'\bpolo\b', 'Polo'),
    (34, r'camici[ae]', 'Camicie'),
    (35, r't-?shirt|\btees?\b|long sleeve|magliet|maglia|jersey|\bshirts?\b', 'T-shirt'),
    (36, r'pantalon|salopette|\bbibs?\b|shorts|bermuda|\bpants\b|trousers', 'Pantaloni e salopette'),
    (37, r'giacc|jacket|giubbott|parka|anorak|smock', 'Giacche e pantaloni'),
]]

# ----------------------------------------------------------------------
# Stivali e waders (2026-11-26): Pesca / Stivali e waders come da Nencini (193 articoli, 5 sottocategorie),
# come per l'abbigliamento solo quelli da pesca (fornitore "Wading Gear", o "wader/wading/scafandro" nel
# nome). Le giacche da wading vanno in Abbigliamento / Giacche.
# ----------------------------------------------------------------------
SW = 'Pesca / Stivali e waders'
SW_SUPPLIER = [
    (r'^\s*wading gear / waders\s*$', SW, True),
    (r'^\s*wading gear / wading boots\s*$', f'{SW} / Scarpe da wading', False),
    (r'^\s*wading gear / wading jackets\s*$', f'{AB} / Giacche e pantaloni', False),
    (r'^\s*footwear / boots\s*$', f'{SW} / Stivali', True),
]
NAME_SW_ENTRY = [(26, r'\bwaders?\b|scafandr|wading (shoe|boot)', SW, True)]   # anche da "Abbigliamento e calzature"
_WADER = r'\bwaders?\b(?=.*(?:{0}))|(?:{0})(?=.*\bwaders?\b)'   # il materiale conta solo per un wader (uno stivale in neoprene è uno stivale)
NAME_SW = [(seq, pattern, f'{SW} / {sub}', weak) for seq, pattern, sub, weak in [
    (27, r'wading (shoe|boot)s?|scarp[ae] da wading|wade boot|trek boot', 'Scarpe da wading', False),
    (28, r'hip waders?|cosciale', 'Stivali', False),   # i cosciali Nencini li mette negli stivali
    (29, _WADER.format(r'neoprene|\bneo\b'), 'Waders neoprene', False),
    (29, _WADER.format(r'breathable|traspirant|gore-?tex|dryzone'), 'Waders traspiranti', False),
    (29, _WADER.format(r'\bpvc\b|rubber|gomma|nylon|tasl[ao]n') + r'|scafandr', 'Waders PVC', False),
    (32, r'stival|\bboots?\b', 'Stivali', False),
]]

# ----------------------------------------------------------------------
# Attrezzatura da campo (2026-11-26): Pesca / Attrezzatura da campo come da Nencini (136 articoli, 7
# sottocategorie), solo l'attrezzatura da pesca (Prologic, JRC, Trabucco…: fornitore "Furniture", "Tents and
# Shelters", "Bankware / Unhooking Mats, Sacks and Slings"). Fornelli e affumicatori restano in Campeggio.
# ----------------------------------------------------------------------
AC = 'Pesca / Attrezzatura da campo'
AC_SUPPLIER = [
    (r'/\s*(unhooking mats|sacks and slings)\s*$', f'{AC} / Cura del pescato', False),
    (r'^\s*furniture / bed chairs\s*$', f'{AC} / Lettini', False),
    (r'^\s*furniture / sleeping bags\s*$', f'{AC} / Sacchi a pelo', False),
    (r'^\s*furniture / chairs\s*$', f'{AC} / Sedie', True),
    (r'^\s*furniture / tables\s*$', f'{AC} / Tavolini', False),
    (r'^\s*tents and shelters / (bivvies|shelters|bivvy accessories)\s*$', f'{AC} / Tende', False),
    (r'^\s*tents and shelters / brollies\s*$|^\s*010/UMB\s*$', f'{AC} / Ombrelloni', True),
    (r'^\s*019/KK-030\s*$', AC, True),
]
NAME_AC = [(seq, pattern, f'{AC} / {sub}', False) for seq, pattern, sub in [
    (28, r'unhooking|materassin|\bmats?\b|sling|\bsack\b|cradle|retainer|weigh', 'Cura del pescato'),
    (29, r'bed ?chair|flatbed|lettin|sleep ?system', 'Lettini'),
    (30, r'sleeping|sacco a pelo|s/bag|bed cover', 'Sacchi a pelo'),
    (31, r'tavolin|\btable\b', 'Tavolini'),
    (32, r'bivvy|shelter|\btend[ae]\b|overwrap|\bwrap\b|porch|\btents?\b', 'Tende'),
    (33, r'umbrella|brolly|ombrell', 'Ombrelloni'),
    (34, r'chair|sedi[ae]|stool|sgabell|recliner', 'Sedie'),
]]

# ----------------------------------------------------------------------
# Belly boat (2026-11-26): categoria senza sottocategorie come da Nencini (53 articoli): belly boat, kayak e
# barchini da pesca con pinne, pompe, supporti per ecoscandaglio e portacanne da barca (Pure Fishing "Boats
# and Motors" e "Rod Holders"). Oggi sono in "Nautica / Barche e motori".
# ----------------------------------------------------------------------
BB = 'Pesca / Belly boat'
BB_SUPPLIER = [
    (r'^\s*boats and motors / ', BB, False),
    (r'^\s*tools and equipment / rod holders\s*$', BB, True),
]
NAME_BB = [(26, r'belly ?bo(at|ot)|float ?tube|kayak', BB, False)]
BB_SCOPE = ['Nautica', 'Pesca / Accessori']

# ----------------------------------------------------------------------
# Accessori (2026-11-26): le 21 sottocategorie di Nencini (1.397 articoli). Pinze coltelli e attrezzi →
# Pinze e forbici, Segnalatori di abboccata → Avvisatori e segnalatori, Bilance e misure → Bilance;
# Manutenzione mulinelli, Altri accessori e il resto di Carpfishing e postazione → Accessori vari (che
# raccoglie anche, debole, tutto quello che non ha un posto più preciso).
# ----------------------------------------------------------------------
PA = 'Pesca / Accessori'
P = lambda sub: f'{PA} / {sub}'
PA_SUPPLIER = [
    (r'/\s*(bite alarms|bite indicators)\s*$', P('Avvisatori e segnalatori'), False),
    (r'/\s*rulers and scales\s*$|^\s*008/ACC\s*$', P('Bilance'), True),
    (r'/\s*(pliers and hook removers|multi-tools)\s*$', P('Pinze e forbici'), True),
    (r'/\s*(knives|scissors)\s*$', P('Coltelli e forbici'), True),
    (r'^\s*tools and equipment / lights\s*$', P('Torce'), False),
    (r'^\s*tools and equipment / buckets\s*$|^\s*porta esche e pasture\s*$', P('Porta esche'), False),
    (r'/\s*lip grips\s*$', P('Boga grip'), False),
    (r'/\s*rod belts\s*$', P('Cinture da combattimento'), False),
    (r'/\s*gaffs\s*$', P('Accessori traina e barca'), False),
    (r'^\s*(014/CAT|fionde)\s*$', P('Fionde e ricambi'), False),
    (r'^\s*elastici roubasienne\s*$', P('Elastici e materiale per montaggio'), False),
    (r'^\s*(010/MATCH|010/VARI|accessori canne e roubasienne)\s*$', P('Elastici e materiale per montaggio'), True),
    (r'^\s*010/SURF\s*$', P('Accessori surfcasting'), True),
    (r'^\s*tools and equipment / (line tools|equipment accessories|equipment maintenance|priests and spikes'
     r'|bait tools)\s*$|^\s*(accessori vari|014/ACC)\s*$', P('Accessori vari'), True),
]
NAME_PA = [(seq, pattern, P(sub), weak) for seq, pattern, sub, weak in [
    (25, r'slamator|hook remover|disgorger|releaser', 'Slamatori', False),
    (26, r'\bboga\b|lip ?grip|fish ?grip', 'Boga grip', False),
    (26, r'bilanci|\bscales?\b|peson|digital weigh', 'Bilance', False),
    (26, r'fiond|catapult|throwing stick|bicchierin', 'Fionde e ricambi', False),
    (26, r'setacc|riddle', 'Setacci', False),
    (26, r'salvanell|tip guard|tip protector', 'Salvanelli', False),
    (26, r'cintura|fighting belt|gimbal|rod belt', 'Cinture da combattimento', False),
    (27, r'lampada (di profondit|siluro)|deep ?light|\blp/\d|underwater light', 'Lampade di profondità', False),
    (28, r'torcia|head ?(lamp|light)|frontale|lampad|\btorch|flashlight', 'Torce', False),
    (28, r'avvisator|segnalator|bite ?alarm|\balarm|indicator|swinger|hanger|receiver|\bbobbin\b(?! holder)',
     'Avvisatori e segnalatori', False),
    (28, r'avvolgilenz|winder|spool box|porta ?bobin', 'Avvolgilenza', False),
    (28, r'morsett|\bvi[cs]e\b|tube fly tool', 'Morsetti', False),
    (29, r'line clipper|dubbing|bobbin holder|hackle|whip finish|winding tool|annodator|knot tool|tying', 'Strumenti da costruzione',
     False),
    (30, r'coltell|knife|sfilett|fillet|affilator|sharpener|forbic|scissor|cutter', 'Coltelli e forbici', False),
    (31, r'pinz|plier|crimper|multi ?tool', 'Pinze e forbici', False),
    (32, r'(?<!bait )elastic|lastic\b|apical|connector|dacron|\bcon[oi]\b|bung|infila elastic', 'Elastici e materiale per montaggio',
     False),
    (33, r'secchi|bucket|bacinell|\bbowl|bait box|porta ?esche', 'Porta esche', False),
    (34, r'raffio|\bgaff|outrigger|divergent|portacanna da barca|sardamatic|trita|chum', 'Accessori traina e barca',
     False),
    (35, r'\bsurf|\bag[oh]i?\b|needle|finger|salvadito|travett', 'Accessori surfcasting', False),
    (36, r'elettric|electric|recuper|rocchett|staffa|kristal fishing', 'Mulinelli elettrici e recuperi', False),
    (45, r'^\W*\w+', 'Accessori vari', True),   # ripiego: tutto il resto degli accessori
]]
PA_TREE = [(P('Pinze, coltelli e attrezzi'), P('Pinze e forbici')),
           (P('Segnalatori di abboccata'), P('Avvisatori e segnalatori')),
           (P('Bilance e misure'), P('Bilance')),
           (P('Manutenzione mulinelli'), P('Accessori vari')),
           (P('Altri accessori'), P('Accessori vari')),
           (P('Carpfishing e postazione'), P('Accessori vari'))]

# Prodotti finiti in Mulinelli/Canne che non lo sono (nome senza parole da mulinello/canna).
WRONG = [
    (r'trecc|braid|geflochten|trenzad|superline', 'Pesca / Fili / Trecciati'),
    (r'fluoro', 'Pesca / Fili / Fluorocarbon'),
    (r'leader|terminal|\bfinali\b|shock', 'Pesca / Fili / Shock leader'),
    (r'monofil|nylon|copolim', 'Pesca / Fili / Nylon'),
    (r'\bfilo\b|\blenza\b|\bline\b|schnur|sedal|\d+ ?mt\b|\b0[.,]\d{2,3} ?mm', 'Pesca / Fili'),
    (r'\bami\b|\bhooks?\b|anzuel|haken', 'Pesca / Ami'),
    (r'pasturatore? (galleggiante|scorrevole)|floating feeder', 'Pesca / Galleggianti / Galleggianti pasturatori'),
    (r'bombard|maracas', 'Pesca / Galleggianti / Bombarde'),
    (r'torpill|olivett', 'Pesca / Piombi / Torpille e sfere'),
    (r'pasturator', 'Pesca / Piombi / Pasturatori'),
    (r'piomb|\bsinker|roccotop', 'Pesca / Piombi'),
    (r'girell|\bsnap|moschetton|swivel|anellin', 'Pesca / Minuteria / Girelle'),
    (r'slamator|\baghi\b|pinza|forbic|coltell', 'Pesca / Accessori / Pinze e forbici'),
    (r'artificial|\bmaggot|\besca\b|\blure', 'Pesca / Esche artificiali'),
    (r'\bborsa|\bbag\b|zaino|marsupio|custodia', 'Pesca / Borse e foderi'),
    (r'valigett', 'Pesca / Valigette e scatole'),
    (r'acquari|aquarium|backdrop', 'Altro / Acquariologia'),
]
# Accessori PER mulinelli/canne: valgono anche se il nome cita mulinelli o canne.
HOLDERS = [
    (r'fodero|portacann|porta ?canne|rod ?bag|rod ?holder|rutentasche|funda', 'Pesca / Borse e foderi / Portacanne'),
    (r'\bolio\b|grasso|\boil\b|grease|lubric', 'Pesca / Accessori / Accessori vari'),
    (r'\bcombos?\b', 'Pesca / Kit pesca'),   # canna + mulinello
]


# ----------------------------------------------------------------------
# Mulinelli (2026-11-26): albero di Nencini Sport, diviso soprattutto per meccanismo (frizione
# anteriore/posteriore, casting, rotanti da traina, elettrici, mosca) e in parte per uso (surfcasting,
# carpfishing). Letti dal loro sito il 26/11 (2.953 articoli): le serie Pure Fishing tornano con le
# sottocategorie del fornitore (Slammer, Spinfisher → frizione anteriore; Fathom, Squall, International
# → traina e drifting; Surfblaster → surfcasting; Hardy, Greys → mosca).
# ----------------------------------------------------------------------
M_FA, M_FP, M_CAST = f'{MUL} / Frizione anteriore', f'{MUL} / Frizione posteriore', f'{MUL} / Casting'
M_SURF, M_CARP, M_TRA = f'{MUL} / Surfcasting', f'{MUL} / Carpfishing', f'{MUL} / Traina e drifting'
M_JIG, M_ELE, M_BOB = f'{MUL} / Jigging', f'{MUL} / Elettrici', f'{MUL} / Bobine'
M_MOS, M_MAN, M_SAL = f'{MUL} / Mosca', f'{MUL} / Manuali', f'{MUL} / Salpapalamito'

# Categoria del fornitore (per tutti i prodotti): (testo, categoria, debole). Deboli quelle che il nome
# può precisare (una "bobina" Trabucco 003/SURF è una bobina; un Penn "Conventional" può essere elettrico).
# Trabucco: 003/FD frizione anteriore, 003/RD posteriore, 003/RUN free spool, 020/220 mosca LMF.
REEL_SUPPLIER = [
    (r'/\s*(spinning|spincast) reels\s*$|^\s*003/(FD|FDR|POWER)\s*$|^\s*mulinelli frizione anteriore\s*$', M_FA, True),
    (r'^\s*003/RD\s*$', M_FP, False),
    (r'/\s*(low profile|round) baitcast reels\s*$|^\s*003/CAST\s*$|^\s*mulinelli rotanti\s*$', M_CAST, True),
    (r'/\s*conventional reels\s*$|^\s*003/TROLL\s*$', M_TRA, True),
    (r'/\s*spare spools\s*$|^\s*bobine, manovelle e varie\s*$', M_BOB, True),
    (r'/\s*fly reels\s*$|^\s*020/220\s*$', M_MOS, False),
    (r'/\s*carp reels\s*$|^\s*003/RUN\s*$|^\s*019/KK-020\s*$', M_CARP, False),
    (r'^\s*003/SURF\s*$', M_SURF, True),
    (r'^\s*mulinelli elettrici\s*$', M_ELE, False),
]
# Tecnica (specifiche) → sottocategoria; (debole?). Feeder, bolognese, match e trota: frizione anteriore.
TECH_REEL = [('Spinning|Light game', M_FA, True), ('Surfcasting', M_SURF, False), ('Carpfishing', M_CARP, False),
             ('Feeder|Bolognese|Match|Trota', M_FA, True), ('Traina|Big game|Bolentino', M_TRA, False),
             ('Jigging', M_JIG, False), ('Mosca', M_MOS, False)]
# Parole nel nome (solo mulinelli): (priorità, testo, categoria, debole).
NAME_REEL = [
    # "Electric Blue" è un colore (Ambassadeur 6500 CS).
    (35, r'elettric|electric(?!\s*(blue|green|purple|pink|lime|chicken|orange|shad))|elektr', M_ELE, False),
    (35, r'salpapalamit', M_SAL, False),
    (35, r'affondator|downrigger', M_MAN, False),
    (40, r'surf', M_SURF, False),
    (40, r'carp|free ?spool|bait ?runner|karpfen|carpa|freilauf|big ?pit', M_CARP, False),
    (40, r'jigging|slow ?pitch|vertical', M_JIG, False),
    (40, r'traina|trolling|big ?game|offshore|drifting|curric|bolentino', M_TRA, False),
    (40, r'baitcast|bajo perfil|perfil bajo|low ?profile|bait ?caster|moltiplicat|multiplicador|multirolle',
     M_CAST, False),
    (40, r'fly ?reel|\bmosca\b|fliegen|mouche|\bfly\b', M_MOS, False),
    (40, r'rear ?drag|frizione posteriore|heckbremse', M_FP, False),
    (40, r'spinning|giratorio|spinnroll|frizione anteriore|front ?drag|light ?game|ultra ?light|bolognes'
         r'|\bmatch\b|inglese|trota|trout|forelle|trucha|\barea\b|feeder|ledger', M_FA, True),
    # Taglia da mulinello a frizione anteriore ("Size 4000", "4000D-XH", "3+1 BB"): debole.
    (45, r'\b(size|taglia):? ?\d{3,5}\b|\d ?\+ ?\d ?bb\b|(?<!\d)[1-9][05]00(?!\d)', M_FA, True),
]
# Categoria Amazon (solo mulinelli).
AMAZON_REEL = [(r'mulinelli da spinning', M_FA, True), (r'mulinelli da surfcasting', M_SURF, False),
               (r'mulinelli da (baitcasting|casting)', M_CAST, False), (r'mulinelli da (traina|drifting)', M_TRA, False),
               (r'mulinelli da pesca a mosca|mulinelli da mosca', M_MOS, False)]

# Cambi dell'albero (vecchia → nuova): si rinomina se la nuova non c'è, altrimenti si unisce (prodotti,
# regole, corrispondenze Amazon e ogni altro riferimento passano alla nuova).
TREE_CHANGES = [
    (f'{MUL} / Spinning', M_FA), (f'{MUL} / Ricambi e bobine', M_BOB),
    (f'{MUL} / Carpfishing e feeder', M_CARP), (f'{MUL} / Traina, jigging e big game', M_TRA),
    (f'{MUL} / Bolognese e match', M_FA), (f'{MUL} / Trota e area', M_FA),
] + ROD_TREE + LINE_TREE + [('Pesca / Minuteria / Ami', 'Pesca / Ami')] + LURE_TREE + BAIT_TREE + [('Pesca / Minuteria / Piombi', PB), ('Pesca / Minuteria / Galleggianti', GA)] + TACKLE_TREE + VA_TREE + GU_TREE + BF_TREE + KIT_TREE + PA_TREE
# Regole predefinite di prima: (tipo, testo, categoria sotto cui stanno) → si disattivano, tranne quelle
# uguali a una regola di adesso (stesso testo, categoria e "debole").
RETIRED = [(k, p, MUL) for k, p in [
    ('supplier', r'/\s*spinning reels\s*$'), ('supplier', r'/\s*low profile baitcast reels\s*$'),
    ('supplier', r'/\s*round baitcast reels\s*$'), ('supplier', r'/\s*conventional reels\s*$'),
    ('supplier', r'/\s*spare spools\s*$'),
    ('technique', 'Carpfishing|Feeder'), ('technique', 'Bolognese|Match'),
    ('technique', 'Traina|Big game|Jigging|Bolentino'), ('technique', 'Trota'),
    ('name', r'carp|feeder|free ?spool|bait ?runner|ledger|karpfen|carpa|freilauf'),
    ('name', r'traina|trolling|big ?game|offshore|drifting|jigging|slow ?pitch|curric|bolentino|vertical|elettric'),
    ('name', r'bolognes|\bmatch\b|inglese'), ('name', r'trota|trout|forelle|trucha|\barea\b'),
    ('name', r'spinning|rotante|giratorio|spinnroll|frizione (anteriore|posteriore)|front ?drag'
             r'|filatura (anteriore|posteriore)|light ?game|ultra ?light'),
]]


# ----------------------------------------------------------------------
# Ami (2026-11-24): sottocategorie di Nencini Sport (docs/nencini_ami_regole.md), lette dal loro sito
# il 24/11 (2.784 articoli). Priorità 1-9, nell'ordine delle loro "regole di decisione": le regole sul
# nome valgono solo per i prodotti già in Ami (o lo diventano con questa proposta); quelle sulla
# categoria del fornitore per tutti. Gli ami sciolti che il nome non distingue (Hisashi senza SW,
# Shinken S-/SBL/C5, Lineaeffe LF) restano in Ami.
# ----------------------------------------------------------------------
AMI = 'Pesca / Ami'   # categoria principale come da Nencini (prima Pesca / Minuteria / Ami)
A_INT, A_LEG, A_MAR = f'{AMI} / Acque interne', f'{AMI} / Ami legati', f'{AMI} / Mare'
A_DRI, A_ASS, A_ANC = f'{AMI} / Drifting, bolentino e traina', f'{AMI} / Assist hook', f'{AMI} / Ancorette'
A_SPI, A_JIG, A_SAB = f'{AMI} / Spinning', f'{AMI} / Jig head', f'{AMI} / Sabiki'
A_SIL, A_CRP, A_TRO = f'{AMI} / Siluro', f'{AMI} / Carpfishing', f'{AMI} / Trota'
SABIKI_WORDS = r'sabik|mitraill|hayabusa|luminous|piumett|feather'

# Categoria del fornitore → sottocategoria (per tutti i prodotti). Trabucco: "002/<serie>_L" = legati
# (nel nome c'è il diametro del filo), "_P" = bustine di ami sciolti, FDR_P = feeder e carp da colpo.
HOOK_SUPPLIER = [
    (1, r'^\s*002/\w+_L\s*$|^\s*021/AMI\s*$|^\s*ami legati\s*$', A_LEG),
    (2, r'^\s*018/SABIKI\s*$|^\s*sabiki\s*$', A_SAB),
    (3, r'^\s*teste piombate\s*$|/\s*jig ?heads\s*$', A_JIG),
    (4, r'^\s*assist hooks\s*$', A_ASS),
    (5, r'^\s*ancorette\s*$', A_ANC),
    (6, r'^\s*ami per artificiali\s*$', A_SPI),
    (7, r'^\s*019/KK-110\s*$', A_CRP),
    (8, r'^\s*ami drifting', A_DRI),
    (9, r'^\s*002/(FDR_P|DRN)\s*$|^\s*ami senza ardiglione\s*$', A_INT),
    (10, r'^\s*002/|^\s*ami\b', AMI),
]

# Serie Tubertini come le classifica Nencini ("Amo Serie 185", "Amo "881"", "Serie TL-9"); le serie che
# Nencini mette in due sottocategorie (10, 50, TL-6E) restano fuori.
_SERIE = r'\b(?:serie|amo|tuber)\s*"?\s*(?:{})(?![\w/-])'
TUB_INT = '2|7|27|29|30|51|175|185|206|212|297|800|804|806|807|808|881|900|tl-?(?:1|2|4e|4s|5|7|9|11|13|14)'
TUB_MAR = '54|229|271|288|610|620|843|2461|5588|5590|tl-?(?:15|16|17)'

# Parole nel nome → sottocategoria (solo prodotti in Ami): (priorità, testo, categoria, tranne se).
HOOK_NAME = [
    (1, r'legat|\btied\b|hook ?length|snell|\bcart(ina|ine)?\.|\d+ ?x ?0[.,]\d{2}|\brigs\b|bait ?band'
        r'|\bleaders?\b|\d+ ?cm\b', A_LEG, SABIKI_WORDS + r'|tokyo|texas|jig'),
    (2, SABIKI_WORDS, A_SAB, None),
    (3, r'jig ?head|test[ae] piombat|testin[ae]|\bned\b|darting', A_JIG, None),
    (4, r'assist', A_ASS, None),
    (5, r'treble|ancorett|\bdouble\b|\bdoppi[oa]?\b', A_ANC, None),
    (6, r'strong single|single hook|\bsingol[oi]\b|offset|minnow|\bworms?\b|weedless|stinger|swimbait|drop ?shot|trailer|\bewg\b|texas|wacky|tokyo',
     A_SPI, None),
    (7, r'silur|\bwels|catfish|madcat|beastly cat', A_SIL, None),
    (7, r'k-?karp|\bkk[ _]|boilie|\bchod\b|d-rig|anti[- ]?(snag|eject)|turn-?down|curved? shank|continental',
     A_CRP, None),
    (7, r'\btrot|trout|forelle', A_TRO, None),
    (7, _SERIE.format(TUB_INT), A_INT, r'\bmare\b|\bsw\b|surf'),
    (7, _SERIE.format(TUB_MAR) + r'|t-?surf', A_MAR, None),
    (7, _SERIE.format('36|56'), A_TRO, None),
    (7, r'baitholder', A_MAR, None),   # BKK Beak Baitholder: mare, non drifting
    # Trabucco: Pro Sword (Match, Micro, Carpa, Canal, Maggot), XPS e Lucifer (misure 12-24) da colpo.
    (7, r'pro ?sword|\bxps|lucifer', A_INT, None),
    (8, r'circle|mut[sz]?u\b|octopus|\bbeak\b|live ?bait|girell|\bgir\.|swivel|traina|trolling|kona|\btuna\b'
        r'|drifting|big ?game', A_DRI, None),
    (9, r'\bmatch\b|feeder|maggot|bigatt|polemaster|\bcolpo\b|breme|bream|\bmais\b|\bcorn\b|roach|canal|method',
     A_INT, None),
    (9, r'\bsw\b|surf|\bmare\b|\bsea\b|saltwater|aberdeen|bolentino|o.?shaughness|beach|orata|sarag|spigol',
     A_MAR, None),
]


def rules():
    """(priorità, tipo, testo, si applica a, categoria, debole, tranne se il nome contiene)"""
    out = []
    for seq, pattern, target in HOOK_SUPPLIER:
        out.append((seq, 'supplier', pattern, [], target, False, None))
    for seq, pattern, target, exclude in HOOK_NAME:
        out.append((seq, 'name', pattern, [AMI], target, False, exclude))
    for sub, target in SUPPLIER:
        out.append((10, 'supplier', rf'/\s*{sub}\s*$', [], target, False, None))
    for pattern, target in HOLDERS:
        out.append((15, 'name', pattern, [MUL, CAN], target, False, None))
    for pattern, target in WRONG:
        out.append((20, 'name', pattern, [MUL], target, False, REEL_WORDS))
        out.append((20, 'name', pattern, [CAN], target, False, ROD_WORDS))
    out.append((25, 'name', r'^\s*(bobina|spool|manovella|knob|pomello)\b', [MUL], M_BOB,
                False, r'mulinell|\breel|carrete|filatura'))
    for pattern, target, weak in REEL_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for pattern, target, weak in TECH_REEL:
        out.append((30, 'technique', pattern, [MUL], target, weak, None))
    for pattern, target, weak in ROD_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for pattern, target, weak in TECH_ROD:
        out.append((30, 'technique', pattern, [CAN], target, weak, None))
    for seq, pattern, target, weak in NAME_REEL:
        out.append((seq, 'name', pattern, [MUL], target, weak, None))
    for pattern, target, weak in AMAZON_REEL:
        out.append((35, 'amazon', pattern, [MUL], target, weak, None))
    for seq, pattern, target, weak in NAME_ROD:
        out.append((seq, 'name', pattern, [CAN], target, weak, None))
    for pattern, target, weak in LINE_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_LINE:
        out.append((seq, 'name', pattern, [FIL], target, weak, None))
    for pattern, target, weak in LURE_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_LURE:
        out.append((seq, 'name', pattern, [ESC], target, weak, None))
    for pattern, target, weak in BAIT_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_BAIT:
        out.append((seq, 'name', pattern, [EP], target, weak, None))
    for pattern, target in WRONG + BAIT_OUT:
        out.append((20, 'name', pattern, [EP], target, False, BAIT_WORDS))
    for pattern, target, weak in SINKER_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_SINKER:
        out.append((seq, 'name', pattern, [PB], target, weak, None))
    for pattern, target, weak in FLOAT_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_FLOAT:
        out.append((seq, 'name', pattern, [GA], target, weak, None))
    for pattern, target, weak in TACKLE_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_TACKLE:
        out.append((seq, 'name', pattern, [MN], target, weak, None))
    for pattern, target, weak in RG_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_RG:
        out.append((seq, 'name', pattern, RG_SCOPE, target, weak, None))
    for pattern, target, weak in PN_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_PN:
        out.append((seq, 'name', pattern, PN_SCOPE, target, weak, None))
    for pattern, target, weak in VA_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_VA:
        out.append((seq, 'name', pattern, VA_SCOPE, target, weak, None))
    for pattern, target, weak in GU_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_GU:
        out.append((seq, 'name', pattern, GU_SCOPE, target, weak, None))
    for pattern, target, weak in BF_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_BF:
        out.append((seq, 'name', pattern, [BF], target, weak, None))
    for pattern, target, weak in KIT_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_KIT:
        out.append((seq, 'name', pattern, [KIT], target, weak, None))
    for pattern, target, weak in AB_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_AB:
        out.append((seq, 'name', pattern, [AB], target, weak, None))
    for pattern, target, weak in SW_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_SW_ENTRY:
        out.append((seq, 'name', pattern, ['Abbigliamento e calzature'], target, weak, None))
    for seq, pattern, target, weak in NAME_SW:
        out.append((seq, 'name', pattern, [SW], target, weak, None))
    for pattern, target, weak in AC_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_AC:
        out.append((seq, 'name', pattern, [AC], target, weak, None))
    for pattern, target, weak in BB_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_BB:
        out.append((seq, 'name', pattern, BB_SCOPE, target, weak, None))
    for pattern, target, weak in PA_SUPPLIER:
        out.append((10, 'supplier', pattern, [], target, weak, None))
    for seq, pattern, target, weak in NAME_PA:
        out.append((seq, 'name', pattern, [PA], target, weak, None))
    return out


def _retired_rods():
    return [(k, p, CAN) for k, p in OLD_ROD]


RETIRED += _retired_rods() + [(k, p, FIL) for k, p in OLD_LINE] + [(k, p, MN) for k, p in OLD_TACKLE] + [(k, p, BF) for k, p in OLD_BAG]
