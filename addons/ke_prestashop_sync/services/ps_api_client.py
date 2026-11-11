"""
Client per la WebService API di PrestaShop.

Autenticazione: HTTP Basic Auth con API key come username, password vuota.
Letture: output_format=JSON
Scritture: body XML (GET-then-PUT per aggiornamenti parziali).
"""

import logging
import re
import xml.etree.ElementTree as ET

import requests

_log = logging.getLogger(__name__)


class PsResourceNotFound(requests.HTTPError):
    """La risorsa PrestaShop richiesta non esiste (HTTP 404).

    Tipicamente: un ps_product_id / ps_combination_id salvato in Odoo che punta
    a un prodotto cancellato su PrestaShop (link orfano).
    """


_PS_ORDER_STATES = {
    1: 'Awaiting check payment',
    2: 'Payment accepted',
    3: 'Processing in progress',
    4: 'Shipped',
    5: 'Delivered',
    6: 'Cancelled',
    7: 'Refunded',
}


class PsApiClient:
    def __init__(self, env):
        params = env['ir.config_parameter'].sudo()
        self._url = (params.get_param('ke_prestashop_sync.url') or '').rstrip('/')
        self._key = params.get_param('ke_prestashop_sync.api_key') or ''
        self._lang_id = int(params.get_param('ke_prestashop_sync.lang_id') or 1)
        self._shop_id = int(params.get_param('ke_prestashop_sync.shop_id') or 1)
        self._category_id = int(params.get_param('ke_prestashop_sync.category_id') or 2)
        self._tax_rules_group_id = int(
            params.get_param('ke_prestashop_sync.tax_rules_group_id') or 1)

    # ------------------------------------------------------------------
    # Primitivi HTTP
    # ------------------------------------------------------------------

    def _ps_key_param(self):
        # L'API key PS viene passata come ws_key query param (più affidabile
        # della Basic Auth con proxy/web server che filtrano Authorization).
        return {'ws_key': self._key}

    @staticmethod
    def _ps_error_detail(text):
        """Estrae i messaggi <error><message> da una risposta di errore PS."""
        try:
            root = ET.fromstring(text or '')
            msgs = []
            for err in root.iter('error'):
                code = (err.findtext('code') or '').strip()
                msg = (err.findtext('message') or '').strip()
                msgs.append(f'{msg} (code {code})' if code else msg)
            msgs = [m for m in msgs if m]
            if msgs:
                return ' | '.join(msgs)
        except ET.ParseError:
            pass
        return (text or '').strip().replace('\n', ' ')[:500]

    def _check(self, resp, what):
        """Solleva un'eccezione leggibile (con il messaggio PS) se la risposta non è OK."""
        if resp.ok:
            return
        detail = self._ps_error_detail(resp.text)
        _log.error('PS %s → HTTP %s: %s', what, resp.status_code, resp.text[:1000])
        exc_cls = PsResourceNotFound if resp.status_code == 404 else requests.HTTPError
        raise exc_cls(
            f'{what} → HTTP {resp.status_code}: {detail}', response=resp)

    def _get_json(self, resource, params=None):
        url = f'{self._url}/api/{resource}'
        p = {'output_format': 'JSON', 'display': 'full'}
        p.update(self._ps_key_param())
        if params:
            p.update(params)
        resp = requests.get(url, params=p, timeout=30)
        self._check(resp, f'GET {resource}')
        return resp.json()

    def _get_xml_str(self, resource, resource_id):
        """Recupera il body XML grezzo di una risorsa (per round-trip PUT)."""
        url = f'{self._url}/api/{resource}/{resource_id}'
        resp = requests.get(url,
                            params=self._ps_key_param() or None, timeout=30)
        self._check(resp, f'GET {resource}/{resource_id}')
        return resp.text

    def _put_xml(self, resource, resource_id, xml_bytes):
        url = f'{self._url}/api/{resource}/{resource_id}'
        resp = requests.put(
            url,
            data=xml_bytes,
            headers={'Content-Type': 'text/xml; charset=UTF-8'},
            params=self._ps_key_param(),
            timeout=30,
        )
        self._check(resp, f'PUT {resource}/{resource_id}')

    def _post_xml(self, resource, xml_bytes):
        """Crea una nuova risorsa e restituisce l'ID creato."""
        url = f'{self._url}/api/{resource}'
        p = {'output_format': 'JSON'}
        p.update(self._ps_key_param())
        resp = requests.post(
            url,
            data=xml_bytes,
            headers={'Content-Type': 'text/xml; charset=UTF-8'},
            params=p,
            timeout=30,
        )
        self._check(resp, f'POST {resource}')
        data = resp.json()
        if not isinstance(data, dict):
            _log.error('PS POST %s: risposta JSON inattesa (tipo %s): %s',
                       resource, type(data).__name__, str(data)[:500])
            return 0
        singular = resource.rstrip('s')
        item = data.get(singular) or data.get(resource)
        if isinstance(item, list):
            item = item[0] if item else {}
        if not isinstance(item, dict):
            item = {}
        return int(item.get('id', 0))

    # ------------------------------------------------------------------
    # Validazione
    # ------------------------------------------------------------------

    def validate_credentials(self):
        if not self._url or not self._key:
            raise ValueError('URL e API Key PrestaShop sono obbligatori.')
        # Usa XML (senza output_format=JSON): il root endpoint PS non supporta JSON
        p = self._ps_key_param()
        resp = requests.get(f'{self._url}/api/', params=p, timeout=30)
        self._check(resp, 'GET /api/')
        return True

    # ------------------------------------------------------------------
    # Prodotti
    # ------------------------------------------------------------------

    def get_product_ids(self):
        """Lista tutti gli ID prodotto disponibili."""
        data = self._get_json('products', {'display': '[id]'})
        products = data.get('products') or []
        return [int(p['id']) for p in products]

    def get_product(self, ps_id):
        """Dettaglio prodotto come dict JSON."""
        data = self._get_json(f'products/{ps_id}')
        return data.get('product', {})

    def get_product_name(self, product_dict):
        """Estrae il nome nella lingua configurata (fallback: prima disponibile)."""
        names = product_dict.get('name', [])
        if isinstance(names, str):
            return names
        for entry in names:
            if int(entry.get('id', 0)) == self._lang_id:
                return entry.get('value', '')
        return names[0].get('value', '') if names else ''

    def get_product_description(self, product_dict, field='description_short'):
        """Estrae descrizione nella lingua configurata."""
        entries = product_dict.get(field, [])
        if isinstance(entries, str):
            return entries
        for entry in entries:
            if int(entry.get('id', 0)) == self._lang_id:
                return entry.get('value', '')
        return entries[0].get('value', '') if entries else ''

    def upload_product_image(self, ps_id, image_bytes):
        """Carica un'immagine del prodotto su PS (multipart POST) e ne restituisce l'ID (0 se non letto)."""
        url = f'{self._url}/api/images/products/{ps_id}'
        mime, filename = self._detect_image_mime(image_bytes)
        resp = requests.post(
            url,
            files={'image': (filename, image_bytes, mime)},
            params={**self._ps_key_param(), 'output_format': 'JSON'},
            timeout=60,
        )
        self._check(resp, f'POST images/products/{ps_id}')
        try:
            return int((resp.json().get('image') or {}).get('id') or 0)
        except ValueError:
            match = re.search(r'<id>\s*(?:<!\[CDATA\[)?(\d+)', resp.text or '')
            return int(match.group(1)) if match else 0

    def get_product_image_ids(self, ps_id):
        """ID delle immagini di un prodotto PS ([] se non ne ha)."""
        url = f'{self._url}/api/images/products/{ps_id}'
        resp = requests.get(url, params=self._ps_key_param() or None, timeout=30)
        if resp.status_code == 404:
            return []
        self._check(resp, f'GET images/products/{ps_id}')
        return [int(x) for x in re.findall(r'<declination[^>]*\bid="(\d+)"', resp.text or '')]

    def delete_product_image(self, ps_id, image_id):
        url = f'{self._url}/api/images/products/{ps_id}/{image_id}'
        resp = requests.delete(url, params=self._ps_key_param() or None, timeout=30)
        if resp.status_code != 404:
            self._check(resp, f'DELETE images/products/{ps_id}/{image_id}')

    def set_combination_images(self, combo_id, image_ids):
        """Associa le immagini (già caricate sul prodotto) a una combinazione."""
        root = ET.fromstring(self._get_xml_str('combinations', combo_id))
        combo = root.find('combination')
        assoc = combo.find('associations')
        if assoc is None:
            assoc = ET.SubElement(combo, 'associations')
        images = assoc.find('images')
        if images is None:
            images = ET.SubElement(assoc, 'images')
        for child in list(images):
            images.remove(child)
        for image_id in image_ids:
            ET.SubElement(ET.SubElement(images, 'image'), 'id').text = str(image_id)
        self._put_xml('combinations', combo_id, ET.tostring(root, encoding='utf-8', xml_declaration=True))

    @staticmethod
    def _detect_image_mime(data):
        if len(data) >= 4 and data[:4] == b'\x89PNG':
            return 'image/png', 'product.png'
        if len(data) >= 2 and data[:2] == b'\xff\xd8':
            return 'image/jpeg', 'product.jpg'
        if len(data) >= 4 and data[:3] == b'GIF':
            return 'image/gif', 'product.gif'
        return 'image/jpeg', 'product.jpg'

    def _update_resource_fields(self, resource, resource_id, updates):
        """GET del body XML, sovrascrive gli elementi indicati e fa PUT.

        updates: dict {xpath_relativo_alla_root: nuovo_testo}.
        """
        xml_str = self._get_xml_str(resource, resource_id)
        root = ET.fromstring(xml_str)
        for xpath, value in updates.items():
            el = root.find(xpath)
            if el is not None:
                el.text = value
        xml_bytes = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        self._put_xml(resource, resource_id, xml_bytes)

    def update_product_price(self, ps_id, new_price):
        """Aggiorna il prezzo di un prodotto su PS (GET → modifica → PUT)."""
        self._update_resource_fields('products', ps_id, {
            './/product/price': f'{float(new_price):.6f}',
        })

    def update_product_active(self, ps_id, active):
        """Attiva/disattiva un prodotto su PS (active = 0/1)."""
        self._update_resource_fields('products', ps_id, {
            './/product/active': str(int(bool(active))),
        })

    @staticmethod
    def _clean_reference(reference):
        """Sanifica un codice riferimento per PrestaShop.

        PS `Validate::isReference` vieta i caratteri < > ; = { } (e in alcune
        installazioni anche #) e il campo DB è varchar(64). Un valore non
        conforme dà HTTP 400 (code 85).
        """
        ref = re.sub(r'[<>;={}#]', '', reference or '')
        ref = re.sub(r'[\x00-\x1f]', '', ref).strip()
        return ref[:64]

    @staticmethod
    def _make_slug(name):
        """Genera un link_rewrite ASCII valido per PrestaShop (obbligatorio alla creazione)."""
        from odoo.addons.ke_product_catalog.models.product_website import slugify
        return slugify(name)

    def find_product_by_reference(self, reference):
        """Cerca un prodotto PS per codice riferimento; restituisce l'ID o None."""
        reference = self._clean_reference(reference)
        if not reference:
            return None
        data = self._get_json('products', {
            'display': '[id]',
            'filter[reference]': f'[{reference}]',
        })
        if not isinstance(data, dict):
            _log.warning('find_product_by_reference: risposta PS inattesa (tipo %s): %s',
                         type(data).__name__, str(data)[:200])
            return None
        products = data.get('products') or []
        if not isinstance(products, list):
            return None
        return int(products[0]['id']) if products else None

    @staticmethod
    def _split_barcode(code):
        """(ean13, upc): PS accetta EAN-13 (13 cifre) e UPC (12 cifre) in campi distinti."""
        code = str(code or '').strip()
        if not code.isdigit():
            return '', ''
        if len(code) == 13:
            return code, ''
        if len(code) == 12:
            return '', code
        return '', ''

    # Limiti dei campi PS 8 (validate dell'ObjectModel Product).
    _META_TITLE_MAX = 255
    _META_DESCRIPTION_MAX = 512

    @staticmethod
    def _cdata(value):
        return (value or '').replace(']]>', ']]&gt;')

    def _seo_xml(self, description, meta_title, meta_description):
        """Blocchi XML dei testi del sito presenti (descrizione, meta title, meta description)."""
        out = ''
        for tag, value in (('description', description),
                           ('meta_title', (meta_title or '')[:self._META_TITLE_MAX]),
                           ('meta_description', (meta_description or '')[:self._META_DESCRIPTION_MAX])):
            if value:
                out += (f'\n    <{tag}>\n      <language id="{self._lang_id}"><![CDATA[{self._cdata(value)}]]>'
                        f'</language>\n    </{tag}>')
        return out

    @staticmethod
    def _clean_name(name):
        # PS isCatalogName vieta ; < > = { } e in questa installazione anche #
        return re.sub(r'[<>={}#]', '', (name or '')).replace(';', ',').strip()[:128]

    def create_product(self, name, reference='', ean13='', price=0.0,
                       description_short='', active=1, category_id=0, manufacturer_id=0,
                       description='', meta_title='', meta_description='', link_rewrite='',
                       width=0.0, height=0.0, depth=0.0, weight=0.0, product_type='standard'):
        """Crea un prodotto su PS e restituisce il nuovo ID (testi del sito: ke_web_* di ke_product_catalog)."""
        ean13, upc = self._split_barcode(ean13)
        name = self._clean_name(name)
        category_id = int(category_id or self._category_id)

        # description_short: tronca a 450 char (limite PS osservato: min ~596, con margine)
        description_short = (description_short or '')[:450]
        # name: XML entities (NO CDATA) — così il parser XML decodifica &amp;→& prima che
        # PS validi con isCatalogName; CDATA è testo letterale, &amp; conterrebbe ';' vietato
        name_xml = name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        # description_short: CDATA senza pre-escaping HTML (solo ]]> va protetto)
        desc_cdata = description_short.replace(']]>', ']]&gt;')
        # reference: sanificato (PS vieta < > ; = { }, max 64 char)
        ref_cdata = self._clean_reference(reference).replace(']]>', ']]&gt;')
        slug = self._make_slug(link_rewrite or name)
        seo = self._seo_xml(description, meta_title, meta_description)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <product>
    <active>{active}</active>
    <state>1</state>
    <condition><![CDATA[new]]></condition>
    <visibility><![CDATA[both]]></visibility>
    <available_for_order>1</available_for_order>
    <show_price>1</show_price>
    <price>{float(price):.6f}</price>
    <product_type>{product_type}</product_type>
    <width>{float(width or 0):.6f}</width>
    <height>{float(height or 0):.6f}</height>
    <depth>{float(depth or 0):.6f}</depth>
    <weight>{float(weight or 0):.6f}</weight>
    <id_tax_rules_group>{self._tax_rules_group_id}</id_tax_rules_group>
    <id_manufacturer>{int(manufacturer_id or 0)}</id_manufacturer>
    <reference><![CDATA[{ref_cdata}]]></reference>
    <ean13>{ean13}</ean13>
    <upc>{upc}</upc>
    <name>
      <language id="{self._lang_id}">{name_xml}</language>
    </name>
    <link_rewrite>
      <language id="{self._lang_id}">{slug}</language>
    </link_rewrite>
    <description_short>
      <language id="{self._lang_id}"><![CDATA[{desc_cdata}]]></language>
    </description_short>{seo}
    <id_category_default>{category_id}</id_category_default>
    <associations>
      <categories>
        <category><id>{category_id}</id></category>
      </categories>
    </associations>
  </product>
