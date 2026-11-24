"""
Nomi di famiglie e varianti dal catalogo del fornitore (2026-11-20).

Per un prodotto le cui varianti attive sono tutte della stessa famiglia del fornitore:
- "Nome fornitore" = nome della famiglia del fornitore (es. "Flex Carp");
- nome del prodotto in Odoo, in ogni lingua = nome sul sito (marca + nome + tipo nella lingua,
  es. "Berkley Flex Carp – Filo monofilo");
- nome delle varianti = le specifiche che le distinguono, il meno possibile: si prendono in un ordine
  fisso (misura, diametro, lunghezza, potenza…) solo quelle che separano varianti ancora uguali, finché
  sono tutte diverse (Flex Carp: basta il diametro, "0,30 mm"; non "0,30 mm – 6,8 kg – 400 m"). Nella
  lingua di ogni valore (le specifiche sono tradotte); se due varianti restano uguali, il codice.
  Attributo: quello della specifica se ne basta una ("Diametro"), altrimenti "Variante".
Le specifiche sono quelle della scheda Specifiche (per variante o per tutto il prodotto), scritte
dall'arricchimento: nessun dato preso altrove.
"""
from odoo import api, models

from ..spec_rules import SPEC_KEYS
from .product_family_move import NO_AUTO_VARIANTS

# Specifiche che danno il nome alle varianti, in ordine di preferenza.
LABEL_KEYS = ('size', 'hook_size', 'diameter', 'length', 'rod_power', 'casting_weight', 'line_rating',
              'sections', 'sinker_weight', 'color', 'hand', 'gear_ratio', 'breaking_strength', 'weight',
              'buoyancy', 'depth', 'scent', 'pieces', 'hooks', 'mono_capacity', 'braid_capacity',
              'line_capacity', 'max_drag', 'bearings', 'transport_length', 'line_type', 'material')
