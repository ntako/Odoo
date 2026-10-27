"""
Test per sync_fba_quantities_page sul ramo pooled — bug P1 trovato in
revisione il 2026-10-27 (vedi TASK.md "Giacenze"): la giacenza precedente
usata per decidere quanto consumare dal transito veniva letta dal campo
condiviso x_amazon_qty del template invece che dalla listing per variante,
quindi lo snapshot appena scritto per una sorella diventava il "precedente"
dell'altra — con due sorelle a quantità diverse e dati Amazon invariati tra
un ciclo e l'altro, il secondo ciclo consumava transito che non era mai
stato davvero ricevuto.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFbaSyncPooledSnapshot(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine

        warehouse = self.env['stock.warehouse'].create({'name': 'ZZT WH FBA', 'code': 'ZZTWFBA'})
        self.env.company.amazon_fba_warehouse_id = warehouse.id
        self.fba_location = warehouse.lot_stock_id

        self.marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Pooled', 'code': 'ZZTPL', 'marketplace_id': 'ZZT_POOL_MKT', 'active': True,
        })
        self.assertTrue(self.marketplace.is_fba_pooled(), 'unico marketplace attivo = pooled per definizione')

        attr = self.env['product.attribute'].create({
            'name': 'ZZT Taglia Pool',
            'value_ids': [(0, 0, {'name': 'S'}), (0, 0, {'name': 'M'})],
        })
        self.tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia pooled', 'type': 'consu', 'is_storable': True,
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)],
            })],
        })
        self.variant_a, self.variant_b = self.tmpl.product_variant_ids
        self.variant_a.write({'x_amazon_asin': 'B0ZZPOOLA', 'x_amazon_sku': 'ZZT-POOL-A'})
        self.variant_b.write({'x_amazon_asin': 'B0ZZPOOLB', 'x_amazon_sku': 'ZZT-POOL-B'})

    def _make_log(self):
        return self.env['ke.amazon.sync.log'].create({
            'name': 'ZZT sync FBA pooled', 'sync_type': 'sync_fba',
        })

    def _get_transit_location(self):
        """Una sola location di transito condivisa (come nel codice reale,
        _get_or_create_transit_location — un parametro di sistema unico),
        riutilizzata per tutte le varianti del test."""
        params = self.env['ir.config_parameter'].sudo()
        loc_id = int(params.get_param('ke_amazon_sync.transit_location_id', 0) or 0)
        location = self.env['stock.location'].browse(loc_id)
        if location.exists():
            return location
        location = self.env['stock.location'].create({
            'name': 'ZZT Transito test', 'usage': 'internal',
            'location_id': self.fba_location.location_id.id,
        })
        params.set_param('ke_amazon_sync.transit_location_id', str(location.id))
        return location

    def _seed_transit(self, variant, qty):
        transit_location = self._get_transit_location()
        self.env['stock.quant'].sudo()._update_available_quantity(variant, transit_location, qty)
        return transit_location

    def _sync(self, items):
        client = FakeSpApiClient(marketplace_id='ZZT_POOL_MKT', inventory_items=items)
        eng = self.SyncEngine(self.env, client)
        log = self._make_log()
        with no_commit(self.env):
            eng.sync_fba_quantities_page(log, page_token=None, asin_totals={})

    def test_unchanged_quantities_on_second_sync_do_not_consume_transit_again(self):
        # Transito capiente per entrambe: una lettura sbagliata del
        # precedente (dal template condiviso invece che dalla listing per
        # variante) consumerebbe comunque qualcosa da qui, osservabile.
        transit = self._seed_transit(self.variant_a, 50)
        self._seed_transit(self.variant_b, 50)

        items = [
            {'sku': 'ZZT-POOL-A', 'asin': 'B0ZZPOOLA', 'qty': 10},
            {'sku': 'ZZT-POOL-B', 'asin': 'B0ZZPOOLB', 'qty': 20},
        ]
        # Primo ciclo: nessuno storico precedente ancora, entrambe "ricevono"
        # legittimamente dal transito la loro quantità iniziale.
        self._sync(items)

        def transit_qty(variant):
            quant = self.env['stock.quant'].search([
                ('product_id', '=', variant.id), ('location_id', '=', transit.id),
            ], limit=1)
            return quant.quantity if quant else 0.0

        transit_a_after_first = transit_qty(self.variant_a)
        transit_b_after_first = transit_qty(self.variant_b)
        self.assertEqual(transit_a_after_first, 40.0, '50 - 10 ricevute')
        self.assertEqual(transit_b_after_first, 30.0, '50 - 20 ricevute')

        # Secondo ciclo: STESSI dati Amazon, nessuna nuova spedizione.
        self._sync(items)

        self.assertEqual(
            transit_qty(self.variant_a), transit_a_after_first,
            'nessun nuovo consumo dal transito di A: la giacenza Amazon non è cambiata',
        )
        self.assertEqual(
            transit_qty(self.variant_b), transit_b_after_first,
            'B non deve perdere transito per colpa dello snapshot di A (bug P1 del 2026-10-27)',
        )

    def test_listing_qty_tracked_per_variant_not_shared(self):
        self._sync([
            {'sku': 'ZZT-POOL-A', 'asin': 'B0ZZPOOLA', 'qty': 10},
            {'sku': 'ZZT-POOL-B', 'asin': 'B0ZZPOOLB', 'qty': 20},
        ])
        listing_a = self.env['ke.amazon.listing'].search([
            ('product_id', '=', self.variant_a.id), ('marketplace_id', '=', self.marketplace.id),
        ])
        listing_b = self.env['ke.amazon.listing'].search([
            ('product_id', '=', self.variant_b.id), ('marketplace_id', '=', self.marketplace.id),
        ])
        self.assertEqual(listing_a.qty, 10)
        self.assertEqual(listing_b.qty, 20)
        # Il campo legacy del template resta un riepilogo dell'ultimo
        # scritto (comportamento invariato, non è quello usato per decidere
        # il consumo transito) — qui l'ultimo processato è B.
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.x_amazon_qty, 20)
