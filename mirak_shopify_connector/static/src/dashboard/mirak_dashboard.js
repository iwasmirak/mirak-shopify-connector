/** @odoo-module **/
// Copyright 2026 Bouchibane Karim. License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const { DateTime } = luxon;

const PAYMENT = {
    paid: ["good", _t("Paid")], partially_paid: ["warn", _t("Partially paid")], pending: ["warn", _t("Pending")],
    authorized: ["info", _t("Authorized")], refunded: ["off", _t("Refunded")],
    partially_refunded: ["info", _t("Partially refunded")], voided: ["off", _t("Voided")], expired: ["off", _t("Expired")],
};
const SHIPPING = {
    fulfilled: ["good", _t("Fulfilled")], to_ship: ["warn", _t("To ship")], cancelled: ["crit", _t("Cancelled")],
    none: ["off", _t("—")],
};
const INVOICE = {
    paid: ["good", _t("Paid")], posted: ["info", _t("Posted")], partial: ["warn", _t("Partial")],
    draft: ["off", _t("Draft")], pending: ["warn", _t("To invoice")], off: ["off", _t("Off")], cancelled: ["off", _t("—")],
};
const STOCK = {
    mismatch: ["crit", _t("Mismatch")], out: ["crit", _t("Out of stock")], low: ["warn", _t("Low")],
    ok: ["good", _t("In sync")], untracked: ["off", _t("Not tracked")],
};

