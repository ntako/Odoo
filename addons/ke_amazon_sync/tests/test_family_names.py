"""
Nome delle famiglie dalla parte comune ai titoli Amazon delle varianti, in
ogni lingua (2026-11-04): prima il nome era il titolo della prima variante
senza il suffisso tra parentesi, e su ES/FR/UK restava la variante nel nome
(es. "…, gris oscuro, XL").
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFamilyNames(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.se = sync_engine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        Lang = self.env['res.lang'].with_context(active_test=False)
        it, es = Lang.search([('code', '=', 'it_IT')]), Lang.search([('code', '=', 'es_ES')])
        (it | es).write({'active': True})
        self.it = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT nomi', 'code': 'ZZNI', 'marketplace_id': 'ZZ_NAME_IT', 'lang_id': it.id, 'active': True,
        })
        self.es = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT ES nomi', 'code': 'ZZNE', 'marketplace_id': 'ZZ_NAME_ES', 'lang_id': es.id, 'active': True,
        })

    def test_common_name(self):
        f = self.se._family_common_name
        self.assertEqual(f(['Carrete Test 1000, gris', 'Carrete Test 2000, rojo']), 'Carrete Test')
        self.assertEqual(f(['Berkley Big Game Leader Clear 40 LB, 90m', 'Berkley Big Game Leader Clear 60 LB, 90m']),
                         'Berkley Big Game Leader Clear')
        self.assertEqual(f(['C4 Disfraz de hombre Línea Accesorios, gris oscuro, XL'], ['Gris oscuro', 'XL']),
                         'C4 Disfraz de hombre Línea Accesorios')
        self.assertEqual(f(['Mulinello Test (Misura 1000)']), 'Mulinello Test')
        # 2026-11-06: niente parole sospese o etichette sole in coda.
        self.assertEqual(f(['Abu Garcia Reel Grease by Abu', 'Abu Garcia Reel Grease by Garcia']),
                         'Abu Garcia Reel Grease')
        self.assertEqual(f(['AKAMI IKEGAMI | COLOR IKY | MM 95', 'AKAMI IKEGAMI | COLOR TNP | MM 95']),
                         'AKAMI IKEGAMI')
        # Parte comune troppo corta: ripiego sul primo titolo ripulito.
        self.assertEqual(f(['Amo 4 nichel', 'Amo 6 bronzo'], ['4 nichel']), 'Amo 4 nichel')

    def _run(self, marketplace_id, mapping, rows):
        eng = self.se.SyncEngine(self.env, FakeSpApiClient(mapping, marketplace_id=marketplace_id))
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT nomi', 'sync_type': 'full_import'})
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, len(rows), True)
        return eng

    def test_names_recomputed_in_each_language_and_listing_stores_title(self):
        mapping = {'B0ZZNAM1': 'B0ZZNAMPAR', 'B0ZZNAM2': 'B0ZZNAMPAR'}
        self._run('ZZ_NAME_IT', mapping, [
            make_report_row('ZZT-NAM-1', asin='B0ZZNAM1', name='Mulinello Test 1000 Nero'),
            make_report_row('ZZT-NAM-2', asin='B0ZZNAM2', name='Mulinello Test 2000 Rosso'),
        ])
        eng = self._run('ZZ_NAME_ES', {'B0ZZNAM1': 'B0ZZNAMPARES', 'B0ZZNAM2': 'B0ZZNAMPARES'}, [
            make_report_row('ZZT-NAM-1', asin='B0ZZNAM1', name='Carrete Test 1000, negro'),
            make_report_row('ZZT-NAM-2', asin='B0ZZNAM2', name='Carrete Test 2000, rojo'),
        ])
        tmpl = self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZNAMPAR')])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', 'B0ZZNAM1')])
        listing_es = variant.x_amazon_listing_ids.filtered(lambda l: l.marketplace_id == self.es)
        self.assertEqual(listing_es.item_name, 'Carrete Test 1000, negro')
        self.assertEqual(listing_es.parent_asin, 'B0ZZNAMPARES', 'famiglia di Amazon su quel marketplace')

        self.assertGreaterEqual(eng.recompute_family_names(tmpl), 2)
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Mulinello Test')
        self.assertEqual(tmpl.with_context(lang='es_ES').name, 'Carrete Test')
        self.assertEqual(eng.recompute_family_names(tmpl), 0, 'secondo giro: niente da cambiare')

    def test_manual_name_is_kept(self):
        mapping = {'B0ZZNAM3': 'B0ZZNAMPAR2', 'B0ZZNAM4': 'B0ZZNAMPAR2'}
        eng = self._run('ZZ_NAME_IT', mapping, [
            make_report_row('ZZT-NAM-3', asin='B0ZZNAM3', name='Canna Prova 2,10m'),
            make_report_row('ZZT-NAM-4', asin='B0ZZNAM4', name='Canna Prova 2,40m'),
        ])
        tmpl = self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZNAMPAR2')])
        tmpl.with_context(lang='it_IT').write({'name': 'Canna spinning Prova (nome scelto a mano)'})
        eng.recompute_family_names(tmpl)
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Canna spinning Prova (nome scelto a mano)')

    def test_name_born_on_other_marketplace_is_recomputed(self):
        # Famiglia Berkley "Power Hawg" (2026-11-06): nata su ES, in italiano
        # aveva il nome spagnolo; il ricalcolo lo credeva scelto a mano.
        mapping = {'B0ZZNAM5': 'B0ZZNAMPAR3', 'B0ZZNAM6': 'B0ZZNAMPAR3'}
        self._run('ZZ_NAME_IT', mapping, [
            make_report_row('ZZT-NAM-5', asin='B0ZZNAM5', name='Esca Morbida Prova Pesca 5 cm Rossa'),
            make_report_row('ZZT-NAM-6', asin='B0ZZNAM6', name='Esca Morbida Prova Pesca 7 cm Verde'),
        ])
        eng = self._run('ZZ_NAME_ES', mapping, [
            make_report_row('ZZT-NAM-5', asin='B0ZZNAM5', name='Cebo Blando Prueba Pesca 5 cm, rojo'),
            make_report_row('ZZT-NAM-6', asin='B0ZZNAM6', name='Cebo Blando Prueba Pesca 7 cm, verde'),
        ])
        tmpl = self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZNAMPAR3')])
        tmpl.with_context(lang='it_IT').write({'name': 'Cebo Blando Prueba Pesca'})
        eng.recompute_family_names(tmpl)
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Esca Morbida Prova Pesca')

    def test_text_copied_from_other_language_is_not_own(self):
        eng = self.se.SyncEngine(self.env, FakeSpApiClient(marketplace_id='ZZ_NAME_IT'))
        tmpl = self.env['product.template'].with_context(lang='es_ES').create({
            'name': 'ZZT Cebo copiado', 'description_sale': 'Descripción ES'})
        tmpl.with_context(lang='it_IT').write({'name': 'ZZT Cebo copiado', 'description_sale': 'Descripción ES'})
        self.assertFalse(eng._has_own_translation(tmpl, 'name', 'it_IT'), 'copia del testo spagnolo')
        tmpl.with_context(lang='it_IT').write({'name': 'ZZT Esca italiana'})
        self.assertTrue(eng._has_own_translation(tmpl, 'name', 'it_IT'))
