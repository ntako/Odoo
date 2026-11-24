"""
Arricchimento dei prodotti esistenti da un catalogo fornitore (2026-11-17).

Modalità "Solo arricchimento" del lavoro di import: non crea prodotti. Ogni
riga del file si abbina ai prodotti già in Odoo — codice del fornitore nel
codice articolo con il prefisso del preset (es. SAP 1636736 = PFS-1636736,
anche con "+"), poi EAN senza zeri iniziali — e scrive, secondo il mapping:
- testo e punti elenco del fornitore per lingua (ke_supplier_copy /
  ke_supplier_bullets di ke_product_catalog: la descrizione di vendita resta
  com'è, il sito usa questi testi dove la lingua li ha);
- specifiche tecniche con fonte "Fornitore" (spec_rules.parse_spec: unità,
  conversioni e valori fissi li decide la specifica; mai sopra i valori
  scritti a mano, prima di quelli Amazon); nelle famiglie per variante;
- categoria e codice del fornitore;
- famiglia del fornitore per variante (codice e nome, es. Pure Fishing "Master
  code" / "Master Description"; 2026-11-20): base delle proposte di famiglia
  di ke_product_catalog;
- peso e dimensioni solo se il prodotto non li ha.
"Solo verifica" conta senza scrivere. Il riepilogo va nel log del lavoro.
"""
import csv
import io
import logging
import re

from odoo import fields

from odoo.addons.ke_product_catalog.spec_rules import SPEC_KEYS, parse_spec, render, render_own
from odoo.addons.ke_product_catalog.models.product_supplier_texts import _split_bullets

from .catalog_create import RowFamilies, apply_names, archived_index, create_families, first_value, revive

_logger = logging.getLogger(__name__)
_COMMIT_EVERY = 200


def code_key(code):
    return (code or '').strip().rstrip('+.').strip().upper()


def ean_key(ean):
    text = str(ean or '').strip()
    if re.fullmatch(r'\d+\.0+', text):      # EAN letto da Excel come numero ("8055400196916.0")
        text = text.split('.')[0]
    return re.sub(r'\D', '', text).lstrip('0')


def _supplier_index(env, tag):
    """Codice fornitore (della variante, per questo fornitore) → varianti attive."""
    out = {}
    if not tag:
        return out
    for v in env['product.product'].search_read([('ke_supplier_tag', '=', tag), ('ke_supplier_code', '!=', False)],
                                                ['ke_supplier_code']):
        out.setdefault(v['ke_supplier_code'].strip().upper(), []).append(v['id'])
    return out


def _price(text):
    """Prezzo da testo ("10,50", "1.234,50", "10.200000000000001", "€ 9,9"); None se non è un numero."""
    text = re.sub(r'[^\d,.-]', '', str(text or ''))
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        return round(float(text), 4) if text else None
    except ValueError:
        return None


def _value(row, line):
    return (row.get(line['column']) if line['column'] else '') or line['default'] or ''


def _variant_index(env, prefix):
    """Codice del fornitore → varianti; EAN → varianti. Solo prodotti attivi: gli archiviati non si toccano."""
    by_code, by_ean = {}, {}
    prefix = (prefix or '').upper()
    for v in env['product.product'].search_read([], ['default_code', 'barcode']):
        code = code_key(v['default_code'])
        if code and code.startswith(prefix):
            by_code.setdefault(code[len(prefix):], []).append(v['id'])
        if v['barcode']:
            by_ean.setdefault(ean_key(v['barcode']), []).append(v['id'])
    return by_code, by_ean


