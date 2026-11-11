"""
Testi della pagina prodotto sul sito (2026-11-11, chiesto dall'utente): nome
breve (H1), meta title, meta description, URL, descrizione breve e completa,
separati dai testi Amazon (titoli lunghi pensati per la ricerca su Amazon).
Traducibili, indipendenti dal negozio (li usa ke_prestashop_sync, li potrà
usare un altro e-commerce). Per le famiglie stanno sul prodotto (template):
sul sito la famiglia è un solo prodotto con combinazioni.

"Proponi testi sito" compila solo i campi vuoti dal nome e dalla
descrizione di vendita (per i prodotti Amazon: titolo, descrizione e punti
elenco); mai sovrascritto un testo scritto a mano.
"""
import html
import re
import unicodedata

from odoo import api, fields, models

META_TITLE_MAX = 60
META_DESCRIPTION_MAX = 160
WEB_NAME_MAX = 70

# Dove tagliare un titolo Amazon per avere marca + modello.
_NAME_CUT = re.compile(r'\s*(?:,|\s[–—-]\s|\s\|\s|\(|:)\s*')


def _plain(text):
    """Testo senza HTML, spazi normalizzati."""
    text = re.sub(r'<\s*(br|/p|/li|/div)\s*/?>', '\n', text or '', flags=re.I)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    return re.sub(r'[ \t]+', ' ', re.sub(r'\s*\n\s*', '\n', text)).strip()


def _cut(text, limit):
    """Taglia a fine parola entro 'limit' caratteri."""
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(' ', 1)[0].rstrip(' ,;:–-')
    return cut or text[:limit]


