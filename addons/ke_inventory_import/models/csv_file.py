import base64
import csv
import io

from odoo import fields, models
from odoo.exceptions import UserError

from .field_mapping import VOLUME_UNIT_SELECTION


def _cell(value):
    """Valore di una cella Excel come testo: numeri interi senza ".0" (EAN, codici), decimali corti."""
    if value is None:
        return ''
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else ('%.10g' % value)
    return str(value).strip()


def _header_index(rows):
    """
    Riga di intestazione tra le prime 15: la prima con quasi tante celle di testo quante la più piena (numeri e
    indirizzi web non contano). Salta righe di titolo (listino Tubertini, fotolistino C4) e righe di soli
    numeri di colonna sopra l'intestazione (file Pure Fishing EMEA: 1, 2, 3… poi indirizzi, poi i nomi).
    """
    def texts(row):
        return sum(1 for v in row if isinstance(v, str) and v.strip() and not v.strip().startswith('http'))
    counts = [texts(r) for r in rows[:15]]
    best = max(counts, default=0)
    if best < 3:
        return 0
    return next(i for i, n in enumerate(counts) if n >= max(3, 0.8 * best))


def file_text(content, delimiter=';'):
    """
    Testo CSV del file (2026-11-20): un file Excel (.xlsx) si converte qui, dal primo foglio, partendo dalla
    riga di intestazione (_header_index). Un CSV si decodifica (UTF-8, se no Latin-1 come il file Trabucco).
    """
    if content[:4] == b'PK\x03\x04':
        import openpyxl
        sheet = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
        rows = list(sheet.iter_rows(values_only=True))
        out = io.StringIO()
        writer = csv.writer(out, delimiter=delimiter)
        for row in rows[_header_index(rows):]:
            cells = [_cell(v) for v in row]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                writer.writerow(cells)
        return out.getvalue()
    try:
        return content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return content.decode('latin-1')


def sheet_images(content):
    """
    Immagini incollate nel primo foglio di un .xlsx (2026-11-24, fotolistino C4: una per modello, sulla riga del
    modello). {numero della riga come nel testo di file_text (1 = prima riga dopo l'intestazione): byte}.
    Le immagini sopra l'intestazione (logo) e su righe vuote non contano.
    """
    if content[:4] != b'PK\x03\x04':
        return {}
    import openpyxl
    sheet = openpyxl.load_workbook(io.BytesIO(content)).worksheets[0]   # non read_only: serve per le immagini
    by_row = {}
    for image in getattr(sheet, '_images', []):
        try:
            by_row.setdefault(image.anchor._from.row, image._data())
        except Exception:   # immagine illeggibile: si salta
            continue
    if not by_row:
        return {}
    rows = list(sheet.iter_rows(values_only=True))
    start = _header_index(rows)
    out, n = {}, -1
    for i, row in enumerate(rows[start:], start):
        if any(_cell(v) for v in row):   # stesse righe di file_text
            n += 1
            if n and i in by_row:
                out[n] = by_row[i]
    return out


class InventoryCsvFile(models.Model):
    _name = 'inventory.csv.file'
    _description = 'CSV File Library'
    _order = 'upload_date desc, name'

    name = fields.Char('Nome', required=True)
    file_data = fields.Binary('File CSV o Excel', attachment=True, required=True)
    file_name = fields.Char('Nome file originale')
    delimiter = fields.Selection([
        (';', 'Semicolon (;)'),
        (',', 'Comma (,)'),
        ('\t', 'Tab'),
    ], default=';', string='Delimitatore', required=True)
    upload_date = fields.Datetime('Data upload', readonly=True, default=fields.Datetime.now)
    notes = fields.Char('Note')

    def _read_first_line(self):
        """Return headers reading only the first logical CSV row.

        Uses csv.reader directly so quoted fields containing embedded newlines
        (e.g. 'UK\\nCopy') are handled correctly and all columns are returned.
        """
        self.ensure_one()
        text = file_text(base64.b64decode(self.file_data), self.delimiter)
        reader = csv.reader(io.StringIO(text), delimiter=self.delimiter)
        headers = next(reader, [])
        return headers

    def action_create_job(self):
        """Open a new import job form pre-loaded with this file."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'inventory.import.job',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_csv_file_id': self.id,
                'default_file_name': self.file_name or self.name,
                'default_delimiter': self.delimiter,
            },
        }

    def action_create_preset(self):
        """Parse headers from this file and open a new preset form with lines populated."""
        self.ensure_one()
        if not self.file_data:
            raise UserError('Nessun file caricato.')

        headers = self._read_first_line()
        if not headers:
            raise UserError('Nessuna intestazione trovata nel CSV.')

        from .import_job import _AUTO_DETECT_MAP

        lines = []
        for i, col in enumerate(headers):
            tgt = _AUTO_DETECT_MAP.get(col, '_attribute')
            if tgt == '_skip':
                tgt = '_attribute'
            lines.append({
                'target': tgt,
                'csv_column': col,
                'sequence': i * 10,
            })

        preset = self.env['inventory.mapping.preset'].create({
            'name': self.name,
            'csv_file_id': self.id,
            'delimiter': self.delimiter,
            'csv_columns': ', '.join(headers),
            'line_ids': [(0, 0, v) for v in lines],
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'inventory.mapping.preset',
            'res_id': preset.id,
            'view_mode': 'form',
            'target': 'current',
        }
