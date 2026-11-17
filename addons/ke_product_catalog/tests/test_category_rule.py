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
        self.assertEqual(self._path(p.proposed_categ_id), 'Pesca / Mulinelli / Spinning')
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
        self.assertEqual(self._path(tmpl.categ_id), 'Pesca / Mulinelli / Spinning')
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
