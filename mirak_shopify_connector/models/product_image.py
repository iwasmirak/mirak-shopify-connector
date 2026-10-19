# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
import base64
import hashlib

from odoo import api, fields, models


class MirakProductImage(models.Model):
    """Ordered image gallery of a product, synchronized with the Shopify product media.
    The first image is also the product's main Odoo image."""
    _name = 'mirak.product.image'
    _description = 'Shopify Product Image'
    _inherit = ['image.mixin']
    _order = 'product_tmpl_id, sequence, id'

    product_tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Description (alt text)')
    product_id = fields.Many2one(
        'product.product', string='Variant', domain="[('product_tmpl_id', '=', product_tmpl_id)]",
        help="Show this image for one variant (size, color…) in Shopify.")
    checksum = fields.Char(compute='_compute_checksum', store=True)
    link_ids = fields.One2many('mirak.product.image.link', 'image_id', string='Shopify Media')

    @api.depends('image_1920')
    def _compute_checksum(self):
        for image in self:
            data = image.image_1920
            image.checksum = hashlib.sha1(base64.b64decode(data)).hexdigest() if data else False

    @api.model_create_multi
    def create(self, vals_list):
        images = super().create(vals_list)
        images.product_tmpl_id._ms_images_changed(self.env.context)
        images._ms_sync_variant_images()
        return images

    def write(self, vals):
        templates = self.product_tmpl_id
        res = super().write(vals)
        if {'image_1920', 'sequence', 'product_id', 'name'} & set(vals):
            (templates | self.product_tmpl_id)._ms_images_changed(self.env.context)
            self._ms_sync_variant_images()
        return res

    def unlink(self):
        templates = self.product_tmpl_id
        if not self.env.context.get('ms_no_export'):
            # Remember the Shopify media to delete at the next export
            for image in self:
                tombstones = [f"{link.store_id.id}:{link.media_gid}" for link in image.link_ids]
                if tombstones:
                    current = list(filter(None, (image.product_tmpl_id.ms_deleted_media or '').split(',')))
                    image.product_tmpl_id.with_context(ms_no_export=True).ms_deleted_media = ','.join(current + tombstones)
        res = super().unlink()
        templates.exists()._ms_images_changed(self.env.context)
        return res

    def _ms_sync_variant_images(self):
        for image in self.filtered('product_id'):
            if image.product_id.image_variant_1920 != image.image_1920:
                image.product_id.with_context(ms_images_internal=True, ms_no_export=True).image_variant_1920 = image.image_1920


class MirakProductImageLink(models.Model):
    _name = 'mirak.product.image.link'
    _description = 'Shopify Media of a Product Image'

    image_id = fields.Many2one('mirak.product.image', required=True, ondelete='cascade', index=True)
    product_tmpl_id = fields.Many2one(related='image_id.product_tmpl_id', store=True, index=True)
    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    media_gid = fields.Char(required=True, index=True)
    checksum = fields.Char(help="Image checksum when it was last synchronized")

    # One Shopify image file can be used by several products: unique per product
    _media_uniq = models.Constraint('unique(store_id, media_gid, product_tmpl_id)',
                                    'This Shopify image is already linked to this product.')


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ms_image_ids = fields.One2many('mirak.product.image', 'product_tmpl_id', string='Shopify Images')
    ms_deleted_media = fields.Char(copy=False)
    ms_images_synced = fields.Boolean(copy=False, help="Images were synchronized once with Shopify")

    @api.model_create_multi
    def create(self, vals_list):
        templates = super().create(vals_list)
        if not self.env.context.get('ms_images_internal'):
            for template in templates.filtered(lambda t: t.image_1920 and not t.ms_image_ids):
                self.env['mirak.product.image'].with_context(ms_images_internal=True).create(
                    {'product_tmpl_id': template.id, 'image_1920': template.image_1920, 'sequence': 1})
        return templates

    def write(self, vals):
        res = super().write(vals)
        if 'image_1920' in vals and not self.env.context.get('ms_images_internal'):
            # Main image changed from the product form: it is the first image of the gallery
            Image = self.env['mirak.product.image'].with_context(ms_images_internal=True)
            for template in self:
                first = template._ms_gallery()[:1]
                if template.image_1920 and first:
                    first.image_1920 = template.image_1920
                elif template.image_1920:
                    Image.create({'product_tmpl_id': template.id, 'image_1920': template.image_1920, 'sequence': 1})
                elif first:
                    first.unlink()
        return res

    def _ms_gallery(self):
        """Gallery in display order (the cached one2many is not re-sorted after a position change)."""
        self.ensure_one()
        return self.ms_image_ids.sorted(lambda i: (i.sequence, i.id))

    def _ms_images_changed(self, context):
        """Gallery changed: refresh the main image and mark the products for export."""
        for template in self.exists():
            main = template._ms_gallery()[:1].image_1920 or False
            if template.image_1920 != main:
                template.with_context(ms_images_internal=True, ms_no_export=True).image_1920 = main
        if not context.get('ms_no_export'):
            self.exists()._ms_mark_dirty({'images'})