def _summary(text, limit):
    """Testo per la meta description: maiuscola iniziale, chiude a fine frase se possibile, altrimenti con "…"."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = text[:1].upper() + text[1:]
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind('. '), head.rfind('! '), head.rfind('? '))
    if end >= limit // 2:
        return head[:end + 1]
    return _cut(text, limit - 1) + '…'


def _esc(text):
    return html.escape(text, quote=False)


def short_name(name):
    """Marca + modello dal titolo: fino alla prima virgola, trattino, barra o parentesi."""
    name = (name or '').strip()
    first = _NAME_CUT.split(name, maxsplit=1)[0].strip()
    if len(first) < 8:  # taglio troppo corto ("DAM - …"): si tiene il titolo
        first = name
    return _cut(first, WEB_NAME_MAX)


def slugify(text):
    text = unicodedata.normalize('NFKD', (text or '').lower()).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text[:128] or 'prodotto'


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ke_web_name = fields.Char(
        'Nome sul sito (H1)', translate=True,
        help='Titolo della pagina e nome nelle liste del sito. Vuoto: nome del prodotto.')
    ke_web_meta_title = fields.Char(
        'Meta title', translate=True,
        help=f'Titolo nei risultati di Google e nella scheda del browser. Consigliati {META_TITLE_MAX} caratteri al massimo.')
    ke_web_meta_description = fields.Text(
        'Meta description', translate=True,
        help=f'Testo sotto il titolo nei risultati di Google. Consigliati {META_DESCRIPTION_MAX} caratteri al massimo.')
    ke_web_slug = fields.Char(
        'URL (slug)', translate=True,
        help='Parte finale dell\'indirizzo della pagina, es. "dam-quick-1". Vuoto: dal nome sul sito.')
    ke_web_short_description = fields.Html(
        'Descrizione breve', translate=True,
        help='Riepilogo accanto alla foto (2–3 righe). Vuoto: descrizione di vendita.')
    ke_web_description = fields.Html('Descrizione', translate=True, help='Descrizione completa della pagina prodotto.')
    ke_web_meta_title_len = fields.Integer('Caratteri meta title', compute='_compute_web_lengths')
    ke_web_meta_description_len = fields.Integer('Caratteri meta description', compute='_compute_web_lengths')

    @api.depends('ke_web_meta_title', 'ke_web_meta_description')
    def _compute_web_lengths(self):
        for tmpl in self:
            tmpl.ke_web_meta_title_len = len(tmpl.ke_web_meta_title or '')
            tmpl.ke_web_meta_description_len = len(tmpl.ke_web_meta_description or '')

    @api.onchange('ke_web_name')
    def _onchange_ke_web_name(self):
        if self.ke_web_name and not self.ke_web_slug:
            self.ke_web_slug = slugify(self.ke_web_name)

    def _ke_web_proposal(self, empty=None):
        """Valori proposti per i campi vuoti (nella lingua del contesto)."""
        self.ensure_one()
        source = self.description_sale or ''
        plain = _plain(source)
        # Un punto elenco Amazon può contenere più righe: una voce per riga.
        bullets = [line.strip(' -•·') for b in re.findall(r'<li[^>]*>(.*?)</li>', source, flags=re.S | re.I)
                   for line in re.split(r'\n|\s*•\s*', _plain(b))]
        bullets = [b[:1].upper() + b[1:] for b in bullets if b]
        body = _plain(re.sub(r'<ul.*?</ul>', '', source, flags=re.S | re.I))
        # 'empty': campi da proporre (scelti per lingua da action_ke_web_propose);
        # None = quelli vuoti nella lingua del contesto.
        need = (lambda f: f in empty) if empty is not None else (lambda f: not _plain(str(self[f] or '')))
        name = (not need('ke_web_name') and self.ke_web_name) or short_name(self.name)
        brand = self.ke_brand_id.name if 'ke_brand_id' in self._fields and self.ke_brand_id else ''
        vals = {}
        if need('ke_web_name'):
            vals['ke_web_name'] = name
        if need('ke_web_meta_title'):
            title = name if not brand or brand.lower() in name.lower() else f'{name} | {brand}'
            vals['ke_web_meta_title'] = _cut(title, META_TITLE_MAX)
        if need('ke_web_meta_description') and (body or bullets or plain):
            vals['ke_web_meta_description'] = _summary(body or ' '.join(bullets) or plain, META_DESCRIPTION_MAX)
        if need('ke_web_slug'):
            vals['ke_web_slug'] = slugify(name)
        if need('ke_web_short_description') and (bullets or body):
            vals['ke_web_short_description'] = (
                '<ul>' + ''.join(f'<li>{_esc(b)}</li>' for b in bullets[:3]) + '</ul>' if bullets
                else f'<p>{_esc(_cut(body, 300))}</p>')
        if need('ke_web_description') and plain:
            paragraphs = ''.join(f'<p>{_esc(p)}</p>' for p in body.split('\n') if p.strip())
            items = ''.join(f'<li>{_esc(b)}</li>' for b in bullets)
            vals['ke_web_description'] = paragraphs + (f'<ul>{items}</ul>' if items else '')
        return vals

    def _ke_web_texts(self):
        """
        Testi per il sito, da usare negli export: i campi della scheda Sito web
        se compilati, altrimenti la proposta automatica (senza salvarla). Il
        nome resta quello del prodotto finché non si compila "Nome sul sito".
        """
        self.ensure_one()
        proposal = self._ke_web_proposal()

        def pick(field, html_field=False):
            value = self[field]
            if html_field and not _plain(value or ''):
                value = False
            return str(value or proposal.get(field) or '')

        return {
            'name': self.ke_web_name or self.name or '',
            'slug': pick('ke_web_slug') or slugify(self.ke_web_name or short_name(self.name)),
            'meta_title': pick('ke_web_meta_title'),
            'meta_description': pick('ke_web_meta_description'),
            'short_description': pick('ke_web_short_description', True),
            'description': pick('ke_web_description', True),
        }

    def _ke_web_langs(self):
        """Lingue attive da proporre: prima quella dell'utente, poi le altre; mai en_US (ripiego tecnico di Odoo)."""
        codes = self.env['res.lang'].search([('active', '=', True)]).mapped('code')
        first = self.env.lang or self.env.user.lang or 'it_IT'
        return [first] * (first in codes) + sorted(c for c in codes if c not in (first, 'en_US'))

    def _ke_raw(self, fields_):
        """Valori per lingua così come salvati (senza il ripiego di Odoo sulle lingue mancanti)."""
        self.env.flush_all()
        self.env.cr.execute(
            f'SELECT {", ".join(fields_)} FROM product_template WHERE id = %s', (self.id,))
        return dict(zip(fields_, (row or {} for row in self.env.cr.fetchone())))

    @staticmethod
    def _ke_own(values, lang, primary):
        """Testo proprio della lingua: presente, non vuoto e non la copia di quello della lingua principale."""
        value = (values or {}).get(lang)
        if not value or not _plain(str(value)):
            return False
        return lang == primary or value != (values or {}).get(primary)

    def action_ke_web_propose(self):
        """
        Compila i campi vuoti della scheda Sito web in ogni lingua attiva
        che ha nome o descrizione propri (2026-11-11: prima solo nella
        lingua dell'utente, e le altre mostravano l'italiano di ripiego).
        Un campo che in una lingua è solo la copia di quello della lingua
        principale conta come vuoto; i testi propri non si toccano.
        """
        web_fields = ['ke_web_name', 'ke_web_meta_title', 'ke_web_meta_description', 'ke_web_slug',
                      'ke_web_short_description', 'ke_web_description']
        langs = self._ke_web_langs()
        primary = langs[0] if langs else self.env.lang
        done = 0
        for tmpl in self:
            changed = False
            for lang in langs:
                raw = tmpl._ke_raw(['name', 'description_sale'] + web_fields)
                own_source = any(self._ke_own(raw[f], lang, primary) for f in ('name', 'description_sale'))
                # Prodotti creati senza lingua (import, azioni pianificate): i testi
                # stanno solo nel ripiego en_US e valgono per la lingua principale.
                if not own_source and lang == primary:
                    own_source = any((raw[f] or {}).get('en_US') for f in ('name', 'description_sale'))
                if not own_source:
                    continue
                empty = {f for f in web_fields if not (
                    self._ke_own(raw[f], lang, primary)
                    or (lang == primary and not (raw[f] or {}).get(lang) and _plain(str((raw[f] or {}).get('en_US') or ''))))}
                if not empty:
                    continue
                vals = tmpl.with_context(lang=lang)._ke_web_proposal(empty=empty)
                if vals:
                    tmpl.with_context(lang=lang).write(vals)
                    changed = True
            done += changed
        if len(self) > 1:
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'Testi sito', 'type': 'success', 'sticky': False,
                           'message': f'Testi proposti per {done} prodotti su {len(self)} (solo campi vuoti).'},
            }
        return True


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def action_ke_web_propose(self):
        """La scheda Sito web compare anche nel modulo della variante (eredita quello del prodotto): testi del prodotto."""
        return self.product_tmpl_id.action_ke_web_propose()
