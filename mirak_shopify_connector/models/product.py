# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
from odoo import api, fields, models
from odoo.tools import sql

# Field changed in Odoo -> kind of data to push to Shopify
TEMPLATE_TRACKED_FIELDS = {
    'name': 'name',
    'description_sale': 'description',
    'list_price': 'price',
    'active': 'status',
    'is_storable': 'tracking',
    'type': 'all',
    'attribute_line_ids': 'structure',
    'ms_sync': 'all',
    'product_tag_ids': 'tags',
    'categ_id': 'category',
}
PRODUCT_TRACKED_FIELDS = {
    'default_code': 'variant',
    'barcode': 'variant',
    'active': 'structure',
    'product_template_attribute_value_ids': 'structure',
}


class MirakShopifyPending(models.Model):
    """Changes of a product still to be sent to one store (each store keeps its own queue)."""
    _name = 'mirak.shopify.pending'
    _description = 'Product Change Waiting for a Shopify Store'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    product_tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade', index=True)
    tags = fields.Char(help="Kinds of data to send: name, price, variant, images, all...")

    _store_tmpl_uniq = models.Constraint('unique(store_id, product_tmpl_id)',
                                         'This product is already waiting for this store.')

    def init(self):
        # Upgrade from the single queue: products waiting for export wait for every store
        cr = self.env.cr
        if not sql.table_exists(cr, 'mirak_shopify_store') or not sql.column_exists(
                cr, 'product_template', 'ms_dirty_fields'):
            return
        cr.execute("SELECT 1 FROM mirak_shopify_pending LIMIT 1")
        if cr.fetchone():
            return
        cr.execute("""
            INSERT INTO mirak_shopify_pending (store_id, product_tmpl_id, tags, create_uid, write_uid,
                                               create_date, write_date)
            SELECT s.id, t.id, COALESCE(NULLIF(t.ms_dirty_fields, ''), 'all'), 1, 1, now(), now()
              FROM product_template t CROSS JOIN mirak_shopify_store s
             WHERE t.ms_pending_export AND t.ms_sync AND s.active
        """)

    def tag_set(self):
        return set(filter(None, ','.join(self.mapped('tags')).split(',')))


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ms_sync = fields.Boolean(
        string='Sync with Shopify',
        default=lambda self: self.env['mirak.shopify.store']._ms_default_sync(
            self.env['product.category'].browse(self.env.context.get('default_categ_id'))),
        help="Create and keep this product up to date in Shopify, including its stock.")
    ms_pending_export = fields.Boolean(string='Waiting for Shopify Export', copy=False)
    ms_dirty_fields = fields.Char(copy=False)
    ms_shopify_status = fields.Selection([
        ('active', 'Active'),
        ('draft', 'Draft'),
        ('archived', 'Archived'),
        ('unlisted', 'Unlisted'),
    ], string='Shopify Status', copy=False, readonly=True)
    ms_map_ids = fields.One2many('mirak.shopify.product.map', 'product_tmpl_id', string='Shopify Links')
    ms_tags_synced = fields.Boolean(copy=False, help="Shopify tags were merged into the Odoo tags once")

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('ms_no_export'):
            Store = self.env['mirak.shopify.store']
            for vals in vals_list:
                if 'ms_sync' not in vals and vals.get('categ_id'):
                    vals['ms_sync'] = Store._ms_default_sync(self.env['product.category'].browse(vals['categ_id']))
        templates = super().create(vals_list)
        templates._ms_mark_dirty({'all'})
        return templates

    def write(self, vals):
        if 'ms_pending_export' in vals and not vals['ms_pending_export'] \
                and not self.env.context.get('ms_pending_summary'):
            # Explicitly marked as sent: nothing waits for any store anymore
            self.env['mirak.shopify.pending'].sudo().search([('product_tmpl_id', 'in', self.ids)]).unlink()
        res = super().write(vals)
        self._ms_mark_dirty({TEMPLATE_TRACKED_FIELDS[f] for f in vals if f in TEMPLATE_TRACKED_FIELDS})
        return res

    @api.onchange('categ_id')
    def _onchange_categ_id_ms_sync(self):
        # Only for products being created: follow the stores' "send new products" policy
        if not self._origin:
            self.ms_sync = self.env['mirak.shopify.store']._ms_default_sync(self.categ_id)

    def _ms_mark_dirty(self, tags, stores=None):
        """Queue the given kinds of changes for every store (or the given stores)."""
        if not tags or self.env.context.get('ms_no_export'):
            return
        templates = self.with_context(active_test=False).filtered('ms_sync')
        if not templates:
            return
        if stores is None:
            # A store connected later starts from what is in Shopify; nothing to queue for it
            stores = self.env['mirak.shopify.store'].sudo().search([('state', '=', 'connected')])
        Pending = self.env['mirak.shopify.pending'].sudo()
        existing = {(p.store_id.id, p.product_tmpl_id.id): p for p in Pending.search(
            [('store_id', 'in', stores.ids), ('product_tmpl_id', 'in', templates.ids)])}
        to_create = []
        for store in stores:
            for template in templates:
                row = existing.get((store.id, template.id))
                if not row:
                    to_create.append({'store_id': store.id, 'product_tmpl_id': template.id,
                                      'tags': ','.join(sorted(tags))})
                elif not tags <= row.tag_set():
                    row.tags = ','.join(sorted(row.tag_set() | tags))
        Pending.create(to_create)
        templates._ms_refresh_pending()

    def _ms_refresh_pending(self):
        """Summary on the product: waiting for at least one store, and which kinds of changes."""
        rows = self.env['mirak.shopify.pending'].sudo().search([('product_tmpl_id', 'in', self.ids)])
        for template in self.with_context(active_test=False):
            mine = rows.filtered(lambda r: r.product_tmpl_id == template)
            tags = ','.join(sorted(mine.tag_set())) or False
            if template.ms_pending_export != bool(mine) or (template.ms_dirty_fields or False) != tags:
                template.with_context(ms_no_export=True, ms_pending_summary=True).write({
                    'ms_pending_export': bool(mine),
                    'ms_dirty_fields': tags,
                })

    def action_ms_push_now(self):
        """Push the selected products (data and stock) to Shopify right away."""
        templates = self.filtered(lambda t: t.ms_sync)
        templates._ms_mark_dirty({'all'})
        for store in self.env['mirak.shopify.store'].search([('state', '=', 'connected')]):
            store._push_products(templates)
            store._sync_inventory(templates.product_variant_ids)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._("Shopify"),
                'message': self.env._("Synchronization done. Check the Shopify logs for details."),
                'type': 'info',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model_create_multi
    def create(self, vals_list):
        products = super().create(vals_list)
        products.product_tmpl_id._ms_mark_dirty({'structure'})
        return products

    def write(self, vals):
        res = super().write(vals)
        tags = {PRODUCT_TRACKED_FIELDS[f] for f in vals if f in PRODUCT_TRACKED_FIELDS}
        if tags:
            self.product_tmpl_id._ms_mark_dirty(tags)
        return res