def do_enrich(job, env, file_string, options, cr, cancel_event=None):
    from .import_job import _parse_linear_to_m, _parse_weight
    from odoo.addons.ke_product_catalog.models.product_family_move import QUIET
    env = env(context=dict(env.context, **QUIET))   # niente tracciamento nel chatter a ogni scrittura
    mapping = options['enrich_mapping']
    dry_run = options.get('dry_run', True)
    text_lang = options.get('spec_text_lang') or 'en_GB'
    lines_by = lambda target: [l for l in mapping if l['target'] == target]
    code_lines, ean_lines = lines_by('default_code'), lines_by('barcode')
    prefix_raw = (options.get('code_prefix') or '').strip()
    suffix = (options.get('code_suffix') or '').strip().upper()
    tag = options.get('supplier_tag') or prefix_raw

    def file_code(row):
        return next((v for v in (_value(row, l).strip() for l in code_lines) if v), '')

    def match_key(raw):
        key = code_key(raw)
        return key[:-len(suffix)] if suffix and key.endswith(suffix) and len(key) > len(suffix) else key

    def row_ean(row):
        return next((e for e in (ean_key(_value(row, l)) for l in ean_lines) if e), '')

    rows = list(csv.DictReader(io.StringIO(file_string), delimiter=options['delimiter']))
    job.write({'total_count': len(rows)})
    cr.commit()

    by_code, by_ean = _variant_index(env, options.get('code_prefix'))
    by_supplier = _supplier_index(env, tag)

    def match(row):
        """(codice del file, varianti attive, abbinata per codice): codice fornitore, codice con prefisso, EAN."""
        raw = file_code(row)
        ids = by_supplier.get(raw.upper()) if raw else None
        if not ids:
            key = match_key(raw)
            ids = by_code.get(key) if key else None
        by_code_match = bool(ids)
        if not ids and options.get('match_ean'):
            ean = row_ean(row)
            ids = by_ean.get(ean) if ean else None
        return raw, ids, by_code_match

    created, touched = None, env['product.template']
    if options.get('create_missing'):
        created, touched = _create_missing(env, rows, options, match, match_key, row_ean, lines_by, by_ean,
                                           dry_run, tag)
        if not dry_run:
            by_code, by_ean = _variant_index(env, options.get('code_prefix'))
            by_supplier = _supplier_index(env, tag)
    Variant = env['product.product'].with_context(active_test=False)
    brand_lines = lines_by('_brand')
    cost_lines = lines_by('standard_price')
    media_lines = lines_by('_media_url')
    Brand = env['ke.product.brand']
    brands = {}
    row_families = RowFamilies(lines_by, options, _value)
    site_langs = set(env['res.lang'].search([('active', '=', True)]).mapped('code'))
    lang_order = sorted(site_langs, key=lambda l: (l == 'en_US', l))   # en_US per ultima

    spec_lines = {}
    for line in lines_by('_spec'):
        if line['spec']:
            spec_lines.setdefault(line['spec'], []).append(line)
    keys = {}
    if spec_lines and not dry_run:
        Key = env['ke.product.spec.key'].sudo()
        keys = {c: Key._ensure(c, SPEC_KEYS[c][1], SPEC_KEYS[c][0]) if c in SPEC_KEYS
                else Key.search([('code', '=', c)], limit=1) for c in spec_lines}
    text_cols = {}
    for target, field in (('_supplier_copy', 'copy'), ('_supplier_bullets', 'bullets')):
        for line in lines_by(target):
            if line['lang']:
                text_cols.setdefault(line['lang'], {})[field] = line
    # en_US (inglese di base) per ultima: se non è attiva, Odoo la riscrive a ogni lingua.
    text_langs = sorted(text_cols, key=lambda l: (l == 'en_US', l))
    dims = {t: (lines_by(t) or [None])[0] for t in ('weight', '_volume_l', '_volume_w', '_volume_h',
                                                   '_weight_unit', '_dim_unit')}

    matched = unmatched = costs_written = media_written = 0
    seen, texts_written, spec_writes, dims_written, spec_found = set(), {}, 0, 0, {}
    names = {}   # template → nomi commerciali delle sue righe (famiglie: la parte comune)
    families = {}   # variante → [codice famiglia, nome, abbinata per codice, ambigua]
    for n, row in enumerate(rows, 1):
        if cancel_event and cancel_event.is_set():
            job.write({'state': 'cancelled', 'completed_at': fields.Datetime.now()})
            cr.commit()
            return
        raw, ids, by_code_match = match(row)
        code = match_key(raw)
        family = row_families.of(row, raw)
        if family is None:          # riga di intestazione (nessun codice): vale per le righe che seguono
            continue
        if not ids:
            unmatched += 1
            continue
        matched += 1
        specs = {}
        for spec_code, lines in spec_lines.items():
            spec = parse_spec(spec_code, [_value(row, l) for l in lines], text_lang)
            if spec:
                specs[spec_code] = spec
                spec_found[spec_code] = spec_found.get(spec_code, 0) + 1

        supplier_name = next((re.sub(r'\s+', ' ', _value(row, l)).strip() for l in lines_by('_supplier_name')
                              if _value(row, l).strip()), '')
        family_ref, family_name = family
        if family_ref:
            for vid in ids:
                _note_family(families, vid, family_ref, family_name, by_code_match)
        brand_name = first_value(row, brand_lines, _value) or options.get('brand_name') or ''
        cost = next((c for c in (_price(_value(row, l)) for l in cost_lines) if c is not None), None)
        urls = [u for u in (_value(row, l).strip() for l in media_lines) if u.startswith('http')]
        for variant in Variant.browse(ids):
            tmpl = variant.product_tmpl_id
            first = tmpl.id not in seen
            seen.add(tmpl.id)
            if supplier_name:
                names.setdefault(tmpl.id, []).append(supplier_name)
            if dry_run:
                continue
            variant_vals = {}
            if raw and tag and (variant.ke_supplier_code != raw or variant.ke_supplier_tag != tag):
                variant_vals.update(ke_supplier_code=raw, ke_supplier_tag=tag)
            # Costo del listino (2026-11-24): solo dove manca o era già del listino; mai sopra il costo del
            # magazzino o scritto a mano (product_cost.py). Prezzo 0 nel listino = nessun prezzo.
            if cost and abs((variant.standard_price or 0.0) - cost) > 0.00001 and variant.ke_supplier_may_write_cost():
                variant_vals['standard_price'] = cost
                costs_written += 1
            if variant_vals:
                variant.with_context(ke_cost_source='supplier').write(variant_vals)
            if urls:
                media_written += env['ke.product.media']._set_source_images(
                    tmpl, variant if len(tmpl.product_variant_ids) > 1 else None, 'supplier',
                    [{'url': u, 'role': 'MAIN' if i == 0 else False} for i, u in enumerate(urls)])
            if first:
                vals = {}
                if brand_name and not tmpl.ke_brand_id:
                    if brand_name not in brands:
                        brands[brand_name] = _resolve_brand(Brand, brand_name)
                    if brands[brand_name]:
                        vals['ke_brand_id'] = brands[brand_name].id
                category = ' / '.join(v for v in (re.sub(r'\s+', ' ', _value(row, l)).strip()
                                                  for l in lines_by('_supplier_category')) if v)
                if category:
                    vals['ke_supplier_category'] = category
                ref = next((_value(row, l).strip() for l in lines_by('_supplier_ref') if _value(row, l).strip()),
                           code)
                if ref:
                    vals['ke_supplier_ref'] = ref
                if vals:
                    tmpl.write(vals)
                for lang in text_langs:
                    if lang not in site_langs and lang != 'en_US':
                        continue
                    cols = text_cols[lang]
                    copy = _value(row, cols['copy']).strip() if 'copy' in cols else ''
                    bullets = '\n'.join(_split_bullets(_value(row, cols['bullets']))) if 'bullets' in cols else ''
                    if copy or bullets:
                        tmpl.with_context(lang=lang).write(
                            {'ke_supplier_copy': copy or False, 'ke_supplier_bullets': bullets or False})
                        texts_written[lang] = texts_written.get(lang, 0) + 1
                dims_written += _write_dims(tmpl, row, dims, options, _parse_weight, _parse_linear_to_m)
            if specs:
                target = variant if len(tmpl.product_variant_ids) > 1 else None
                for lang in lang_order:
                    values = {keys[c]: _render_supplier(s, lang) for c, s in specs.items() if keys.get(c)}
                    spec_writes += tmpl._ke_set_specs({k: v for k, v in values.items() if v}, 'supplier',
                                                      lang, variant=target)
        if n % _COMMIT_EVERY == 0:
            job.write({'processed_count': n, 'updated_count': len(seen), 'skipped_count': unmatched})
            cr.commit()
            env.invalidate_all()   # cache vuota a ogni blocco: con 20.000 righe rallentava riga dopo riga

    names_written = 0
    if names and not dry_run:
        Tmpl = env['product.template'].with_context(active_test=False)
        for tmpl_id, tmpl_names in names.items():
            name = _common_name(tmpl_names)
            tmpl = Tmpl.browse(tmpl_id)
            if name and tmpl.ke_supplier_name != name:
                tmpl.ke_supplier_name = name
                names_written += 1
    families_written = _write_families(Variant, families) if families and not dry_run else 0
    if created and touched and not dry_run:
        created['names'] = apply_names(env, touched, cr)
    ambiguous = sum(1 for f in families.values() if f[3])
    labels = {c: SPEC_KEYS[c][1]['it_IT'] if c in SPEC_KEYS else c for c in spec_found}
    prefix = (options.get('code_prefix') or '').upper()
    file_codes = {next((code_key(_value(r, l)) for l in code_lines), '') for r in rows}
    missing = sorted(c for c in by_code if c not in file_codes)
    summary = [
        'ARRICCHIMENTO — VERIFICA (nessuna modifica scritta)' if dry_run else 'ARRICCHIMENTO ESEGUITO',
        f'Righe nel file: {len(rows)}; abbinate a prodotti Odoo: {matched}; non in Odoo: {unmatched}',
        f'Prodotti Odoo coinvolti: {len(seen)}',
    ]
    if created:
        summary += _created_summary(created, dry_run)
    if not dry_run:
        summary += [
            f'Costi scritti o cambiati (varianti): {costs_written}; immagini (indirizzi) aggiornate: {media_written}',
            'Testi del fornitore scritti per lingua: '
            + (', '.join(f'{k} {v}' for k, v in sorted(texts_written.items())) or 'nessuno'),
            f'Valori di specifiche scritti o aggiornati (tutte le lingue): {spec_writes}',
            f'Prodotti con dimensioni/peso completati: {dims_written}',
            f'Nomi commerciali del fornitore scritti: {names_written}',
        ]
    if families:
        summary.append(
            f'Famiglie del fornitore: {len({f[0] for f in families.values() if not f[3]})} famiglie per '
            f'{len(families) - ambiguous} varianti'
            + (f' ({families_written} scritte o cambiate)' if not dry_run else '')
            + (f'; {ambiguous} varianti con famiglie diverse dallo stesso EAN, lasciate com\'erano' if ambiguous else ''))
    if spec_found:
        summary.append('Specifiche trovate nelle righe abbinate: ' + ', '.join(
            f'{labels[c]} {n}' for c, n in sorted(spec_found.items(), key=lambda kv: -kv[1])))
    if prefix:
        summary.append(f'Codici {prefix}… in Odoo che non sono nel file: {len(missing)}'
                       + (f' (es. {", ".join(prefix + c for c in missing[:10])})' if missing else ''))
    job.write({
        'state': 'done', 'completed_at': fields.Datetime.now(), 'processed_count': len(rows),
        'updated_count': len(seen), 'skipped_count': unmatched, 'import_count': (created or {}).get('variants_created', 0),
        'error_log': '\n'.join(summary),
    })
    cr.commit()
    _logger.info('Import job %d (arricchimento): %s', job.id, ' | '.join(summary))


