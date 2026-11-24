"""Proposte di famiglia dal catalogo del fornitore (2026-11-20)."""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFamilyProposal(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Proposal = self.env['ke.product.family.proposal']
        # Le varianti di altri prodotti del database (dati demo o reali) non devono contare.
        self.env['product.product'].search([('ke_supplier_family_ref', '!=', False)]).write(
            {'ke_supplier_family_ref': False})
        attr = self.env['product.attribute'].create({
            'name': 'ZZF Modello', 'value_ids': [(0, 0, {'name': n}) for n in ('A', 'B', 'C', 'D')]})
        self.mixed = self.env['product.template'].create({
            'name': 'ZZF Berkley PowerBait',
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})]})

    def _simple(self, name, ref):
        tmpl = self.env['product.template'].create({'name': name})
        tmpl.product_variant_id.write({'ke_supplier_family_ref': ref, 'ke_supplier_family_name': f'Fam {ref}'})
        return tmpl

    def _by_ref(self):
        return {p.family_ref: p for p in self.Proposal.search([('state', '=', 'pending')])}

    def test_merge_split_and_untouched(self):
        a, b, c, d = self.mixed.product_variant_ids
        # Prodotto misto: 3 varianti della famiglia F1, 1 di F2.
        (a | b | c).write({'ke_supplier_family_ref': 'F1', 'ke_supplier_family_name': 'PowerBait Fry'})
        d.write({'ke_supplier_family_ref': 'F2', 'ke_supplier_family_name': 'Power Hawg'})
        # F3: sparsa su due prodotti semplici. F4: già a posto (un prodotto, da sola).
        w1, w2 = self._simple('ZZF Whiplash 0,10', 'F3'), self._simple('ZZF Whiplash 0,12', 'F3')
        self._simple('ZZF Beast', 'F4')

        self.Proposal.propose()
        props = self._by_ref()
        self.assertNotIn('F4', props, 'famiglia già a posto: nessuna proposta')
        self.assertEqual(props['F3'].kind, 'merge')
        self.assertEqual(props['F3'].tmpl_ids, w1 | w2)
        self.assertEqual(props['F3'].target_tmpl_id, w1, 'a parità di varianti il prodotto più vecchio')
        # Nel prodotto misto F1 è la famiglia principale: lo tiene; F2 ne avrà uno nuovo.
        self.assertEqual(props['F1'].kind, 'split')
        self.assertEqual(props['F1'].target_tmpl_id, self.mixed)
        self.assertEqual(props['F2'].kind, 'split')
        self.assertFalse(props['F2'].target_tmpl_id)
        self.assertFalse(props['F2'].certain)
        self.assertEqual(props['F2'].variant_ids, d)

    def test_discarded_stays_discarded_until_something_changes(self):
        self._simple('ZZF Sick 1', 'F5')
        w2 = self._simple('ZZF Sick 2', 'F5')
        self.Proposal.propose()
        self._by_ref()['F5'].action_discard()
        self.Proposal.propose()
        self.assertNotIn('F5', self._by_ref(), 'scartata e niente è cambiato')
        self._simple('ZZF Sick 3', 'F5')
        self.Proposal.propose()
        self.assertEqual(self._by_ref()['F5'].tmpl_count, 3, 'nuova variante: la proposta torna')
        self.assertIn(w2, self._by_ref()['F5'].tmpl_ids)

    def test_loose_variants_are_reported(self):
        a, b, c, d = self.mixed.product_variant_ids
        (a | b).write({'ke_supplier_family_ref': 'F6'})
        self._simple('ZZF Fuori', 'F6')
        self.Proposal.propose()
        warnings = self._by_ref()['F6'].warnings or ''
        self.assertIn('non sono nel file del fornitore', warnings)
        self.assertIn('2 in "ZZF Berkley PowerBait"', warnings)