class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    @api.model_create_multi
    def create(self, vals_list):
        items = super().create(vals_list)
        items._ms_mark_products()
        return items

    def write(self, vals):
        self._ms_mark_products()
        res = super().write(vals)
        self._ms_mark_products()
        return res

    def unlink(self):
        self._ms_mark_products()
        return super().unlink()

    def _ms_mark_products(self):
        """A price rule of a pricelist used for Shopify changed: resend the prices of its products."""
        if self.env.context.get('ms_no_export'):
            return
        Store = self.env['mirak.shopify.store'].sudo()
        pricelists = Store.search([('price_source', '=', 'pricelist')]).pricelist_id | Store.search(
            [('compare_at_source', '=', 'pricelist')]).compare_at_pricelist_id | self.env[
            'mirak.shopify.price.list'].sudo().search([('pricelist_id', '!=', False)]).pricelist_id
        for item in self.filtered(lambda i: i.pricelist_id in pricelists):
            templates = item.product_tmpl_id | item.product_id.product_tmpl_id
            if item.applied_on == '2_product_category' and item.categ_id:
                templates |= self.env['product.template'].search([
                    ('categ_id', 'child_of', item.categ_id.id), ('ms_sync', '=', True)])
            elif item.applied_on == '3_global':
                templates |= self.env['mirak.shopify.product.map'].sudo().search([]).product_tmpl_id
            templates._ms_mark_dirty({'price'})


class ProductTemplateAttributeValue(models.Model):
    _inherit = 'product.template.attribute.value'

    def write(self, vals):
        res = super().write(vals)
        if 'price_extra' in vals:
            self.product_tmpl_id._ms_mark_dirty({'price'})
        return res
