# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
from datetime import timedelta

from odoo import api, fields, models

LOW_STOCK = 5
STEPS = [
    # operation, label, enabled(store)
    ('product_in', "Products · Shopify → Odoo", lambda s: s.product_shopify_to_odoo),
    ('product_out', "Products · Odoo → Shopify", lambda s: s.product_odoo_to_shopify),
    ('order_in', "Orders", lambda s: s.import_orders),
    ('fulfillment_out', "Shipping (fulfillments)", lambda s: s.import_orders and s.fulfillment_mode != 'none'),
    ('stock', "Stock", lambda s: s.stock_odoo_to_shopify or s.stock_shopify_to_odoo),
    ('invoice', "Invoices & payments", lambda s: s.import_orders and s.invoice_trigger != 'none'),
]


class MirakShopifyStoreDashboard(models.Model):
    _inherit = 'mirak.shopify.store'

    @api.model
    def get_dashboard_data(self, store_id=False, period=30):
        stores = self.search([])
        store = stores.filtered(lambda s: s.id == store_id)[:1] or stores[:1]
        result = {'stores': [{'id': s.id, 'name': s.name, 'domain': s.shop_domain} for s in stores],
                  'store': False}
        if not store:
            return result
        period = max(1, min(int(period or 30), 365))
        now = fields.Datetime.now()
        result.update({
            'store': store._dashboard_header(now),
            'period': period,
            'currency': store.company_id.currency_id.name,
            'kpis': store._dashboard_kpis(now, period),
            'revenue': store._dashboard_revenue(now, period),
            'steps': store._dashboard_steps(now),
            'runs': store._dashboard_runs(now),
            'orders': store._dashboard_orders(),
            'alerts': store._dashboard_alerts(now),
            'stock': store._dashboard_stock(),
            'activity': store._dashboard_activity(),
        })
        result['kpis']['attention'] = len(result['alerts'])
        result['kpis']['attention_critical'] = len([a for a in result['alerts'] if a['level'] == 'critical'])
        return result

    # ------------------------------------------------------------------

    def _dashboard_header(self, now):
        sudo = self.sudo()
        next_sync = (self.last_sync + timedelta(minutes=self.sync_interval)) if self.last_sync and self.auto_sync else False
        return {
            'id': self.id,
            'name': self.name,
            'domain': self.shop_domain,
            'connected': self.state == 'connected',
            'license_valid': self.license_valid,
            'license_status': self.license_status,
            'license_required': self.license_required,
            'auth_method': self.auth_method,
            'token_expires_at': sudo.token_expires_at and fields.Datetime.to_string(sudo.token_expires_at),
            'last_sync': self.last_sync and fields.Datetime.to_string(self.last_sync),
            'next_sync': next_sync and fields.Datetime.to_string(next_sync),
            'auto_sync': self.auto_sync,
            'interval': self.sync_interval,
            'location': self.location_id.name or '',
            'warehouse': self.warehouse_id.name or '',
            'now': fields.Datetime.to_string(now),
        }

    def _store_orders_domain(self, start=None, end=None):
        domain = [('ms_store_id', '=', self.id)]
        if start:
            domain.append(('date_order', '>=', start))
        if end:
            domain.append(('date_order', '<', end))
        return domain

    def _order_shipping_state(self, order):
        if order.state == 'cancel':
            return 'cancelled'
        outgoing = order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state != 'cancel')
        if outgoing and all(p.state == 'done' for p in outgoing):
            return 'fulfilled'
        return 'to_ship' if outgoing else 'none'

    def _dashboard_kpis(self, now, period):
        SaleOrder = self.env['sale.order']
        start, prev_start = now - timedelta(days=period), now - timedelta(days=2 * period)
        orders = SaleOrder.search(self._store_orders_domain(start))
        valid = orders.filtered(lambda o: o.state != 'cancel')
        revenue = sum(valid.mapped('amount_total'))
        previous = sum(SaleOrder.search(self._store_orders_domain(prev_start, start) + [('state', '!=', 'cancel')])
                       .mapped('amount_total'))
        shipping = [self._order_shipping_state(o) for o in orders]

        maps = self.env['mirak.shopify.product.map'].search([('store_id', '=', self.id)])
        tracked = maps.filtered(lambda m: m.shopify_tracked and m.product_id.is_storable and m.qty_initialized)
        quantities = self._shopify_qty(tracked.product_id, self._main_warehouses()) if tracked else {}
        matched = len(tracked.filtered(lambda m: quantities.get(m.product_id.id) == m.last_qty))
        return {
            'revenue': revenue,
            'revenue_delta': ((revenue - previous) / previous * 100) if previous else None,
            'orders': len(orders),
            'orders_fulfilled': shipping.count('fulfilled'),
            'orders_to_ship': shipping.count('to_ship'),
            'orders_cancelled': shipping.count('cancelled'),
            'products': len(maps),
            'products_from_shopify': len(maps.filtered(lambda m: m.origin == 'shopify')),
            'products_from_odoo': len(maps.filtered(lambda m: m.origin == 'odoo')),
            'stock_total': len(tracked),
            'stock_matched': matched,
            'errors_24h': self.env['mirak.shopify.log'].search_count([
                ('store_id', '=', self.id), ('level', '=', 'error'), ('create_date', '>=', now - timedelta(hours=24))]),
        }

    def _dashboard_revenue(self, now, period):
        start = (now - timedelta(days=period - 1)).date()
        totals = {}
        groups = self.env['sale.order']._read_group(
            self._store_orders_domain(fields.Datetime.to_datetime(str(start))) + [('state', '!=', 'cancel')],
            ['date_order:day'], ['amount_total:sum'])
        for day, amount in groups:
            totals[str(day)[:10]] = amount
        return [{'date': str(start + timedelta(days=i)), 'amount': totals.get(str(start + timedelta(days=i)), 0.0)}
                for i in range(period)]

    def _dashboard_steps(self, now):
        Log = self.env['mirak.shopify.log']
        steps = []
        for operation, label, enabled in STEPS:
            operations = ['stock_in', 'stock_out'] if operation == 'stock' else [operation]
            if not enabled(self):
                steps.append({'key': operation, 'label': label, 'status': 'off', 'message': "turned off", 'when': False})
                continue
            last = Log.search([('store_id', '=', self.id), ('operation', 'in', operations)], limit=1)
            recent_error = Log.search_count([('store_id', '=', self.id), ('operation', 'in', operations),
                                             ('level', '=', 'error'), ('create_date', '>=', now - timedelta(hours=1))])
            status = 'critical' if recent_error else {'warning': 'warning', 'error': 'critical'}.get(last.level, 'good')
            steps.append({
                'key': operation, 'label': label, 'status': status,
                'message': (last.message or '').split('\n')[0][:110] if last else "no activity yet",
                'when': self.last_sync and fields.Datetime.to_string(self.last_sync),
            })
        return steps

    def _dashboard_runs(self, now):
        logs = self.env['mirak.shopify.log'].search_read(
            [('store_id', '=', self.id), ('level', 'in', ('warning', 'error')),
             ('create_date', '>=', now - timedelta(hours=24))], ['level', 'create_date'])
        hours = []
        for h in range(24, 0, -1):
            start, end = now - timedelta(hours=h), now - timedelta(hours=h - 1)
            levels = [log['level'] for log in logs if start <= log['create_date'] < end]
            status = 'critical' if 'error' in levels else ('warning' if 'warning' in levels else 'good')
            hours.append({'hours_ago': h, 'status': status, 'errors': levels.count('error'),
                          'warnings': levels.count('warning')})
        return hours

    def _dashboard_orders(self):
        result = []
        for order in self.env['sale.order'].search(self._store_orders_domain(), order='date_order desc, id desc', limit=8):
            invoices = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state != 'cancel')
            if invoices:
                payment = set(invoices.mapped('payment_state'))
                invoice = 'paid' if payment <= {'paid', 'in_payment'} else (
                    'partial' if 'partial' in payment else ('draft' if 'draft' in invoices.mapped('state') else 'posted'))
            else:
                invoice = 'off' if self.invoice_trigger == 'none' else ('cancelled' if order.state == 'cancel' else 'pending')
            result.append({
                'id': order.id,
                'shopify': order.ms_order_name or '',
                'name': order.name,
                'customer': order.partner_id.name or '',
                'amount': order.amount_total,
                'payment': (order.ms_financial_status or '').lower(),
                'shipping': self._order_shipping_state(order),
                'invoice': invoice,
            })
        return result

    def _dashboard_stock(self):
        maps = self.env['mirak.shopify.product.map'].search([('store_id', '=', self.id)])
        storable = maps.filtered(lambda m: m.product_id.is_storable and m.product_id.active)
        quantities = self._odoo_qty(storable.product_id, self._main_warehouses()) if storable else {}
        rows = []
        for link in storable:
            qty = quantities.get(link.product_id.id, 0)
            if not link.shopify_tracked:
                status, rank = 'untracked', 4
            elif link.qty_initialized and self._apply_stock_rules(qty) != link.last_qty:
                status, rank = 'mismatch', 0
            elif qty <= 0:
                status, rank = 'out', 1
            elif qty <= LOW_STOCK:
                status, rank = 'low', 2
            else:
                status, rank = 'ok', 3
            rows.append({'product_id': link.product_id.id, 'tmpl_id': link.product_tmpl_id.id,
                         'name': link.product_id.display_name, 'odoo': qty,
                         'shopify': link.last_qty if link.shopify_tracked and link.qty_initialized else None,
                         'status': status, 'rank': rank})
        rows.sort(key=lambda r: (r['rank'], r['odoo'], r['name']))
        return rows[:6]

    def _dashboard_activity(self):
        return [{'id': log.id, 'level': log.level, 'message': (log.message or '').split('\n')[0][:140],
                 'date': fields.Datetime.to_string(log.create_date)}
                for log in self.env['mirak.shopify.log'].search([('store_id', '=', self.id)], limit=8)]

    def _dashboard_alerts(self, now):
        alerts = []
        Log = self.env['mirak.shopify.log']

        def add(level, title, text, action=None):
            alerts.append({'level': level, 'title': title, 'text': text, 'action': action or False})

        store_action = {'type': 'store', 'label': "Open store settings"}
        if not self.license_valid:
            add('critical', "License not valid", self.license_status or '', store_action)
        elif self.license_expiring:
            days = self.license_days_left
            add('warning', "License expires today" if days <= 0 else f"License expires in {days} day(s)",
                f"Synchronization stops when it expires. To renew it, contact {self.ms_contact}.", store_action)
        if self.ms_is_lite:
            add('info', "Lite edition",
                "Several stores, multi-location stock, real-time updates, invoices and payouts, returns, customers, "
                f"tags, collections, translations and Markets prices are in the full version: contact {self.ms_contact}.")
        if self.state != 'connected':
            add('critical', "Store not connected", "Synchronization is stopped until the connection is tested.",
                store_action)

        if self.import_orders and self.invoice_trigger != 'none':
            failing = self.env['sale.order'].search([('ms_store_id', '=', self.id), ('ms_accounting_try', '!=', False)])
            if failing:
                last = Log.search([('store_id', '=', self.id), ('operation', '=', 'invoice'), ('level', '=', 'error')], limit=1)
                add('critical', f"{len(failing)} order(s) cannot be invoiced",
                    (last.message or '').split(': ', 1)[-1][:180],
                    {'type': 'orders', 'label': "View orders", 'ids': failing.ids})
        if not self.income_account_id:
            journal = self.env['account.journal'].search([('type', '=', 'sale'), ('company_id', '=', self.company_id.id)], limit=1)
            if journal.default_account_id and not journal.default_account_id.active:
                add('critical' if self.invoice_trigger != 'none' else 'warning',
                    "Sales account archived",
                    f"The {journal.name} journal uses the archived account {journal.default_account_id.display_name}: "
                    f"invoices will fail. Set the Shopify sales account or fix the journal.", store_action)

        failed_fulfillments = self.env['stock.picking'].search([
            ('sale_id.ms_store_id', '=', self.id), ('ms_fulfillment_error', '!=', False), ('ms_fulfillment_gid', '=', False)])
        if failed_fulfillments:
            add('critical', f"{len(failed_fulfillments)} delivery(ies) not sent to Shopify",
                failed_fulfillments[0].ms_fulfillment_error,
                {'type': 'pickings', 'label': "View deliveries", 'ids': failed_fulfillments.ids})

        errors = Log.search_count([('store_id', '=', self.id), ('level', '=', 'error'),
                                   ('create_date', '>=', now - timedelta(hours=24))])
        if errors:
            add('serious', f"{errors} synchronization error(s) in the last 24 h",
                "Open the logs to see the details.", {'type': 'logs', 'label': "Open logs"})

        to_ship = self.env['sale.order'].search([('ms_store_id', '=', self.id), ('state', '=', 'sale')]).filtered(
            lambda o: self._order_shipping_state(o) == 'to_ship')
        if to_ship:
            add('warning', f"{len(to_ship)} order(s) waiting for shipment",
                ", ".join(to_ship[:3].mapped(lambda o: o.ms_order_name or o.name)) + ("…" if len(to_ship) > 3 else ""),
                {'type': 'orders', 'label': "View orders", 'ids': to_ship.ids})

        stale = self.env['product.template'].search_count([
            ('ms_sync', '=', True), ('ms_pending_export', '=', True), ('write_date', '<', now - timedelta(minutes=30))])
        if stale and self.product_odoo_to_shopify:
            add('warning', f"{stale} product(s) waiting to be sent to Shopify",
                "They were changed more than 30 minutes ago; check the logs for errors.", {'type': 'logs', 'label': "Open logs"})

        if self.auth_method == 'client_credentials' and self.sudo().token_expires_at and \
                self.sudo().token_expires_at < now and self.state == 'connected':
            add('warning', "Access token expired", "It is renewed automatically at the next synchronization.")

        untracked = self.env['mirak.shopify.product.map'].search(
            [('store_id', '=', self.id), ('shopify_tracked', '=', False)])
        if untracked:
            add('info', f"{len(untracked)} product(s) not tracked in Shopify",
                f"“{untracked[0].product_id.name}”" + (f" and {len(untracked) - 1} more" if len(untracked) > 1 else "")
                + " — stock not synchronized.",
                {'type': 'maps', 'label': "View products", 'ids': untracked.ids})
        order = {'critical': 0, 'serious': 1, 'warning': 2, 'info': 3}
        alerts.sort(key=lambda a: order[a['level']])
        return alerts
