import base64

from odoo import http
from odoo.http import request

_CHUNK_PREFIX = '__ke_csv_chunk_'


class KeCsvUploadController(http.Controller):

    @http.route(
        '/ke_inventory_import/upload_chunk',
        type='http', auth='user', methods=['POST'], csrf=False,
    )
    def upload_chunk(self, upload_id=None, chunk_index=None, ufile=None, **kw):
        """Receive a single chunk and persist it as a temporary ir.attachment."""
        if not upload_id or chunk_index is None or not ufile:
            return request.make_json_response({'error': 'Parametri mancanti.'}, status=400)

        data = ufile.read()
        b64 = base64.b64encode(data).decode('ascii')

        # Zero-padded index so lexicographic sort equals numeric sort (up to 999 chunks)
        chunk_name = f'{_CHUNK_PREFIX}{upload_id}_{int(chunk_index):04d}'
        request.env['ir.attachment'].create({
            'name': chunk_name,
            'datas': b64,
            'res_model': 'inventory.csv.file',
            'res_id': 0,
            'type': 'binary',
        })
        return request.make_json_response({'ok': True, 'chunk': int(chunk_index)})

    @http.route(
        '/ke_inventory_import/upload_finalize',
        type='http', auth='user', methods=['POST'], csrf=False,
    )
    def upload_finalize(
        self, upload_id=None, file_name=None,
        record_id=None, name=None, delimiter=';', **kw
    ):
        """Assemble all chunks and write to inventory.csv.file."""
        if not upload_id:
            return request.make_json_response({'error': 'upload_id mancante.'}, status=400)

        Attachment = request.env['ir.attachment']
        chunks = Attachment.search(
            [('name', 'like', f'{_CHUNK_PREFIX}{upload_id}_')],
            order='name asc',
        )
        if not chunks:
            return request.make_json_response({'error': 'Nessun chunk trovato.'}, status=404)

        try:
            full_data = b''.join(base64.b64decode(c.datas) for c in chunks)
            b64 = base64.b64encode(full_data).decode('ascii')

            if record_id:
                record = request.env['inventory.csv.file'].browse(int(record_id))
                if not record.exists():
                    return request.make_json_response({'error': 'Record non trovato.'}, status=404)
                record.write({'file_data': b64, 'file_name': file_name})
            else:
                record = request.env['inventory.csv.file'].create({
                    'name': name or file_name or 'CSV import',
                    'file_data': b64,
                    'file_name': file_name,
                    'delimiter': delimiter,
                })
        finally:
            chunks.unlink()

        return request.make_json_response({
            'record_id': record.id,
            'file_name': file_name,
        })
