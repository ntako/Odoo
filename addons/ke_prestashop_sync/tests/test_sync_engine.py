"""Test del SyncEngine con un client PrestaShop finto (nessuna chiamata di rete)."""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_prestashop_sync.services.ps_api_client import (
    PsApiClient, PsResourceNotFound)
from odoo.addons.ke_prestashop_sync.services.sync_engine import SyncEngine


@tagged('post_install', '-at_install')
class TestPsApiClient(TransactionCase):

    def test_clean_reference(self):
        self.assertEqual(PsApiClient._clean_reference('ART#12=3;{x}'), 'ART123x')
        self.assertEqual(len(PsApiClient._clean_reference('R' * 100)), 64)
        self.assertEqual(PsApiClient._clean_reference('  ABC/1 2  '), 'ABC/1 2')
        self.assertEqual(PsApiClient._clean_reference(None), '')


class FakePsClient:
    """Implementa la sola superficie usata dal SyncEngine, in memoria."""

    def __init__(self):
        self.created_products = []
        self.price_updates = []
        self.combo_price_updates = []
        self.stock_updates = []
        self.deactivated = []
        self.product_updates = []
        self.created_manufacturers = []
        self.created_combinations = []
        self.combination_updates = []
        self.stock_by_product = {}
        self.missing_ids = set()
        self._next_id = 500

    def _guard(self, ps_id):
        if ps_id in self.missing_ids:
            raise PsResourceNotFound(f'GET products/{ps_id} → HTTP 404')

    # --- prodotti ---
    def get_product_ids(self):
        return [101]

    def get_product(self, ps_id):
        self._guard(ps_id)
        return {
            'id': ps_id,
            'reference': 'REF101',
            'ean13': '',
            'price': '9.990000',
            'name': [{'id': 1, 'value': 'Prodotto Test'}],
            'description_short': [{'id': 1, 'value': '<p>Descrizione</p>'}],
        }

    def get_product_name(self, product_dict):
        names = product_dict.get('name')
        return names[0]['value'] if isinstance(names, list) else (names or '')

    def get_product_description(self, product_dict, field='description_short'):
        entries = product_dict.get(field)
        return entries[0]['value'] if isinstance(entries, list) else (entries or '')

    def create_product(self, **kwargs):
        self._next_id += 1
        self.created_products.append(kwargs)
        return self._next_id

    def find_product_by_reference(self, reference):
        return None

    def update_product_price(self, ps_id, price):
        self._guard(ps_id)
        self.price_updates.append((ps_id, price))

    def update_product(self, ps_id, **kwargs):
        self._guard(ps_id)
        self.product_updates.append((ps_id, kwargs))

    # --- categorie / marche ---
    def get_categories(self):
        return [
            {'id': 1, 'parent': 0, 'name': 'Root', 'level': 0, 'active': True},
            {'id': 2, 'parent': 1, 'name': 'Home', 'level': 1, 'active': True},
            {'id': 30, 'parent': 2, 'name': 'Mulinelli', 'level': 2, 'active': True},
            {'id': 31, 'parent': 30, 'name': 'Spinning', 'level': 3, 'active': True},
        ]

    def get_manufacturers(self):
        return [{'id': 5, 'name': 'Bad Bass Tech'}]

    def create_manufacturer(self, name):
        self._next_id += 1
        self.created_manufacturers.append(name)
        return self._next_id

    # --- combinazioni ---
    def get_or_create_product_option(self, name):
        return 900

    def get_or_create_option_value(self, option_id, value):
        return 901

    def create_combination(self, **kwargs):
        self._next_id += 1
        self.created_combinations.append(kwargs)
        return self._next_id

    def update_combination(self, combo_id, **kwargs):
        self.combination_updates.append((combo_id, kwargs))

    def update_product_active(self, ps_id, active):
        self.deactivated.append((ps_id, active))

    def get_combination(self, combo_id):
        return {'id': combo_id, 'price': '0.000000'}

    def update_combination_price(self, combo_id, price_impact):
        self.combo_price_updates.append((combo_id, price_impact))

    # --- stock ---
    def get_stock_availables(self, ps_product_id):
        return self.stock_by_product.get(ps_product_id, [{'id': 7, 'id_product_attribute': '0', 'quantity': '0'}])

    def update_stock_available(self, **kwargs):
        self.stock_updates.append(kwargs)

    # --- ordini ---
    def get_order_ids(self, date_from=None, states=None):
        return [1]

    def get_order(self, order_id):
        return {
            'id': 1,
            'id_customer': 2,
            'id_address_delivery': 3,
            'id_address_invoice': 3,
            'reference': 'ABCDEFGHI',
            'current_state': 2,
            'date_add': '2026-01-01 10:00:00',
            'associations': {'order_rows': [{
                'product_id': 101,
                'product_attribute_id': 0,
                'product_quantity': '2',
                'unit_price_tax_excl': '9.99',
                'product_name': 'Prodotto Test',
            }]},
        }

    def get_customer(self, customer_id):
        return {'email': 'mario.rossi@example.com', 'firstname': 'Mario', 'lastname': 'Rossi'}

    def get_address(self, address_id):
        return {
            'firstname': 'Mario', 'lastname': 'Rossi',
            'address1': 'Via Roma 1', 'address2': '',
            'city': 'Milano', 'postcode': '20100',
            'phone': '021234567', 'id_country': 10,
        }

    def get_country(self, country_id):
        return {'iso_code': 'IT'}

    def order_state_label(self, state_id):
        return 'Payment accepted'


