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
- peso e dimensioni solo se il prodotto non li ha.
"Solo verifica" conta senza scrivere. Il riepilogo va nel log del lavoro.
"""
import csv
import io
import logging
import re

from odoo import fields

from odoo.addons.ke_product_catalog.spec_rules import SPEC_KEYS, parse_spec, render
from odoo.addons.ke_product_catalog.models.product_supplier_texts import _split_bullets

_logger = logging.getLogger(__name__)
_COMMIT_EVERY = 200


def code_key(code):
    return (code or '').strip().rstrip('+.').strip().upper()


def ean_key(ean):
    return re.sub(r'\D', '', str(ean or '')).lstrip('0')


def _value(row, line):
    return (row.get(line['column']) if line['column'] else '') or line['default'] or ''


def _variant_index(env, prefix):
    """Codice del fornitore → varianti; EAN → varianti."""
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
    mapping = options['enrich_mapping']
    dry_run = options.get('dry_run', True)
    text_lang = options.get('spec_text_lang') or 'en_GB'
    lines_by = lambda target: [l for l in mapping if l['target'] == target]
    code_lines, ean_lines = lines_by('default_code'), lines_by('barcode')

    rows = list(csv.DictReader(io.StringIO(file_string), delimiter=options['delimiter']))
    job.write({'total_count': len(rows)})
    cr.commit()

    by_code, by_ean = _variant_index(env, options.get('code_prefix'))
    Variant = env['product.product'].with_context(active_test=False)
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

    matched = unmatched = 0
    seen, texts_written, spec_writes, dims_written, spec_found = set(), {}, 0, 0, {}
    names = {}   # template → nomi commerciali delle sue righe (famiglie: la parte comune)
    for n, row in enumerate(rows, 1):
        if cancel_event and cancel_event.is_set():
            job.write({'state': 'cancelled', 'completed_at': fields.Datetime.now()})
            cr.commit()
            return
        code = next((code_key(_value(row, l)) for l in code_lines if code_key(_value(row, l))), '')
        ids = by_code.get(code) if code else None
        if not ids and options.get('match_ean'):
            ean = next((ean_key(_value(row, l)) for l in ean_lines if ean_key(_value(row, l))), '')
            ids = by_ean.get(ean) if ean else None
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
        for variant in Variant.browse(ids):
            tmpl = variant.product_tmpl_id
            first = tmpl.id not in seen
            seen.add(tmpl.id)
            if supplier_name:
                names.setdefault(tmpl.id, []).append(supplier_name)
            if dry_run:
                continue
            if first:
                vals = {}
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
                    values = {keys[c]: render(s, lang) for c, s in specs.items() if keys.get(c)}
                    spec_writes += tmpl._ke_set_specs({k: v for k, v in values.items() if v}, 'supplier',
                                                      lang, variant=target)
        if n % _COMMIT_EVERY == 0:
            job.write({'processed_count': n, 'updated_count': len(seen), 'skipped_count': unmatched})
            cr.commit()

    names_written = 0
    if names and not dry_run:
        Tmpl = env['product.template'].with_context(active_test=False)
        for tmpl_id, tmpl_names in names.items():
            name = _common_name(tmpl_names)
            tmpl = Tmpl.browse(tmpl_id)
            if name and tmpl.ke_supplier_name != name:
                tmpl.ke_supplier_name = name
                names_written += 1
    labels = {c: SPEC_KEYS[c][1]['it_IT'] if c in SPEC_KEYS else c for c in spec_found}
    prefix = (options.get('code_prefix') or '').upper()
    file_codes = {next((code_key(_value(r, l)) for l in code_lines), '') for r in rows}
    missing = sorted(c for c in by_code if c not in file_codes)
    summary = [
        'ARRICCHIMENTO — VERIFICA (nessuna modifica scritta)' if dry_run else 'ARRICCHIMENTO ESEGUITO',
        f'Righe nel file: {len(rows)}; abbinate a prodotti Odoo: {matched}; non in Odoo: {unmatched}',
        f'Prodotti Odoo coinvolti: {len(seen)}',
    ]
    if not dry_run:
        summary += [
            'Testi del fornitore scritti per lingua: '
            + (', '.join(f'{k} {v}' for k, v in sorted(texts_written.items())) or 'nessuno'),
            f'Valori di specifiche scritti o aggiornati (tutte le lingue): {spec_writes}',
            f'Prodotti con dimensioni/peso completati: {dims_written}',
            f'Nomi commerciali del fornitore scritti: {names_written}',
        ]
    if spec_found:
        summary.append('Specifiche trovate nelle righe abbinate: ' + ', '.join(
            f'{labels[c]} {n}' for c, n in sorted(spec_found.items(), key=lambda kv: -kv[1])))
    if prefix:
        summary.append(f'Codici {prefix}… in Odoo che non sono nel file: {len(missing)}'
                       + (f' (es. {", ".join(prefix + c for c in missing[:10])})' if missing else ''))
    job.write({
        'state': 'done', 'completed_at': fields.Datetime.now(), 'processed_count': len(rows),
        'updated_count': len(seen), 'skipped_count': unmatched, 'import_count': 0,
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
