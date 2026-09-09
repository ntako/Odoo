"""Test del SyncEngine con un client PrestaShop finto (nessuna chiamata di rete)."""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_prestashop_sync.services.ps_api_client import PsApiClient
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
        self._next_id = 500

    # --- prodotti ---
    def get_product_ids(self):
        return [101]

    def get_product(self, ps_id):
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
        self.price_updates.append((ps_id, price))

    def update_product_active(self, ps_id, active):
        self.deactivated.append((ps_id, active))

    def get_combination(self, combo_id):
        return {'id': combo_id, 'price': '0.000000'}

    def update_combination_price(self, combo_id, price_impact):
        self.combo_price_updates.append((combo_id, price_impact))

    # --- stock ---
    def get_stock_availables(self, ps_product_id):
        return [{'id': 7, 'id_product_attribute': '0', 'quantity': '0'}]

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

    def test_warehouse_filter_limits_price_sync(self):
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
        params.set_param('ke_prestashop_sync.filter_by_warehouse', 'True')

        self.engine.sync_prices_to_prestashop(self._log('sync_prices'))

        updated_ids = [ps_id for ps_id, _ in self.client.price_updates]
        self.assertIn(201, updated_ids)
        self.assertNotIn(202, updated_ids)

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
            "UPDATE ke_prestashop_sync_sync_log SET write_date = NOW() - INTERVAL '3 hours' WHERE id = %s",
            (stale.id,))
        stale.invalidate_recordset(['write_date'])
        Log._cron_run_queue()
        self.assertEqual(stale.state, 'error')
