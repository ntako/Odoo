"""Traduzione dei testi del sito con un traduttore finto (nessuna chiamata di rete)."""
import json
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_product_translate.models.translate_provider import TranslateProvider
from odoo.addons.ke_product_translate.services.product_texts import ProductTextTranslation
from odoo.addons.ke_product_translate.services.translators import DeepLTranslator, TranslatorError


class FakeTranslator:
    """Traduce aggiungendo il prefisso della lingua; conta le chiamate."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def translate(self, texts, source, target, html=False):
        if self.fail:
            raise TranslatorError('servizio giù')
        self.calls.append((tuple(texts), source, target, html))
        return [f'[{target[:2]}] {t}' for t in texts]

    def check(self):
        return 'ok'


@tagged('post_install', '-at_install')
class TestProductTranslate(TransactionCase):

    def setUp(self):
        super().setUp()
        for code in ('it_IT', 'fr_FR', 'de_DE'):
            self.env['res.lang']._activate_lang(code)
        self.tmpl = self.env['product.template'].with_context(lang='it_IT').create({
            'name': 'ZZT Mulinello Quick, titolo lungo', 'type': 'consu',
            'ke_web_meta_title': 'ZZT Mulinello Quick',
            'description_sale': 'Mulinello robusto.<ul><li>Frizione anteriore</li></ul>'})
        self.provider = self.env['ke.translate.provider'].create({'name': 'Finto', 'kind': 'libretranslate',
                                                                  'url': 'http://x'})

    def test_translates_empty_fields_and_derives_slug(self):
        fake = FakeTranslator()
        engine = ProductTextTranslation(self.env, fake, 'it_IT', 'empty', 'write')
        result = engine.translate_product(self.tmpl, ['fr_FR'])
        fr = self.tmpl.with_context(lang='fr_FR')
        self.assertEqual(fr.ke_web_meta_title, '[fr] ZZT Mulinello Quick')
        self.assertEqual(fr.ke_web_name, '[fr] ZZT Mulinello Quick, titolo lungo')
        self.assertEqual(fr.ke_web_slug, 'fr-zzt-mulinello-quick', 'URL dal nome tradotto, non tradotto')
        self.assertIn('Frizione anteriore', fr.ke_web_description)
        self.assertEqual({c[3] for c in fake.calls}, {False, True}, 'testi semplici e HTML in due chiamate')
        self.assertEqual(self.tmpl.with_context(lang='it_IT').ke_web_meta_title, 'ZZT Mulinello Quick',
                         'lingua di partenza intatta')
        self.assertIn('fr_FR', result)
        self.assertFalse(engine.translate_product(self.tmpl, ['fr_FR']), 'seconda volta: niente da fare')

    def test_keeps_own_texts_unless_all(self):
        self.tmpl.with_context(lang='fr_FR').write({'ke_web_meta_title': 'Moulinet maison'})
        ProductTextTranslation(self.env, FakeTranslator(), 'it_IT', 'empty', 'write').translate_product(
            self.tmpl, ['fr_FR'])
        self.assertEqual(self.tmpl.with_context(lang='fr_FR').ke_web_meta_title, 'Moulinet maison')
        ProductTextTranslation(self.env, FakeTranslator(), 'it_IT', 'all', 'write').translate_product(
            self.tmpl, ['fr_FR'])
        self.assertEqual(self.tmpl.with_context(lang='fr_FR').ke_web_meta_title, '[fr] ZZT Mulinello Quick')

    def test_seo_limits_after_translation(self):
        long = 'Parola ' * 40
        self.tmpl.with_context(lang='it_IT').write({'ke_web_meta_title': long, 'ke_web_meta_description': long})
        ProductTextTranslation(self.env, FakeTranslator(), 'it_IT', 'all', 'write').translate_product(
            self.tmpl, ['de_DE'])
        de = self.tmpl.with_context(lang='de_DE')
        self.assertLessEqual(len(de.ke_web_meta_title), 60)
        self.assertLessEqual(len(de.ke_web_meta_description), 160)

    def test_proposals_accept_and_replace(self):
        engine = ProductTextTranslation(self.env, FakeTranslator(), 'it_IT', 'empty', 'proposal', provider=self.provider)
        engine.translate_product(self.tmpl, ['fr_FR'])
        Proposal = self.env['ke.translate.proposal']
        pending = Proposal.search([('product_tmpl_id', '=', self.tmpl.id), ('state', '=', 'pending')])
        self.assertTrue(pending)
        self.assertFalse(self.tmpl._ke_own(self.tmpl._ke_raw(['ke_web_meta_title'])['ke_web_meta_title'],
                                           'fr_FR', 'it_IT'), 'proposte: niente scritto')
        engine.translate_product(self.tmpl, ['fr_FR'])
        self.assertEqual(len(Proposal.search([('product_tmpl_id', '=', self.tmpl.id), ('state', '=', 'pending')])),
                         len(pending), 'la nuova proposta sostituisce quella in attesa')
        title = Proposal.search([('product_tmpl_id', '=', self.tmpl.id), ('field_name', '=', 'ke_web_meta_title'),
                                 ('state', '=', 'pending')])
        title.new_value = 'Moulinet Quick corrigé'
        title.action_accept()
        self.assertEqual(self.tmpl.with_context(lang='fr_FR').ke_web_meta_title, 'Moulinet Quick corrigé')
        self.assertEqual(title.state, 'accepted')

    def test_job_runs_in_turns_and_stops_on_errors(self):
        tmpls = self.tmpl | self.env['product.template'].with_context(lang='it_IT').create(
            [{'name': f'ZZT prodotto {i}', 'type': 'consu', 'description_sale': f'Testo {i}'} for i in range(2)])
        job = self.env['ke.translate.job'].enqueue({'name': 't', 'provider_id': self.provider.id,
                                                    'source_lang': 'it_IT', 'target_langs': 'fr_FR,de_DE'}, tmpls)
        with patch.object(self.env.cr, 'commit', lambda: None), \
                patch.object(TranslateProvider, '_translator', lambda self: FakeTranslator()):
            job._run_turn(budget=-1)
            self.assertEqual((job.state, job.done_count), ('queued', 1), 'turno finito: si riaccoda')
            job._run_turn()
        self.assertEqual((job.state, job.done_count, job.translated_count), ('done', 3, 3))
        self.assertIn('[FINE]', job.log_lines)
        job2 = self.env['ke.translate.job'].enqueue({'name': 't2', 'provider_id': self.provider.id,
                                                     'source_lang': 'it_IT', 'target_langs': 'fr_FR', 'scope': 'all'},
                                                    tmpls | tmpls[0].copy() | tmpls[1].copy() | tmpls[2].copy())
        with patch.object(self.env.cr, 'commit', lambda: None), \
                patch.object(TranslateProvider, '_translator', lambda self: FakeTranslator(fail=True)):
            job2._run_turn()
        self.assertEqual(job2.state, 'failed', '5 errori di fila: si ferma')
        self.assertEqual(len(json.loads(job2.remaining_ids)), 2, 'il prodotto fallito resta da fare')

    def test_wizard_small_selection_runs_directly(self):
        wiz = self.env['ke.translate.wizard'].with_context(
            active_model='product.template', active_ids=self.tmpl.ids).create(
            {'provider_id': self.provider.id, 'apply': 'write'})
        self.assertNotIn('en_US', wiz.target_lang_ids.mapped('code'))
        self.assertNotIn('it_IT', wiz.target_lang_ids.mapped('code'))
        with patch.object(TranslateProvider, '_translator', lambda self: FakeTranslator()):
            wiz.action_translate()
        self.assertTrue(self.tmpl.with_context(lang='de_DE').ke_web_meta_title.startswith('[de]'))

    def test_code_like_names_are_copied(self):
        self.tmpl.with_context(lang='it_IT').write({'ke_web_name': '3D NEEDLEFISH PULSET 18CM',
                                                    'ke_web_meta_title': '3D NEEDLEFISH PULSET | Savage Gear'})
        fake = FakeTranslator()
        ProductTextTranslation(self.env, fake, 'it_IT', 'all', 'write').translate_product(self.tmpl, ['fr_FR'])
        fr = self.tmpl.with_context(lang='fr_FR')
        self.assertEqual(fr.ke_web_name, '3D NEEDLEFISH PULSET 18CM')
        self.assertEqual(fr.ke_web_meta_title, '3D NEEDLEFISH PULSET | Savage Gear')
        self.assertEqual(fr.ke_web_slug, '3d-needlefish-pulset-18cm')
        self.assertTrue(fr.ke_web_meta_description.startswith('[fr]'), 'le descrizioni si traducono')
        self.assertFalse(any('NEEDLEFISH' in t for call in fake.calls for t in call[0]))

    def test_deepl_codes_and_endpoint(self):
        self.assertEqual(DeepLTranslator('abc:fx').url, 'https://api-free.deepl.com')
        self.assertEqual(DeepLTranslator('abc').url, 'https://api.deepl.com')
        d = DeepLTranslator('abc:fx')
        self.assertEqual((d._target('en_GB'), d._target('fr_FR'), d._target('en_US')), ('EN-GB', 'FR', 'EN-US'))
