from odoo import api, fields, models
from odoo.exceptions import UserError

from .field_mapping import MAPPING_TARGETS, VOLUME_UNIT_SELECTION, WEIGHT_UNIT_SELECTION
from .import_job import _AUTO_DETECT_MAP


# Misura (2026-11-20): dà il nome alle varianti di mulinelli, abbigliamento, attrezzatura.
_SIZE_COLUMNS = ['Reel Size', 'Apparel Size', 'Equipment Size', 'Tackle Size']


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
    # Import integrato (2026-11-20): crea gli articoli mancanti nella loro famiglia, riusa gli archiviati.
    create_missing = fields.Boolean(
        'Crea gli articoli mancanti',
        help='Gli articoli del file che non sono in Odoo vengono creati nella loro famiglia del fornitore '
             '(aggiunti alla famiglia esistente o in una famiglia nuova); quelli archiviati vengono riattivati, '
             'mai duplicati. Poi l\'arricchimento e i nomi del fornitore.')
    code_suffix = fields.Char(
        'Suffisso del codice nel file',
        help='Parte finale del codice del file che in Odoo non c\'è (es. Tubertini "20181XX" = TUB-20181: "XX").')
    brand_name = fields.Char('Marca (se il file non ha la colonna)')
    family_from_name = fields.Boolean(
        'Famiglie dal nome',
        help='Per i file senza codice famiglia: articoli con lo stesso nome a meno delle misure finali '
             '("Marginal Carp X 1,50 Mt.", "… 1,80 Mt.") sono la stessa famiglia.')

    create_skip_values = fields.Text('Non creare né riattivare se')
    sheet_images = fields.Boolean('Immagini incollate nel foglio')
    # Giacenze e costi dal magazzino (2026-11-24): vedi inventory.import.job.
    stock_mode = fields.Boolean('Giacenze e costi dal magazzino')
    qty_skip_values = fields.Text('Niente quantità per')
    sublocations = fields.Boolean('Ubicazioni come sotto-posizioni', default=True)
    stock_location_id = fields.Many2one('stock.location', 'Posizione del magazzino', domain=[('usage', '=', 'internal')])

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
        'fields': [('SAP', 'default_code'), ('EAN/UPC', 'barcode'), ('SAP Description', 'name'),
                   ('Master Description', '_supplier_name'), ('Brand', '_brand'),
                   ('Master code', '_supplier_family_ref'), ('Master Description', '_supplier_family_name'),
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
            ('color', ['Color']), ('size', _SIZE_COLUMNS), ('material', ['Rod Blank Material', 'Reel Spool Material']),
            ('weight', ['Bait Weight - Grams', 'Reel Weight (g)', 'Rod Weight (g)', 'Net Weight']),
        ],
    }

    # File di novembre 2026, colonna "Drop vs New": i fuori catalogo non si creano né si riattivano
    # (decisione 2026-11-24); "Pending Drop" e "New" sì.
    _PUREFISHING_SKIP = 'Dropped'

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
            'create_skip_values': self._PUREFISHING_SKIP,
            'line_ids': [(0, 0, l) for l in lines],
        })


    # Altri fornitori (2026-11-20, file in docs/Fornitori): nessun codice famiglia tranne C4 (righe di
    # intestazione "Modello"); niente specifiche né testi in più lingue. Costo = prezzo di listino.
    _SUPPLIER_PRESETS = [
        {'name': 'Trabucco (catalogo)', 'code_prefix': 'TRA-', 'brand_name': 'Trabucco', 'family_from_name': True,
         'fields': [('Codice Articolo', 'default_code'), ('Descrizione Articolo', 'name'), ('EAN', 'barcode'),
                    ('Prezzo', 'standard_price'), ('Codice Categoria Articolo', '_supplier_category')]},
        # Marca: colonna "Brand" (Tubertini, Seika, Ryobi…); vuota in 1.379 righe su 7.072 → Tubertini.
        {'name': 'Tubertini (catalogo)', 'code_prefix': 'TUB-', 'code_suffix': 'XX', 'brand_name': 'Tubertini',
         'family_from_name': True,
         'fields': [('Codice', 'default_code'), ('Descrizione', 'name'), ('Brand', '_brand'),
                    ('Barcode EAN13', 'barcode'), ('Gruppo', '_supplier_category'),
                    ('Prezzo Listino', 'standard_price')]},
        {'name': 'Lineaeffe (catalogo)', 'code_prefix': 'LNF-', 'brand_name': 'Lineaeffe', 'family_from_name': True,
         'fields': [('CODE', 'default_code'), ('CODICE A BARRE', 'barcode'), ('DESCRIZIONE', 'name'),
                    ('€ PRICE LIST 2026', 'standard_price')]},
        {'name': 'C4 / H.Dessault (fotolistino)', 'code_prefix': 'C4C-',
         'fields': [('CODICE', 'default_code'), ('Codice a barre', 'barcode'), ('DESCRIZIONE', 'name'),
                    ('MODELLO + link', '_supplier_family_name'), ('LINEA', '_brand'),
                    ('LINEA', '_supplier_category'), ('FAMIGLIA', '_supplier_category'),
                    ('Riservato', 'standard_price')],
         'sheet_images': True},   # 321 immagini nel foglio, una per modello (2026-11-24)
    ]

    @api.model
    def _ke_create_supplier_presets(self):
        """Preset dei fornitori senza codice famiglia (2026-11-20), se non ci sono già (poi modificabili)."""
        for conf in self._SUPPLIER_PRESETS:
            existing = self.search([('name', '=', conf['name'])], limit=1)
            if existing:
                if conf.get('brand_name') and not existing.brand_name:
                    existing.brand_name = conf['brand_name']
                if conf.get('sheet_images') and not existing.sheet_images:
                    existing.sheet_images = True
                continue
            self.create({
                'name': conf['name'], 'delimiter': ';', 'enrich_only': True, 'match_ean': True,
                'create_missing': True,   # decisione 2026-11-20: si creano tutti gli articoli del listino
                'code_prefix': conf['code_prefix'], 'code_suffix': conf.get('code_suffix') or False,
                'brand_name': conf.get('brand_name') or False, 'family_from_name': conf.get('family_from_name'),
                'sheet_images': conf.get('sheet_images', False),
                'line_ids': [(0, 0, {'csv_column': c, 'target': t, 'sequence': (i + 1) * 10})
                             for i, (c, t) in enumerate(conf['fields'])],
            })

    # File del magazzino (2026-11-24, docs/MagazzinoArundo.csv): giacenze di Cavallino e costi reali.
    # Niente quantità per Pure Fishing e per 5 articoli Sbirulino (MA.GIA.); il costo sì per tutti.
    _STOCK_PRESET = {
        'name': 'Magazzino Arundo (giacenze e costi)',
        'fields': [('MPN', 'default_code'), ('EAN', 'barcode'), ('ASIN', '_asin'), ("Q.TA'", '_qty'),
                   ('UBICAZIONE', '_location'), ('PREZZO UNITARIO', 'standard_price')],
        'skip': ['PURE FISHING', '0000100300030', '0000100200035', '0000100120030', '0000100160020',
                 '0000100130020'],
    }

    @api.model
    def _ke_create_stock_preset(self):
        """Preset del file del magazzino, se non c'è già (poi modificabile)."""
        conf = self._STOCK_PRESET
        if self.search_count([('name', '=', conf['name'])]):
            return
        warehouse = self.env['stock.warehouse'].search([('code', '=', 'CAV')], limit=1)
        self.create({
            'name': conf['name'], 'delimiter': ',', 'stock_mode': True, 'sublocations': True,
            'stock_location_id': warehouse.lot_stock_id.id or False,
            'qty_skip_values': '\n'.join(conf['skip']),
            'line_ids': [(0, 0, {'csv_column': c, 'target': t, 'sequence': (i + 1) * 10})
                         for i, (c, t) in enumerate(conf['fields'])],
        })

    @api.model
    def _ke_add_purefishing_family_lines(self):
        """
        Preset Pure Fishing creato prima del 2026-11-20: aggiunge le colonne arrivate dopo, se mancano —
        famiglia del fornitore ("Master code", "Master Description"), marca ("Brand") e specifica "Misura"
        (Reel Size…) — e "Non creare né riattivare se: Dropped" se vuoto.
        A ogni aggiornamento del modulo: una colonna tolta a mano ritorna (si toglie di nuovo senza problemi).
        """
        preset = self.search([('name', '=', self._PUREFISHING_ENRICH['name'])], limit=1)
        if not preset:
            return
        if not preset.create_skip_values:
            preset.create_skip_values = self._PUREFISHING_SKIP
        seq = max(preset.line_ids.mapped('sequence') or [0])
        new = []
        if not preset.line_ids.filtered(lambda l: l.target == '_supplier_family_ref'):
            new += [{'csv_column': 'Master code', 'target': '_supplier_family_ref'},
                    {'csv_column': 'Master Description', 'target': '_supplier_family_name'}]
        if not preset.line_ids.filtered(lambda l: l.target == '_brand'):
            new.append({'csv_column': 'Brand', 'target': '_brand'})   # marca solo dove il prodotto non l'ha
        if not preset.line_ids.filtered(lambda l: l.target == '_spec' and l.spec_key_id.code == 'size'):
            from odoo.addons.ke_product_catalog.spec_rules import SPEC_KEYS
            seq_key, labels = SPEC_KEYS['size']
            key = self.env['ke.product.spec.key'].sudo()._ensure('size', labels, seq_key)
            new += [{'csv_column': c, 'target': '_spec', 'spec_key_id': key.id} for c in _SIZE_COLUMNS]
        for i, vals in enumerate(new, 1):
            vals.update(preset_id=preset.id, sequence=seq + 10 * i)
        if new:
            self.env['inventory.mapping.preset.line'].create(new)


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
