from odoo import fields, models

# ---------------------------------------------------------------------------
# Ordered list of all valid mapping targets shown in the dropdown.
# Targets starting with '_' have special handling in import_job.py.
# All other targets are direct product.template field names (standard Odoo).
# ---------------------------------------------------------------------------
MAPPING_TARGETS = [
    # --- Speciali ---
    ('_skip',         '— Ignora colonna —'),
    # --- Catalogo fornitore (2026-11-17, usati dall'arricchimento) ---
    ('_spec',              'Specifica tecnica (scegli quale)'),
    ('_supplier_copy',     'Testo fornitore (scegli la lingua)'),
    ('_supplier_bullets',  'Punti elenco fornitore (scegli la lingua)'),
    ('_supplier_category', 'Categoria fornitore (più colonne unite con " / ")'),
    ('_supplier_ref',      'Codice fornitore'),
    ('_supplier_name',     'Nome commerciale fornitore (nome sul sito)'),
    ('_supplier_family_ref',  'Codice famiglia fornitore (raggruppa le varianti)'),
    ('_supplier_family_name', 'Nome famiglia fornitore'),
    ('_brand',             'Marca'),
    ('_media_url',         'Immagine (indirizzo: galleria, senza scaricarla)'),
    ('_image_url',    'URL immagine prodotto (scarica e salva)'),
    ('_attribute',    'Attributo prodotto'),
    ('_category',     'Categoria (trova/crea)'),
    ('_supplier',     'Fornitore ordini acquisto (trova/crea partner)'),
    ('_qty',          'Quantità stock (rettifica inventario)'),
    # --- Giacenze e costi dal magazzino (2026-11-24) ---
    ('_asin',         'ASIN Amazon (per riconoscere il prodotto)'),
    ('_location',     'Ubicazione in magazzino (es. M1-R10-S1-DX)'),
    ('_lot',          'Numero lotto / seriale'),
    ('_product_type', 'Tipo prodotto Odoo (consu / service / combo)'),
    ('_volume_l',     'Volume - Lunghezza (L)'),
    ('_volume_w',     'Volume - Larghezza (W)'),
    ('_volume_h',     'Volume - Altezza (H)'),
    ('_weight_unit',  'Unità peso per riga (colonna dedicata)'),
    ('_dim_unit',     'Unità dimensioni L/W/H per riga (colonna dedicata)'),
    ('_volume_unit',  'Unità volume diretto per riga (colonna dedicata)'),
    # --- Campi standard Odoo ---
    ('name',              'Nome prodotto [name]'),
    ('default_code',      'Codice interno [default_code]'),
    ('barcode',           'Barcode [barcode]'),
    ('list_price',        'Prezzo vendita [list_price]'),
    ('standard_price',    'Prezzo costo [standard_price]'),
    ('weight',            'Peso [weight] kg'),
    ('volume',            'Volume diretto [volume] m³'),
    ('description_sale',  'Descrizione vendita [description_sale]'),
]

# Maps each direct-field target to the field_type expected by _prepare_product_vals.
FIELD_TYPE_MAP = {
    'name':             'char',
    'default_code':     'char',
    'barcode':          'barcode',
    'list_price':       'float',
    'weight':           'weight',
    'volume':           'dimension',  # uses _extract_number, then unit conversion applied outside
    'description_sale': 'char',
}

# Volume unit → factor to convert linear dim³ (or direct volume) to m³
VOLUME_UNIT_FACTORS = {
    'mm':  1e-9,          # mm³  → m³
    'cm':  1e-6,          # cm³  → m³
    'dm':  1e-3,          # dm³  → m³
    'm':   1.0,           # m³   → m³ (no conversion)
    'in':  1.6387064e-5,  # in³  → m³
    'ft':  2.8316847e-2,  # ft³  → m³
}

VOLUME_UNIT_SELECTION = [
    ('mm', 'Millimetri (mm)'),
    ('cm', 'Centimetri (cm)'),
    ('dm', 'Decimetri (dm)'),
    ('m',  'Metri (m)'),
    ('in', 'Pollici (in)'),
    ('ft', 'Piedi (ft)'),
]

# Weight unit → factor to convert to kg
WEIGHT_UNIT_FACTORS = {
    'g':   1e-3,        # grams  → kg
    'kg':  1.0,         # kg     → kg
    'oz':  0.028349523, # oz     → kg
    'lb':  0.45359237,  # lb     → kg
}

WEIGHT_UNIT_SELECTION = [
    ('g',  'Grammi (g)'),
    ('kg', 'Chilogrammi (kg)'),
    ('oz', 'Once (oz)'),
    ('lb', 'Libbre (lb)'),
]



def _lang_selection(self):
    langs = self.env['res.lang'].get_installed()
    return langs + ([('en_US', 'English (US)')] if 'en_US' not in dict(langs) else [])


class MappingLineExtras(models.AbstractModel):
    """Campi delle righe di mapping per le destinazioni del catalogo fornitore (2026-11-17)."""
    _name = 'inventory.mapping.line.extras'
    _description = 'Campi aggiuntivi delle righe di mapping'

    spec_key_id = fields.Many2one(
        'ke.product.spec.key', string='Specifica',
        help='Per "Specifica tecnica": quale. Unità, conversioni e valori fissi li decide la specifica. '
             'Più colonne sulla stessa specifica: vale la prima con un valore (per i valori fissi, '
             'es. la tecnica, si leggono tutte insieme).')
    lang_code = fields.Selection(_lang_selection, string='Lingua',
                                 help='Per testo e punti elenco del fornitore: la lingua della colonna.')


class InventoryFieldMappingLine(models.Model):
    _name = 'inventory.field.mapping.line'
    _inherit = ['inventory.mapping.line.extras']
    _description = 'CSV Column Mapping Line'
    _order = 'sequence, id'

    job_id = fields.Many2one(
        'inventory.import.job',
        string='Import Job',
        ondelete='cascade',
        required=True,
    )
    sequence = fields.Integer(default=10)
    csv_column = fields.Char('Colonna CSV')
    target = fields.Selection(
        MAPPING_TARGETS,
        string='Campo destinazione',
        default='_skip',
        required=True,
    )
    default_value = fields.Char('Valore di default')