</prestashop>""".encode('utf-8')
        return self._post_xml('products', xml)

    # Campi che il GET restituisce ma che PS rifiuta in scrittura.
    _PRODUCT_READONLY = ('manufacturer_name', 'quantity', 'position_in_category')

    def update_product(self, ps_id, name, reference='', ean13='', price=0.0,
                       description_short='', active=1, category_id=0, manufacturer_id=0,
                       replace_category=False, description='', meta_title='', meta_description='',
                       link_rewrite='', replace_texts=False, width=0.0, height=0.0, depth=0.0,
                       weight=0.0, product_type=''):
        """
        Aggiorna un prodotto PS esistente (GET → modifica → PUT): nome e
        descrizione breve nella lingua configurata, prezzo, codici, marca.
        Categoria predefinita e associazione alle categorie: sostituite solo
        con replace_category o se il prodotto su PS è soltanto nella radice /
        nella categoria predefinita (2026-11-10: l'albero del negozio è più
        dettagliato di quello Odoo, la scelta fatta sul sito resta).
        L'indirizzo (link_rewrite) non viene cambiato.
        """
        ean13, upc = self._split_barcode(ean13)
        category_id = int(category_id or self._category_id)
        root = ET.fromstring(self._get_xml_str('products', ps_id))
        prod = root.find('product')

        def set_text(tag, value):
            el = prod.find(tag)
            if el is not None:
                el.text = value

        set_text('price', f'{float(price):.6f}')
        set_text('reference', self._clean_reference(reference))
        set_text('ean13', ean13)
        set_text('upc', upc)
        set_text('id_manufacturer', str(int(manufacturer_id or 0)))
        cats = prod.find('associations/categories')
        current = {int(c.findtext('id') or 0) for c in (list(cats) if cats is not None else [])}
        current.add(int(prod.findtext('id_category_default') or 0))
        set_category = replace_category or not (current - {0, 1, 2, self._category_id})
        if set_category:
            set_text('id_category_default', str(category_id))
        set_text('active', str(int(bool(active))))
        # Tipo prodotto: solo se su PS è vuoto (prodotti creati prima del 2026-11-11).
        type_el = prod.find('product_type')
        if product_type and type_el is not None and not (type_el.text or '').strip():
            type_el.text = product_type
        # Dimensioni e peso dell'imballo (spedizione): sempre, se Odoo li ha.
        for tag, value in (('width', width), ('height', height), ('depth', depth), ('weight', weight)):
            if value:
                set_text(tag, f'{float(value):.6f}')
        # Testi (nome, descrizioni, meta, URL): solo con replace_texts. Deciso con
        # l'utente il 2026-11-11: i testi si scrivono alla creazione; sui
        # prodotti già sul sito si aggiornano solo con l'opzione "Aggiorna testi".
        texts = [('name', self._clean_name(name)), ('description_short', (description_short or '')[:450])]
        if description:
            texts.append(('description', description))
        if meta_title:
            texts.append(('meta_title', meta_title[:self._META_TITLE_MAX]))
        if meta_description:
            texts.append(('meta_description', meta_description[:self._META_DESCRIPTION_MAX]))
        if link_rewrite:
            texts.append(('link_rewrite', self._make_slug(link_rewrite)))
        for tag, value in (texts if replace_texts else []):
            el = prod.find(tag)
            for lang in (el.findall('language') if el is not None else []):
                if int(lang.get('id', 0)) == self._lang_id:
                    lang.text = value
        if set_category and cats is not None:
            for child in list(cats):
                cats.remove(child)
            cat = ET.SubElement(cats, 'category')
            ET.SubElement(cat, 'id').text = str(category_id)
        for tag in self._PRODUCT_READONLY:
            el = prod.find(tag)
            if el is not None:
                prod.remove(el)
        self._put_xml('products', ps_id, ET.tostring(root, encoding='utf-8', xml_declaration=True))

    # ------------------------------------------------------------------
    # Categorie e marche
    # ------------------------------------------------------------------

    def _lang_value(self, value):
        if isinstance(value, list):
            for entry in value:
                if int(entry.get('id', 0)) == self._lang_id:
                    return entry.get('value', '')
            return value[0].get('value', '') if value else ''
        return value or ''

    def get_categories(self):
        """Tutte le categorie PS: [{'id', 'parent', 'name', 'level', 'active'}]."""
        data = self._get_json('categories', {'display': '[id,id_parent,name,level_depth,active]'})
        rows = (data.get('categories') or []) if isinstance(data, dict) else []
        return [{
            'id': int(c['id']), 'parent': int(c.get('id_parent') or 0),
            'name': self._lang_value(c.get('name')), 'level': int(c.get('level_depth') or 0),
            'active': str(c.get('active')) == '1',
        } for c in (rows if isinstance(rows, list) else []) if isinstance(c, dict)]

    def get_manufacturers(self):
        """Tutte le marche PS: [{'id', 'name'}]."""
        data = self._get_json('manufacturers', {'display': '[id,name]'})
        rows = (data.get('manufacturers') or []) if isinstance(data, dict) else []
        return [{'id': int(m['id']), 'name': m.get('name') or ''}
                for m in (rows if isinstance(rows, list) else []) if isinstance(m, dict)]

    def create_manufacturer(self, name):
        """Crea una marca PS e restituisce il suo ID."""
        name_xml = self._clean_name(name).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <manufacturer>
    <active>1</active>
    <name>{name_xml}</name>
  </manufacturer>
</prestashop>""".encode('utf-8')
        return self._post_xml('manufacturers', xml)

    # ------------------------------------------------------------------
    # Opzioni / Combinazioni (prodotti configurabili)
    # ------------------------------------------------------------------

    def get_or_create_product_option(self, attribute_name):
        """Trova o crea un product_option (gruppo attributo, es. 'Taglia') in PS."""
        data = self._get_json('product_options')
        options = data.get('product_options') or [] if isinstance(data, dict) else []
        if not isinstance(options, list):
            options = [options]
        for opt in options:
            if not isinstance(opt, dict):
                continue
            for field in ('public_name', 'name'):
                names = opt.get(field, [])
                if isinstance(names, str):
                    if names.lower() == attribute_name.lower():
                        return int(opt['id'])
                elif isinstance(names, list):
                    for n in names:
                        if (int(n.get('id', 0)) == self._lang_id
                                and n.get('value', '').lower() == attribute_name.lower()):
                            return int(opt['id'])
        name_cdata = attribute_name.replace(']]>', ']]&gt;')
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <product_option>
    <is_color_group>0</is_color_group>
    <group_type>select</group_type>
    <name><language id="{self._lang_id}"><![CDATA[{name_cdata}]]></language></name>
    <public_name><language id="{self._lang_id}"><![CDATA[{name_cdata}]]></language></public_name>
  </product_option>
