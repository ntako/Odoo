from odoo import api, fields, models


class ProductMedia(models.Model):
    """
    Un'immagine del prodotto come INDIRIZZO, non come file: la galleria
    completa resta fuori da Odoo (stima a occhio: decine di GB per ~7.000
    prodotti × 5-6 immagini, Odoo conserva l'originale più 4 ridimensionate)
    e si scarica solo quando serve, es. alla pubblicazione su PrestaShop.
    In Odoo resta scaricata solo l'immagine principale (image_1920 /
    image_variant_1920), gestita da chi importa.

    Ogni fonte (Amazon, file del fornitore, manuale) ha le sue righe: chi
    importa aggiorna solo quelle della propria fonte (_set_source_images),
    senza toccare le altre. La scelta di quale fonte usare per la
    pubblicazione spetta a chi pubblica (es. per marca).
    """
    _name = 'ke.product.media'
    _description = 'Immagine prodotto (indirizzo)'
    _order = 'product_tmpl_id, product_id, source, sequence, id'

    product_tmpl_id = fields.Many2one(
        'product.template', string='Prodotto', required=True, index=True, ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product', string='Variante', index=True, ondelete='cascade',
        domain="[('product_tmpl_id', '=', product_tmpl_id)]",
        help="Variante a cui appartiene l'immagine (es. il colore). Vuoto: immagine "
             "dell'intero prodotto, valida per tutte le varianti.",
    )
    url = fields.Char('Indirizzo', required=True)
    sequence = fields.Integer('Ordine', default=10)
    role = fields.Char(
        'Ruolo', help="Ruolo dell'immagine presso la fonte. Amazon: MAIN (principale), "
                      "PT01…PTnn (galleria), SWCH (campione colore).",
    )
    width = fields.Integer('Larghezza (px)')
    height = fields.Integer('Altezza (px)')
    source = fields.Selection(
        [('amazon', 'Amazon'), ('supplier', 'Fornitore'), ('manual', 'Manuale')],
        string='Fonte', required=True, default='manual',
    )
    source_ref = fields.Char(
        'Riferimento fonte', help='Dettaglio della fonte, es. il marketplace Amazon '
                                  'o il file del fornitore da cui arriva l\'indirizzo.',
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            self.product_tmpl_id = self.product_id.product_tmpl_id

    @api.model_create_multi
    def create(self, vals_list):
        # Righe aggiunte dalla scheda della variante: il prodotto si ricava da lì.
        for vals in vals_list:
            if vals.get('product_id') and not vals.get('product_tmpl_id'):
                vals['product_tmpl_id'] = self.env['product.product'].browse(vals['product_id']).product_tmpl_id.id
        return super().create(vals_list)

    @api.model
    def _set_source_images(self, template, variant, source, images, source_ref=False):
        """
        Sostituisce le immagini di una fonte per un prodotto/variante con
        l'elenco 'images' (lista ordinata di dict con 'url' e, se noti,
        'role', 'width', 'height'): aggiorna quelle con lo stesso
        indirizzo, crea le nuove, elimina quelle non più presenti. Le righe
        delle altre fonti e degli altri prodotti/varianti non vengono
        toccate. Nessuna scrittura se non è cambiato niente (chiamato a ogni
        riga del Full Import). Elenco vuoto = nessuna modifica: una risposta
        senza immagini non deve cancellare una galleria già nota.
        Ritorna True se ha modificato qualcosa.
        """
        images = [i for i in (images or []) if i.get('url')]
        if not images or not template:
            return False
        Media = self.sudo()
        existing = Media.search([
            ('product_tmpl_id', '=', template.id),
            ('product_id', '=', variant.id if variant else False),
            ('source', '=', source),
        ])
        by_url = {m.url: m for m in existing}
        wanted, seen = [], set()
        for index, image in enumerate(images):
            if image['url'] in seen:
                continue
            seen.add(image['url'])
            wanted.append({
                'url': image['url'],
                'sequence': (index + 1) * 10,
                'role': image.get('role') or False,
                'width': image.get('width') or 0,
                'height': image.get('height') or 0,
                'source_ref': source_ref or False,
            })
        changed = False
        for vals in wanted:
            record = by_url.pop(vals['url'], None)
            if record is None:
                Media.create({
                    **vals, 'source': source, 'product_tmpl_id': template.id,
                    'product_id': variant.id if variant else False,
                })
                changed = True
                continue
            diff = {k: v for k, v in vals.items() if record[k] != v}
            if diff:
                record.write(diff)
                changed = True
        if by_url:
            Media.browse([m.id for m in by_url.values()]).unlink()
            changed = True
        return changed
