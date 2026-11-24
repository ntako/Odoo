"""
Import del catalogo fornitore integrato con i prodotti esistenti (2026-11-20).

Con "Crea gli articoli mancanti", prima dell'arricchimento (enrich.py):
1. riga che corrisponde a un prodotto ARCHIVIATO (codice con prefisso, codice nudo come negli import di
   marzo, EAN) → riattivato e col codice giusto, mai un doppione (Odoo rifiuterebbe anche l'EAN);
2. riga senza prodotto → creata DENTRO la sua famiglia del fornitore: aggiunta come variante alla famiglia
   che c'è già in Odoo (stesso codice famiglia), oppure in una famiglia nuova insieme alle altre righe
   della stessa famiglia; una famiglia di una riga sola è un prodotto semplice.
Poi l'arricchimento riempie tutto (testi, specifiche, costo, marca, immagini) e alla fine i nomi
(ke_apply_supplier_names) per i prodotti creati o riattivati.

Famiglia di una riga: la colonna "codice famiglia"; se manca, il nome famiglia; se il file non ha
famiglie e il preset lo chiede, il nome dell'articolo senza le misure finali ("Marginal Carp X 1,50 Mt."
→ "Marginal Carp X"; Trabucco: la parte prima di "*"). Righe senza codice con un nome famiglia (righe di
intestazione, es. il fotolistino C4) valgono per le righe che seguono.
"""
import re

_UNIT_WORDS = {'mt', 'm', 'mm', 'cm', 'gr', 'g', 'kg', 'lb', 'lbs', 'pz', 'pcs', 'nr', 'n', 'misura', 'size',
               'tg', 'taglia', 'ml', 'lt', 'l', 'oz', 'xs', 's', 'xl', 'xxl', 'xxxl', 'cl', 'mis'}


def name_stem(text):
    """Nome dell'articolo senza le misure finali (famiglia ricavata dal nome)."""
    text = re.sub(r'\s+', ' ', (text or '').split('*')[0]).strip()
    text = re.sub(r'[!#]+$', '', text).strip()
    tokens = text.split(' ')
    while len(tokens) > 1 and (re.search(r'\d', tokens[-1]) or tokens[-1].lower().strip('.,') in _UNIT_WORDS):
        tokens.pop()
    return ' '.join(tokens).strip(' -,.') or text


def first_value(row, lines, value_fn):
    return next((v for v in (re.sub(r'\s+', ' ', value_fn(row, l)).strip() for l in lines) if v), '')


class RowFamilies:
    """Famiglia del fornitore di ogni riga, con le righe di intestazione che valgono per le successive."""

    def __init__(self, lines_by, options, value_fn):
        self.ref_lines = lines_by('_supplier_family_ref')
        self.name_lines = lines_by('_supplier_family_name')
        self.item_name_lines = lines_by('name') + lines_by('_supplier_name')
        self.from_name = options.get('family_from_name')
        self.value = value_fn
        self.current = ('', '')

    def of(self, row, code):
        """(codice famiglia, nome famiglia) della riga; per una riga di intestazione la ricorda e torna None."""
        ref = first_value(row, self.ref_lines, self.value)
        name = first_value(row, self.name_lines, self.value)
        if not code:
            if ref or name:
                self.current = (ref, name)
            return None
        if not ref and not name:
            ref, name = self.current
        if not ref and name:
            ref = 'N:' + name.casefold()
        if not ref and self.from_name:
            stem = name_stem(first_value(row, self.item_name_lines, self.value))
            if stem:
                ref, name = 'N:' + stem.casefold(), stem
        return ref, name