</prestashop>""".encode('utf-8')
        return self._post_xml('product_options', xml)

    def get_or_create_option_value(self, option_id, value_name):
        """Trova o crea un product_option_value (es. 'M') dentro un gruppo PS."""
        data = self._get_json('product_option_values', {
            'filter[id_attribute_group]': f'[{option_id}]',
        })
        values = data.get('product_option_values') or [] if isinstance(data, dict) else []
        if not isinstance(values, list):
            values = [values]
        for val in values:
            if not isinstance(val, dict):
                continue
            names = val.get('name', [])
            if isinstance(names, str):
                if names.lower() == value_name.lower():
                    return int(val['id'])
            elif isinstance(names, list):
                for n in names:
                    if (int(n.get('id', 0)) == self._lang_id
                            and n.get('value', '').lower() == value_name.lower()):
                        return int(val['id'])
        val_cdata = value_name.replace(']]>', ']]&gt;')
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <product_option_value>
    <id_attribute_group>{option_id}</id_attribute_group>
    <name><language id="{self._lang_id}"><![CDATA[{val_cdata}]]></language></name>
  </product_option_value>
</prestashop>""".encode('utf-8')
        return self._post_xml('product_option_values', xml)

    def get_combination(self, combo_id):
        """Dettaglio combinazione come dict JSON."""
        data = self._get_json(f'combinations/{combo_id}')
        return data.get('combination', {}) if isinstance(data, dict) else {}

    def update_combination_price(self, combo_id, price_impact):
        """Aggiorna l'impatto prezzo di una combinazione su PS."""
        self._update_resource_fields('combinations', combo_id, {
            './/combination/price': f'{float(price_impact):.6f}',
        })

    def update_combination(self, combo_id, price_impact=0.0, reference='', ean13=''):
        """Aggiorna impatto prezzo, riferimento ed EAN/UPC di una combinazione (GET → PUT)."""
        ean, upc = self._split_barcode(ean13)
        root = ET.fromstring(self._get_xml_str('combinations', combo_id))
        combo = root.find('combination')
        for tag, value in (('price', f'{float(price_impact):.6f}'),
                           ('reference', self._clean_reference(reference)),
                           ('ean13', ean), ('upc', upc)):
            el = combo.find(tag)
            if el is not None:
                el.text = value
        self._put_xml('combinations', combo_id, ET.tostring(root, encoding='utf-8', xml_declaration=True))

    def create_combination(self, ps_product_id, option_value_ids,
                           reference='', ean13='', price_impact=0.0):
        """Crea una combinazione PS e restituisce il suo ID."""
        ean13, upc = self._split_barcode(ean13)
        ref_cdata = self._clean_reference(reference).replace(']]>', ']]&gt;')
        ov_xml = ''.join(
            f'<product_option_value><id>{ov}</id></product_option_value>'
            for ov in option_value_ids
        )
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <combination>
    <id_product>{ps_product_id}</id_product>
    <reference><![CDATA[{ref_cdata}]]></reference>
    <ean13>{ean13}</ean13>
    <upc>{upc}</upc>
    <price>{float(price_impact):.6f}</price>
    <minimal_quantity>1</minimal_quantity>
    <associations>
      <product_option_values>{ov_xml}</product_option_values>
    </associations>
  </combination>
