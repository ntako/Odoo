"""
Descrizioni di vendita dal catalogo fornitore (2026-11-11, chiesto
dall'utente). L'import del catalogo (marzo 2026) ha salvato i testi del
fornitore come attributi informativi del prodotto, uno per lingua ("IT
Copy", "UK Copy", "FR Copy", … e "IT Bullets", "UKBullets", …), ma ha
scritto la descrizione di vendita solo in italiano, con i punti elenco su
una riga unita da "•". I prodotti che non sono su Amazon (o non hanno avuto
le traduzioni dal Full Import) mostravano così l'italiano in ogni lingua.

L'azione compone la descrizione di ogni lingua da "Copy" + "Bullets" e la
scrive solo dove la lingua non ha un testo proprio (copia dell'italiano =
vuoto). In italiano rifà solo il testo che è già quello del fornitore, per
avere i punti elenco come voci vere; le descrizioni Amazon e quelle scritte
a mano non si toccano.
"""
import re

from odoo import fields, models

from .product_website import _esc, _plain

# Lingua Odoo → prefisso degli attributi del catalogo fornitore.
SUPPLIER_LANGS = {'it_IT': 'IT', 'en_GB': 'UK', 'fr_FR': 'FR', 'es_ES': 'ES', 'de_DE': 'DE'}


def _attr_key(name):
    """'IT\\nCopy', 'IT Copy' → 'itcopy'; 'UKBullets' → 'ukbullets'."""
    return re.sub(r'\s+', '', name or '').lower()


def _split_bullets(text):
    """Punti elenco del fornitore: separati da "•", "_" o a capo."""
    parts = re.split(r'\s*[•\n]\s*|_(?=\S)', text or '')
    return [p.strip(' -·') for p in parts if p and p.strip(' -·')]


def supplier_description(copy, bullets):
    """HTML della descrizione: paragrafi della "Copy" e punti elenco."""
    html = ''.join(f'<p>{_esc(p.strip())}</p>' for p in (copy or '').split('\n') if p.strip())
    items = _split_bullets(bullets)
    if items:
        html += '<ul>' + ''.join(f'<li>{_esc(b)}</li>' for b in items) + '</ul>'
    return html


def _same_text(a, b):
    """Stesso testo a meno di formattazione (tag, "•", spazi): per riconoscere il testo del fornitore."""
    norm = lambda t: re.sub(r'[\s•_·-]+', '', _plain(t or '')).lower()
    return bool(a) and bool(b) and norm(a) == norm(b)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Testi del catalogo fornitore per lingua (2026-11-17, import "Catalogo
    # fornitore"): separati dalla descrizione di vendita (Amazon o scritta a
    # mano), che resta com'è. Il sito li preferisce, dove la lingua li ha.
    ke_supplier_copy = fields.Text('Testo fornitore', translate=True)
    ke_supplier_bullets = fields.Text('Punti elenco fornitore', translate=True,
                                      help='Un punto per riga.')
    ke_supplier_category = fields.Char(
        'Categoria fornitore', help='Categoria e sottocategoria nel catalogo del fornitore.')
    ke_supplier_ref = fields.Char('Codice fornitore', help='Codice articolo nel catalogo del fornitore.')
    ke_supplier_name = fields.Char(
        'Nome fornitore',
        help='Nome commerciale nel catalogo del fornitore (es. "Sick Braid"). Il nome sul sito diventa '
             'marca + questo nome + tipo di prodotto nella lingua (es. "Berkley Sick Braid – Filo trecciato"), '
             'salvo un "Nome sul sito" scritto a mano.')

    def _ke_supplier_site_name(self, lang):
        """Nome sul sito composto dai dati del fornitore nella lingua, o '' se manca il nome commerciale."""
        self.ensure_one()
        if not self.ke_supplier_name:
            return ''
        from ..spec_rules import product_kind, site_name
        brand = self.ke_brand_id.name if 'ke_brand_id' in self._fields and self.ke_brand_id else ''
        return site_name(brand, self.ke_supplier_name, product_kind(self.ke_supplier_category), lang)

    def _ke_supplier_texts(self):
        """{prefisso lingua: {'copy': testo, 'bullets': testo}} dagli attributi informativi del prodotto."""
        self.ensure_one()
        texts = {}
        for ptav in self.attribute_line_ids.product_template_value_ids:
            key = _attr_key(ptav.attribute_id.with_context(lang='en_US').name)
            match = re.fullmatch(r'([a-z]{2})(copy|bullets)', key)
            if match:
                value = ptav.product_attribute_value_id.with_context(lang='en_US').name
                texts.setdefault(match.group(1).upper(), {})[match.group(2)] = value
        return texts

    def action_ke_supplier_descriptions(self):
        """Descrizione di vendita per lingua dal catalogo fornitore (solo dove manca un testo proprio)."""
        langs = {c for c in self.env['res.lang'].search([('active', '=', True)]).mapped('code')}
        primary = 'it_IT'
        updated = writes = 0
        for tmpl in self:
            supplier = tmpl._ke_supplier_texts()
            if not supplier:
                continue
            changed = False
            for lang, prefix in SUPPLIER_LANGS.items():
                src = supplier.get(prefix)
                if lang not in langs or not src or not (src.get('copy') or src.get('bullets')):
                    continue
                new = supplier_description(src.get('copy'), src.get('bullets'))
                raw = tmpl._ke_raw(['description_sale'])['description_sale']
                current = raw.get(lang) or (raw.get('en_US') if lang == primary else None)
                if lang == primary:
                    old_supplier = (src.get('copy') or '') + ' ' + (src.get('bullets') or '')
                    if current and not _same_text(current, old_supplier):
                        continue  # testo Amazon o scritto a mano
                elif self._ke_own(raw, lang, primary):
                    continue
                if current == new:
                    continue
                tmpl.with_context(lang=lang).write({'description_sale': new})
                writes += 1
                changed = True
            updated += changed
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': 'Descrizioni dal catalogo fornitore', 'type': 'success', 'sticky': False,
                       'message': f'{updated} prodotti aggiornati ({writes} descrizioni) su {len(self)}.'},
        }
