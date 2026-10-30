"""
Test per SyncEngine.recover_renamed_family_parent_asin — recupero delle
famiglie che Amazon ha solo rinominato (stesso gruppo di varianti, nuovo
parent ASIN) durante un Full Import già eseguito. Nessuna chiamata Amazon:
il metodo rilegge solo il testo già salvato di ke.amazon.sync.log.log_lines,
quindi i log fittizi qui sotto riproducono ESATTAMENTE il formato scritto da
_process_variant_listing (vedi sync_engine.py, ramo "conflitto: parent_asin
diverso") — se quel testo cambia, va aggiornato anche qui.
"""
from .common import no_commit

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine


def _conflict_line(sku, asin, family_name, family_id, old_asin, new_asin):
    return (
        f'SALTATO (conflitto): {sku} / {asin} risulta già nella famiglia '
        f'"{family_name}" (id={family_id}, parent_asin={old_asin}) ma questa riga '
        f'indica un parent_asin diverso ({new_asin}) — probabile ASIN non '
        f'aggiornato nel report Amazon, serve verifica manuale'
    )


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestRecoverRenamedFamilies(TransactionCase):

    def setUp(self):
        super().setUp()
        self.engine = SyncEngine(self.env, None)

    def _make_source_log(self, lines):
        return self.env['ke.amazon.sync.log'].create({
            'name': 'ZZT Full Import test', 'sync_type': 'full_import', 'state': 'done',
            'log_lines': '\n'.join(lines),
        })

    def _make_recovery_log(self, source_log):
        return self.env['ke.amazon.sync.log'].create({
            'name': 'ZZT Recupero test', 'sync_type': 'recover_renamed_families',
            'state': 'running', 'parent_log_id': source_log.id,
        })

    def test_pure_rename_is_fixed(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia rinominata', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'OLDPARENT01',
        })
        lines = [
            _conflict_line('SKU-A', 'CHILDA', tmpl.name, tmpl.id, 'OLDPARENT01', 'NEWPARENT01'),
            _conflict_line('SKU-B', 'CHILDB', tmpl.name, tmpl.id, 'OLDPARENT01', 'NEWPARENT01'),
        ]
        source_log = self._make_source_log(lines)
        log_rec = self._make_recovery_log(source_log)
        with no_commit(self.env):
            fixed, skipped_other, skipped_collision = self.engine.recover_renamed_family_parent_asin(
                log_rec, source_log.id,
            )
        self.assertEqual((fixed, skipped_other, skipped_collision), (1, 0, 0))
        self.assertEqual(tmpl.x_amazon_parent_asin, 'NEWPARENT01')
        self.assertEqual(log_rec.state, 'done')

    def test_split_family_is_not_touched(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia divisa', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'OLDPARENT02',
        })
        lines = [
            _conflict_line('SKU-C', 'CHILDC', tmpl.name, tmpl.id, 'OLDPARENT02', 'NEWPARENT02A'),
            _conflict_line('SKU-D', 'CHILDD', tmpl.name, tmpl.id, 'OLDPARENT02', 'NEWPARENT02B'),
        ]
        source_log = self._make_source_log(lines)
        log_rec = self._make_recovery_log(source_log)
        with no_commit(self.env):
            fixed, skipped_other, skipped_collision = self.engine.recover_renamed_family_parent_asin(
                log_rec, source_log.id,
            )
        self.assertEqual((fixed, skipped_other, skipped_collision), (0, 1, 0))
        self.assertEqual(tmpl.x_amazon_parent_asin, 'OLDPARENT02', 'una famiglia divisa non va toccata')

    def test_merged_families_are_not_touched(self):
        tmpl1 = self.env['product.template'].create({
            'name': 'ZZT Famiglia unita 1', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'OLDPARENT03',
        })
        tmpl2 = self.env['product.template'].create({
            'name': 'ZZT Famiglia unita 2', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'OLDPARENT04',
        })
        lines = [
            _conflict_line('SKU-E', 'CHILDE', tmpl1.name, tmpl1.id, 'OLDPARENT03', 'NEWPARENTSHARED'),
            _conflict_line('SKU-F', 'CHILDF', tmpl2.name, tmpl2.id, 'OLDPARENT04', 'NEWPARENTSHARED'),
        ]
        source_log = self._make_source_log(lines)
        log_rec = self._make_recovery_log(source_log)
        with no_commit(self.env):
            fixed, skipped_other, skipped_collision = self.engine.recover_renamed_family_parent_asin(
                log_rec, source_log.id,
            )
        self.assertEqual((fixed, skipped_other, skipped_collision), (0, 2, 0))
        self.assertEqual(tmpl1.x_amazon_parent_asin, 'OLDPARENT03')
        self.assertEqual(tmpl2.x_amazon_parent_asin, 'OLDPARENT04')

    def test_collision_with_existing_template_is_not_touched(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia da rinominare', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'OLDPARENT05',
        })
        # Un'altra famiglia, creata per errore durante l'import stesso, già
        # occupa il nuovo parent_asin.
        duplicate = self.env['product.template'].create({
            'name': 'ZZT Famiglia duplicata per errore', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'NEWPARENT05',
        })
        lines = [_conflict_line('SKU-G', 'CHILDG', tmpl.name, tmpl.id, 'OLDPARENT05', 'NEWPARENT05')]
        source_log = self._make_source_log(lines)
        log_rec = self._make_recovery_log(source_log)
        with no_commit(self.env):
            fixed, skipped_other, skipped_collision = self.engine.recover_renamed_family_parent_asin(
                log_rec, source_log.id,
            )
        self.assertEqual((fixed, skipped_other, skipped_collision), (0, 0, 1))
        self.assertEqual(tmpl.x_amazon_parent_asin, 'OLDPARENT05')
        self.assertEqual(duplicate.x_amazon_parent_asin, 'NEWPARENT05')

    def test_already_changed_parent_asin_is_not_overwritten(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia già aggiornata', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'SOMETHINGELSE',
        })
        lines = [_conflict_line('SKU-H', 'CHILDH', tmpl.name, tmpl.id, 'OLDPARENT06', 'NEWPARENT06')]
        source_log = self._make_source_log(lines)
        log_rec = self._make_recovery_log(source_log)
        with no_commit(self.env):
            fixed, skipped_other, skipped_collision = self.engine.recover_renamed_family_parent_asin(
                log_rec, source_log.id,
            )
        self.assertEqual((fixed, skipped_other, skipped_collision), (0, 0, 0))
        self.assertEqual(tmpl.x_amazon_parent_asin, 'SOMETHINGELSE')

    def test_no_source_log_sets_error_state(self):
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': 'ZZT Recupero senza fonte', 'sync_type': 'recover_renamed_families', 'state': 'running',
        })
        with no_commit(self.env):
            fixed, skipped_other, skipped_collision = self.engine.recover_renamed_family_parent_asin(
                log_rec, source_log_id=999999,
            )
        self.assertEqual((fixed, skipped_other, skipped_collision), (0, 0, 0))
        self.assertEqual(log_rec.state, 'error')
