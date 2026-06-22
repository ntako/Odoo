/** @odoo-module **/
import { BinaryField, binaryField } from "@web/views/fields/binary/binary_field";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useState, useRef } from "@odoo/owl";

const CHUNK_SIZE = 4 * 1024 * 1024; // 4 MB per chunk

async function postFormData(url, formData) {
    const res = await fetch(url, { method: "POST", body: formData });
    const json = await res.json();
    if (!res.ok || json.error) throw new Error(json.error || `HTTP ${res.status}`);
    return json;
}

export class CsvUploadField extends BinaryField {
    static template = "ke_inventory_import.CsvUploadField";

    setup() {
        super.setup();
        this.uploadProgress = useState({ value: 0, active: false, label: "" });
        this.csvFileInput = useRef("csvFileInput");
        this.action = useService("action");
        this.notification = useService("notification");
    }

    onClickUpload() {
        this.csvFileInput.el.click();
    }

    async onFileChange(ev) {
        if (!ev.target.files || !ev.target.files.length) return;
        const file = ev.target.files[0];
        const uploadId = crypto.randomUUID();
        const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
        const recordId = this.props.record.resId;

        this.uploadProgress.active = true;
        this.uploadProgress.value = 0;
        this.uploadProgress.label = `Chunk 0 / ${totalChunks}`;

        try {
            // ── 1. Upload chunks ────────────────────────────────────────────
            for (let i = 0; i < totalChunks; i++) {
                const start = i * CHUNK_SIZE;
                const chunk = file.slice(start, start + CHUNK_SIZE);

                const fd = new FormData();
                fd.append("ufile", chunk);
                fd.append("upload_id", uploadId);
                fd.append("chunk_index", i);
                await postFormData("/ke_inventory_import/upload_chunk", fd);

                this.uploadProgress.value = Math.round(((i + 1) / totalChunks) * 90);
                this.uploadProgress.label = `Chunk ${i + 1} / ${totalChunks}`;
            }

            // ── 2. Finalize ─────────────────────────────────────────────────
            this.uploadProgress.value = 95;
            this.uploadProgress.label = "Assemblaggio...";

            const fd = new FormData();
            fd.append("upload_id", uploadId);
            fd.append("file_name", file.name);
            if (recordId) {
                fd.append("record_id", recordId);
            } else {
                fd.append("name", this.props.record.data.name || file.name);
                fd.append("delimiter", this.props.record.data.delimiter || ";");
            }
            const result = await postFormData("/ke_inventory_import/upload_finalize", fd);

            this.uploadProgress.value = 100;

            // ── 3. Update UI ────────────────────────────────────────────────
            if (recordId) {
                await this.props.record.load();
            } else {
                await this.action.doAction({
                    type: "ir.actions.act_window",
                    res_model: "inventory.csv.file",
                    res_id: result.record_id,
                    views: [[false, "form"]],
                    target: "current",
                });
            }
        } catch (err) {
            this.notification.add(
                `Upload fallito: ${err.message}`,
                { type: "danger", title: "Errore upload CSV" }
            );
        } finally {
            this.uploadProgress.active = false;
            ev.target.value = "";
        }
    }
}

export const csvUploadField = {
    ...binaryField,
    component: CsvUploadField,
};

registry.category("fields").add("csv_upload", csvUploadField);
