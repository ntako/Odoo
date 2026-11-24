"""
Spostamento delle varianti in una famiglia (applicazione delle proposte di famiglia, 2026-11-20).

Le varianti si SPOSTANO (stesso product.product, product_tmpl_id cambiato), non si ricreano: giacenze,
movimenti, ordini, inserzioni Amazon, EAN, codici, costi restano attaccati alla variante.

Struttura delle famiglie come quella della sync Amazon: un solo attributo "che crea varianti" per
prodotto (valori compositi, mai combinazioni incrociate che farebbero nascere varianti fantasma).
Tutto con create_product_product=False: Odoo non crea né archivia varianti da solo; i valori degli
attributi li assegniamo noi variante per variante.

Cosa segue la variante:
- specifiche: le sue (per variante) passano alla famiglia; quelle del vecchio prodotto (valide per
  tutte le sue varianti) diventano sue per variante, se non ne ha già una per quella specifica;
- immagini (galleria): le sue passano; quelle di un vecchio prodotto semplice diventano sue; l'immagine
  principale del vecchio prodotto diventa l'immagine della variante, se non ne ha una;
- prezzo: se il vecchio prodotto aveva un prezzo diverso dalla famiglia, diventa il prezzo della
  variante (ke_list_price);
- fornitori (listino acquisti) del vecchio prodotto semplice.
Una famiglia che era un prodotto semplice: le sue specifiche diventano della sua variante (prima
valevano per "tutte le varianti", cioè per lei sola).

I prodotti rimasti senza varianti si archiviano (mai cancellati: ordini e movimenti passati li usano).
Ai prodotti che restano si tolgono i valori degli attributi che nessuna variante usa più (altrimenti
un'aggiunta successiva, es. dalla sync Amazon, ricreerebbe le varianti spostate).

Aggancio per gli altri moduli (Amazon, PrestaShop): _ke_family_blockers, _ke_family_new_vals,
_ke_family_before_move, _ke_family_after_move su product.template.
"""
from odoo import api, models
from odoo.exceptions import UserError

NO_AUTO_VARIANTS = {'create_product_product': False}
# Operazioni in blocco: niente tracciamento nel chatter a ogni scrittura (era 1/5 del tempo); i messaggi di
# riepilogo sui prodotti restano (message_post).
QUIET = {'tracking_disable': True, 'mail_notrack': True, 'mail_create_nolog': True}