</prestashop>""".encode('utf-8')
        return self._post_xml('combinations', xml)

    # ------------------------------------------------------------------
    # Stock (stock_availables)
    # ------------------------------------------------------------------

    def get_stock_availables(self, ps_product_id):
        """Restituisce lista di dict stock_available per un prodotto PS."""
        data = self._get_json('stock_availables', {
            'filter[id_product]': f'[{ps_product_id}]',
        })
        return data.get('stock_availables') or []

    def update_stock_available(self, stock_id, ps_product_id,
                                ps_attribute_id, quantity, id_shop=None):
        """Aggiorna la quantità disponibile su PS."""
        id_shop = id_shop or self._shop_id
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <stock_available>
    <id>{stock_id}</id>
    <id_product>{ps_product_id}</id_product>
    <id_product_attribute>{ps_attribute_id}</id_product_attribute>
    <id_shop>{id_shop}</id_shop>
    <id_shop_group>0</id_shop_group>
    <quantity>{int(quantity)}</quantity>
    <depends_on_stock>0</depends_on_stock>
    <out_of_stock>2</out_of_stock>
  </stock_available>
</prestashop>""".encode('utf-8')
        self._put_xml('stock_availables', stock_id, xml)

    # ------------------------------------------------------------------
    # Ordini
    # ------------------------------------------------------------------

    def get_order_ids(self, date_from=None, states=None):
        """
        Lista ID ordini. date_from: stringa 'YYYY-MM-DD HH:MM:SS'.
        states: lista di int (ID stato PS).
        """
        params = {'display': '[id]', 'sort': '[id_ASC]'}
        if date_from:
            params['filter[date_add]'] = f'>[{date_from}]'
        if states:
            state_str = '|'.join(str(s) for s in states)
            params['filter[current_state]'] = f'[{state_str}]'
        data = self._get_json('orders', params)
        orders = data.get('orders') or []
        return [int(o['id']) for o in orders]

    def get_order(self, order_id):
        """Dettaglio ordine."""
        data = self._get_json(f'orders/{order_id}')
        return data.get('order', {})

    def get_customer(self, customer_id):
        """Dati cliente PS."""
        data = self._get_json(f'customers/{customer_id}')
        return data.get('customer', {})

    def get_address(self, address_id):
        """Indirizzo PS."""
        data = self._get_json(f'addresses/{address_id}')
        return data.get('address', {})

    def get_country(self, country_id):
        """Paese PS (per mappare il country_id Odoo)."""
        data = self._get_json(f'countries/{country_id}')
        return data.get('country', {})

    @staticmethod
    def order_state_label(state_id):
        return _PS_ORDER_STATES.get(int(state_id), f'Stato {state_id}')
