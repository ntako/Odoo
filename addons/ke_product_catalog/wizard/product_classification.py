"""
Classificazione dei prodotti portabile da un database all'altro (2026-11-17).

La classificazione (categoria di ogni prodotto) si prepara nel database
locale e va riportata in produzione. L'import standard di Odoo riconosce i
prodotti solo per ID, che fra due database non coincidono per i prodotti
creati dopo la copia: qui il file usa il codice articolo (o l'EAN) e il
percorso completo della categoria ("Pesca / Mulinelli / Spinning").
"""
import base64
import csv
import io

from odoo import api, fields, models
from odoo.exceptions import UserError

_SEP = ' / '
_HEADER = ['Codice', 'EAN', 'Prodotto', 'Categoria']


def path_key(path):
    """Percorso confrontabile: parti senza spazi ai bordi, minuscole."""
    return tuple(p.strip().lower() for p in (path or '').split('/') if p.strip())


def code_key(code):
    """Codice confrontabile: senza spazi, maiuscole e "+" finale (pezzi rietichettati Amazon)."""
    return (code or '').strip().rstrip('+').strip().upper()


class ProductClassificationExport(models.TransientModel):
    _name = 'ke.product.classification.export'
    _description = 'Esporta classificazione prodotti'

    scope = fields.Selection([
        ('selected', 'Prodotti selezionati'),
        ('all', 'Tutti i prodotti attivi'),
    ], string='Prodotti', required=True, default='all')
    selected_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer('Saltati (senza codice né EAN)', readonly=True)
    exported_count = fields.Integer('Prodotti esportati', readonly=True)
    file_data = fields.Binary('File', readonly=True)
    file_name = fields.Char(readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        ids = self.env.context.get('active_ids') or []
        if self.env.context.get('active_model') == 'product.template' and ids:
            vals['scope'] = 'selected'
            vals['selected_count'] = len(ids)
        return vals

    def _products(self):
        Tmpl = self.env['product.template']
        if self.scope == 'selected':
            return Tmpl.browse(self.env.context.get('active_ids') or []).exists()
        return Tmpl.search([('type', '!=', 'service')])

    @staticmethod
    def _row(tmpl):
        variants = tmpl.product_variant_ids
        code = tmpl.default_code or next((v.default_code for v in variants if v.default_code), '')
        ean = next((v.barcode for v in variants if v.barcode), '')
        return [code or '', ean or '', tmpl.name or '', tmpl.categ_id.complete_name or '']

    def action_export(self):
        out = io.StringIO()
        writer = csv.writer(out, delimiter=';')
        writer.writerow(_HEADER)
        skipped = exported = 0
        for tmpl in self._products():
            row = self._row(tmpl)
            if not row[0] and not row[1]:
                skipped += 1   # senza codice né EAN non si potrebbe riconoscere nell'altro database
                continue
            writer.writerow(row)
            exported += 1
        self.write({
            'skipped_count': skipped, 'exported_count': exported,
            'file_data': base64.b64encode(out.getvalue().encode('utf-8-sig')),
            'file_name': f'classificazione-prodotti-{fields.Date.today()}.csv',
        })
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new',
        }


