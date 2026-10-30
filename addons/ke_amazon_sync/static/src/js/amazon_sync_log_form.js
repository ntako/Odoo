/** @odoo-module **/
import { registry } from "@web/core/registry";
import { formView } from "@web/views/form/form_view";
import { FormController } from "@web/views/form/form_controller";
import { onMounted, onWillUnmount } from "@odoo/owl";

class AmazonSyncLogFormController extends FormController {
    setup() {
        super.setup();
        let intervalId;
        onMounted(() => {
            intervalId = setInterval(async () => {
                if (this.model.root.data.state === "running") {
                    await this.model.root.load();
                    this.model.notify();
                }
            }, 10000);
        });
        onWillUnmount(() => {
            clearInterval(intervalId);
        });
    }
}

registry.category("views").add("amazon_sync_log_form", {
    ...formView,
    Controller: AmazonSyncLogFormController,
});
