from odoo import api, fields, models


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

    # Sintassi Odoo 19: _sql_constraints (lista di tuple) non è più supportato,
    # sostituito da models.Constraint come attributo di classe — verificato
    # con un'installazione locale reale (Docker/odoo/), non solo per sentito
    # dire: il vecchio stile installava comunque ma con un warning esplicito
    # "no longer supported, please define models.Constraint".
    _marketplace_id_uniq = models.Constraint(
        'unique(marketplace_id)',
        'Esiste già un marketplace con questo Marketplace ID.',
    )

    @api.depends('name', 'code')
    def _compute_display_name(self):
        # Odoo 19: name_get() non esiste più (rimosso dall'ORM), il suo posto
        # è preso da questo compute — un altro caso verificato con
        # l'installazione locale, non solo dedotto dal changelog.
        for rec in self:
            rec.display_name = f'{rec.name} ({rec.code})' if rec.code else rec.name