def _common_name(names):
    """
    Nome commerciale di un prodotto: se le sue righe ne hanno più d'uno (famiglia
    Amazon che riunisce più prodotti del fornitore, es. "Flex Pike Deadbait" e
    "Flex Zander"), la parte iniziale comune a parole intere ("Flex"); se non
    ce n'è, il più frequente.
    """
    distinct = list(dict.fromkeys(n for n in names if n))
    if len(distinct) <= 1:
        return distinct[0] if distinct else ''
    words = [n.split() for n in distinct]
    common = []
    for group in zip(*words):
        if len({w.lower() for w in group}) > 1:
            break
        common.append(group[0])
    if common:
        return ' '.join(common)
    return max(distinct, key=names.count)


def _render_supplier(spec, lang):
    """
    Valore del fornitore nella lingua. Un testo libero (colore, materiale…) il fornitore lo dà in una lingua
    sola: si scrive uguale in tutte (2026-11-20). Prima restavano, nella stessa riga, le traduzioni
    automatiche di Amazon ("Limone posteriore" per "Lemon Back", "Tigre antincendio" per "Firetiger"),
    finite anche nei nomi delle varianti.
    """
    return render(spec, lang) or render_own(spec)


def _create_missing(env, rows, options, match, match_key, row_ean, lines_by, by_ean, dry_run, tag):
    """
    Passo prima dell'arricchimento (catalog_create.py): archiviati riattivati, articoli mancanti creati nella
    loro famiglia. Ritorna (resoconto, prodotti toccati).
    """
    report = {'revived': 0, 'variants_created': 0, 'families_new': 0, 'families_existing': 0,
              'families': 0, 'skipped_ean': 0, 'errors': [], 'names': 0}
    prefix = (options.get('code_prefix') or '').strip()
    arch_code, arch_raw, arch_ean = archived_index(env, prefix, code_key, ean_key)
    families = RowFamilies(lines_by, options, _value)
    name_lines = lines_by('name')
    brand_lines = lines_by('_brand')
    taken_ean = set(by_ean) | set(arch_ean)
    revive_ids, wanted, groups, seen_codes = [], {}, {}, set()
    # Articoli già in Odoo per famiglia del file (2026-11-20): il codice famiglia lo ricevono solo dopo, con
    # l'arricchimento; senza questo i nuovi articoli finivano in una famiglia nuova accanto a quella esistente
    # (259 proposte da riunire dopo gli import su zz_prodcopy). Una variante sotto più famiglie non conta.
    matched, refs_of = {}, {}
    for row in rows:
        raw, ids, _by_code = match(row)
        family = families.of(row, raw)
        if family is None or not raw:
            continue
        if ids:
            if family[0]:
                matched.setdefault(family[0], set()).update(ids)
                for vid in ids:
                    refs_of.setdefault(vid, set()).add(family[0])
            continue
        key = match_key(raw)
        if key in seen_codes:          # stessa riga due volte nel file
            continue
        seen_codes.add(key)
        ean = row_ean(row)
        vid = arch_code.get(key) or arch_raw.get(key) or (arch_ean.get(ean) if ean and options.get('match_ean')
                                                          else None)
        code = f'{prefix}{key}'
        if vid:
            if vid not in wanted:
                revive_ids.append(vid)
                wanted[vid] = code
            continue
        ref, family_name = family
        vals = {'default_code': code, 'ke_supplier_code': raw, 'ke_supplier_tag': tag or False,
                'ke_supplier_family_ref': ref or False, 'ke_supplier_family_name': family_name or False}
        if ean and ean not in taken_ean:
            vals['barcode'] = ean
            taken_ean.add(ean)
        elif ean:
            report['skipped_ean'] += 1
        item_name = first_value(row, name_lines, _value)
        name = family_name or item_name or code
        brand = first_value(row, brand_lines, _value) or options.get('brand_name') or ''
        groups.setdefault(ref or f'R:{key}', [name, [], brand])[1].append([vals, item_name or key])
    for _name, items, _brand in groups.values():
        _differing_labels(items)
    report['revived'] = len(revive_ids)
    report['families'] = len(groups)
    report['variants_created'] = sum(len(items) for _n, items, _b in groups.values())
    if dry_run:
        return report, env['product.template']
    touched = revive(env, revive_ids, wanted)
    env.cr.commit()
    matched = {ref: [v for v in ids if len(refs_of[v]) == 1] for ref, ids in matched.items()}
    report['variants_created'] = 0
    Brand = env['ke.product.brand']
    brands = {}
    by_brand = {}
    for ref, (name, items, brand) in groups.items():
        by_brand.setdefault(brand, {})[ref] = (name, items)
    for brand, brand_groups in by_brand.items():
        if brand not in brands:
            brands[brand] = _resolve_brand(Brand, brand) if brand else Brand
        touched |= create_families(env, {r: (n, [tuple(i) for i in items]) for r, (n, items) in brand_groups.items()},
                                   options, brands[brand], report, matched)
    return report, touched


