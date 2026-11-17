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
    # Arricchimento dei prodotti esistenti (2026-11-17): vedi inventory.import.job.
    enrich_only = fields.Boolean('Solo arricchimento prodotti esistenti')
    code_prefix = fields.Char('Prefisso codice in Odoo')
    match_ean = fields.Boolean('Abbina anche per EAN', default=True)
    spec_text_lang = fields.Selection(
        lambda self: self.env['inventory.import.job']._spec_lang_selection(),
        string='Lingua dei testi delle specifiche', default='en_GB')

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


    # ------------------------------------------------------------------
    # Preset predefiniti
    # ------------------------------------------------------------------

    _PUREFISHING_ENRICH = {
        'name': 'Pure Fishing EMEA (arricchimento)',
        'fields': [('SAP', 'default_code'), ('EAN/UPC', 'barcode'), ('Master Description', '_supplier_name'),
                   ('Category', '_supplier_category'), ('SubCategory', '_supplier_category'),
                   ('Each Dimension - Length', '_volume_l'), ('Each Dimension - Width', '_volume_w'),
                   ('Each Dimension - Height', '_volume_h'),
                   ('Each Dimension - Unit of Dimension', '_dim_unit'),
                   ('Each Dimension - Gross Weight', 'weight'),
                   ('Each Dimension - Unit of Weight', '_weight_unit')],
        # lingua Odoo → (colonna testo, colonna punti elenco)
        'texts': [('en_GB', 'UK\nCopy', 'UKBullets'), ('it_IT', 'IT\nCopy', 'IT\nBullets'),
                  ('de_DE', 'DE\nCopy', 'DE\nBullets'), ('fr_FR', 'FR\nCopy', 'FR\nBullets'),
                  ('es_ES', 'ES\nCopy', 'ES\nBullets'), ('en_US', 'UK\nCopy', 'UKBullets')],
        # specifica → colonne in ordine di preferenza
        'specs': [
            ('technique', ['Category', 'SubCategory', 'Product Type']), ('water', ['Water Type']),
            ('species', ['Primary Species']), ('line_type', ['SubCategory', 'Product Type']),
            ('diameter', ['Advertised Diameter']), ('breaking_strength', ['Break Strength']),
            ('length', ['Rod Length', 'Advertised Length', 'Bait Length (cm)']),
            ('gear_ratio', ['Gear Ratio']), ('bearings', ['Bearing Count']), ('max_drag', ['Max Drag']),
            ('mono_capacity', ['Mono Capacity m/mm']), ('braid_capacity', ['Braid Capacity m/mm']),
            ('recovery', ['Recovery Rate']), ('hand', ['Reel Handle Position']), ('drag_type', ['Drag Type']),
            ('rod_power', ['Rod Power']), ('rod_action', ['Rod Action']), ('sections', ['Number of Pieces']),
            ('casting_weight', ['Lure Rating']), ('line_rating', ['Line Rating']),
            ('transport_length', ['Transport Length']), ('guides', ['Number of guides With Tip']),
            ('buoyancy', ['Bait Buoyancy']), ('depth', ['Swimming Depth - Meters']),
            ('hook_size', ['Hook Size']), ('hooks', ['Number of Hooks']), ('scent', ['Bait Scent/Flavor']),
            ('color', ['Color']), ('material', ['Rod Blank Material', 'Reel Spool Material']),
            ('weight', ['Bait Weight - Grams', 'Reel Weight (g)', 'Rod Weight (g)', 'Net Weight']),
        ],
    }

    @api.model
    def _ke_create_purefishing_enrich_preset(self):
        """Preset di arricchimento del catalogo Pure Fishing EMEA (2026-11-17), se non c'è già."""
        conf = self._PUREFISHING_ENRICH
        if self.search_count([('name', '=', conf['name'])]):
            return
        from odoo.addons.ke_product_catalog.spec_rules import SPEC_KEYS
        Key = self.env['ke.product.spec.key'].sudo()
        installed = dict(self.env['res.lang'].get_installed())
        lines, seq = [], 0
        for column, target in conf['fields']:
            seq += 10
            lines.append({'csv_column': column, 'target': target, 'sequence': seq})
        for lang, copy_col, bullets_col in conf['texts']:
            if lang not in installed and lang != 'en_US':
                continue
            for column, target in ((copy_col, '_supplier_copy'), (bullets_col, '_supplier_bullets')):
                seq += 10
                lines.append({'csv_column': column, 'target': target, 'lang_code': lang, 'sequence': seq})
        for code, columns in conf['specs']:
            seq_key, labels = SPEC_KEYS[code]
            key = Key._ensure(code, labels, seq_key)
            for column in columns:
                seq += 10
                lines.append({'csv_column': column, 'target': '_spec', 'spec_key_id': key.id, 'sequence': seq})
        self.create({
            'name': conf['name'], 'delimiter': ';', 'enrich_only': True, 'code_prefix': 'PFS-',
            'match_ean': True, 'spec_text_lang': 'en_GB', 'weight_unit': 'kg', 'volume_unit': 'cm',
            'line_ids': [(0, 0, l) for l in lines],
        })


class InventoryMappingPresetLine(models.Model):
    _name = 'inventory.mapping.preset.line'
    _inherit = ['inventory.mapping.line.extras']
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
                'spec_key_id': line.spec_key_id.id,
                'lang_code': line.lang_code,
            }
            for line in job.mapping_line_ids
        ]

        if self.preset_id:
            preset = self.preset_id
            preset.line_ids.unlink()
            preset.write({'name': self.name or preset.name, 'line_ids': [(0, 0, v) for v in line_vals],
                          **job._enrich_options_vals()})
        else:
            if not self.name:
                raise UserError('Inserisci un nome per il preset.')
            preset = self.env['inventory.mapping.preset'].create({
                'name': self.name,
                'line_ids': [(0, 0, v) for v in line_vals],
                **job._enrich_options_vals(),
            })

        job.preset_id = preset
        return {'type': 'ir.actions.act_window_close'}
