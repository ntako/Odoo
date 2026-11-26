from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCategoryRules(TransactionCase):
    """Classificazione in Odoo con regole e proposte (2026-11-17)."""

    def setUp(self):
        super().setUp()
        self.env['res.lang']._activate_lang('it_IT')
        Rule = self.env['ke.product.category.rule']
        self.Categ = self.env['product.category']
        self.reels = Rule._category_by_path('Pesca / Mulinelli', create=True)
        Rule._category_by_path('Pesca / Canne', create=True)
        Rule.search([]).unlink()
        Rule.action_load_default_rules()
        self.Proposal = self.env['ke.product.category.proposal']

    def _product(self, name, **vals):
        return self.env['product.template'].with_context(lang='it_IT').create(
            {'name': name, 'categ_id': self.reels.id, **vals})

    def _proposal(self, tmpl):
        self.Proposal.propose_for(tmpl)
        return self.Proposal.search([('product_tmpl_id', '=', tmpl.id), ('state', '=', 'pending')])

    def _path(self, categ):
        return categ.complete_name

    def test_default_rules_loaded_once(self):
        n = self.env['ke.product.category.rule'].search_count([])
        self.assertGreater(n, 50)
        self.env['ke.product.category.rule'].action_load_default_rules()
        self.assertEqual(self.env['ke.product.category.rule'].search_count([]), n, 'nessun doppione')

    def test_supplier_category_is_sure(self):
        p = self._proposal(self._product('Penn Slammer IV', ke_supplier_category='Reels / Spinning Reels'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Mulinelli / Frizione anteriore')
        self.assertTrue(p.certain)

    def test_line_in_reels_goes_to_lines_most_specific(self):
        p = self._proposal(self._product('Berkley filo trecciato 150 m 0,16 mm'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Fili / Trecciati')
        self.assertTrue(p.certain, '"Fili" e "Fili / Trecciati" non sono in disaccordo')
        reel = self._proposal(self._product('Mulinello con filo trecciato incluso'))
        self.assertFalse(reel.filtered(lambda r: 'Fili' in r.proposed_categ_id.complete_name),
                         'mulinello con il filo incluso: non è un filo')

    def test_weak_rule_gives_way_and_strong_conflict_is_flagged(self):
        surf = self._product('Mulinello Longcast Surf 6000')
        p = self._proposal(surf)
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Mulinelli / Surfcasting')
        self.assertTrue(p.certain)
        both = self._product('Mulinello Surf Carp 6000')
        p = self._proposal(both)
        self.assertFalse(p.certain, 'due regole forti in disaccordo: da verificare')
        self.assertIn('ma', p.reason)

    def test_apply_and_repropose(self):
        tmpl = self._product('Mulinello da spinning 3000')
        p = self._proposal(tmpl)
        p.action_apply()
        self.assertEqual(self._path(tmpl.categ_id), 'Pesca / Mulinelli / Frizione anteriore')
        self.assertEqual(p.state, 'applied')
        self.assertFalse(self._proposal(tmpl), 'già nella categoria proposta: nessuna proposta')

    def test_priority_and_generic_technique_lists(self):
        line = self._product('Filo fluorocarbon surf 0,30 mm')
        p = self._proposal(line)
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Fili / Fluorocarbon')
        self.assertTrue(p.certain, 'la regola "è un filo" ha priorità sulla tecnica da surf dei mulinelli')
        key = self.env['ke.product.spec.key']._ensure('technique', {'it_IT': 'Tecnica'})
        reel = self._product('Mulinello tuttofare 4000 surf')
        reel._ke_set_specs({key: 'Spinning, Surfcasting, Feeder, Bolognese, Trota/area'}, 'amazon', 'it_IT')
        p = self._proposal(reel)
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Mulinelli / Surfcasting')
        self.assertTrue(p.certain, 'cinque tecniche Amazon insieme non contano: decide il nome')

    # --- Ami (2026-11-24): sottocategorie come Nencini Sport -------------------------------------

    def _in(self, path, name, **vals):
        categ = self.env['ke.product.category.rule']._category_by_path(path, create=True)
        return self._product(name, categ_id=categ.id, **vals)

    def test_hooks_supplier_and_second_pass(self):
        p = self._proposal(self._in('Altro', 'Amo 2N C/Hercules N.22', ke_supplier_category='Ami Legati'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Ami / Ami legati')
        self.assertTrue(p.certain)
        # Trabucco appena importato: il fornitore dice solo "amo", il nome dice mare → subito "Ami / Mare".
        p = self._proposal(self._in('Altro', 'HISASHI HOOK 10500BN SW', ke_supplier_category='002/HIS_P'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Ami / Mare')
        p = self._proposal(self._in('Altro', 'HISASHI HOOK 10003', ke_supplier_category='002/HIS_P'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Ami', 'serie sconosciuta: resta in Ami')

    def test_hooks_never_moved_up(self):
        tmpl = self._in('Pesca / Ami / Mare', 'HISASHI HOOK 10003', ke_supplier_category='002/HIS_P')
        self.assertFalse(self._proposal(tmpl), 'già in una sottocategoria di Ami: nessuna proposta verso "Ami"')

    def test_hooks_name_rules_only_inside_hooks(self):
        lure = self._in('Pesca / Esche artificiali', 'Minnow 9 cm con ancorette treble')
        self.assertFalse(self._proposal(lure).filtered(lambda r: 'Ami' in r.proposed_categ_id.complete_name))

    def test_tubertini_series(self):
        cases = [('Amo Serie 185 Bn', 'Acque interne'), ('Amo Mare Serie 2/Gl', 'Mare'), ('Amo "229"', 'Mare'),
                 ('Amo Serie Tl-13', 'Acque interne'), ('Amo Serie TL-15', 'Mare'), ('Amo Serie 56 Gm St.', 'Trota'),
                 ('Amo Tuber "277" S/Mutu', 'Drifting, bolentino e traina'), ('Amo Minnow (Fm0961F)', 'Spinning'),
                 ('XPS_HOOKS', 'Acque interne'), ('Ancoretta St-36 Black', 'Ancorette')]
        for name, sub in cases:
            p = self._proposal(self._in('Pesca / Ami', name))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Ami / {sub}', name)
        self.assertFalse(self._proposal(self._in('Pesca / Ami', 'Amo Serie "10" N.20-1/0')),
                         'serie in due sottocategorie da Nencini: resta in Ami')

    # --- Mulinelli (2026-11-26): albero Nencini Sport e migrazione ---------------------------------

    def test_reels_nencini_tree(self):
        cases = [('Penn Slammer IV 6500', 'Reels / Spinning Reels', 'Frizione anteriore'),
                 ('Penn Surfblaster III 7000', 'Reels / Spinning Reels', 'Surfcasting'),
                 ('Penn Fathom II Electric', 'Reels / Conventional Reels', 'Elettrici'),
                 ('Penn Squall II Lever Drag', 'Reels / Conventional Reels', 'Traina e drifting'),
                 ('Hardy Marquis LWT', 'Reels / Fly Reels', 'Mosca'),
                 ('Abu Garcia Revo SX', 'Reels / Low Profile Baitcast Reels', 'Casting'),
                 ('BOBINA RICAMBIO XENOS XRS SURF', '003/SURF', 'Bobine'),
                 ('MUL. MANTIS RTX', '003/RD', 'Frizione posteriore'),
                 ('MUL. MAXXIS BR', '003/RUN', 'Carpfishing')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Altro', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Mulinelli / {sub}', name)

    def test_tree_changes_rename_and_merge(self):
        Rule = self.env['ke.product.category.rule']
        old_bolo = Rule._category_by_path('Pesca / Mulinelli / Bolognese e match', create=True)
        tmpl = self._product('Mulinello bolognese 3000', categ_id=old_bolo.id)
        old_rule = Rule.create({'kind': 'name', 'pattern': r'trota|trout|forelle|trucha|\barea\b',
                                'target_categ_id': old_bolo.id})
        Rule.action_load_default_rules()
        self.assertFalse(old_bolo.exists(), 'unita a "Frizione anteriore"')
        self.assertEqual(self._path(tmpl.categ_id), 'Pesca / Mulinelli / Frizione anteriore')
        self.assertFalse(old_rule.active, 'regola di prima disattivata')
        self.assertFalse(Rule._category_by_path('Pesca / Mulinelli / Spinning'), 'rinominata')

    # --- Canne (2026-11-26) ------------------------------------------------------------------------

    def test_rods_nencini_tree(self):
        cases = [('ASTORE MASTER BLS', '012/BOLO', 'Bolognesi'),
                 ('INSPIRON FD MASTER', '015/FEEDER', 'Feeder e ledgering'),
                 ('ENERGHIA XR DYNAMIC MATCH', '015/MATCH', 'Inglesi e match'),
                 ('ANDROMEDA SURF MASTER', '016/SURF', 'Surfcasting e beach ledgering'),
                 ('ANTILIA LIGHT DRIFTING', '016/BOAT', 'Traina e drifting'),
                 ('ACHAB PRO BOAT', '016/BOAT', 'Bolentino'),
                 ('Canna Tubertini Seika Vertical', 'Canne Vertical', 'Jigging e slow pitch'),
                 ('Penn Squadron Eging 862', 'Rods / Spinning Rods', 'Eging'),
                 ('Abu Garcia Beast Pro Spinning', 'Rods / Spinning Rods', 'Spinning'),
                 ('Greys Wing Fly Rod #5', 'Rods / Fly Rods', 'Mosca'),
                 ('CIMINO T-FORCE XSR', '013/ROUB', 'Vette')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Altro', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Canne / {sub}', name)

    def test_rods_old_rules_retired_or_kept(self):
        Rule = self.env['ke.product.category.rule']
        carp = Rule._category_by_path('Pesca / Canne / Carpfishing', create=True)
        old = Rule.create({'kind': 'supplier', 'pattern': r'/\s*coarse rods\s*$', 'target_categ_id': carp.id})
        Rule.action_load_default_rules()
        self.assertFalse(old.active, '"Coarse Rods" ora va in Feeder e ledgering: la vecchia si disattiva')
        coarse = Rule.search([('pattern', '=', r'/\s*coarse rods\s*$')])
        self.assertEqual(coarse.target_categ_id.name, 'Feeder e ledgering')
        self.assertEqual(Rule.search_count([('kind', '=', 'name'), ('pattern', '=', r'surf|beach'),
                                            ('target_categ_id.complete_name', '=like', 'Pesca / Canne%')]), 1,
                         'regola uguale di prima: resta, nessun doppione')

    # --- Fili (2026-11-26) -------------------------------------------------------------------------

    def test_lines_nencini_tree(self):
        cases = [('Berkley X5 Braid 150 m', 'Line / Superline', 'Trecciati'),
                 ('T-FORCE FLUOROC. SW LEADER', '005/MONO/010', 'Fluorocarbon'),
                 ('T-FORCE TROLLING mt.1000/4b', '005/MONO/080', 'Big game'),
                 ('T-FORCE MATCH-PRO mt. 100/10b', '005/MONO/020', 'Nylon'),
                 ('Berkley Trilene Fluorocoated', 'Line / Monofilament', 'Fluorocoated'),
                 ('Savage Gear 7x7 Wire Leader', 'Terminal Tackle / Leaders', 'Cavetti in acciaio'),
                 ('BOS LEADER CLEAR 60 M', 'Shock Leaders', 'Shock leader'),
                 ('Greys Platinum WF6 Fly Line', 'Line / Fly Line', 'Code')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Altro', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Fili / {sub}', name)

    def test_hooks_moved_to_top_level(self):
        Rule = self.env['ke.product.category.rule']
        ami = Rule._category_by_path('Pesca / Ami')
        ami.parent_id = Rule._category_by_path('Pesca / Minuteria', create=True)   # come prima del 26/11
        ami.name = 'Ami'
        tmpl = self._product('Amo Serie 185 Bn', categ_id=Rule._category_by_path('Pesca / Minuteria / Ami / Mare').id)
        Rule.action_load_default_rules()
        self.assertEqual(ami.complete_name, 'Pesca / Ami', 'spostata, stesso record')
        self.assertEqual(self._path(tmpl.categ_id), 'Pesca / Ami / Mare', 'le sottocategorie seguono')
        self.assertFalse(Rule._category_by_path('Pesca / Minuteria / Ami'))

    # --- Esche artificiali (2026-11-26) ------------------------------------------------------------

    def test_lures_nencini_tree(self):
        cases = [('DAM Standard Spinner', 'Bait / Hard Bait', 'Wire baits'),
                 ('Abu Garcia Droppen', 'Bait / Hard Bait', 'Wire baits'),
                 ('Abu Garcia Toby', 'Bait / Hard Bait', 'Wire baits'),
                 ('Savage Gear Seeker ISP', 'Bait / Hard Bait', 'Jigs'),
                 ('Savage Gear Swimsquid Inchiku', 'Bait / Hard Bait', 'Jigs'),
                 ('Savage Gear Squid Dealer', 'Bait / Hard Bait', 'Egi'),
                 ('Savage Gear 3D Goby Shad', 'Bait / Soft Bait', 'Soft baits'),
                 ('Savage Gear 3D Goby Crank', 'Bait / Hard Bait', 'Hard baits'),
                 ('Berkley Pulse Slurp', 'Bait / Hard Bait', 'Soft baits'),
                 ('Abu Garcia Skillinge', 'Bait / Hard Bait', 'Hard baits')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Altro', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Esche artificiali / {sub}', name)

    # --- Esche e pasture (2026-11-26) --------------------------------------------------------------

    def test_baits_nencini_tree(self):
        cases = [('Berkley PowerBait Trout Bait', 'Bait / Dough', 'Inneschi pronti'),
                 ('Gulp! Alive! Attractant', 'Bait / Attractant', 'Pasture liquide'),
                 ('Dynamite Carptec Boilies Salmon 15 mm', '', 'Boiles'),
                 ('Colmic gomma arabica 600 gr', '', 'Colla'),
                 ('Trabucco XP 3000 pastura cavedani', '', 'Sfarinati')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Esche e pasture', name, ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Esche e pasture / {sub}', name)
        reel = self._proposal(self._in('Pesca / Esche e pasture', 'DAM Quick 1 Pink FD', ke_supplier_category='Reels / Spinning Reels'))
        self.assertEqual(self._path(reel.proposed_categ_id), 'Pesca / Mulinelli / Frizione anteriore')

    # --- Piombi (2026-11-26) -----------------------------------------------------------------------

    def test_sinkers_moved_and_classified(self):
        cases = [('Trabucco Airtek Inline Cage Feeder', 'Pasturatori'), ('Colmic Sonda Neoprene 10 gr', 'Sonde'),
                 ('Savage Gear Bullet Cork Screw Head', 'Screw-in'), ('Fonderia Roma Temolino 15 gr', 'Temolino'),
                 ('K-K XTR Lead Flat Pear', 'Carpfishing')]
        for name, sub in cases:
            p = self._proposal(self._in('Pesca / Piombi', name))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Piombi / {sub}', name)
        out = self._proposal(self._in('Pesca / Esche e pasture', 'Stonfo pasturatore regolabile'))
        self.assertEqual(self._path(out.proposed_categ_id), 'Pesca / Piombi / Pasturatori')

    # --- Galleggianti (2026-11-26) -----------------------------------------------------------------

    def test_floats_nencini_tree(self):
        cases = [('Gall. HT PRO FORCE SLIDER', '001/SLID45', 'Scorrevoli'), ('Gall. AGUGLIA 45', '001/PIOM45', 'Mare'),
                 ('Gall. Ingl. CLASSIC MATCH', '001/WAG', 'Inglese'), ('B.A.R Bombarda Galleggiante', 'Bombarde', 'Bombarde'),
                 ('Colmic Aguglia Starlite piombato 15 gr', '', 'Mare')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Galleggianti', name, ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Galleggianti / {sub}', name)
        out = self._proposal(self._in('Pesca / Esche e pasture', 'Sbirulino Kit Bombarda Jolly'))
        self.assertEqual(self._path(out.proposed_categ_id), 'Pesca / Galleggianti / Bombarde')

    # --- Minuteria (2026-11-26) --------------------------------------------------------------------

    def test_tackle_nencini_tree(self):
        cases = [('BKK Split Ring 41', 'Split ring'), ('Savage Gear Wire Crimps 1.2mm', 'Crimps'),
                 ('Colmic Barrel Swivels Black', 'Girelle'), ('BKK Duolock Snap-52', 'Moschettoni'),
                 ('Berkley Fast Snaps', 'Attacchi'), ('Stonfo art.720 Anti Tangle Micro', 'Anti-tangle'),
                 ('Stonfo perline tonde morbide', 'Perline'), ('Tubertini Trave T-110', 'Terminali')]
        for name, sub in cases:
            p = self._proposal(self._in('Pesca / Minuteria', name))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Minuteria / {sub}', name)

    # --- Reggicanne e supporti (2026-11-26) --------------------------------------------------------

    def test_rod_rests_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        Rule._category_by_path('Pesca / Accessori / Carpfishing e postazione', create=True)
        cases = [('Element Tri-Sky Rod Pod', 'Bankware and Carp Ca / Rod Pods', 'Rod pod'),
                 ('Suprema Tripod B', 'Bankware and Carp Ca / Tripods', 'Tripodi e treppiedi'),
                 ('Snatch Rod Rest', 'Bankware and Carp Ca / Bankware Accessories', 'Rod pod'),
                 ('Colmic Double EVA Rod Rest 38 cm', '', 'Reggicanne da sedia'),
                 ('Ignesti picchetto telescopico 110', '', 'Picchetti e puntali')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Accessori / Carpfishing e postazione', name,
                                        ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Reggicanne e supporti / {sub}', name)

    # --- Panieri (2026-11-26) ----------------------------------------------------------------------

    def test_seat_boxes_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        Rule._category_by_path('Pesca / Borse e foderi', create=True)
        cases = [('Shakespeare Seat Box Sherpa', 'Fishing Storage / Tackle Seat Boxes', 'Cassoni surfcasting'),
                 ('Seatbox Side Tray', 'Fishing Storage / Tackle Seat Boxes', 'Piatti laterali'),
                 ('Superteam Seatbox System', 'Fishing Storage / Tackle Seat Boxes', 'Panchetti'),
                 ('GENIUS', '011/SEAT-3', 'Accessori'),
                 ('Trabucco GNT-X36 Wheel Kit System', '', 'Carrelli e kit trasporto')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Borse e foderi', name, ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Panieri / {sub}', name)
        self.assertFalse(self._proposal(self._in('Pesca / Borse e foderi', 'Cruzade Rig Station')).filtered(
            lambda p: 'Panieri' in p.proposed_categ_id.complete_name))

    # --- Valigette e scatole (2026-11-26) ----------------------------------------------------------

    def test_tackle_boxes_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        old = Rule._category_by_path('Pesca / Contenitori e borse / Cassette e scatole', create=True)
        box = self._product('Scatola porta minuteria', categ_id=old.id)
        Rule.action_load_default_rules()
        self.assertEqual(self._path(box.categ_id), 'Pesca / Valigette e scatole', 'unita alla nuova')
        self.assertFalse(old.exists())
        cases = [('Three-Drawer Tackle Box', 'Fishing Storage / Tray Tackle Boxes', 'Valigette'),
                 ('Lurebox 4B Smoke', 'Fishing Storage / Tackle Organizers', 'Porta artificiali'),
                 ('EDGE Utility Box 3700 Standard', 'Fishing Storage / Utility Boxes', 'Porta minuteria e varie'),
                 ('Slim Waterproof Fly Box', 'Fishing Storage / Fly Boxes', 'Porta mosche')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Borse e foderi', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Valigette e scatole / {sub}', name)

    # --- Guadini, nasse e bilance (2026-11-26) -----------------------------------------------------

    def test_nets_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        Rule._category_by_path('Pesca / Accessori', create=True)
        cases = [('Savage Gear Full Frame Landing Net', 'Nets / Landing Nets', 'Guadini'),
                 ('Superteam Carp Keepnet 3.0m', 'Nets / Keepnets', 'Nasse'),
                 ('Trabucco GNT Net Head Speed Rubba', '008/NET', 'Teste di guadino'),
                 ('Trabucco Hydrus NXT Landing Net mt. 5,00', '008/HAN/TLS', 'Pali per guadino'),
                 ('Bulox testa guadino dacron', '', 'Teste di guadino')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Accessori', name, ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Guadini, nasse e bilance / {sub}', name)

    # --- Borse e foderi (2026-11-26) ---------------------------------------------------------------

    def test_bags_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        old = Rule._category_by_path('Pesca / Contenitori e borse / Foderi e portacanne', create=True)
        rod_bag = self._product('Fodero 2 canne', categ_id=old.id)
        Rule.action_load_default_rules()
        self.assertEqual(self._path(rod_bag.categ_id), 'Pesca / Borse e foderi / Portacanne')
        self.assertFalse(Rule._category_by_path('Pesca / Contenitori e borse'))
        cases = [('Avenger Padded Holdall 10ft', 'Fishing Storage / Holdalls', 'Portacanne'),
                 ('Defender II Accessory Bag', 'Fishing Storage / Specialty Bags and Boxes', 'Borse carpfishing'),
                 ('Penn Cool Bag M', '', 'Borse frigo'),
                 ('Savage Gear Lure Bag', 'Fishing Storage / Tackle Bags', 'Borse porta artificiali'),
                 ('Colmic Pantera L portanasse', '', 'Portanasse')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Borse e foderi', name, ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Borse e foderi / {sub}', name)

    # --- Kit pesca (2026-11-26) --------------------------------------------------------------------

    def test_kits_nencini_tree(self):
        cases = [('Cardinal Classic Allround Combo', 'Combos / Spinning Combos', 'Spinning'),
                 ('Charger Carp 10FD Combo', 'Combos / Spinning Combos', 'Carpfishing'),
                 ('Revo X Casting Combo', 'Combos / Baitcast Combos', 'Casting'),
                 ('Warfare Boat Combo', 'Combos / Conventional Combos', 'Traina'),
                 ('Firebird Feeder Combo', 'Combos / Coarse Combos', 'Feeder e ledgering'),
                 ('Cedar Canyon Elite Fly Combo', 'Combos / Fly Combos', 'Mosca')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Kit pesca', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Kit pesca / {sub}', name)
        out = self._proposal(self._product('Max X Spinning Combo with Line'))
        self.assertEqual(self._path(out.proposed_categ_id), 'Pesca / Kit pesca / Spinning', 'da Mulinelli')

    # --- Abbigliamento (2026-11-26) ----------------------------------------------------------------

    def test_apparel_fishing_only(self):
        cases = [('Savage Gear SG4 Hoodie', 'Apparel / Shirts', 'Felpe'),
                 ('Savage Gear Fleece Jacket', 'Apparel / Outerwear', 'Pile e softshell'),
                 ('Penn Cap', 'Apparel / Headwear', 'Cappelli'),
                 ('Abu Garcia Beast Polarized Sunglasses', 'Eyewear / Sunglasses', 'Occhiali'),
                 ('Berkley Gloves', 'Apparel / Gloves', 'Guanti')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Abbigliamento e calzature / Abbigliamento', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Abbigliamento / {sub}', name)
        generic = self._in('Abbigliamento e calzature / Abbigliamento', 'Felpa con cappuccio uomo')
        self.assertFalse(self._proposal(generic).filtered(lambda p: 'Pesca' in p.proposed_categ_id.complete_name),
                         'abbigliamento generico: resta dov\'è')

    # --- Stivali e waders (2026-11-26) -------------------------------------------------------------

    def test_waders_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        Rule._category_by_path('Abbigliamento e calzature / Calzature e waders', create=True)
        Rule.action_load_default_rules()   # la regola "wader/scafandro" vale per Abbigliamento e calzature
        cases = [('Helmsdale Neo Chest Stockingfoot Wader', 'Wading Gear / Waders', 'Waders neoprene'),
                 ('Dryzone Breathable Chest Wader', 'Wading Gear / Waders', 'Waders traspiranti'),
                 ('Nylon Waders', 'Wading Gear / Waders', 'Waders PVC'),
                 ('SG8 Felt Wading Boot', 'Wading Gear / Wading Boots', 'Scarpe da wading'),
                 ('Flex Neoprene Rubber Boots', 'Footwear / Boots', 'Stivali')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Abbigliamento e calzature / Calzature e waders', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Stivali e waders / {sub}', name)
        p = self._proposal(self._in('Abbigliamento e calzature / Calzature e waders', 'Stivale scafandro in doppio PVC'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Stivali e waders / Waders PVC')
        flip = self._in('Abbigliamento e calzature / Calzature e waders', 'SEAC Maui infradito in gomma')
        self.assertFalse(self._proposal(flip).filtered(lambda p: 'Pesca' in p.proposed_categ_id.complete_name))

    # --- Attrezzatura da campo (2026-11-26) --------------------------------------------------------

    def test_camp_nencini_tree(self):
        cases = [('Avenger 8 Leg Bedchair', 'Furniture / Bed Chairs', 'Lettini'),
                 ('Defender II Sleeping Bag', 'Furniture / Sleeping Bags', 'Sacchi a pelo'),
                 ('C-Series 2Man Bivvy', 'Tents and Shelters / Bivvies', 'Tende'),
                 ('Inspire Pro-Tect Unhooking Mat', 'Bankware and Carp Ca / Unhooking Mats', 'Cura del pescato'),
                 ('Eco Fishing Chair', 'Furniture / Chairs', 'Sedie')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Campeggio e outdoor / Sedie e arredo', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Attrezzatura da campo / {sub}', name)

    # --- Belly boat (2026-11-26) -------------------------------------------------------------------

    def test_belly_boat(self):
        Rule = self.env['ke.product.category.rule']
        Rule._category_by_path('Nautica / Barche e motori', create=True)
        Rule.action_load_default_rules()
        for name, supplier in [('Carbon Optix 190 Belly Boat', 'Boats and Motors / Boats'),
                               ('Fish Finder Holder Small', 'Boats and Motors / Boat Accessories'),
                               ('Quick Draw Rod Holder', 'Tools and Equipment / Rod Holders')]:
            p = self._proposal(self._in('Nautica / Barche e motori', name, ke_supplier_category=supplier))
            self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Belly boat', name)
        p = self._proposal(self._in('Nautica / Barche e motori', 'Rapala float tube FT 150'))
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Belly boat')

    # --- Accessori (2026-11-26) --------------------------------------------------------------------

    def test_accessories_nencini_tree(self):
        Rule = self.env['ke.product.category.rule']
        old = Rule._category_by_path('Pesca / Accessori / Segnalatori di abboccata', create=True)
        alarm = self._product('Prologic C-Series Alarm', categ_id=old.id)
        Rule.action_load_default_rules()
        self.assertEqual(self._path(alarm.categ_id), 'Pesca / Accessori / Avvisatori e segnalatori')
        cases = [('Rapala boga grip 15 cm', '', 'Boga grip'), ('Digital Fish Scale 10kg', '', 'Bilance'),
                 ('Stonfo slamatore little', '', 'Slamatori'), ('Telescopic Gaff', 'Tools and Equipment / Gaffs',
                                                                'Accessori traina e barca'),
                 ('Line Stripper Max', 'Tools and Equipment / Line Tools', 'Accessori vari'),
                 ('Qualcosa senza parole chiave', '', 'Accessori vari')]
        for name, supplier, sub in cases:
            p = self._proposal(self._in('Pesca / Accessori', name, ke_supplier_category=supplier or False))
            self.assertEqual(self._path(p.proposed_categ_id), f'Pesca / Accessori / {sub}', name)
