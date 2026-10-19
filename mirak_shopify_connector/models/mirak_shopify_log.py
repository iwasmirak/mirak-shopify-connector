# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
from datetime import timedelta

from odoo import api, fields, models


class MirakShopifyLog(models.Model):
    _name = 'mirak.shopify.log'
    _description = 'Shopify Sync Log'
    _order = 'id desc'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    level = fields.Selection([
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ], required=True, default='info')
    operation = fields.Selection([
        ('connection', 'Connection'),
        ('product_in', 'Product Shopify → Odoo'),
        ('product_out', 'Product Odoo → Shopify'),
        ('stock_in', 'Stock Shopify → Odoo'),
        ('stock_out', 'Stock Odoo → Shopify'),
        ('order_in', 'Order Shopify → Odoo'),
        ('order_out', 'Order Odoo → Shopify'),
        ('customer_in', 'Customer Shopify → Odoo'),
        ('customer_out', 'Customer Odoo → Shopify'),
        ('fulfillment_out', 'Fulfillment Odoo → Shopify'),
        ('invoice', 'Invoices and Payments'),
    ], required=True)
    message = fields.Text(required=True)
    product_tmpl_id = fields.Many2one('product.template', string='Product', ondelete='set null')
    reference = fields.Char(help="Shopify ID concerned by this entry")

    @api.autovacuum
    def _gc_old_logs(self):
        now = fields.Datetime.now()
        for store in self.env['mirak.shopify.store'].with_context(active_test=False).search([]):
            self.search([
                ('store_id', '=', store.id),
                ('create_date', '<', now - timedelta(days=store.log_retention_days)),
            ]).unlink()
