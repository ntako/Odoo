"""
Traduzione dei testi del sito di un prodotto (2026-11-13).

Fonte: i testi del sito nella lingua di partenza come li manda l'export
(_ke_web_texts: campi della scheda Sito web, altrimenti la proposta dal nome
e dalla descrizione). Destinazione: i campi ke_web_* nelle lingue di arrivo.
L'URL non si traduce: si ricava dal nome tradotto.

scope 'empty': solo i campi che nella lingua di arrivo non hanno un testo
proprio (vuoti o copia della lingua di partenza); 'all': tutti.
apply 'write': scrive; 'proposal': crea proposte da approvare.
"""
from odoo.addons.ke_product_catalog.models.product_website import (
    META_DESCRIPTION_MAX, META_TITLE_MAX, _cut, _plain, _summary, short_name, slugify)

# (campo Odoo, chiave di _ke_web_texts, HTML)
TEXT_FIELDS = [
    ('ke_web_name', 'name', False),
    ('ke_web_meta_title', 'meta_title', False),
    ('ke_web_meta_description', 'meta_description', False),
    ('ke_web_short_description', 'short_description', True),
    ('ke_web_description', 'description', True),
]
SLUG_FIELD = 'ke_web_slug'
# Nome e meta title: se sono (quasi) tutti in maiuscolo sono codici e modelli
# del fornitore ("3D NEEDLEFISH PULSET 18CM"), che un traduttore classico
# stravolge ("3D NÉCESSAIRE POULET", prova del 2026-11-13): si copiano.
NAME_FIELDS = ('ke_web_name', 'ke_web_meta_title')


def looks_like_code(text):
    """Testo fatto di sigle e modelli: almeno l'80% delle lettere in maiuscolo."""
    letters = [c for c in (text or '').split(' | ')[0] if c.isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) >= 0.8
ALL_FIELDS = [f for f, _k, _h in TEXT_FIELDS] + [SLUG_FIELD]


class ProductTextTranslation:

    def __init__(self, env, translator, source_lang, scope='empty', apply='write', provider=None, job=None):
        self.env = env
        self.translator = translator
        self.source = source_lang
        self.scope = scope
        self.apply = apply
        self.provider = provider
        self.job = job
        self.chars = 0

    def _todo(self, tmpl, raw, lang):
        """Campi da tradurre in 'lang'."""
        if self.scope == 'all':
            return set(ALL_FIELDS)
        return {f for f in ALL_FIELDS if not tmpl._ke_own(raw.get(f), lang, self.source)}

    @staticmethod
    def _fit(field, value):
        """Limiti SEO dopo la traduzione (un testo tradotto può allungarsi)."""
        if field == 'ke_web_meta_title':
            return _cut(value, META_TITLE_MAX)
        if field == 'ke_web_meta_description':
            return _summary(value, META_DESCRIPTION_MAX)
        return value

    def translate_product(self, tmpl, target_langs):
        """
        Traduce un prodotto nelle lingue indicate. Ritorna {lingua: valori}
        (solo i campi tradotti); scrive o crea le proposte secondo 'apply'.
        """
        tmpl.ensure_one()
        src = tmpl.with_context(lang=self.source)._ke_web_texts()
        raw = tmpl._ke_raw(ALL_FIELDS)
        result = {}
        for lang in target_langs:
            if lang == self.source:
                continue
            todo = self._todo(tmpl, raw, lang)
            plain = [(f, src[k]) for f, k, html in TEXT_FIELDS if f in todo and not html and _plain(src[k])]
            vals = {f: v for f, v in plain if f in NAME_FIELDS and looks_like_code(v)}
            plain = [(f, v) for f, v in plain if f not in vals]
            rich = [(f, src[k]) for f, k, html in TEXT_FIELDS if f in todo and html and _plain(src[k])]
            if not plain and not rich and not vals and SLUG_FIELD not in todo:
                continue
            for group, html in ((plain, False), (rich, True)):
                if not group:
                    continue
                texts = [v for _f, v in group]
                self.chars += sum(len(t) for t in texts)
                out = self.translator.translate(texts, self.source, lang, html=html)
                for (field, _src), value in zip(group, out):
                    value = self._fit(field, (value or '').strip())
                    if _plain(value):
                        vals[field] = value
            if SLUG_FIELD in todo:
                name = vals.get('ke_web_name') or (tmpl.with_context(lang=lang).ke_web_name
                                                   if tmpl._ke_own(raw.get('ke_web_name'), lang, self.source) else '')
                if name:
                    vals[SLUG_FIELD] = slugify(short_name(name))
            if not vals:
                continue
            result[lang] = vals
            if self.apply == 'proposal':
                self._propose(tmpl, raw, lang, vals, src)
            else:
                tmpl.with_context(lang=lang, mail_notrack=True).write(vals)
        return result

    def _propose(self, tmpl, raw, lang, vals, src):
        Proposal = self.env['ke.translate.proposal'].sudo()
        # Una sola proposta in attesa per prodotto, lingua e campo: la nuova sostituisce la vecchia.
        Proposal.search([('product_tmpl_id', '=', tmpl.id), ('lang', '=', lang),
                         ('field_name', 'in', list(vals)), ('state', '=', 'pending')]).unlink()
        source_by_field = {f: src.get(k, '') for f, k, _h in TEXT_FIELDS}
        Proposal.create([{
            'product_tmpl_id': tmpl.id,
            'lang': lang,
            'field_name': field,
            'current_value': str((raw.get(field) or {}).get(lang) or ''),
            'source_value': source_by_field.get(field, ''),
            'new_value': value,
            'operation': 'translate',
            'provider_id': self.provider.id if self.provider else False,
            'job_id': self.job.id if self.job else False,
        } for field, value in vals.items()])
