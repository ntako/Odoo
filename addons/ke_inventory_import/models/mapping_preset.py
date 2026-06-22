from odoo import api, fields, models
from odoo.exceptions import UserError

from .field_mapping import MAPPING_TARGETS, VOLUME_UNIT_SELECTION, WEIGHT_UNIT_SELECTION
from .import_job import _AUTO_DETECT_MAP


class InventoryMappingPreset(models.Model):
    _name = 'inventory.mapping.preset'
    _description = 'Mapping Preset'
    _order = 'name'

    name = fields.Char('Nome preset', required=True)
    csv_file_id = fields.Many2one(
        'inventory.csv.file',
        string='File CSV (libreria)',
        ondelete='set null',
        help='Seleziona un file dalla libreria per rilevare automaticamente le colonne',
    )
    delimiter = fields.Selection([
        (';', 'Semicolon (;)'),
        (',', 'Comma (,)'),
        ('\t', 'Tab'),
    ], default=';', string='Delimitatore')
    csv_columns = fields.Char(
        'Colonne rilevate', readonly=True,
        help='Elenco colonne trovate nel CSV',
    )
    weight_unit = fields.Selection(
        WEIGHT_UNIT_SELECTION,
        string='Unità peso',
        default='g',
        help='Unità di misura del peso nel CSV (usata quando il valore non contiene un\'unità esplicita)',
    )
    volume_unit = fields.Selection(
        VOLUME_UNIT_SELECTION,
        string='Unità dimensioni volume',
        default='cm',
        help='Unità di misura per le colonne dimensione (L×W×H) o volume diretto',
    )
    line_ids = fields.One2many(
        'inventory.mapping.preset.line', 'preset_id',
        string='Mapping colonne',
    )

    @api.onchange('csv_file_id')
    def _onchange_csv_file_id(self):
        if self.csv_file_id and self.csv_file_id.delimiter:
            self.delimiter = self.csv_file_id.delimiter

    def action_parse_sample(self):
        """Read CSV headers from csv_file_id and recreate line_ids."""
        self.ensure_one()
        if not self.csv_file_id:
            raise UserError('Seleziona un file CSV dalla libreria.')

        headers = self.csv_file_id._read_first_line()
        if not headers:
            raise UserError('Nessuna intestazione trovata nel CSV.')

        self.line_ids.unlink()
        lines = []
        for i, col in enumerate(headers):
            tgt = _AUTO_DETECT_MAP.get(col, '_attribute')
            if tgt == '_skip':
                tgt = '_attribute'
            lines.append({
                'preset_id': self.id,
                'target': tgt,
                'csv_column': col,
                'sequence': i * 10,
            })
        self.env['inventory.mapping.preset.line'].create(lines)
        self.csv_columns = ', '.join(headers)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }


class InventoryMappingPresetLine(models.Model):
    _name = 'inventory.mapping.preset.line'
    _description = 'Mapping Preset Line'
    _order = 'sequence, id'

    preset_id = fields.Many2one(
        'inventory.mapping.preset',
        string='Preset',
        ondelete='cascade',
        required=True,
    )
    sequence = fields.Integer(default=10)
    target = fields.Selection(
        MAPPING_TARGETS,
        string='Campo destinazione',
        default='_skip',
        required=True,
    )
    csv_column = fields.Char('Colonna CSV')
    default_value = fields.Char('Valore di default')


class InventoryMappingPresetSaveWizard(models.TransientModel):
    _name = 'inventory.mapping.preset.save.wizard'
    _description = 'Salva Mapping come Preset'

    job_id = fields.Many2one('inventory.import.job', string='Job', required=True)
    preset_id = fields.Many2one(
        'inventory.mapping.preset',
        string='Preset esistente',
        help='Lascia vuoto per creare un nuovo preset, oppure seleziona uno da sovrascrivere',
    )
    name = fields.Char(
        'Nome nuovo preset',
        help='Obbligatorio solo se non selezioni un preset esistente',
    )

    @api.onchange('preset_id')
    def _onchange_preset_id(self):
        if self.preset_id:
            self.name = self.preset_id.name

    def action_save(self):
        self.ensure_one()
        job = self.job_id
        if not job.mapping_line_ids:
            raise UserError('Nessuna riga di mapping da salvare.')

        line_vals = [
            {
                'sequence': line.sequence,
                'target': line.target,
                'csv_column': line.csv_column or '',
                'default_value': line.default_value or '',
            }
            for line in job.mapping_line_ids
        ]

        if self.preset_id:
            preset = self.preset_id
            preset.line_ids.unlink()
            preset.write({'name': self.name or preset.name, 'line_ids': [(0, 0, v) for v in line_vals]})
        else:
            if not self.name:
                raise UserError('Inserisci un nome per il preset.')
            preset = self.env['inventory.mapping.preset'].create({
                'name': self.name,
                'line_ids': [(0, 0, v) for v in line_vals],
            })

        job.preset_id = preset
        return {'type': 'ir.actions.act_window_close'}