VARIANT_LABELS = {'it_IT': 'Variante', 'en_US': 'Variant', 'en_GB': 'Variant', 'es_ES': 'Variante',
                  'fr_FR': 'Variante', 'de_DE': 'Variante'}


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def _ke_variants_renamed(self, variants):
        """Aggancio: varianti con un nome nuovo (es. PrestaShop deve aggiornare le combinazioni)."""

    @api.model
    def _ke_name_langs(self):
        return sorted(set(self.env['res.lang'].search([('active', '=', True)]).mapped('code')) | {'en_US'},
                      key=lambda l: (l == 'en_US', l))

    def _ke_supplier_family(self):
        """(codice, nome) della famiglia del fornitore se tutte le varianti attive sono di quella, o None."""
        self.ensure_one()
        refs = set(self.product_variant_ids.mapped('ke_supplier_family_ref'))
        if len(refs) != 1 or not next(iter(refs)):
            return None
        variant = self.product_variant_ids.filtered('ke_supplier_family_name')[:1]
        return next(iter(refs)), variant.ke_supplier_family_name or ''

    def _ke_variant_spec_values(self):
        """
        {variante: {codice specifica: {lingua: valore}}}: la specifica della variante, se no quella del prodotto.
        Solo valori del fornitore o scritti a mano: i testi liberi di Amazon (es. colori "Light – Bianco
        naturale.") non sono nomi presentabili.
        """
        self.ensure_one()
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT s.product_id, k.code, s.value FROM ke_product_spec s
              JOIN ke_product_spec_key k ON k.id = s.key_id
             WHERE s.product_tmpl_id = %s AND s.source IN ('supplier', 'manual')""", (self.id,))
        common, own = {}, {}
        for product_id, code, value in self.env.cr.fetchall():
            (own.setdefault(product_id, {}) if product_id else common)[code] = value or {}
        return {v: {**common, **own.get(v.id, {})} for v in self.product_variant_ids}

    @staticmethod
    def _ke_pick_label_keys(values):
        """Specifiche minime che rendono diverse le varianti (in ordine LABEL_KEYS, poi le altre)."""
        def canon(value):
            return (value.get('en_US') or next(iter(value.values()), '') or '').strip().casefold() if value else ''
        variants = list(values)
        # Solo specifiche che tutte le varianti hanno: un nome vuoto per qualcuna non serve a nessuno.
        complete = {c for c in {c for v in variants for c in values[v]} if all(canon(values[v].get(c)) for v in variants)}
        codes = [c for c in LABEL_KEYS if c in complete] + sorted(complete - set(LABEL_KEYS))
        groups, chosen = [variants], []
        for code in codes:
            if all(len(g) == 1 for g in groups):
                break
            split = []
            for group in groups:
                by_value = {}
                for v in group:
                    by_value.setdefault(canon(values[v].get(code)), []).append(v)
                split += by_value.values()
            if len(split) > len(groups):
                chosen.append(code)
                groups = split
        return chosen

    def _ke_label_attribute(self, codes):
        Attr = self.env['product.attribute'].with_context(lang='en_US')
        labels = SPEC_KEYS[codes[0]][1] if len(codes) == 1 and codes[0] in SPEC_KEYS else VARIANT_LABELS
        name = labels.get('en_US') or labels.get('en_GB')
        attr = Attr.search([('name', '=', name), ('create_variant', '=', 'always')], limit=1)
        if not attr:
            attr = Attr.create({'name': name, 'create_variant': 'always'})
            self._ke_write_translations(attr, {l: labels.get(l) for l in self._ke_name_langs()} | {'en_US': name})
        return attr

    @api.model
    def _ke_write_translations(self, record, names):
        """
        Nome in ogni lingua, inglese di base per ultimo (con en_US non attiva, scrivere un'altra lingua
        riscrive anche l'inglese). Confronto col valore SALVATO per lingua, non con quello letto: una lingua
        senza traduzione si legge con il ripiego sull'inglese e sembrerebbe già giusta (stesso errore delle
        specifiche del 2026-11-17).
        """
        # Chi chiama ha già visto che qualcosa è diverso (sui valori salvati): si scrivono tutte le lingue,
        # en_US per ultima, senza rileggere a ogni lingua.
        for lang in sorted(names, key=lambda l: l == 'en_US'):
            if names[lang]:
                record.with_context(lang=lang).name = names[lang]

    def ke_apply_supplier_names(self, rename_product=True, rename_variants=True, dry_run=False):
        """Applica i nomi del fornitore ai prodotti (vedi docstring del modulo). Ritorna le righe del resoconto."""
        lines = []
        langs = self._ke_name_langs()
        for tmpl in self:
            family = tmpl._ke_supplier_family()
            if not family:
                continue
            _ref, family_name = family
            changes = []
            if family_name and tmpl.ke_supplier_name != family_name:
                changes.append(f'nome fornitore "{family_name}"')
                if not dry_run:
                    tmpl.ke_supplier_name = family_name
            if rename_product and (family_name or tmpl.ke_supplier_name):
                new_names = {lang: tmpl._ke_preview_site_name(lang, family_name) for lang in langs}
                current = tmpl._ke_raw(['name'])['name']
                differ = {l: n for l, n in new_names.items() if n and (current.get(l) or '') != n}
                if differ:
                    changes.append(f'nome "{new_names.get("it_IT") or new_names["en_US"]}"')
                    if not dry_run:
                        for lang, name in differ.items():
                            tmpl.with_context(lang=lang).name = name
            if rename_variants and len(tmpl.product_variant_ids) > 1:
                note = tmpl._ke_rename_variants(langs, dry_run)
                if note:
                    changes.append(note)
            if changes:
                lines.append(f'{tmpl.display_name}: ' + '; '.join(changes))
        return lines

    def _ke_preview_site_name(self, lang, family_name):
        """Nome sul sito con il nome della famiglia (anche prima di averlo scritto: per "Solo verifica")."""
        from ..spec_rules import product_kind, site_name
        brand = self.ke_brand_id.name if self.ke_brand_id else ''
        return site_name(brand, family_name or self.ke_supplier_name, product_kind(self.ke_supplier_category), lang)

    def _ke_rename_variants(self, langs, dry_run):
        self.ensure_one()
        lines = self.attribute_line_ids.filtered(lambda l: l.attribute_id.create_variant != 'no_variant')
        if len(lines) != 1:
            return 'varianti non rinominate (attributi che creano varianti: %d)' % len(lines)
        values = self._ke_variant_spec_values()
        codes = self._ke_pick_label_keys(values)
        if not codes:
            return ''
        labels = {}
        for variant in self.product_variant_ids:
            labels[variant] = {lang: ' – '.join(filter(None, (
                (values[variant].get(c) or {}).get(lang) or (values[variant].get(c) or {}).get('en_US')
                or next(iter((values[variant].get(c) or {}).values()), '') for c in codes))) for lang in langs}
        # Due varianti con lo stesso nome in una qualsiasi lingua: entrambe col codice.
        counts = {}
        for label in labels.values():
            for lang in langs:
                key = (lang, (label.get(lang) or '').casefold())
                counts[key] = counts.get(key, 0) + 1
        for variant, label in labels.items():
            if not label['en_US'] or any(counts[(l, (label.get(l) or '').casefold())] > 1 for l in langs):
                for lang in langs:
                    label[lang] = f'{label[lang]} ({variant.default_code or variant.id})'.strip()
        line = lines
        attr = self._ke_label_attribute(codes)
        current = {v: v.product_template_attribute_value_ids.filtered(lambda p: p.attribute_line_id == line)
                   for v in self.product_variant_ids}
        raw_names = {}
        if current:
            self.env.flush_all()
            ids = [p.product_attribute_value_id.id for p in current.values() if p]
            self.env.cr.execute('SELECT id, name FROM product_attribute_value WHERE id = ANY(%s)', (ids,))
            raw_names = dict(self.env.cr.fetchall())
        todo = {v: l for v, l in labels.items()
                if current[v].attribute_id != attr
                or any(l.get(lang) and (raw_names.get(current[v].product_attribute_value_id.id) or {}).get(lang) != l[lang]
                       for lang in langs)}
        summary = ', '.join(labels[v].get('it_IT') or labels[v]['en_US'] for v in self.product_variant_ids)
        if not todo:
            return ''
        if dry_run:
            return f'varianti [{attr.with_context(lang="it_IT").name}]: {summary}'
        # Cambiare attributo solo se nessuna variante archiviata usa ancora i valori della riga attuale.
        archived_use = self.with_context(active_test=False).product_variant_ids.filtered(
            lambda v: not v.active and v.product_template_attribute_value_ids & line.product_template_value_ids)
        if attr != line.attribute_id and archived_use:
            attr = line.attribute_id
        Value = self.env['product.attribute.value'].with_context(lang='en_US')
        # Valori dell'attributo letti una volta (la ricerca per nome tradotto, ripetuta per ogni variante,
        # rallentava con migliaia di valori).
        self.env.flush_all()
        self.env.cr.execute("SELECT id, name->>'en_US', name FROM product_attribute_value WHERE attribute_id = %s",
                            (attr.id,))
        known = {}
        for vid, en_name, raw in self.env.cr.fetchall():
            known.setdefault(en_name, (vid, raw or {}))
        value_of = {}
        for variant, label in labels.items():
            found = known.get(label['en_US'])
            if found:
                value = Value.browse(found[0])
                raw = found[1]
            else:
                value = Value.create({'attribute_id': attr.id, 'name': label['en_US']})
                raw = {'en_US': label['en_US']}
                known[label['en_US']] = (value.id, raw)
            if any(label.get(l) and raw.get(l) != label[l] for l in label):
                self._ke_write_translations(value, label)
                known[label['en_US']] = (value.id, dict(label))
            value_of[variant] = value
        Line = self.env['product.template.attribute.line'].with_context(**NO_AUTO_VARIANTS)
        new_line = self.attribute_line_ids.filtered(lambda l: l.attribute_id == attr)
        all_values = self.env['product.attribute.value'].browse({v.id for v in value_of.values()})
        if new_line:
            new_line.with_context(**NO_AUTO_VARIANTS).write({'value_ids': [(4, v.id) for v in all_values]})
        else:
            new_line = Line.create({'product_tmpl_id': self.id, 'attribute_id': attr.id,
                                    'value_ids': [(6, 0, all_values.ids)]})
        ptav = {p.product_attribute_value_id: p for p in new_line.product_template_value_ids}
        renamed = self.product_variant_ids.filtered(
            lambda v: v.product_template_attribute_value_ids != ptav[value_of[v]])
        try:
            # Tutte insieme, un salvataggio; uno alla volta solo se due varianti si scambiano i valori.
            with self.env.cr.savepoint():
                for variant in renamed.with_context(**NO_AUTO_VARIANTS):
                    variant.product_template_attribute_value_ids = [(6, 0, [ptav[value_of[variant]].id])]
                self.env.flush_all()
        except Exception:   # noqa: BLE001 — unicità della combinazione durante uno scambio
            self.env.invalidate_all()
            for variant in renamed.with_context(**NO_AUTO_VARIANTS):
                variant.product_template_attribute_value_ids = [(6, 0, [ptav[value_of[variant]].id])]
                self.env.flush_all()
        if new_line != line:
            self._ke_drop_values(line, line.product_template_value_ids, archive_line=True)
        self._ke_prune_attribute_values()
        if renamed:
            self._ke_variants_renamed(renamed)
        return f'varianti [{attr.with_context(lang="it_IT").name}]: {summary}'
