/** @odoo-module **/
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { registry } from "@web/core/registry";
import { onMounted, onWillUnmount } from "@odoo/owl";

const REFRESH_INTERVAL_MS = 5000;

/**
 * Custom list controller for inventory.import.job.
 * Polls every 5 s and reloads the list only while at least one visible
 * record is in "running" state.  Stops automatically when none are running.
 */
class KeJobsListController extends ListController {
    setup() {
        super.setup();
        let timerId = null;
        let loading = false;
        let alive = true;

        onMounted(() => {
            timerId = setInterval(async () => {
                if (!alive || loading) return;
                const hasRunning = this.model.root.records.some(
                    (r) => r.data.state === "running"
                );
                if (!hasRunning) return;
                loading = true;
                try {
                    await this.model.root.load();
                } catch (_e) {
                    // transient error — retry next tick
                } finally {
                    loading = false;
                }
            }, REFRESH_INTERVAL_MS);
        });

        onWillUnmount(() => {
            alive = false;
            clearInterval(timerId);
        });
    }
}

registry.category("views").add("ke_jobs_list", {
    ...listView,
    Controller: KeJobsListController,
});