def _differing_labels(items):
    """
    Nome di partenza delle varianti di una famiglia: la parte del nome dell'articolo che cambia tra le righe
    ("Marginal Carp X 1,50 Mt." / "… 1,80 Mt." → "1,50 Mt.", "1,80 Mt."). Le famiglie con specifiche del
    fornitore li ricevono poi dalle specifiche (ke_apply_supplier_names).
    """
    if len(items) < 2:
        return
    words = [re.split(r'[\s*]+', str(label).strip()) for _vals, label in items]
    common = 0
    for group in zip(*words):
        if len({w.casefold() for w in group}) > 1:
            break
        common += 1
    for item, w in zip(items, words):
        rest = ' '.join(w[common:]).strip(' -,')
        item[1] = rest or item[0].get('default_code')


def _resolve_brand(Brand, name):
    """Marca: il nome intero, se no la prima parola ("C4 SPEARFISHING" → C4), se no creata "Da verificare"."""
    brand, ignored = Brand._match(name)
    if brand or ignored:
        return brand
    first = name.split()[0] if name.split() else ''
    if first and first != name:
        brand, ignored = Brand._match(first)
        if brand or ignored:
            return brand
    return Brand.find_or_create(name)


def _created_summary(report, dry_run):
    if dry_run:
        lines = [f'CREAZIONE (verifica): {report["revived"]} prodotti archiviati da riattivare; '
                 f'{report["variants_created"]} articoli da creare in {report["families"]} famiglie '
                 f'(nuove o aggiunte a famiglie esistenti)']
    else:
        lines = [f'CREAZIONE: {report["revived"]} prodotti archiviati riattivati; {report["variants_created"]} '
                 f'articoli creati — {report["families_new"]} famiglie nuove, {report["families_existing"]} '
                 f'aggiunte a famiglie esistenti; nomi applicati a {report["names"]} prodotti']
    if report['skipped_ean']:
        lines.append(f'Articoli creati senza EAN (EAN già usato da un altro prodotto): {report["skipped_ean"]}')
    if report['errors']:
        lines.append(f'Famiglie non create ({len(report["errors"])}): ' + ' | '.join(report['errors'][:10]))
    return lines


