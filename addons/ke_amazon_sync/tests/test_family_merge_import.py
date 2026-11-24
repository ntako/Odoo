"""
Famiglie riorganizzate in Odoo dal catalogo del fornitore (2026-11-20) e Full Import successivo:
nessun doppione, nessuna famiglia ricreata, le nuove varianti delle famiglie Amazon unite arrivano
nella famiglia Odoo.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install')
class TestFamilyMergeImport(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine, _MatchMaps
        self.SyncEngine, self.MatchMaps = SyncEngine, _MatchMaps
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.env['product.product'].search([('ke_supplier_family_ref', '!=', False)]).write(
            {'ke_supplier_family_ref': False})
        # A, B: prodotti semplici su Amazon; C, D: famiglia Amazon P2; E: famiglia Amazon P1 (una variante).
        self.parents = {'B0ZZFMC001': 'B0ZZFMP002', 'B0ZZFMD001': 'B0ZZFMP002', 'B0ZZFME001': 'B0ZZFMP001'}
        self.rows = [make_report_row(sku, asin=asin) for sku, asin in (
            ('ZZFM-A', 'B0ZZFMA001'), ('ZZFM-B', 'B0ZZFMB001'), ('ZZFM-C', 'B0ZZFMC001'),
            ('ZZFM-D', 'B0ZZFMD001'), ('ZZFM-E', 'B0ZZFME001'))]
        self._import(self.rows)
        self.variants = self.env['product.product'].search([('default_code', 'in', [
            'ZZFM-A', 'ZZFM-B', 'ZZFM-C', 'ZZFM-D', 'ZZFM-E'])])
        self.assertEqual(len(self.variants.product_tmpl_id), 4, 'A, B, famiglia P2, famiglia P1')
        self.variants.write({'ke_supplier_family_ref': 'WHIP8', 'ke_supplier_family_name': 'Whiplash 8'})

    def _import(self, rows):
        eng = self.SyncEngine(self.env, FakeSpApiClient(self.parents))
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZFM import', 'sync_type': 'full_import'})
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, 50, True)
        return log

    def _apply(self):
        Proposal = self.env['ke.product.family.proposal']
        Proposal.propose()
        prop = Proposal.search([('family_ref', '=', 'WHIP8'), ('state', '=', 'pending')])
        self.assertEqual(prop.kind, 'merge')
        prop.action_apply()
        self.assertEqual(prop.state, 'applied')
        return prop.target_tmpl_id

    def test_merge_then_full_import(self):
        p2_family = self.variants.filtered(lambda v: v.default_code == 'ZZFM-C').product_tmpl_id
        p1_family = self.variants.filtered(lambda v: v.default_code == 'ZZFM-E').product_tmpl_id
        simple_a = self.variants.filtered(lambda v: v.default_code == 'ZZFM-A').product_tmpl_id
        target = self._apply()
        self.assertEqual(target, p2_family, 'destinazione: la famiglia con più varianti')
        self.assertEqual(target.product_variant_ids, self.variants)
        self.assertFalse(p1_family.active or simple_a.active, 'prodotti svuotati archiviati')
        self.assertEqual(target.x_amazon_parent_asin, 'B0ZZFMP002')
        self.assertEqual(target._ke_alias_list(), ['B0ZZFMP001'])
        variant_a = self.variants.filtered(lambda v: v.default_code == 'ZZFM-A')
        self.assertEqual((variant_a.x_amazon_sku, variant_a.x_amazon_asin), ('ZZFM-A', 'B0ZZFMA001'),
                         'SKU e ASIN del semplice passano alla variante')

        # Full Import di nuovo, con due varianti nuove: una della famiglia Amazon P2, una di P1 (unita).
        self.parents.update({'B0ZZFMF001': 'B0ZZFMP002', 'B0ZZFMG001': 'B0ZZFMP001'})
        Tmpl = self.env['product.template']
        before = Tmpl.search_count([])
        log = self._import(self.rows + [
            make_report_row('ZZFM-F', asin='B0ZZFMF001'), make_report_row('ZZFM-G', asin='B0ZZFMG001')])
        self.assertEqual(log.products_error, 0, log.log_lines)
        self.assertEqual(Tmpl.search_count([]), before, 'nessun prodotto nuovo, nessuna famiglia ricreata')
        new = self.env['product.product'].search([('default_code', 'in', ('ZZFM-F', 'ZZFM-G'))])
        self.assertEqual(new.product_tmpl_id, target, 'le nuove varianti arrivano nella famiglia Odoo')
        self.assertEqual(target.product_variant_ids, self.variants | new)
        self.assertEqual(log.products_created, 2, log.log_lines)
        self.assertFalse(simple_a.active or p1_family.active, 'niente riattivato')

    def test_family_of_simple_products_gets_internal_parent(self):
        simples = self.variants.filtered(lambda v: v.default_code in ('ZZFM-A', 'ZZFM-B'))
        (self.variants - simples).write({'ke_supplier_family_ref': 'OTHER'})
        Proposal = self.env['ke.product.family.proposal']
        Proposal.propose()
        prop = Proposal.search([('family_ref', '=', 'WHIP8'), ('state', '=', 'pending')])
        prop.action_apply()
        target = prop.target_tmpl_id
        self.assertTrue(target.x_amazon_parent_asin.startswith('ODOO-FAM-'))
        maps = self.MatchMaps(self.env, self.SyncEngine(self.env, None)._build_barcode_map())
        maps.add_listing_fallback(self.env['ke.amazon.listing'].search([('product_id', 'in', simples.ids)]))
        self.assertFalse(maps.find_simple('ZZFM-B', 'B0ZZFMB001', None),
                         'mai trattata da prodotto semplice (la sync le scriverebbe sopra)')
        self.assertEqual(maps.find_known_family('ZZFM-B'), target)

        before = self.env['product.template'].search_count([])
        log = self._import(self.rows)
        self.assertEqual(log.products_error, 0, log.log_lines)
        self.assertEqual(self.env['product.template'].search_count([]), before)
        self.assertEqual(target.product_variant_ids, simples)
        self.assertTrue(target.x_amazon_parent_asin.startswith('ODOO-FAM-'), 'non "promossa" col nome Amazon')
