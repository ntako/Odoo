"""
Descrizioni nella lingua del marketplace (2026-11-04): dopo l'import da
capo solo 714 prodotti su 2.835 avevano la descrizione es_ES. Tre cause:
- il Catalog API veniva saltato per i prodotti "completi", ma il controllo
  sulla descrizione leggeva il testo di ripiego (italiano);
- il client chiedeva i testi sempre in it_IT, anche su ES/FR/UK;
- fra più voci di un attributo si prendeva la prima, non quella della lingua.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


def _localized(values):
    return [{'value': v, 'language_tag': tag, 'marketplace_id': 'X'} for tag, v in values]


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestCatalogLanguage(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.se = sync_engine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.es_lang = self.env['res.lang'].with_context(active_test=False).search([('code', '=', 'es_ES')])
        self.es_lang.write({'active': True})
        self.es = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Spagna lingua', 'code': 'ZZLE', 'marketplace_id': 'ZZ_LANG_ES',
            'lang_id': self.es_lang.id, 'active': True,
        })

    def test_cat_val_prefers_marketplace_language(self):
        attrs = {'product_description': _localized([('it_IT', 'Descrizione'), ('es_ES', 'Descripción')])}
        self.assertEqual(self.se._cat_val(attrs, 'product_description', 'es_ES'), 'Descripción')
        self.assertEqual(self.se._cat_val(attrs, 'product_description', 'fr_FR'), 'Descrizione',
                         'lingua assente: prima voce come prima')
        self.assertEqual(self.se._cat_val({'brand': [{'value': 'Marca'}]}, 'brand', 'es_ES'), 'Marca')

    def test_merge_catalog_picks_description_and_bullets_in_language(self):
        catalog = {'attributes': {
            'product_description': _localized([('it_IT', 'Descrizione'), ('es_ES', 'Descripción')]),
            'bullet_point': _localized([('it_IT', 'Punto'), ('es_ES', 'Punto ES 1'), ('es_ES', 'Punto ES 2')]),
        }}
        product = {}
        self.se._merge_catalog(product, catalog, 'es_ES')
        self.assertEqual(product['description'], 'Descripción')
        self.assertEqual(product['bullet_points'], ['Punto ES 1', 'Punto ES 2'])

    def _complete_template(self, sku, asin):
        return self.env['product.template'].with_context(lang='it_IT').create({
            'name': 'ZZT completo IT', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
            'x_amazon_category': 'Categoria', 'product_length': 10.0,
            'x_amazon_product_type': 'TEST_TYPE', 'x_amazon_browse_node_id': '1',
            'x_amazon_brand': 'Marca', 'description_sale': 'Descrizione italiana',
        })

    def test_needs_catalog_data_checks_language_slot(self):
        tmpl = self._complete_template('ZZT-LANG-NEED', 'B0ZZLANGNEED')
        self.assertFalse(self.se._needs_catalog_data(tmpl))
        self.assertFalse(self.se._needs_catalog_data(tmpl, 'it_IT'))
        self.assertTrue(self.se._needs_catalog_data(tmpl, 'es_ES'),
                        'la descrizione italiana non basta per il marketplace spagnolo')

    def test_client_locale_follows_marketplace_language(self):
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        self.assertEqual(SpApiClient(self.env, marketplace_id='ZZ_LANG_ES').locale, 'es_ES')
        self.assertEqual(SpApiClient(self.env, marketplace_id='ZZ_LANG_ES', locale='fr_FR').locale, 'fr_FR')
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.locale', 'it_IT')
        self.assertEqual(SpApiClient(self.env, marketplace_id='ZZ_NO_MARKETPLACE').locale, 'it_IT')

    def test_secondary_marketplace_fetches_and_writes_translated_description(self):
        sku, asin = 'ZZT-LANG-ES', 'B0ZZLANGES1'
        tmpl = self._complete_template(sku, asin)
        requested = []

        class EsClient(FakeSpApiClient):
            locale = 'es_ES'

            def get_catalog_items_batch(self, asins):
                requested.extend(asins)
                out = super().get_catalog_items_batch(asins)
                for a in asins:
                    out[a]['attributes'] = {'product_description': _localized([
                        ('it_IT', 'Descrizione italiana'), ('es_ES', 'Descripción española'),
                    ])}
                return out

        eng = self.se.SyncEngine(self.env, EsClient(marketplace_id='ZZ_LANG_ES'))
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT lingua ES', 'sync_type': 'full_import'})
        with no_commit(self.env):
            eng.full_import_report_rows(log, [make_report_row(sku, asin=asin, name='Nombre ES')], 0, 1, True)

        self.assertIn(asin, requested, 'il Catalog API va chiamato: manca la descrizione es_ES')
        tmpl.invalidate_recordset()
        self.assertIn('Descripción española', tmpl.with_context(lang='es_ES').description_sale)
        self.assertEqual(tmpl.with_context(lang='it_IT').description_sale, 'Descrizione italiana',
                         'la descrizione italiana non si tocca')

    def _es_family_client(self, mapping, marketplace_id, descriptions):
        class Client(FakeSpApiClient):
            def get_catalog_items_batch(self, asins):
                out = super().get_catalog_items_batch(asins)
                for a in asins:
                    if descriptions:
                        out[a]['attributes'] = {'product_description': _localized(descriptions)}
                return out
        return Client(mapping, marketplace_id=marketplace_id)

    def _run_rows(self, client, rows):
        eng = self.se.SyncEngine(self.env, client)
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT lingua famiglia', 'sync_type': 'full_import'})
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, len(rows), True)
        return log

    def test_family_template_gets_text_in_secondary_language(self):
        it_lang = self.env['res.lang'].with_context(active_test=False).search([('code', '=', 'it_IT')])
        it_lang.write({'active': True})
        self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Italia lingua', 'code': 'ZZLI', 'marketplace_id': 'ZZ_LANG_IT',
            'lang_id': it_lang.id, 'active': True,
        })
        mapping = {'B0ZZLFAM1': 'B0ZZLFAMPAR', 'B0ZZLFAM2': 'B0ZZLFAMPAR', 'B0ZZLFAM3': 'B0ZZLFAMPAR'}
        self._run_rows(self._es_family_client(mapping, 'ZZ_LANG_IT', [('it_IT', 'Descrizione famiglia')]), [
            make_report_row('ZZT-LFAM-1', asin='B0ZZLFAM1', name='Mulinello Test (Misura 1000)'),
            make_report_row('ZZT-LFAM-2', asin='B0ZZLFAM2', name='Mulinello Test (Misura 2000)'),
        ])
        tmpl = self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZLFAMPAR')])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(len(tmpl.product_variant_ids), 2)

        # ES: una variante già esistente e una nuova nella stessa famiglia.
        self._run_rows(self._es_family_client(mapping, 'ZZ_LANG_ES', [('es_ES', 'Descripción familia')]), [
            make_report_row('ZZT-LFAM-1', asin='B0ZZLFAM1', name='Carrete Test (Tamaño 1000)'),
            make_report_row('ZZT-LFAM-3', asin='B0ZZLFAM3', name='Carrete Test (Tamaño 3000)'),
        ])
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='es_ES').name, 'Carrete Test')
        self.assertIn('Descripción familia', tmpl.with_context(lang='es_ES').description_sale)
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Mulinello Test', "l'italiano non si tocca")
        self.assertIn('Descrizione famiglia', tmpl.with_context(lang='it_IT').description_sale)