export class MirakDashboard extends Component {
    static template = "mirak_shopify_connector.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ data: null, storeId: false, period: 30, syncing: false, hover: null, tick: 0 });
        this.periods = [[1, _t("Today")], [7, _t("7 days")], [30, _t("30 days")], [90, _t("90 days")]];
        onWillStart(() => this.load());
        onMounted(() => {
            this.refresher = setInterval(() => this.load(), 60000);
            this.clock = setInterval(() => this.state.tick++, 30000);
        });
        onWillUnmount(() => {
            clearInterval(this.refresher);
            clearInterval(this.clock);
        });
    }

    async load() {
        const data = await this.orm.call("mirak.shopify.store", "get_dashboard_data", [], {
            store_id: this.state.storeId,
            period: this.state.period,
        });
        this.state.data = data;
        this.state.storeId = data.store ? data.store.id : false;
    }

    // ----- formatting -----------------------------------------------------
    money(value) {
        return (value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    short(value) {
        if (value >= 1000) {
            return (value / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "k";
        }
        return Math.round(value).toLocaleString();
    }
    ago(utc) {
        void this.state.tick;
        if (!utc) {
            return _t("never");
        }
        const date = DateTime.fromSQL(utc, { zone: "utc" });
        const minutes = Math.round(DateTime.utc().diff(date, "minutes").minutes);
        if (minutes < 1) {
            return _t("just now");
        }
        if (minutes < 60) {
            return _t("%s min ago", minutes);
        }
        if (minutes < 60 * 24) {
            return _t("%s h ago", Math.round(minutes / 60));
        }
        return date.toLocal().toFormat("dd LLL HH:mm");
    }
    until(utc) {
        void this.state.tick;
        if (!utc) {
            return "";
        }
        const minutes = Math.round(DateTime.fromSQL(utc, { zone: "utc" }).diff(DateTime.utc(), "minutes").minutes);
        if (minutes <= 0) {
            return _t("now");
        }
        return minutes < 60 ? _t("in %s min", minutes) : _t("in %s h", Math.round(minutes / 60));
    }
    time(utc) {
        const date = DateTime.fromSQL(utc, { zone: "utc" }).toLocal();
        return date.hasSame(DateTime.local(), "day") ? date.toFormat("HH:mm") : date.toFormat("dd LLL HH:mm");
    }
    tag(map, key) {
        const [cls, label] = map[key] || ["off", key || "—"];
        return { cls, label };
    }
    paymentTag(key) { return this.tag(PAYMENT, key); }
    shippingTag(key) { return this.tag(SHIPPING, key); }
    invoiceTag(key) { return this.tag(INVOICE, key); }
    stockTag(key) { return this.tag(STOCK, key); }

    // ----- derived values -------------------------------------------------
    get stockPercent() {
        const k = this.state.data.kpis;
        return k.stock_total ? Math.round((k.stock_matched / k.stock_total) * 100) : 100;
    }
    get chart() {
        const points = this.state.data.revenue;
        const max = Math.max(...points.map((p) => p.amount), 0);
        const step = max <= 0 ? 1 : Math.pow(10, Math.floor(Math.log10(max)));
        const top = max <= 0 ? 100 : Math.ceil(max / step) * step;
        const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => top * f);
        const every = points.length <= 7 ? 1 : points.length <= 31 ? 7 : 15;
        return {
            ticks: ticks.reverse(),
            bars: points.map((p, i) => ({
                ...p,
                index: i,
                height: top ? (p.amount / top) * 100 : 0,
                label: i % every === 0 && points.length - 1 - i > every / 2 ? this.dayLabel(p.date) : "",
            })),
            lastLabel: points.length ? this.dayLabel(points[points.length - 1].date) : "",
            total: points.reduce((s, p) => s + p.amount, 0),
        };
    }
    dayLabel(iso) {
        return DateTime.fromISO(iso).toFormat("d LLL");
    }
    runTitle(run) {
        const parts = [_t("%s h ago", run.hours_ago)];
        if (run.errors) {
            parts.push(_t("%s error(s)", run.errors));
        }
        if (run.warnings) {
            parts.push(_t("%s warning(s)", run.warnings));
        }
        if (!run.errors && !run.warnings) {
            parts.push(_t("no problem"));
        }
        return parts.join(" · ");
    }

    // ----- actions --------------------------------------------------------
    async setPeriod(days) {
        this.state.period = days;
        await this.load();
    }
    async setStore(ev) {
        this.state.storeId = parseInt(ev.target.value);
        await this.load();
    }
    async syncNow() {
        if (this.state.syncing) {
            return;
        }
        this.state.syncing = true;
        try {
            await this.orm.call("mirak.shopify.store", "action_sync_now", [[this.state.storeId]]);
            this.notification.add(_t("Synchronization started in the background. The dashboard updates as it progresses."),
                                  { type: "info" });
        } finally {
            // Give the background job a moment, then refresh
            setTimeout(async () => {
                this.state.syncing = false;
                await this.load();
            }, 5000);
        }
    }
    openStore() {
        this.action.doAction({
            type: "ir.actions.act_window", res_model: "mirak.shopify.store", res_id: this.state.storeId,
            views: [[false, "form"]],
        });
    }
    newStore() {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "mirak.shopify.store", views: [[false, "form"]] });
    }
    openList(model, name, domain) {
        this.action.doAction({
            type: "ir.actions.act_window", name, res_model: model, domain,
            views: [[false, "list"], [false, "form"]],
        });
    }
    openLogs() {
        this.openList("mirak.shopify.log", _t("Shopify Logs"), [["store_id", "=", this.state.storeId]]);
    }
    openOrders(ids) {
        const domain = ids ? [["id", "in", ids]] : [["ms_store_id", "=", this.state.storeId]];
        this.openList("sale.order", _t("Shopify Orders"), domain);
    }
    openRecord(model, id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: model, res_id: id, views: [[false, "form"]] });
    }
    openProducts() {
        this.openList("mirak.shopify.product.map", _t("Shopify Product Links"), [["store_id", "=", this.state.storeId]]);
    }
    alertAction(alert) {
        const a = alert.action;
        if (a.type === "store") {
            this.openStore();
        } else if (a.type === "logs") {
            this.openLogs();
        } else if (a.type === "orders") {
            this.openOrders(a.ids);
        } else if (a.type === "pickings") {
            this.openList("stock.picking", _t("Deliveries"), [["id", "in", a.ids]]);
        } else if (a.type === "maps") {
            this.openList("mirak.shopify.product.map", _t("Shopify Product Links"), [["id", "in", a.ids]]);
        }
    }
}

registry.category("actions").add("mirak_shopify_dashboard", MirakDashboard);
