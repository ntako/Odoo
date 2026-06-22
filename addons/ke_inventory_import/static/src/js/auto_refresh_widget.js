/** @odoo-module **/
import { Component, onMounted, onWillUnmount, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";

const REFRESH_INTERVAL_MS = 5000;

/**
 * Invisible widget that auto-refreshes the form record every 5 s
 * while the job state is "running". Stops automatically when the
 * state changes to "done" or "error".
 */
class KeAutoRefresh extends Component {
    static template = "ke_inventory_import.AutoRefresh";
    static props = {
        record: Object,
        readonly: { type: Boolean, optional: true },
    };

    setup() {
        let timerId = null;
        let loading = false;
        let alive = true;   // becomes false after onWillUnmount

        const stop = () => {
            clearInterval(timerId);
            timerId = null;
        };

        const start = () => {
            if (timerId !== null) return; // already polling
            timerId = setInterval(async () => {
                if (!alive || loading) return;
                if (this.props.record.data.state !== "running") {
                    stop();
                    return;
                }
                loading = true;
                try {
                    await this.props.record.load();
                } catch (_e) {
                    // transient error — next tick will retry
                } finally {
                    loading = false;
                }
            }, REFRESH_INTERVAL_MS);
        };

        // Start immediately if the record is already running when the widget mounts
        onMounted(() => {
            if (this.props.record.data.state === "running") {
                start();
            }
        });

        // Always clean up when navigating away
        onWillUnmount(() => {
            alive = false;
            stop();
        });

        // React to state transitions (e.g. 'done' or 'error' arrives after a load)
        useEffect(
            (state) => {
                if (state === "running") {
                    start();
                } else {
                    stop();
                }
            },
            () => [this.props.record.data.state]
        );
    }
}

registry.category("view_widgets").add("ke_auto_refresh", {
    component: KeAutoRefresh,
});