def archived_index(env, prefix, code_key, ean_key):
    """Varianti archiviate (o di prodotti archiviati): codice con prefisso, codice nudo, EAN → id."""
    env.flush_all()
    env.cr.execute("""
        SELECT pp.id, pp.default_code, pp.barcode FROM product_product pp
          JOIN product_template pt ON pt.id = pp.product_tmpl_id
         WHERE NOT (pp.active AND pt.active)""")
    by_code, by_raw, by_ean = {}, {}, {}
    prefix = (prefix or '').upper()
    for vid, code, barcode in env.cr.fetchall():
        key = code_key(code)
        if key and prefix and key.startswith(prefix):
            by_code.setdefault(key[len(prefix):], vid)
        elif key:
            by_raw.setdefault(key, vid)
        if barcode:
            by_ean.setdefault(ean_key(barcode), vid)
    return by_code, by_raw, by_ean


def revive(env, variant_ids, wanted_codes):
    """Riattiva varianti archiviate (e i loro prodotti); codice con prefisso se libero. Ritorna i prodotti."""
    Variant = env['product.product'].with_context(active_test=False)
    templates = env['product.template']
    for variant in Variant.browse(variant_ids):
        tmpl = variant.product_tmpl_id
        if not tmpl.active:
            tmpl.with_context(create_product_product=False).write({'active': True})
        if not variant.active:
            variant.active = True
        code = wanted_codes.get(variant.id)
        if code and variant.default_code != code and not env['product.product'].search_count(
                [('default_code', '=', code), ('id', '!=', variant.id)], limit=1):
            variant.default_code = code
        tmpl.message_post(body='Riattivato dall\'import del catalogo fornitore (articolo presente nel file).')
        templates |= tmpl
    return templates


def create_families(env, groups, options, brand, report, matched=None):
    """
    groups: {codice famiglia: (nome famiglia, [(valori variante, etichetta)])}. Aggiunge le varianti alla
    famiglia esistente (stesso codice famiglia del fornitore, o articoli dello stesso file abbinati sotto
    quella famiglia: 'matched' = {codice famiglia: [id varianti]}) o crea la famiglia. Ritorna i prodotti toccati.
    """
    matched = matched or {}
    Tmpl = env['product.template'].with_context(create_product_product=False)
    Variant = env['product.product']
    tag = options.get('supplier_tag') or ''
    touched = env['product.template']
    for n, (ref, (family_name, items)) in enumerate(groups.items(), 1):
        target = env['product.template']
        if ref and not ref.startswith('R:'):
            existing = Variant.search([('ke_supplier_family_ref', '=', ref),
                                       ('ke_supplier_tag', 'in', [tag, False])])
            existing |= Variant.browse(matched.get(ref, [])).filtered(lambda v: v.active and v.product_tmpl_id.active)
            if existing:
                target = max(existing.product_tmpl_id,
                             key=lambda t: (len(existing & t.product_variant_ids), -t.id))
        try:
            with env.cr.savepoint():
                added_to_existing = bool(target)
                if not target:
                    target = Tmpl.create({
                        'name': family_name or items[0][0].get('default_code'),
                        'type': 'consu', 'is_storable': True, 'list_price': 0.0,
                        'sale_ok': True, 'purchase_ok': True,
                        'ke_brand_id': brand.id if brand else False,
                        'ke_supplier_name': family_name or False,
                    })
                target.ke_family_add_variants(items)
                report['families_existing' if added_to_existing else 'families_new'] += 1
                report['variants_created'] += len(items)
                touched |= target
        except Exception as exc:   # noqa: BLE001 — una famiglia che non si crea non ferma le altre
            report['errors'].append(f'{family_name or ref}: {exc}')
        if n % 50 == 0:
            env.cr.commit()
            env.invalidate_all()
    env.cr.commit()
    return touched


def apply_names(env, templates, cr):
    """Nomi del fornitore (prodotto e varianti) ai prodotti creati o riattivati, a blocchi."""
    ids = templates.ids
    done = 0
    for i in range(0, len(ids), 100):
        done += len(env['product.template'].browse(ids[i:i + 100]).ke_apply_supplier_names())
        cr.commit()
        env.invalidate_all()
    return done