def _note_family(families, vid, ref, name, by_code):
    """
    Famiglia del fornitore di una variante. L'abbinamento per codice vale più di quello per EAN; due righe
    dello stesso livello con famiglie diverse (un EAN riusato) → ambigua, non si scrive.
    """
    current = families.get(vid)
    if current is None or (by_code and not current[2]):
        families[vid] = [ref, name, by_code, False]
    elif current[2] == by_code and current[0] != ref:
        current[3] = True


def _write_families(Variant, families):
    written = 0
    for vid, (ref, name, _by_code, ambiguous) in families.items():
        if ambiguous:
            continue
        variant = Variant.browse(vid)
        vals = {}
        if variant.ke_supplier_family_ref != ref:
            vals['ke_supplier_family_ref'] = ref
        if name and variant.ke_supplier_family_name != name:
            vals['ke_supplier_family_name'] = name
        if vals:
            variant.write(vals)
            written += 1
    return written


def _write_dims(tmpl, row, dims, options, parse_weight, parse_linear_to_m):
    """Peso (kg) e dimensioni (cm) della confezione, solo nei campi vuoti del prodotto."""
    vals = {}
    weight_unit = (_value(row, dims['_weight_unit']).strip().lower() if dims['_weight_unit'] else '') \
        or options.get('weight_unit') or 'g'
    dim_unit = (_value(row, dims['_dim_unit']).strip().lower() if dims['_dim_unit'] else '') \
        or options.get('volume_unit') or 'cm'
    if dims['weight'] and not tmpl.weight:
        kg = parse_weight(_value(row, dims['weight']), weight_unit)
        if 0 < kg < 500:
            vals['weight'] = round(kg, 3)
    for target, field in (('_volume_l', 'product_length'), ('_volume_w', 'product_width'),
                          ('_volume_h', 'product_height')):
        if dims[target] and field in tmpl._fields and not tmpl[field]:
            cm = parse_linear_to_m(_value(row, dims[target]), dim_unit) * 100
            if 0 < cm < 1000:
                vals[field] = round(cm, 2)
    if vals:
        tmpl.write(vals)
    return bool(vals)
