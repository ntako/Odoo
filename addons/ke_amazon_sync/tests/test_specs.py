"""Specifiche tecniche dal Catalog API (2026-11-17): unità uniformi, valori fissi, lingue."""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services import specs
from .common import no_commit

REEL = {
    'fishing_technique': [{'value': 'Spinnfischen', 'language_tag': 'it_IT'}],
    'gearbox_ratio': [{'value': 5.6}],
    'bearing': [{'number': [{'value': 6}], 'material': [{'value': 'Acciaio', 'language_tag': 'it_IT'}]}],
    'fishing_reel_maximum_drag': [{'value': 30.0, 'unit': 'pounds'}],
    'item_weight': [{'value': 284.0, 'unit': 'grams'}],
    'hand_orientation': [{'value': 'Adatto per utenti destri e mancini', 'language_tag': 'it_IT'}],
    'line_capacity': [{'value': '[EO] 10/200 - 12/200" (Estimated Value)', 'language_tag': 'it_IT'}],
    'color': [{'value': 'Nero', 'language_tag': 'it_IT'}],
    'material': [{'value': 'La canna della lenza è probabilmente realizzata in fibra', 'language_tag': 'it_IT'}],
}
LINE = {
    'fishing_line_type': [{'value': 'Intrecciato', 'language_tag': 'it_IT'}],
    'line_size': [{'value': 0.16, 'unit': 'millimeters'}],
    'breaking_strength': [{'value': 0.04, 'unit': 'pounds'}],
    'item_length': [{'value': 1.0, 'unit': 'meters'}],
    'strand': [{'type': [{'value': '8 fili', 'language_tag': 'it_IT'}]}],
}


class TestSpecRules(TransactionCase):

    def test_reel_units_vocab_and_cleanup(self):
        out = specs.extract('FISHING_REEL', REEL)
        self.assertEqual(specs.render(out['technique'], 'it_IT'), 'Spinning', '"Spinnfischen" riconosciuto')
        self.assertEqual(specs.render(out['technique'], 'de_DE'), 'Spinnfischen')
        self.assertEqual(specs.render(out['gear_ratio'], 'it_IT'), '5,6:1')
        self.assertEqual(specs.render(out['gear_ratio'], 'en_GB'), '5.6:1')
        self.assertEqual(specs.render(out['bearings'], 'it_IT'), '6')
        self.assertEqual(specs.render(out['max_drag'], 'it_IT'), '13,6 kg', 'libbre → kg')
        self.assertEqual(specs.render(out['hand'], 'fr_FR'), 'Ambidextre')
        self.assertEqual(specs.render_own(out['line_capacity']), '10/200 - 12/200', 'note tolte')
        self.assertNotIn('material', out, 'frase al posto del materiale: scartata')
        self.assertIsNone(specs.render(out['color'], 'es_ES'), 'testo libero solo nella lingua del marketplace')

    def test_line_out_of_range_and_length_from_name(self):
        out = specs.extract('FISHING_LINE', LINE, name='Filo trecciato 300 m 0,16 mm')
        self.assertEqual(specs.render(out['line_type'], 'es_ES'), 'Trenzado')
        self.assertEqual(specs.render(out['diameter'], 'it_IT'), '0,16 mm')
        self.assertNotIn('breaking_strength', out, '0,04 libbre su un trecciato: valore assurdo, scartato')
        self.assertEqual(specs.render(out['length'], 'it_IT'), '300 m', '"1.000"/1 m: vale il nome')
        self.assertEqual(specs.render(out['strands'], 'it_IT'), '8')


class _Client:
    def __init__(self, items):
        self.items = items

    def get_catalog_items_batch(self, asins):
        return {a: self.items[a] for a in asins if a in self.items}


