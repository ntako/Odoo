from odoo import fields, models


class AmazonMarketplace(models.Model):
    _name = 'ke.amazon.marketplace'
    _description = 'Marketplace Amazon'
    _order = 'sequence, name'

    name = fields.Char('Nome', required=True, help="Es. 'Italia', 'Germania'.")
    code = fields.Char('Codice', required=True, help="Codice breve, es. 'IT', 'DE'.")
    marketplace_id = fields.Char(
        'Marketplace ID', required=True,
        help='ID marketplace Amazon usato nelle chiamate SP-API (es. APJ6JRA9NG5V4 per IT).',
    )
    active = fields.Boolean(
        'Attivo', default=False,
        help='Solo i marketplace attivi vengono inclusi nelle sync multi-marketplace '
             '(fase successiva). Il marketplace usato oggi da questo modulo (Impostazioni → '
             'Amazon → Marketplace ID) va attivato qui perché la migrazione dei dati esistenti '
             'lo trovi.',
    )
    currency_id = fields.Many2one('res.currency', string='Valuta')
    sequence = fields.Integer('Sequenza', default=10)

    _sql_constraints = [
        ('marketplace_id_uniq', 'unique(marketplace_id)',
         'Esiste già un marketplace con questo Marketplace ID.'),
    ]

    def name_get(self):
        return [(rec.id, f'{rec.name} ({rec.code})') for rec in self]
