# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    ms_store_id = fields.Many2one('mirak.shopify.store', string='Shopify Store', copy=False, index=True, readonly=True)
    ms_order_gid = fields.Char(string='Shopify Order ID', copy=False, index=True, readonly=True)
    ms_order_name = fields.Char(string='Shopify Order', copy=False, readonly=True)
    ms_financial_status = fields.Char(string='Shopify Payment Status', copy=False, readonly=True)
    ms_fulfillment_status = fields.Char(string='Shopify Fulfillment Status', copy=False, readonly=True)
    ms_updated_at = fields.Char(copy=False, readonly=True)
    ms_gateway = fields.Char(string='Shopify Payment Method', copy=False, readonly=True)
    ms_amount_received = fields.Float(string='Paid in Shopify', copy=False, readonly=True)
    ms_amount_refunded = fields.Float(string='Refunded in Shopify', copy=False, readonly=True)
    ms_received_registered = fields.Float(copy=False, readonly=True)
    ms_refund_registered = fields.Float(copy=False, readonly=True)
    ms_accounting_pending = fields.Boolean(copy=False, index=True)
    ms_accounting_try = fields.Datetime(copy=False, readonly=True,
                                        help="Last failed invoicing attempt (retried one hour later)")
    ms_location_id = fields.Many2one('mirak.shopify.location', string='Shopify Location', copy=False, readonly=True,
                                     help="Shopify location fulfilling the order")
    ms_cancel_pending = fields.Boolean(string='Cancellation to Send to Shopify', copy=False, readonly=True)
    ms_cancel_try = fields.Datetime(copy=False, readonly=True,
                                    help="Last failed attempt to cancel the order in Shopify (retried one hour later)")

    _ms_order_uniq = models.Constraint(
        'unique(ms_store_id, ms_order_gid)',
        'This Shopify order has already been imported.',
    )

    def _action_cancel(self):
        res = super()._action_cancel()
        if not self.env.context.get('ms_from_shopify'):
            # Cancelled in Odoo: cancel it in Shopify too, when the store says so
            self.filtered(lambda o: o.ms_order_gid and o.ms_store_id.cancel_in_shopify).write(
                {'ms_cancel_pending': True, 'ms_cancel_try': False})
        return res


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    ms_line_gid = fields.Char(string='Shopify Line ID', copy=False, index=True)

    def _prepare_invoice_line(self, **optional_values):
        vals = super()._prepare_invoice_line(**optional_values)
        store = self.order_id.ms_store_id.sudo()
        if store and not self.display_type:
            if (store.shipping_product_id and self.product_id == store.shipping_product_id
                    and store.shipping_account_id):
                vals['account_id'] = store.shipping_account_id.id
            elif store.income_account_id:
                vals['account_id'] = store.income_account_id.id
        return vals


class AccountMove(models.Model):
    _inherit = 'account.move'

    ms_refund_gid = fields.Char(string='Shopify Refund', copy=False, index=True, readonly=True)


class MirakShopifyPaymentJournal(models.Model):
    _name = 'mirak.shopify.payment.journal'
    _description = 'Shopify Payment Method Journal'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade')
    gateway = fields.Char(string='Shopify Payment Method', required=True,
                          help="Payment method name as shown on Shopify orders, e.g. "
                               "'shopify_payments', 'manual', 'Cash on Delivery (COD)'.")
    journal_id = fields.Many2one('account.journal', required=True, domain="[('type', 'in', ('bank', 'cash'))]")


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    ms_fulfillment_gid = fields.Char(string='Shopify Fulfillment', copy=False, readonly=True)
    ms_fulfillment_error = fields.Char(string='Shopify Fulfillment Error', copy=False, readonly=True)
    ms_fulfillment_try = fields.Datetime(copy=False, readonly=True)
