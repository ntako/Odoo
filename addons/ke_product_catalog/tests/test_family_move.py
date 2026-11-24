"""Applicazione delle proposte di famiglia: spostamento delle varianti (2026-11-20)."""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFamilyMove(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Tmpl = self.env['product.template']
        self.Proposal = self.env['ke.product.family.proposal']
        self.env['product.product'].search([('ke_supplier_family_ref', '!=', False)]).write(
            {'ke_supplier_family_ref': False})
        self.stock = self.env.ref('stock.stock_location_stock')
        self.key = self.env['ke.product.spec.key'].create({'code': 'zzf_diameter', 'name': 'ZZF Diametro'})

    def _simple(self, name, code, price, ref='F1', qty=0, diameter=None):
        tmpl = self.Tmpl.create({'name': name, 'default_code': code, 'list_price': price, 'is_storable': True})
        tmpl.product_variant_id.write({'ke_supplier_family_ref': ref, 'ke_supplier_family_name': 'Whiplash 8'})
        if qty:
            self.env['stock.quant']._update_available_quantity(tmpl.product_variant_id, self.stock, qty)
        if diameter:
            self.env['ke.product.spec'].create({'product_tmpl_id': tmpl.id, 'key_id': self.key.id,
                                                'value': diameter, 'source': 'supplier'})
        return tmpl

    def _spec(self, variant):
        return self.env['ke.product.spec'].search([('product_id', '=', variant.id), ('key_id', '=', self.key.id)])

    def test_merge_simple_products(self):
        t1 = self._simple('ZZF Whiplash 0,10', 'ZZF-10', 20.0, qty=3, diameter='0,10 mm')
        t2 = self._simple('ZZF Whiplash 0,12', 'ZZF-12', 22.0, qty=5, diameter='0,12 mm')
        v1, v2 = t1.product_variant_id, t2.product_variant_id
        self.Proposal.propose()
        prop = self.Proposal.search([('family_ref', '=', 'F1'), ('state', '=', 'pending')])
        self.assertEqual(prop.target_tmpl_id, t1)
        prop.action_apply()
        self.assertEqual(prop.state, 'applied')

        self.assertEqual(t1.product_variant_ids, v1 | v2, 'stesse varianti, spostate')
        self.assertEqual((v2.default_code, v2.qty_available), ('ZZF-12', 5), 'codice e giacenza seguono')
        self.assertFalse(t2.active, 'prodotto rimasto vuoto: archiviato')
        self.assertTrue(t2.exists())
        self.assertEqual(v2.lst_price, 22.0, 'il suo prezzo diventa il prezzo della variante')
        self.assertEqual(v1.lst_price, 20.0)
        self.assertEqual(self._spec(v1).value, '0,10 mm', 'le specifiche del semplice restano della sua variante')
        self.assertEqual(self._spec(v2).value, '0,12 mm')
        self.assertFalse(self.env['ke.product.spec'].search([('product_tmpl_id', '=', t1.id),
                                                             ('product_id', '=', False)]))
        self.assertEqual(t1.ke_supplier_name, 'Whiplash 8')
        # Nomi delle varianti dalle specifiche che le distinguono (applicati insieme alla proposta).
        self.assertEqual(sorted(v.with_context(lang='en_US').product_template_attribute_value_ids.name
                                for v in (v1 | v2)), ['0,10 mm', '0,12 mm'])
        # Odoo ricalcola le varianti (es. una riga attributo toccata dopo): nessuna variante nuova.
        t1._create_variant_ids()
        self.assertEqual(t1.with_context(active_test=False).product_variant_ids, v1 | v2)

    def test_split_mixed_product_into_new_family(self):
        attr = self.env['product.attribute'].create({
            'name': 'ZZF Modello', 'value_ids': [(0, 0, {'name': n}) for n in ('Fry 2"', 'Fry 3"', 'Hawg')]})
        mixed = self.Tmpl.create({
            'name': 'ZZF Berkley PowerBait', 'list_price': 5.0, 'is_storable': True,
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})]})
        a, b, hawg = mixed.product_variant_ids
        (a | b).write({'ke_supplier_family_ref': 'FRY', 'ke_supplier_family_name': 'PowerBait Fry'})
        hawg.write({'ke_supplier_family_ref': 'HAWG', 'ke_supplier_family_name': 'PowerBait Power Hawg',
                    'default_code': 'ZZF-HAWG'})
        self.env['stock.quant']._update_available_quantity(hawg, self.stock, 7)
        self.Proposal.propose()
        props = {p.family_ref: p for p in self.Proposal.search([('state', '=', 'pending')])}
        self.assertFalse(props['HAWG'].target_tmpl_id)
        props['HAWG'].action_apply()

        new = hawg.product_tmpl_id
        self.assertNotEqual(new, mixed)
        self.assertEqual(new.product_variant_ids, hawg)
        self.assertEqual(new.name, 'PowerBait Power Hawg')
        self.assertEqual(hawg.qty_available, 7)
        self.assertEqual(mixed.product_variant_ids, a | b)
        self.assertEqual(mixed.attribute_line_ids.value_ids.mapped('name'), ['Fry 2"', 'Fry 3"'],
                         'il valore della variante uscita non resta sul vecchio prodotto')
        mixed._create_variant_ids()
        new._create_variant_ids()
        self.assertEqual(mixed.with_context(active_test=False).product_variant_ids, a | b, 'nessuna variante fantasma')
        self.assertEqual(new.with_context(active_test=False).product_variant_ids, hawg)

    def test_stale_proposal_is_not_applied(self):
        t1 = self._simple('ZZF Sick 1', 'ZZF-S1', 10.0, ref='F2')
        self._simple('ZZF Sick 2', 'ZZF-S2', 10.0, ref='F2')
        self.Proposal.propose()
        prop = self.Proposal.search([('family_ref', '=', 'F2'), ('state', '=', 'pending')])
        self._simple('ZZF Sick 3', 'ZZF-S3', 10.0, ref='F2')
        result = prop.action_apply()
        self.assertEqual(prop.state, 'pending')
        self.assertIn('Calcola proposte', result['params']['message'])
        self.assertEqual(len(t1.product_variant_ids), 1)
