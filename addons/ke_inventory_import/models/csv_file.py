import base64
import csv
import io

from odoo import fields, models
from odoo.exceptions import UserError

from .field_mapping import VOLUME_UNIT_SELECTION


class InventoryCsvFile(models.Model):
    _name = 'inventory.csv.file'
    _description = 'CSV File Library'
    _order = 'upload_date desc, name'

    name = fields.Char('Nome', required=True)
    file_data = fields.Binary('File CSV', attachment=True, required=True)
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
        content = base64.b64decode(self.file_data)
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('latin-1')

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