@tagged('post_install', '-at_install')
class TestSyncEngine(TransactionCase):

    def setUp(self):
        super().setUp()
        # Il motore fa commit a ogni passo (sopravvive a un riavvio): nei test
        # Odoo 19 lo vieta, qui diventa un no-op (come no_commit di ke_amazon_sync).
        patcher = patch.object(self.env.cr, 'commit', lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = FakePsClient()
        self.engine = SyncEngine(self.env, self.client)

    def _log(self, sync_type):
        return self.env['ke.prestashop.sync.log'].create({
            'name': 'test', 'sync_type': sync_type, 'state': 'running',
        })

    def test_import_products_creates_template(self):
        self.engine.import_products_from_ps(self._log('import_products'))
        tmpl = self.env['product.template'].search([('ps_product_id', '=', 101)])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(tmpl.name, 'Prodotto Test')
        self.assertAlmostEqual(tmpl.list_price, 9.99, places=2)

    def test_import_products_matches_by_reference(self):
        existing = self.env['product.template'].create({
            'name': 'Preesistente', 'default_code': 'REF101', 'type': 'consu',
        })
        self.engine.import_products_from_ps(self._log('import_products'))
        self.assertEqual(existing.ps_product_id, 101)
        self.assertEqual(
            self.env['product.template'].search_count([('default_code', '=', 'REF101')]), 1)

    def test_export_simple_product_creates_on_ps(self):
        tmpl = self.env['product.template'].create({
            'name': 'Da Esportare', 'default_code': 'EXP1',
            'list_price': 5.0, 'type': 'consu',
        })
        self.engine.export_products_to_ps(self._log('export_products'), product_ids=tmpl.ids)
        self.assertTrue(self.client.created_products)
        self.assertTrue(tmpl.ps_product_id)

    def test_export_only_in_stock_uses_free_qty(self):
        wh = self.env['stock.warehouse'].search([], limit=1)
        other_wh = self.env['stock.warehouse'].create({
            'name': 'Altro WH', 'code': 'ALT2',
        })
        p_here = self.env['product.template'].create({
            'name': 'Disponibile qui', 'default_code': 'S1', 'list_price': 3.0,
            'type': 'consu', 'is_storable': True,
        })
        p_elsewhere = self.env['product.template'].create({
            'name': 'Disponibile altrove', 'default_code': 'S2', 'list_price': 3.0,
            'type': 'consu', 'is_storable': True,
        })
        p_none = self.env['product.template'].create({
            'name': 'Senza giacenza', 'default_code': 'S3', 'list_price': 3.0,
            'type': 'consu', 'is_storable': True,
        })
        Quant = self.env['stock.quant']
        Quant._update_available_quantity(p_here.product_variant_id, wh.lot_stock_id, 4)
        Quant._update_available_quantity(
            p_elsewhere.product_variant_id, other_wh.lot_stock_id, 4)

        self.engine.export_products_to_ps(
            self._log('export_products'),
            product_ids=(p_here + p_elsewhere + p_none).ids,
            only_in_stock=True, warehouse_id=wh.id)

        names = [kw['name'] for kw in self.client.created_products]
        self.assertIn('Disponibile qui', names)
        self.assertNotIn('Disponibile altrove', names)
        self.assertNotIn('Senza giacenza', names)

    def test_export_relinks_orphaned_product(self):
        self.client.missing_ids = {777}
        tmpl = self.env['product.template'].create({
            'name': 'Orfano', 'default_code': 'ORF1', 'list_price': 8.0,
            'type': 'consu', 'ps_product_id': 777,
        })
        self.engine.export_products_to_ps(
            self._log('export_products'), product_ids=tmpl.ids, only_in_stock=False)
        self.assertNotEqual(tmpl.ps_product_id, 777)
        self.assertTrue(tmpl.ps_product_id)
        self.assertEqual(len(self.client.created_products), 1)

    def test_sync_prices_clears_orphaned_link(self):
        self.client.missing_ids = {888}
        tmpl = self.env['product.template'].create({
            'name': 'Orfano prezzo', 'ps_product_id': 888,
            'list_price': 5.0, 'type': 'consu',
        })
        self.engine.sync_prices_to_prestashop(self._log('sync_prices'))
        self.assertEqual(tmpl.ps_product_id, 0)

    def test_reset_all_ps_links(self):
        t1 = self.env['product.template'].create({
            'name': 'Collegato', 'type': 'consu',
            'ps_product_id': 10, 'ps_image_synced': True,
        })
        n_t, _n_v = self.engine.reset_all_ps_links()
        self.assertEqual(t1.ps_product_id, 0)
        self.assertFalse(t1.ps_image_synced)
        self.assertGreaterEqual(n_t, 1)

    def test_export_batches_and_reenqueues(self):
        Log = self.env['ke.prestashop.sync.log']
        self.env['product.template'].search([]).write({'ps_sync_enabled': False})
        for n in range(3):
            self.env['product.template'].create({
                'name': f'Lotto {n}', 'default_code': f'L{n}',
                'list_price': 1.0, 'type': 'consu',
            })
        self.env['ir.config_parameter'].sudo().set_param('ke_prestashop_sync.export_all', 'True')
        with patch.object(SyncEngine, '_EXPORT_BATCH', 2):
            self.engine.export_products_to_ps(self._log('export_products'))
        self.assertEqual(len(self.client.created_products), 2)
        self.assertTrue(Log.search([
            ('state', '=', 'queued'), ('sync_type', '=', 'export_products')]))

    def test_export_deactivates_archived_products(self):
        tmpl = self.env['product.template'].create({
            'name': 'Archiviato', 'type': 'consu', 'ps_product_id': 999,
        })
        tmpl.action_archive()
        self.engine.export_products_to_ps(self._log('export_products'))
        self.assertIn((999, 0), self.client.deactivated)

    def test_import_order_creates_sale_order(self):
        self.env['product.template'].create({
            'name': 'Prodotto Test', 'ps_product_id': 101,
            'list_price': 9.99, 'type': 'consu',
        })
        self.engine.import_orders_from_ps(self._log('import_orders'))
        order = self.env['sale.order'].search([('ps_order_id', '=', 1)])
        self.assertEqual(len(order), 1)
        self.assertEqual(order.partner_id.email, 'mario.rossi@example.com')
        self.assertEqual(order.order_line.product_uom_qty, 2)
        self.assertEqual(order.partner_shipping_id.city, 'Milano')
        self.assertEqual(order.partner_shipping_id.country_id.code, 'IT')

    def test_import_order_is_idempotent(self):
        self.env['product.template'].create({
            'name': 'Prodotto Test', 'ps_product_id': 101,
            'list_price': 9.99, 'type': 'consu',
        })
        self.engine.import_orders_from_ps(self._log('import_orders'))
        self.engine.import_orders_from_ps(self._log('import_orders'))
        self.assertEqual(
            self.env['sale.order'].search_count([('ps_order_id', '=', 1)]), 1)

    def test_price_sync_covers_linked_products_without_stock(self):
        # 2026-11-05: le sync di prezzi e giacenze riguardano tutti i prodotti
        # già su PS (prima il filtro magazzino li limitava ai soli con giacenza).
        wh = self.env['stock.warehouse'].search([], limit=1)
        p_in = self.env['product.template'].create({
            'name': 'In magazzino', 'ps_product_id': 201, 'list_price': 5.0,
            'type': 'consu', 'is_storable': True,
        })
        p_out = self.env['product.template'].create({
            'name': 'Fuori magazzino', 'ps_product_id': 202, 'list_price': 5.0,
            'type': 'consu', 'is_storable': True,
        })
        self.env['stock.quant']._update_available_quantity(
            p_in.product_variant_id, wh.lot_stock_id, 10)
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_prestashop_sync.warehouse_id', str(wh.id))

        self.engine.sync_prices_to_prestashop(self._log('sync_prices'))

        updated_ids = [ps_id for ps_id, _ in self.client.price_updates]
        self.assertIn(201, updated_ids)
        self.assertIn(202, updated_ids)

    def test_sync_prices_updates_when_different(self):
        self.env['product.template'].create({
            'name': 'Prodotto Test', 'ps_product_id': 101,
            'list_price': 12.50, 'type': 'consu',
        })
        self.engine.sync_prices_to_prestashop(self._log('sync_prices'))
        self.assertEqual(self.client.price_updates, [(101, 12.50)])

    def test_queue_enqueue_and_run(self):
        Log = self.env['ke.prestashop.sync.log']
        self.env['product.template'].create({
            'name': 'Prodotto Test', 'ps_product_id': 101,
            'list_price': 12.50, 'type': 'consu',
        })
        with patch.object(type(Log), '_get_client', return_value=self.client):
            log_id = Log.enqueue('sync_prices', 'sync_prices_to_prestashop')
            self.assertEqual(Log.browse(log_id).state, 'queued')
            Log._cron_run_queue()
        log = Log.browse(log_id)
        self.assertEqual(log.state, 'done')
        self.assertEqual(self.client.price_updates, [(101, 12.50)])

    def test_queue_reaps_stale_running_job(self):
        Log = self.env['ke.prestashop.sync.log']
        stale = Log.create({
            'name': 'vecchio', 'sync_type': 'sync_stock', 'state': 'running',
        })
        self.env.cr.execute(
            "UPDATE ke_prestashop_sync_log SET write_date = NOW() - INTERVAL '3 hours' WHERE id = %s",
            (stale.id,))
        stale.invalidate_recordset(['write_date'])
        Log._cron_run_queue()
        self.assertEqual(stale.state, 'error')


@tagged('post_install', '-at_install')
class TestPsCatalogExport(TransactionCase):
    """Export con categorie mappate, marche, prezzi variante, giacenze dal magazzino (2026-11-05)."""

    def setUp(self):
        super().setUp()
        # Il motore fa commit a ogni passo (sopravvive a un riavvio): nei test
        # Odoo 19 lo vieta, qui diventa un no-op (come no_commit di ke_amazon_sync).
        patcher = patch.object(self.env.cr, 'commit', lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = FakePsClient()
        self.engine = SyncEngine(self.env, self.client)
        self.wh = self.env['stock.warehouse'].search([], limit=1)
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('ke_prestashop_sync.warehouse_id', str(self.wh.id))
        self.params.set_param('ke_prestashop_sync.export_all', False)
        self.env['ke.prestashop.category']._sync_from_prestashop(self.client)
        self.ps_spinning = self.env['ke.prestashop.category'].search([('ps_id', '=', 31)])

    def _log(self):
        return self.env['ke.prestashop.sync.log'].create({
            'name': 'test', 'sync_type': 'export_products', 'state': 'running'})

    def _stock(self, product, qty, warehouse=None):
        self.env['stock.quant']._update_available_quantity(product, (warehouse or self.wh).lot_stock_id, qty)

    def test_category_tree_paths(self):
        self.assertEqual(self.ps_spinning.complete_name, 'Mulinelli / Spinning')

    def test_export_uses_mapped_category_and_brand(self):
        parent = self.env['product.category'].create({'name': 'ZZT Mulinelli', 'ps_category_id': self.ps_spinning.id})
        child = self.env['product.category'].create({'name': 'ZZT Spinning', 'parent_id': parent.id})
        brand = self.env['ke.product.brand'].create({
            'name': 'ZZT Bad Bass', 'alias_ids': [(0, 0, {'name': 'Bad Bass Tech'})]})
        new_brand = self.env['ke.product.brand'].create({'name': 'ZZT Marca Nuova'})
        t1 = self.env['product.template'].create({
            'name': 'ZZT PS 1', 'default_code': 'ZZT-PS-1', 'list_price': 10.0, 'type': 'consu',
            'categ_id': child.id, 'ke_brand_id': brand.id})
        t2 = self.env['product.template'].create({
            'name': 'ZZT PS 2', 'default_code': 'ZZT-PS-2', 'list_price': 10.0, 'type': 'consu',
            'ke_brand_id': new_brand.id})
        self.engine.export_products_to_ps(self._log(), product_ids=(t1 + t2).ids)
        by_name = {kw['name']: kw for kw in self.client.created_products}
        self.assertEqual(by_name['ZZT PS 1']['category_id'], 31, 'categoria del padre mappato')
        self.assertEqual(by_name['ZZT PS 1']['manufacturer_id'], 5, 'marca PS trovata dal nome alternativo')
        self.assertEqual(by_name['ZZT PS 2']['category_id'], 0, 'non mappata: categoria predefinita')
        self.assertEqual(self.client.created_manufacturers, ['ZZT Marca Nuova'])
        self.assertEqual(new_brand.ps_manufacturer_id, by_name['ZZT PS 2']['manufacturer_id'])

    def test_default_exports_only_products_in_stock(self):
        self.env['product.template'].search([]).write({'ps_sync_enabled': False})
        here = self.env['product.template'].create({'name': 'ZZT qui', 'type': 'consu', 'is_storable': True})
        other_wh = self.env['stock.warehouse'].create({'name': 'ZZT altro', 'code': 'ZZT9'})
        there = self.env['product.template'].create({'name': 'ZZT altrove', 'type': 'consu', 'is_storable': True})
        none = self.env['product.template'].create({'name': 'ZZT niente', 'type': 'consu', 'is_storable': True})
        self._stock(here.product_variant_id, 3)
        self._stock(there.product_variant_id, 3, other_wh)
        self.engine.export_products_to_ps(self._log())
        names = {kw['name'] for kw in self.client.created_products}
        self.assertEqual(names, {'ZZT qui'})
        self.params.set_param('ke_prestashop_sync.export_all', 'True')
        self.engine.export_products_to_ps(self._log())
        names = {kw['name'] for kw in self.client.created_products}
        self.assertTrue({'ZZT altrove', 'ZZT niente'} <= names)

    def test_configurable_variant_price_and_stock(self):
        attr = self.env['product.attribute'].create({
            'name': 'ZZT Misura PS', 'value_ids': [(0, 0, {'name': '1000'}), (0, 0, {'name': '2000'})]})
        tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia PS', 'list_price': 20.0, 'type': 'consu', 'is_storable': True,
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})],
        })
        small, big = tmpl.product_variant_ids.sorted(lambda v: v.product_template_attribute_value_ids.name)
        big.ke_list_price = 26.0
        self._stock(big, 4)
        self.engine.export_products_to_ps(self._log(), product_ids=tmpl.ids)
        impacts = sorted(c['price_impact'] for c in self.client.created_combinations)
        self.assertEqual(impacts, [0.0, 6.0])
        self.assertEqual(self.client.created_products[0]['price'], 20.0)
        # Listino PrestaShop +10%: padre 22, impatto 6,60.
        pricelist = self.env['product.pricelist'].create({
            'name': 'ZZT sito', 'currency_id': self.env.company.currency_id.id,
            'item_ids': [(0, 0, {'applied_on': '3_global', 'compute_price': 'formula',
                                 'base': 'list_price', 'price_discount': -10.0})]})
        self.params.set_param('ke_prestashop_sync.pricelist_id', str(pricelist.id))
        self.engine.export_products_to_ps(self._log(), product_ids=tmpl.ids, full_resync=True)
        self.assertAlmostEqual(self.client.product_updates[-1][1]['price'], 22.0, places=4)
        impacts = sorted(kw['price_impact'] for _id, kw in self.client.combination_updates)
        self.assertAlmostEqual(impacts[-1], 6.6, places=4)

    def test_stock_sync_pushes_warehouse_qty_and_zero(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT stock PS', 'type': 'consu', 'is_storable': True, 'ps_product_id': 321})
        other_wh = self.env['stock.warehouse'].create({'name': 'ZZT altro 2', 'code': 'ZZT8'})
        self._stock(tmpl.product_variant_id, 5, other_wh)
        self.client.stock_by_product[321] = [{'id': 70, 'id_product_attribute': '0', 'quantity': '9'}]
        log = self.env['ke.prestashop.sync.log'].create({'name': 't', 'sync_type': 'sync_stock', 'state': 'running'})
        self.engine.sync_stock_to_prestashop(log)
        pushed = [u for u in self.client.stock_updates if u['ps_product_id'] == 321]
        self.assertEqual(pushed[-1]['quantity'], 0, 'giacenza solo in un altro magazzino: su PS va a zero')
