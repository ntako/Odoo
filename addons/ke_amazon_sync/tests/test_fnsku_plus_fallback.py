"""
Test dell'abbinamento SKU → prodotto nell'Import FNSKU
(process_fnsku_report_rows, _find_fnsku_variant).

Il report FBA può contenere sia la riga storica ('PFS-1234') sia quella
nuova dopo un rifornimento ('PFS-1234+'). Prima il fallback con/senza '+'
mandava entrambe sullo stesso prodotto e vinceva l'ultima elaborata, a
seconda dell'ordine del report. Ora il fallback si applica solo se univoco
e se lo SKU alternativo non ha una propria riga nel report; uno SKU
presente su più prodotti viene saltato invece di sceglierne uno.

SyncEngine(env, None): nessun marketplace corrente, ramo "pooled".
"""
from odoo.tests.common import TransactionCase, tagged

from .common import no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFnskuPlusFallback(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.engine = SyncEngine(self.env, None)
        self.log = self.env['ke.amazon.sync.log'].create({
            'name': 'ZZT import FNSKU +', 'sync_type': 'import_fnsku',
        })

    def _simple(self, sku, name='ZZT semplice +'):
        return self.env['product.template'].create({
            'name': name, 'type': 'consu', 'is_storable': True, 'x_amazon_sku': sku,
        })

    def _process(self, rows):
        with no_commit(self.env):
            return self.engine.process_fnsku_report_rows(self.log, rows)

    def test_unique_fallback_still_matches(self):
        """Solo la riga nuova nel report, prodotto non ancora riconciliato:
        il fallback '+' univoco continua ad abbinare (convenzione esistente)."""
        tmpl = self._simple('ZZT-PLUS-1')
        result = self._process([{'sku': 'ZZT-PLUS-1+', 'fnsku': 'X00NEW1'}])
        self.assertEqual(result, (1, 0, 0))
        self.assertEqual(tmpl.x_amazon_fnsku, 'X00NEW1')

    def test_historical_and_new_rows_product_on_new_sku(self):
        """Prodotto già sullo SKU nuovo: vince la riga esatta, qualunque sia
        l'ordine del report; la riga storica viene saltata."""
        tmpl = self._simple('ZZT-PLUS-2+')
        for rows in (
            [{'sku': 'ZZT-PLUS-2+', 'fnsku': 'X00NEW2'}, {'sku': 'ZZT-PLUS-2', 'fnsku': 'X00OLD2'}],
            [{'sku': 'ZZT-PLUS-2', 'fnsku': 'X00OLD2'}, {'sku': 'ZZT-PLUS-2+', 'fnsku': 'X00NEW2'}],
        ):
            tmpl.x_amazon_fnsku = False
            self.assertEqual(self._process(rows), (1, 1, 0))
            self.assertEqual(tmpl.x_amazon_fnsku, 'X00NEW2')
        self.assertIn('SALTATO ZZT-PLUS-2 ', self.log.log_lines)

    def test_historical_and_new_rows_product_on_old_sku(self):
        """Prodotto ancora sullo SKU storico: riceve l'FNSKU coerente con il
        proprio SKU, non quello della riga nuova."""
        tmpl = self._simple('ZZT-PLUS-3')
        result = self._process([
            {'sku': 'ZZT-PLUS-3', 'fnsku': 'X00OLD3'},
            {'sku': 'ZZT-PLUS-3+', 'fnsku': 'X00NEW3'},
        ])
        self.assertEqual(result, (1, 1, 0))
        self.assertEqual(tmpl.x_amazon_fnsku, 'X00OLD3')

    def test_ambiguous_fallback_is_skipped(self):
        """Fallback '+' che porta a due prodotti: nessuno dei due riceve
        l'FNSKU."""
        tmpl_a = self._simple('ZZT-PLUS-4+', name='ZZT A')
        tmpl_b = self._simple('ZZT-PLUS-4+', name='ZZT B')
        result = self._process([{'sku': 'ZZT-PLUS-4', 'fnsku': 'X00AMB4'}])
        self.assertEqual(result, (0, 1, 0))
        self.assertFalse(tmpl_a.x_amazon_fnsku)
        self.assertFalse(tmpl_b.x_amazon_fnsku)
        self.assertIn('SALTATO ZZT-PLUS-4 (abbinamento ambiguo', self.log.log_lines)

    def test_ambiguous_exact_sku_is_skipped(self):
        """Stesso SKU esatto su due prodotti: prima limit=1 ne sceglieva uno
        a caso, ora si salta."""
        tmpl_a = self._simple('ZZT-PLUS-5', name='ZZT A')
        tmpl_b = self._simple('ZZT-PLUS-5', name='ZZT B')
        result = self._process([{'sku': 'ZZT-PLUS-5', 'fnsku': 'X00AMB5'}])
        self.assertEqual(result, (0, 1, 0))
        self.assertFalse(tmpl_a.x_amazon_fnsku)
        self.assertFalse(tmpl_b.x_amazon_fnsku)

    def test_multi_variant_exact_sku_blocks_fallback(self):
        """SKU esatto su un template con più varianti (non risolvibile) e
        SKU con '+' su un altro prodotto assente dal report: niente
        fallback, nessuno dei due riceve l'FNSKU."""
        attr = self.env['product.attribute'].create({
            'name': 'ZZT Taglia + multi',
            'value_ids': [(0, 0, {'name': 'S'}), (0, 0, {'name': 'M'})],
        })
        family = self.env['product.template'].create({
            'name': 'ZZT famiglia + multi', 'type': 'consu', 'is_storable': True,
            'x_amazon_sku': 'ZZT-PLUS-7',
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)],
            })],
        })
        other = self._simple('ZZT-PLUS-7+', name='ZZT altro prodotto')
        result = self._process([{'sku': 'ZZT-PLUS-7', 'fnsku': 'X00FAM7'}])
        self.assertEqual(result, (0, 1, 0))
        self.assertFalse(family.x_amazon_fnsku)
        self.assertFalse(any(family.product_variant_ids.mapped('x_amazon_fnsku')))
        self.assertFalse(other.x_amazon_fnsku)

    def test_unique_exact_variant_match(self):
        """Match esatto univoco su una variante di famiglia: invariato."""
        attr = self.env['product.attribute'].create({
            'name': 'ZZT Taglia +',
            'value_ids': [(0, 0, {'name': 'S'}), (0, 0, {'name': 'M'})],
        })
        tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia +', 'type': 'consu', 'is_storable': True,
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)],
            })],
        })
        small, medium = tmpl.product_variant_ids
        small.write({'x_amazon_sku': 'ZZT-PLUS-6S'})
        medium.write({'x_amazon_sku': 'ZZT-PLUS-6M'})
        result = self._process([{'sku': 'ZZT-PLUS-6S', 'fnsku': 'X00SMALL'}])
        self.assertEqual(result, (1, 0, 0))
        self.assertEqual(small.x_amazon_fnsku, 'X00SMALL')
        self.assertFalse(medium.x_amazon_fnsku)
