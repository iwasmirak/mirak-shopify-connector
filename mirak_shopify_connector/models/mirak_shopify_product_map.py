# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
from odoo import fields, models


class MirakShopifyProductMap(models.Model):
    """Link between one Odoo variant and one Shopify variant."""
    _name = 'mirak.shopify.product.map'
    _description = 'Shopify Product Link'
    _order = 'product_tmpl_id, id'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', required=True, ondelete='cascade', index=True)
    product_tmpl_id = fields.Many2one(related='product_id.product_tmpl_id', store=True, index=True)
    shopify_product_gid = fields.Char(required=True, index=True)
    shopify_variant_gid = fields.Char(required=True, index=True)
    inventory_item_gid = fields.Char()
    shopify_tracked = fields.Boolean(default=True, help="Inventory is tracked in Shopify for this variant")
    shopify_updated_at = fields.Char(
        help="Shopify updatedAt of the product at the last synchronization; "
             "used to ignore our own changes when polling Shopify.")
    origin = fields.Selection([
        ('shopify', 'Shopify'),
        ('odoo', 'Odoo'),
    ], required=True, default='shopify', help="Platform where the link was created")
    qty_initialized = fields.Boolean(help="Initial stock alignment between both platforms is done")
    last_qty = fields.Integer(
        string='Last Synced Quantity',
        help="Quantity both platforms agreed on at the last stock synchronization")
    last_stock_sync = fields.Datetime()
    drop_since = fields.Datetime(
        help="Shopify quantity lower than expected since this date (probably an order not imported yet)")
    collection_gid = fields.Char(help="Shopify collection of the product's Odoo category (category collections)")
    stock_level_ids = fields.One2many('mirak.shopify.stock.level', 'map_id', string='Stock at Other Locations')

    _variant_uniq = models.Constraint(
        'unique(store_id, shopify_variant_gid)',
        'This Shopify variant is already linked to a product.',
    )
    _product_uniq = models.Constraint(
        'unique(store_id, product_id)',
        'This product is already linked to a Shopify variant.',
    )
