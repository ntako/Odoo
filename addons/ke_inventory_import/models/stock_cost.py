"""
Giacenze e costi dal file del magazzino (2026-11-24, es. MagazzinoArundo.csv).

Modalità "Giacenze e costi dal magazzino" del lavoro di import: non crea prodotti. Ogni riga si riconosce
tra i prodotti attivi, in quest'ordine:
- EAN (vince sugli altri: nel file alcuni ASIN sono scambiati tra due articoli vicini);
- codice: codice articolo, anche senza "+" finale, senza gli zeri iniziali del file ("0000001561222") e
  senza il prefisso del fornitore in Odoo ("LNF-3001015"), o codice del fornitore della variante;
- ASIN: della variante, del prodotto semplice o delle inserzioni (se c'è ke_amazon_sync).
Una chiave che porta a più prodotti non vale; senza un prodotto solo la riga è "non trovata".

Per ogni prodotto riconosciuto:
- costo: scritto come costo manuale (product_cost.py: i listini non lo cambiano più); prezzo vuoto o 0 = niente;
- quantità: la posizione scelta (es. CAV/Arundo) e le sue sotto-posizioni prendono le quantità del file,
  per ubicazione (sotto-posizione creata se manca; righe dello stesso articolo sommate); le altre
  sotto-posizioni per quell'articolo vanno a zero. Gli articoli che non sono nel file non si toccano.
  Righe con una cella uguale a un valore di "Niente quantità per" (es. "PURE FISHING", un codice): niente
  quantità, il costo sì. Quantità vuota = niente quantità.
"Solo verifica" conta senza scrivere. Il riepilogo va nel log del lavoro.
"""
import csv
import io
import logging
import re
from collections import Counter, defaultdict

from odoo import fields

from .enrich import _price, ean_key

_logger = logging.getLogger(__name__)
_CHUNK = 200
_PREFIX = re.compile(r'^[A-Z0-9]{2,4}-(?=.)')


def _code_keys(code):
    """Chiavi di un codice: com'è (senza "+"; il "." resta: è un altro ASIN), e senza zeri iniziali."""
    key = (code or '').strip().rstrip('+').strip().upper()
    keys = {key, key.lstrip('0')}
    return {k for k in keys if k}


class StockIndex:
    """Prodotti attivi per EAN, codice e ASIN (insiemi di id variante)."""

    def __init__(self, env):
        self.ean, self.code, self.code_weak, self.asin = (defaultdict(set) for _ in range(4))
        cr = env.cr
        env.flush_all()
        Variant = env['product.product']
        has_asin = 'x_amazon_asin' in Variant._fields
        cr.execute(f"""
            SELECT pp.id, pp.default_code, pp.barcode, pp.ke_supplier_code,
                   {'pp.x_amazon_asin, pt.x_amazon_asin,' if has_asin else 'NULL, NULL,'}
                   COUNT(*) OVER (PARTITION BY pt.id)
              FROM product_product pp JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE pp.active AND pt.active""")
        for vid, code, ean, supplier_code, vasin, tasin, siblings in cr.fetchall():
            for key in _code_keys(code):
                self.code[key].add(vid)
                stripped = _PREFIX.sub('', key)
                if stripped != key:
                    self.code_weak[stripped].add(vid)
                    self.code_weak[stripped.lstrip('0')].add(vid)
            for key in _code_keys(supplier_code):
                self.code_weak[key].add(vid)
            if ean and ean_key(ean):
                self.ean[ean_key(ean)].add(vid)
            for asin in (vasin, tasin if siblings == 1 else None):
                if asin:
                    self.asin[asin.strip().upper()].add(vid)
        if 'ke.amazon.listing' in env:
            cr.execute("""SELECT l.product_id, l.asin FROM ke_amazon_listing l
                            JOIN product_product pp ON pp.id = l.product_id WHERE pp.active AND l.asin IS NOT NULL""")
            for vid, asin in cr.fetchall():
                self.asin[asin.strip().upper()].add(vid)

    @staticmethod
    def _one(ids):
        return next(iter(ids)) if len(ids) == 1 else None

    def match(self, code, ean, asin):
        """
        (id variante o None, come, avviso). Ordine: EAN, codice esatto, ASIN, codice "debole" (senza prefisso
        del fornitore in Odoo o codice fornitore: lo stesso numero può essere di un altro fornitore, quindi
        per ultimo). Avvisi solo per i contrasti tra EAN, codice esatto e ASIN.
        """
        by_ean = self.ean.get(ean_key(ean), set()) if ean_key(ean) else set()
        keys = set(_code_keys(code)) if code else set()
        by_code = set().union(*(self.code.get(k, set()) for k in keys)) if keys else set()
        by_weak = set().union(*(self.code_weak.get(k, set()) for k in keys)) if keys and not by_code else set()
        asin = asin.strip().upper()
        by_asin = self.asin.get(asin, set()) if re.fullmatch(r'[A-Z0-9]{10}', asin) else set()
        strong = (('EAN', by_ean), ('codice', by_code), ('ASIN', by_asin))
        for how, ids in strong + (('codice senza prefisso', by_weak),):
            vid = self._one(ids)
            if vid:
                notes = [f'{h} indica un altro prodotto, vale {how}' for h, i in strong
                         if h != how and self._one(i) and self._one(i) != vid]
                return vid, how, '; '.join(notes)
        return None, '', '; '.join(f'{h} di più prodotti' for h, ids in strong + (('codice senza prefisso', by_weak),)
                                   if len(ids) > 1)


