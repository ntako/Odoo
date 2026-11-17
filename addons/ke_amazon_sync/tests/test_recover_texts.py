"""
Recupero dei testi italiani da Amazon IT (2026-11-17): nomi e descrizioni
mancanti o copiati da un'altra lingua dal Full Import, presi dal titolo
dell'inserzione o dal Catalog API, solo se davvero in italiano.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import no_commit


class _CatalogClient:
    def __init__(self, items, marketplace_id):
        self.items = items
        self.marketplace_id = marketplace_id
        self.locale = 'it_IT'
        self.calls = []

    def get_catalog_items_batch(self, asins):
        self.calls.append(list(asins))
        return {a: self.items[a] for a in asins if a in self.items}


@tagged('post_install', '-at_install')
class TestRecoverTexts(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.se = sync_engine
        Lang = self.env['res.lang'].with_context(active_test=False)
        it, es = Lang.search([('code', '=', 'it_IT')]), Lang.search([('code', '=', 'es_ES')])
        (it | es).write({'active': True})
        self.it = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT testi', 'code': 'ZZTI', 'marketplace_id': 'ZZ_TXT_IT', 'lang_id': it.id, 'active': True})
        self.es = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT ES testi', 'code': 'ZZTE', 'marketplace_id': 'ZZ_TXT_ES', 'lang_id': es.id, 'active': True})
        self.log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT testi', 'sync_type': 'recover_texts'})

    def _product(self, name_es, sku, asin, it_title='', desc_es=''):
        tmpl = self.env['product.template'].with_context(lang='es_ES').create({
            'name': name_es, 'default_code': sku, 'description_sale': desc_es or False})
        # Il Full Import scriveva il testo spagnolo anche in italiano.
        tmpl.with_context(lang='it_IT').write({'name': name_es, 'description_sale': desc_es or False})
        variant = tmpl.product_variant_id
        Listing = self.env['ke.amazon.listing']
        Listing.create({'product_id': variant.id, 'marketplace_id': self.it.id, 'asin': asin,
                        'sku': sku, 'item_name': it_title or False})
        Listing.create({'product_id': variant.id, 'marketplace_id': self.es.id, 'asin': asin,
                        'sku': sku, 'item_name': name_es})
        return tmpl

    def _run(self, items):
        client = _CatalogClient(items, 'ZZ_TXT_IT')
        eng = self.se.SyncEngine(self.env, client)
        with no_commit(self.env):
            result = eng.recover_marketplace_texts(self.log, self.it)
        return client, result

    def test_name_from_catalog_and_italian_description_only(self):
        tmpl = self._product('Carrete giratorio Test', 'ZZT-RT-1', 'B0ZZRT0001', desc_es='Carrete ligero.')
        item = {
            'summaries': [{'marketplaceId': 'ZZ_TXT_IT', 'itemName': 'Mulinello da spinning Test'}],
            'attributes': {
                'product_description': [{'value': 'Mulinello leggero.', 'language_tag': 'it_IT'},
                                        {'value': 'Carrete ligero.', 'language_tag': 'es_ES'}],
                'bullet_point': [{'value': 'Frizione anteriore', 'language_tag': 'it_IT'}],
            },
        }
        client, (updated, skipped, errors) = self._run({'B0ZZRT0001': item})
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Mulinello da spinning Test')
        self.assertEqual(tmpl.with_context(lang='es_ES').name, 'Carrete giratorio Test', 'lo spagnolo resta')
        desc = tmpl.with_context(lang='it_IT').description_sale
        self.assertIn('Mulinello leggero.', desc)
        self.assertIn('Frizione anteriore', desc)
        self.assertNotIn('Carrete', desc)
        listing = tmpl.product_variant_id.x_amazon_listing_ids.filtered(lambda l: l.marketplace_id == self.it)
        self.assertEqual(listing.item_name, 'Mulinello da spinning Test', 'titolo IT salvato sull\'inserzione')
        self.assertEqual((updated, errors), (1, 0))

    def test_saved_title_used_without_call_and_no_foreign_description(self):
        tmpl = self._product('Caña Test', 'ZZT-RT-2', 'B0ZZRT0002', it_title='Canna Test')
        tmpl.with_context(lang='it_IT').write({'description_sale': 'Descrizione scritta a mano.'})
        es_only = {'attributes': {'product_description': [{'value': 'Solo en español.', 'language_tag': 'es_ES'}]}}
        client, _res = self._run({'B0ZZRT0002': es_only})
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Canna Test')
        self.assertEqual(tmpl.with_context(lang='it_IT').description_sale, 'Descrizione scritta a mano.',
                         'descrizione italiana propria: non si tocca')
        self.assertFalse(client.calls, 'titolo IT già salvato e descrizione a posto: nessuna chiamata')

    def test_name_equal_to_marketplace_title_is_already_right(self):
        """Nome uguale in tutte le lingue ("Abu Garcia® Atom"): Odoo non salva it_IT uguale a en_US, ma è giusto."""
        tmpl = self.env['product.template'].create({'name': 'ZZT Atom', 'default_code': 'ZZT-RT-3'})
        tmpl.with_context(lang='it_IT').write({'description_sale': 'Ondulante classico.'})
        variant = tmpl.product_variant_id
        self.env['ke.amazon.listing'].create({'product_id': variant.id, 'marketplace_id': self.it.id,
                                              'asin': 'B0ZZRT0003', 'sku': 'ZZT-RT-3', 'item_name': 'ZZT Atom'})
        client, (updated, skipped, errors) = self._run({})
        self.assertFalse(client.calls)
        self.assertEqual((updated, skipped), (0, 0))
        self.assertIn('Restano senza testo proprio in it_IT: 0 nomi', self.log.log_lines)