@tagged('post_install', '-at_install')
class TestImportSpecs(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.se = sync_engine
        Lang = self.env['res.lang'].with_context(active_test=False)
        langs = Lang.search([('code', 'in', ('it_IT', 'es_ES', 'de_DE'))])
        langs.write({'active': True})
        it = langs.filtered(lambda l: l.code == 'it_IT')
        self.it = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT spec', 'code': 'ZZSI', 'marketplace_id': 'ZZ_SPEC_IT', 'lang_id': it.id, 'active': True})
        self.log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT spec', 'sync_type': 'import_specs'})

    def _listing(self, variant, asin, sku):
        self.env['ke.amazon.listing'].create({'product_id': variant.id, 'marketplace_id': self.it.id,
                                              'asin': asin, 'sku': sku})

    def test_simple_and_family_specs_in_every_language(self):
        reel = self.env['product.template'].create({'name': 'ZZT Mulinello spec'})
        self._listing(reel.product_variant_id, 'B0ZZSP0001', 'ZZT-SP-1')
        attr = self.env['product.attribute'].create({
            'name': 'ZZT Diametro spec', 'value_ids': [(0, 0, {'name': '0,16'}), (0, 0, {'name': '0,20'})]})
        line = self.env['product.template'].create({
            'name': 'ZZT Trecciato 150 m',
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})]})
        v16, v20 = line.product_variant_ids
        self._listing(v16, 'B0ZZSP0016', 'ZZT-SP-16')
        self._listing(v20, 'B0ZZSP0020', 'ZZT-SP-20')
        line20 = dict(LINE, line_size=[{'value': 0.20, 'unit': 'millimeters'}])
        items = {
            'B0ZZSP0001': {'attributes': REEL, 'productTypes': [{'productType': 'FISHING_REEL'}]},
            'B0ZZSP0016': {'attributes': LINE, 'productTypes': [{'productType': 'FISHING_LINE'}]},
            'B0ZZSP0020': {'attributes': line20, 'productTypes': [{'productType': 'FISHING_LINE'}]},
        }
        eng = self.se.SyncEngine(self.env, _Client(items))
        with no_commit(self.env):
            updated, empty, errors = eng.import_marketplace_specs(self.log, self.it, primary=True)
        self.assertEqual((updated, errors), (3, 0))

        def vals(tmpl, lang, variant=None):
            return {s.key_id.code: s.value for s in tmpl.with_context(lang=lang).ke_spec_ids
                    if s.product_id == (variant or self.env['product.product'])}
        self.assertEqual(vals(reel, 'it_IT')['gear_ratio'], '5,6:1')
        self.assertEqual(vals(reel, 'it_IT')['color'], 'Nero')
        self.assertEqual(vals(reel, 'de_DE')['technique'], 'Spinnfischen', 'valori fissi anche in tedesco')
        self.assertEqual(vals(line, 'it_IT', v16)['diameter'], '0,16 mm')
        self.assertEqual(vals(line, 'it_IT', v20)['diameter'], '0,2 mm')
        self.assertEqual(vals(line, 'es_ES', v20)['line_type'], 'Trenzado')

        # Valore scritto a mano: resta; un valore che Amazon non ha più: tolto (giro principale).
        spec = reel.ke_spec_ids.filtered(lambda s: s.key_id.code == 'color')
        spec.with_context(lang='it_IT').write({'value': 'Nero opaco', 'source': 'manual'})
        items['B0ZZSP0001'] = {'attributes': {k: v for k, v in REEL.items() if k != 'gearbox_ratio'},
                               'productTypes': [{'productType': 'FISHING_REEL'}]}
        with no_commit(self.env):
            eng.import_marketplace_specs(self.log, self.it, primary=True)
        reel.invalidate_recordset()
        self.assertEqual(vals(reel, 'it_IT')['color'], 'Nero opaco')
        self.assertNotIn('gear_ratio', vals(reel, 'it_IT'))


class TestPhysicalValues(TransactionCase):
    """Peso e dimensioni da Amazon solo se mancano (2026-11-17)."""

    def test_amazon_does_not_overwrite_weight_and_dimensions(self):
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        tmpl = self.env['product.template'].create({'name': 'ZZT peso', 'weight': 0.38})
        vals = SyncEngine._physical_vals(tmpl, 0.907, 20.0, None, 5.0)
        self.assertNotIn('weight', vals, 'peso già presente (es. dal fornitore): resta')
        if 'product_length' in tmpl._fields:
            self.assertEqual(vals, {'product_length': 20.0, 'product_height': 5.0})