def _value(row, line):
    return ((row.get(line['column']) if line['column'] else '') or line['default'] or '').strip()


def _qty(text):
    return _price(text) if str(text or '').strip() else None


def do_stock(job, env, file_string, options, cr, cancel_event=None):
    from odoo.addons.ke_product_catalog.models.product_family_move import QUIET
    env = env(context=dict(env.context, **QUIET))
    mapping = options['stock_mapping']
    dry_run = options.get('dry_run', True)
    lines_by = lambda target: [l for l in mapping if l['target'] == target]

    def first(row, target):
        return next((v for v in (_value(row, l) for l in lines_by(target)) if v), '')

    skip = {v.strip().casefold() for v in (options.get('qty_skip_values') or '').splitlines() if v.strip()}
    rows = list(csv.DictReader(io.StringIO(file_string), delimiter=options['delimiter']))
    job.write({'total_count': len(rows)})
    cr.commit()

    index = StockIndex(env)
    qty_plan = defaultdict(lambda: defaultdict(float))   # variante → ubicazione → quantità
    costs, cost_conflicts = {}, []
    unmatched, warnings = [], []
    how_count = Counter()
    skipped_qty = 0
    for n, row in enumerate(rows, 2):
        if cancel_event and cancel_event.is_set():
            job.write({'state': 'cancelled', 'completed_at': fields.Datetime.now()})
            cr.commit()
            return
        code, ean, asin = first(row, 'default_code'), first(row, 'barcode'), first(row, '_asin')
        if not (code or ean or asin):
            continue
        vid, how, warning = index.match(code, ean, asin)
        label = f'riga {n} ({code or ean or asin})'
        if warning:
            warnings.append(f'{label}: {warning}')
        if not vid:
            unmatched.append(label)
            continue
        how_count[how] += 1
        cost = _price(first(row, 'standard_price'))
        if cost:
            if vid in costs and abs(costs[vid] - cost) > 0.00001:
                cost_conflicts.append(f'{label}: {cost} (vale {costs[vid]}, riga precedente)')
            costs.setdefault(vid, cost)
        qty = _qty(first(row, '_qty'))
        if qty is None:
            continue
        if skip and any(str(v).strip().casefold() in skip for v in row.values() if v):
            skipped_qty += 1
            continue
        location = first(row, '_location') if options.get('sublocations') else ''
        qty_plan[vid][location] += qty

    Variant = env['product.product']
    to_cost = {vid: c for vid, c in costs.items()
               if abs((Variant.browse(vid).standard_price or 0.0) - c) > 0.00001
               or Variant.browse(vid).ke_cost_source != 'manual'}
    cost_changed = sum(1 for vid, c in to_cost.items() if abs((Variant.browse(vid).standard_price or 0.0) - c) > 0.00001)
    not_storable = [vid for vid in qty_plan if not Variant.browse(vid).is_storable]
    for vid in not_storable:
        qty_plan.pop(vid)
    moves = {'locations': 0, 'quants': 0}
    if not dry_run:
        _write_costs(env, to_cost, cr)
        if qty_plan:
            moves = _apply_quantities(env, qty_plan, options['location_id'], cr, job)

    pieces = sum(sum(locs.values()) for locs in qty_plan.values())
    head = 'SOLO VERIFICA — nessuna modifica.' if dry_run else 'GIACENZE E COSTI AGGIORNATI'
    report = [
        head,
        f'Righe nel file: {len(rows)}; riconosciute: {sum(how_count.values())} '
        f'({", ".join(f"{h} {c}" for h, c in how_count.most_common())}); non trovate: {len(unmatched)}',
        f'Costi (prodotti con prezzo nel file: {len(costs)}): {cost_changed} {"da cambiare" if dry_run else "cambiati"}, '
        f'{len(to_cost) - cost_changed} uguali segnati come costo manuale (i listini non li cambiano più)',
        f'Quantità: {len(qty_plan)} prodotti, {pieces:g} pezzi'
        + (f'; {moves["locations"]} ubicazioni nuove, {moves["quants"]} giacenze cambiate' if not dry_run else '')
        + f'; righe senza quantità per "Niente quantità per": {skipped_qty}',
    ]
    if not_storable:
        report.append(f'Prodotti non stoccabili, quantità saltata: {len(not_storable)} (es. '
                      + ', '.join(Variant.browse(not_storable[:5]).mapped('display_name')) + ')')
    for title, items in (('Non trovate', unmatched), ('Da controllare', warnings),
                         ('Stesso prodotto con prezzi diversi', cost_conflicts)):
        if items:
            report.append(f'{title} ({len(items)}): ' + '; '.join(items[:40]) + (' …' if len(items) > 40 else ''))
    job.write({'state': 'done', 'completed_at': fields.Datetime.now(), 'processed_count': len(rows),
               'updated_count': len(set(to_cost) | set(qty_plan)), 'skipped_count': len(unmatched),
               'error_log': '\n'.join(report)})
    cr.commit()