# Campi del prodotto di partenza copiati su una famiglia nuova (oltre al nome).
_NEW_FAMILY_FIELDS = (
    'categ_id', 'type', 'is_storable', 'uom_id', 'list_price', 'sale_ok', 'purchase_ok',
    'taxes_id', 'supplier_taxes_id', 'ke_brand_id', 'weight', 'product_length', 'product_width',
    'product_height', 'ke_supplier_category', 'company_id', 'tracking', 'invoice_policy',
)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # ------------------------------------------------------------------
    # Agganci (estesi da ke_amazon_sync, ke_prestashop_sync)
    # ------------------------------------------------------------------

    @api.model
    def _ke_family_blockers(self, target, variants):
        """Motivi per cui lo spostamento non si fa in automatico (lista vuota: si può)."""
        out = []
        if target and len(self._ke_variant_lines(target)) > 1:
            out.append(f'"{target.name}" ha più attributi che creano varianti: da sistemare a mano')
        if target and target.has_dynamic_attributes():
            out.append(f'"{target.name}" ha attributi con varianti create su richiesta')
        if variants.filtered(lambda v: not v.active):
            out.append('alcune varianti sono archiviate')
        return out

    @api.model
    def _ke_family_new_vals(self, source, name):
        """Valori di una famiglia nuova, presi dal prodotto da cui arriva la maggior parte delle varianti."""
        vals = {'name': name}
        for field in _NEW_FAMILY_FIELDS:
            if field not in source._fields:
                continue
            value = source[field]
            vals[field] = [(6, 0, value.ids)] if source._fields[field].type == 'many2many' else (
                value.id if source._fields[field].type == 'many2one' else value)
        return vals

    def _ke_family_before_move(self, variants):
        """Prima dello spostamento (self = famiglia di destinazione, ancora com'era)."""

    def _ke_family_after_move(self, variants, sources, emptied):
        """Dopo lo spostamento. sources: prodotti di partenza; emptied: quelli archiviati perché vuoti."""

    # ------------------------------------------------------------------
    # Spostamento
    # ------------------------------------------------------------------

    @api.model
    def _ke_variant_lines(self, tmpl):
        return tmpl.attribute_line_ids.filtered(lambda l: l.attribute_id.create_variant != 'no_variant')

    @api.model
    def _ke_variant_label(self, variant):
        names = variant.product_template_attribute_value_ids.filtered(
            lambda v: v.attribute_id.create_variant != 'no_variant').mapped('name')
        return ' – '.join(n for n in names if n) or variant.default_code or variant.barcode or f'#{variant.id}'

    @api.model
    def _ke_family_attribute(self, target, moving):
        """Attributo della famiglia: quello che ha già; o quello comune alle varianti che arrivano; o "Variante"."""
        lines = self._ke_variant_lines(target)
        if lines:
            return lines.attribute_id
        attrs = {tuple(v.product_template_attribute_value_ids.attribute_id.ids) for v in moving}
        if len(attrs) == 1 and len(next(iter(attrs))) == 1:
            attr = self.env['product.attribute'].browse(next(iter(attrs))[0])
            if attr.create_variant == 'always':
                return attr
        Attr = self.env['product.attribute']
        return Attr.search([('name', '=', 'Variante'), ('create_variant', '=', 'always')], limit=1) or \
            Attr.create({'name': 'Variante', 'create_variant': 'always'})

    @api.model
    def ke_family_move(self, variants, target=None, name=None, family_name=None):
        """
        Porta 'variants' nella famiglia 'target' (o in una nuova, chiamata 'name'). Ritorna la famiglia.
        Da chiamare dentro un savepoint: in caso di errore non deve restare niente a metà.
        """
        variants = variants.exists()
        if not variants:
            raise UserError('Nessuna variante da spostare.')
        blockers = self._ke_family_blockers(target, variants)
        if blockers:
            raise UserError('; '.join(blockers))
        Tmpl = self.with_context(**NO_AUTO_VARIANTS)
        if not target:
            source = max(variants.product_tmpl_id, key=lambda t: (len(variants & t.product_variant_ids), -t.id))
            target = Tmpl.create(self._ke_family_new_vals(source, name or family_name or source.name))
            for lang in self.env['res.lang'].search([('active', '=', True)]).mapped('code'):
                target.with_context(lang=lang).name = name or family_name or source.name
        target = target.with_context(**NO_AUTO_VARIANTS)
        moving = variants.filtered(lambda v: v.product_tmpl_id != target)
        was_single = len(target.product_variant_ids) <= 1 and not self._ke_variant_lines(target)
        target._ke_family_before_move(moving)

        # Valori dell'attributo: quelli delle varianti già nella famiglia restano; le nuove prendono la loro
        # etichetta (es. "0,16 mm" o il codice), resa unica se serve.
        attr = self._ke_family_attribute(target, moving)
        line = target.attribute_line_ids.filtered(lambda l: l.attribute_id == attr)
        keep = target.product_variant_ids if was_single else self.env['product.product']
        used = set() if was_single else {self._ke_variant_label(v).casefold() for v in target.product_variant_ids}
        labels = {}
        for variant in keep | moving:
            label = self._ke_variant_label(variant)
            for candidate in (label, f'{label} ({variant.default_code})', f'{label} (#{variant.id})'):
                if candidate.casefold() not in used:
                    label = candidate
                    break
            used.add(label.casefold())
            labels[variant] = label
        Value = self.env['product.attribute.value']
        known = self._ke_attribute_values(attr)
        values = {}
        for variant, label in labels.items():
            if label not in known:
                known[label] = Value.create({'attribute_id': attr.id, 'name': label}).id
            values[variant] = Value.browse(known[label])
        all_values = Value.browse([v.id for v in values.values()])
        if line:
            line.write({'value_ids': [(4, v.id) for v in all_values]})
        else:
            line = self.env['product.template.attribute.line'].with_context(**NO_AUTO_VARIANTS).create({
                'product_tmpl_id': target.id, 'attribute_id': attr.id, 'value_ids': [(6, 0, all_values.ids)]})
        ptav = {p.product_attribute_value_id: p for p in line.product_template_value_ids}

        if was_single and target.product_variant_ids:
            single = target.product_variant_ids
            target._ke_single_to_family(single)
            for variant in single:
                variant.with_context(**NO_AUTO_VARIANTS).product_template_attribute_value_ids = \
                    [(6, 0, [ptav[values[variant]].id])]
            self.env.flush_all()

        sources = moving.product_tmpl_id
        source_single = {s: len(s.product_variant_ids) == 1 for s in sources}
        moved_from, source_of = {}, {}
        for variant in moving:
            source_of[variant.id] = variant.product_tmpl_id
            moved_from.setdefault(variant.product_tmpl_id, []).append(variant.default_code or variant.display_name)
        # A blocchi (2026-11-20, 18.000 varianti da riunire dopo l'import): prima i valori di tutte (combinazione
        # unica già nei vecchi prodotti), un salvataggio, poi il prodotto di tutte in una scrittura. Scritti
        # insieme, Odoo salverebbe il prodotto con la vecchia combinazione e violerebbe l'unicità.
        for variant in moving.with_context(**NO_AUTO_VARIANTS):
            variant.product_template_attribute_value_ids = [(6, 0, [ptav[values[variant]].id])]
        self.env.flush_all()
        moving.with_context(**NO_AUTO_VARIANTS).write({'product_tmpl_id': target.id})
        self.env.flush_all()
        target._ke_carry_batch(moving, source_of, source_single)

        emptied = self.browse()
        for source in sources:
            if not source.product_variant_ids:
                # Nessun messaggio sul prodotto archiviato (uno per prodotto era 1/6 del tempo con migliaia di
                # prodotti): lo elenca il messaggio sulla famiglia.
                source.write({'active': False})
                emptied |= source
            else:
                source._ke_prune_attribute_values()
                source.message_post(body=f'Varianti spostate nella famiglia "{target.name}" (id={target.id}): '
                                         + ', '.join(moved_from[source]))
        if family_name and 'ke_supplier_name' in target._fields:
            target.ke_supplier_name = family_name
        target.message_post(body='Famiglia riorganizzata: arrivate ' + ', '.join(
            f'{v.default_code or v.display_name}' for v in moving) + (
            f' (da {", ".join(s.name for s in sources)})' if sources else ''))
        target._ke_family_after_move(moving, sources, emptied)
        self.env.flush_all()
        return target

    @api.model
    def _ke_attribute_values(self, attr):
        """{nome: id} dei valori di un attributo, letti una volta (cercarli per nome uno alla volta rallentava
        con migliaia di valori)."""
        self.env.flush_all()
        self.env.cr.execute("""SELECT id, COALESCE(name->>%s, name->>'en_US') FROM product_attribute_value
                                WHERE attribute_id = %s ORDER BY id""", (self.env.lang or 'en_US', attr.id))
        known = {}
        for vid, name in self.env.cr.fetchall():
            known.setdefault(name, vid)
        return known

    def _ke_single_to_family(self, variant):
        """Il prodotto (semplice fin qui) diventa famiglia: le specifiche "per tutte le varianti" erano della
        sua unica variante e lo restano."""
        self.ensure_one()
        specs = self.env['ke.product.spec'].search([('product_tmpl_id', '=', self.id), ('product_id', '=', False)])
        specs.write({'product_id': variant.id})

    def _ke_carry_batch(self, moving, source_of, source_single):
        """
        Dati che seguono le varianti dai vecchi prodotti (self = famiglia di destinazione), con una ricerca per
        famiglia invece che per variante: specifiche (quelle della variante; quelle "per tutte" del vecchio
        prodotto diventano sue — spostate se il prodotto aveva solo lei, copiate se no), galleria e listino
        acquisti di un vecchio prodotto semplice, immagine principale se la variante non ne ha una, prezzo.
        """
        self.ensure_one()
        Spec = self.env['ke.product.spec']
        own = Spec.search([('product_id', 'in', moving.ids)])
        own.write({'product_tmpl_id': self.id})
        mine = {(s.product_id.id, s.key_id.id) for s in own}
        sources = self.env['product.template'].browse({s.id for s in source_of.values()})
        variant_of_source = {}
        for vid, source in source_of.items():
            variant_of_source.setdefault(source.id, []).append(vid)
        singles = sources.filtered(lambda t: source_single.get(t))
        move_to = {}
        for spec in Spec.search([('product_tmpl_id', 'in', sources.ids), ('product_id', '=', False)]):
            for vid in variant_of_source[spec.product_tmpl_id.id]:
                if (vid, spec.key_id.id) in mine:
                    continue
                if source_single.get(spec.product_tmpl_id):
                    move_to.setdefault(vid, []).append(spec.id)
                else:
                    spec.copy({'product_tmpl_id': self.id, 'product_id': vid})
        for vid, spec_ids in move_to.items():
            Spec.browse(spec_ids).write({'product_tmpl_id': self.id, 'product_id': vid})
        Media = self.env['ke.product.media']
        Media.search([('product_id', 'in', moving.ids)]).write({'product_tmpl_id': self.id})
        for media in Media.search([('product_tmpl_id', 'in', singles.ids), ('product_id', '=', False)]):
            media.write({'product_tmpl_id': self.id, 'product_id': variant_of_source[media.product_tmpl_id.id][0]})
        for seller in self.env['product.supplierinfo'].search([('product_tmpl_id', 'in', singles.ids)]):
            seller.write({'product_tmpl_id': self.id, 'product_id': variant_of_source[seller.product_tmpl_id.id][0]})
        # Immagine principale: solo dove il vecchio prodotto ne ha una e la variante no (lette dagli allegati,
        # senza caricare le immagini).
        self.env.flush_all()
        self.env.cr.execute("""SELECT res_id FROM ir_attachment WHERE res_model = 'product.template'
                                 AND res_field = 'image_1920' AND res_id = ANY(%s)""", (sources.ids,))
        with_image = {r[0] for r in self.env.cr.fetchall()}
        self.env.cr.execute("""SELECT res_id FROM ir_attachment WHERE res_model = 'product.product'
                                 AND res_field = 'image_variant_1920' AND res_id = ANY(%s)""", (moving.ids,))
        has_own = {r[0] for r in self.env.cr.fetchall()}
        for variant in moving:
            source = source_of[variant.id]
            if source.id in with_image and variant.id not in has_own:
                variant.image_variant_1920 = source.image_1920
            if 'ke_list_price' in variant._fields and not variant.ke_list_price \
                    and abs(source.list_price - self.list_price) > 0.005:
                variant.ke_list_price = source.list_price

    def _ke_prune_attribute_values(self):
        """Toglie dalle righe attributo i valori che nessuna variante (anche archiviata) usa più."""
        self.ensure_one()
        used = self.with_context(active_test=False).product_variant_ids.product_template_attribute_value_ids
        for line in self.attribute_line_ids:
            unused = line.product_template_value_ids.filtered(lambda p: p not in used)
            if unused and unused != line.product_template_value_ids:
                self._ke_drop_values(line, unused)

    @api.model
    def _ke_drop_values(self, line, ptavs, archive_line=False):
        """
        Toglie valori da una riga attributo (o archivia la riga) ARCHIVIANDOLI (ptav_active=False), come fa
        Odoo quando non può cancellarli: un valore archiviato non genera varianti. Senza il tentativo di
        cancellazione di Odoo (un salvataggio e i controlli su tutte le tabelle collegate per ogni valore: 1/3
        del tempo con migliaia di famiglie). Nessuna variante usa più questi valori (chi chiama lo sa).
        """
        ctx = dict(NO_AUTO_VARIANTS, update_product_template_attribute_values=False)
        vals = {'value_ids': [(3, p.product_attribute_value_id.id) for p in ptavs]}
        if archive_line:
            vals = {'active': False}
        line.with_context(**ctx).write(vals)
        ptavs.write({'ptav_active': False})

    # ------------------------------------------------------------------
    # Varianti nuove (import del catalogo fornitore, 2026-11-20)
    # ------------------------------------------------------------------

    def ke_family_add_variants(self, items):
        """
        Aggiunge varianti nuove alla famiglia self. items: lista di (valori della variante, etichetta).
        Stessa struttura delle altre famiglie: un attributo che crea varianti (quello che ha, o "Variante"),
        un valore per variante; mai combinazioni incrociate. Un prodotto semplice diventa famiglia (la sua
        variante prende come etichetta il codice). Ritorna le varianti create.
        """
        self.ensure_one()
        Product = self.env['product.product'].with_context(**NO_AUTO_VARIANTS)
        if not items:
            return Product
        tmpl = self.with_context(**NO_AUTO_VARIANTS)
        lines = self._ke_variant_lines(tmpl)
        if len(lines) > 1:
            raise UserError(f'"{self.name}" ha più attributi che creano varianti: varianti da aggiungere a mano')
        if not tmpl.product_variant_ids and len(items) == 1 and not lines:
            vals, _label = items[0]
            return Product.create(dict(vals, product_tmpl_id=tmpl.id))
        attr = lines.attribute_id or self._ke_family_attribute(tmpl, self.env['product.product'])
        line = lines
        existing = tmpl.product_variant_ids
        used = {self._ke_variant_label(v).casefold() for v in existing} if line else set()
        Value = self.env['product.attribute.value']
        labels = []
        if existing and not line:
            for variant in existing:
                label = self._ke_variant_label(variant)
                used.add(label.casefold())
                labels.append((variant, label))
        new_labels = []
        for vals, label in items:
            label = label or vals.get('default_code') or vals.get('barcode') or 'Variante'
            for candidate in (label, f"{label} ({vals.get('default_code')})"):
                if candidate.casefold() not in used:
                    label = candidate
                    break
            used.add(label.casefold())
            new_labels.append(label)
        known = self._ke_attribute_values(attr)
        values = {}
        for label in [l for _v, l in labels] + new_labels:
            if label not in known:
                known[label] = Value.create({'attribute_id': attr.id, 'name': label}).id
            values[label] = Value.browse(known[label])
        all_values = Value.browse([v.id for v in values.values()])
        if line:
            line.with_context(**NO_AUTO_VARIANTS).write({'value_ids': [(4, v.id) for v in all_values]})
        else:
            line = self.env['product.template.attribute.line'].with_context(**NO_AUTO_VARIANTS).create({
                'product_tmpl_id': tmpl.id, 'attribute_id': attr.id, 'value_ids': [(6, 0, all_values.ids)]})
        ptav = {p.product_attribute_value_id: p for p in line.product_template_value_ids}
        if labels:
            tmpl._ke_single_to_family(existing)
            for variant, label in labels:
                variant.with_context(**NO_AUTO_VARIANTS).product_template_attribute_value_ids = \
                    [(6, 0, [ptav[values[label]].id])]
            self.env.flush_all()
        created = Product.create([dict(vals, product_tmpl_id=tmpl.id,
                                       product_template_attribute_value_ids=[(6, 0, [ptav[values[label]].id])])
                                  for (vals, _l), label in zip(items, new_labels)])
        return created