class ProductClassificationImport(models.TransientModel):
    """
    Riapplica una classificazione esportata: per ogni riga cerca il prodotto
    (codice del prodotto o di una variante, poi EAN) e gli assegna la
    categoria del percorso, creando quelle mancanti. Tocca solo la categoria.
    """
    _name = 'ke.product.classification.import'
    _description = 'Importa classificazione prodotti'

    file_data = fields.Binary('File CSV', required=True)
    file_name = fields.Char('Nome file')
    dry_run = fields.Boolean(
        'Solo verifica', default=True,
        help='Mostra cosa cambierebbe senza scrivere niente. Toglila per applicare.')
    result = fields.Text('Risultato', readonly=True)

    # ------------------------------------------------------------------
    def _rows(self):
        text = base64.b64decode(self.file_data).decode('utf-8-sig')
        try:
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ';'
        rows = list(csv.reader(io.StringIO(text), dialect))
        if not rows:
            raise UserError('Il file è vuoto.')
        header = [h.strip().lower() for h in rows[0]]
        cols = {}
        for name in ('codice', 'ean', 'categoria'):
            if name not in header:
                raise UserError(f'Manca la colonna "{name.capitalize()}". '
                                f'Colonne attese: {", ".join(_HEADER)}.')
            cols[name] = header.index(name)
        get = lambda row, name: row[cols[name]].strip() if len(row) > cols[name] else ''
        return [(n, get(r, 'codice'), get(r, 'ean'), get(r, 'categoria'))
                for n, r in enumerate(rows[1:], 2) if any(c.strip() for c in r)]

    def _indexes(self):
        """Codice → {template: codici esatti} e EAN → {template}; anche archiviati."""
        by_code, by_ean = {}, {}
        Variant = self.env['product.product'].with_context(active_test=False)
        for v in Variant.search_read([], ['default_code', 'barcode', 'product_tmpl_id']):
            tid = v['product_tmpl_id'][0]
            if v['default_code']:
                by_code.setdefault(code_key(v['default_code']), {}).setdefault(tid, set()).add(
                    v['default_code'].strip().upper())
            if v['barcode']:
                by_ean.setdefault(v['barcode'].strip(), {}).setdefault(tid, set())
        Tmpl = self.env['product.template'].with_context(active_test=False)
        for t in Tmpl.search_read([('default_code', '!=', False)], ['default_code']):
            by_code.setdefault(code_key(t['default_code']), {}).setdefault(t['id'], set()).add(
                t['default_code'].strip().upper())
        active = set(Tmpl.search([('active', '=', True)]).ids)
        return by_code, by_ean, active

    @staticmethod
    def _pick(candidates, raw_code, active):
        """Un solo prodotto fra i candidati, o None se resta ambiguo.

        Stesso codice su più prodotti (quasi sempre un attivo e un vecchio
        archiviato, 2026-11-17): vince l'attivo, poi il codice identico al
        file (con o senza "+").
        """
        tids = set(candidates)
        if len(tids) > 1 and tids & active:
            tids &= active
        if len(tids) > 1 and raw_code:
            exact = {t for t in tids if raw_code.strip().upper() in candidates[t]}
            tids = exact or tids
        return next(iter(tids)) if len(tids) == 1 else None

    def _category(self, path, cache, created):
        """Categoria del percorso; le mancanti si creano (in verifica: solo contate)."""
        parts = [p.strip() for p in path.split('/') if p.strip()]
        if not parts:
            return None
        Categ = self.env['product.category']
        parent = Categ.browse()
        for depth, name in enumerate(parts, 1):
            sub = tuple(p.lower() for p in parts[:depth])
            if sub not in cache:
                found = Categ.browse()
                if not isinstance(parent, str):   # sotto una categoria da creare non c'è niente
                    found = Categ.search([('parent_id', '=', parent.id or False)]).filtered(
                        lambda c: (c.name or '').strip().lower() == name.lower())[:1]
                if not found:
                    created.add(_SEP.join(parts[:depth]))
                    # In verifica un segnaposto (il percorso), altrimenti la si crea.
                    found = (_SEP.join(parts[:depth]) if self.dry_run
                             else Categ.create({'name': name, 'parent_id': parent.id or False}))
                cache[sub] = found
            parent = cache[sub]
        return parent

    def action_import(self):
        rows = self._rows()
        by_code, by_ean, active = self._indexes()
        Tmpl = self.env['product.template'].with_context(active_test=False)
        cache, created = {}, set()
        changed = same = 0
        not_found, ambiguous, no_categ = [], [], []
        moves = {}
        for line, code, ean, path in rows:
            if not path:
                no_categ.append(f'riga {line} ({code or ean})')
                continue
            candidates = by_code.get(code_key(code)) if code else None
            if not candidates and ean:
                candidates = by_ean.get(ean)
            if not candidates:
                not_found.append(code or ean or f'riga {line}')
                continue
            tid = self._pick(candidates, code, active)
            if not tid:
                ambiguous.append(f'{code or ean} ({len(candidates)} prodotti)')
                continue
            tmpl = Tmpl.browse(tid)
            categ = self._category(path, cache, created)
            current = tmpl.categ_id.complete_name or ''
            target = categ if isinstance(categ, str) else categ.complete_name
            if path_key(current) == path_key(target):
                same += 1
                continue
            changed += 1
            moves[(current, target)] = moves.get((current, target), 0) + 1
            if not self.dry_run:
                tmpl.categ_id = categ

        head = 'VERIFICA (nessuna modifica scritta)' if self.dry_run else 'IMPORT ESEGUITO'
        lines = [
            head,
            f'Righe nel file: {len(rows)}',
            f'Prodotti {"da spostare" if self.dry_run else "spostati"}: {changed}',
            f'Già nella categoria giusta: {same}',
            f'Categorie {"da creare" if self.dry_run else "create"}: {len(created)}',
            f'Codici non trovati: {len(not_found)}',
            f'Codici ambigui (più prodotti): {len(ambiguous)}',
            f'Righe senza categoria: {len(no_categ)}',
        ]
        if moves:
            lines.append('\nSpostamenti (da → a: prodotti):')
            for (src, dst), n in sorted(moves.items(), key=lambda kv: -kv[1])[:40]:
                lines.append(f'  {src or "(nessuna)"} → {dst}: {n}')
        for title, items in (('Categorie nuove', sorted(created)), ('Non trovati', not_found),
                             ('Ambigui', ambiguous), ('Senza categoria', no_categ)):
            if items:
                lines.append(f'\n{title} ({len(items)}):')
                lines.extend(f'  {i}' for i in items[:50])
                if len(items) > 50:
                    lines.append(f'  … e altri {len(items) - 50}')
        self.result = '\n'.join(lines)
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new',
        }