def _write_costs(env, to_cost, cr):
    Variant = env['product.product'].with_context(ke_cost_source='manual')
    by_cost = defaultdict(list)
    for vid, cost in to_cost.items():
        by_cost[cost].append(vid)
    for i, (cost, vids) in enumerate(by_cost.items(), 1):
        Variant.browse(vids).write({'standard_price': cost, 'ke_cost_source': 'manual'})
        if i % _CHUNK == 0:
            cr.commit()
            env.invalidate_all()
    cr.commit()


def _apply_quantities(env, qty_plan, root_id, cr, job):
    """Porta la posizione e le sue sotto-posizioni alle quantità del file, prodotto per prodotto."""
    Location = env['stock.location']
    root = Location.browse(root_id)
    locations, created = {'': root.id}, 0
    for name in sorted({loc for locs in qty_plan.values() for loc in locs if loc}):
        loc = Location.search([('location_id', '=', root.id), ('name', '=', name)], limit=1)
        if not loc:
            loc = Location.create({'name': name, 'location_id': root.id, 'usage': 'internal'})
            created += 1
        locations[name] = loc.id
    cr.commit()
    Quant = env['stock.quant'].with_context(inventory_mode=True, inventory_name='Magazzino (import giacenze)')
    vids, changed = list(qty_plan), 0
    for start in range(0, len(vids), _CHUNK):
        chunk = vids[start:start + _CHUNK]
        wanted = {}
        for vid in chunk:
            for name, qty in qty_plan[vid].items():
                key = (vid, locations[name])
                wanted[key] = wanted.get(key, 0.0) + qty
        to_apply, seen = Quant.browse(), set()
        for quant in Quant.search([('product_id', 'in', chunk), ('location_id', 'child_of', root.id)]):
            key = (quant.product_id.id, quant.location_id.id)
            target = 0.0 if key in seen else wanted.get(key, 0.0)   # più giacenze nello stesso posto: una sola
            seen.add(key)
            if abs(quant.quantity - target) > 0.00001:
                quant.inventory_quantity = target
                to_apply |= quant
        for (vid, loc_id), qty in wanted.items():
            if (vid, loc_id) not in seen and qty:
                to_apply |= Quant.create({'product_id': vid, 'location_id': loc_id, 'inventory_quantity': qty})
        if to_apply:
            to_apply._apply_inventory()
            to_apply.inventory_quantity_set = False
            changed += len(to_apply)
        job.write({'processed_count': start + len(chunk)})
        cr.commit()
        env.invalidate_all()
    return {'locations': created, 'quants': changed}
