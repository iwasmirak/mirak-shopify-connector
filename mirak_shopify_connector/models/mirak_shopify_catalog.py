# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Full-version feature: not included in the Lite edition (fields kept for upgrades)."""
import logging
from datetime import timedelta

from odoo import api, fields, models

from .mirak_edition import full_version_error
from odoo.exceptions import UserError, ValidationError

from .shopify_client import ShopifyError

_logger = logging.getLogger(__name__)


class MirakShopifyCollection(models.Model):
    """Shopify collection holding the products of one Odoo category."""
    _name = 'mirak.shopify.collection'
    _description = 'Shopify Collection of a Category'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    categ_id = fields.Many2one('product.category', required=True, ondelete='cascade')
    gid = fields.Char(string='Shopify Collection ID', required=True)
    name = fields.Char()

    _store_categ_uniq = models.Constraint('unique(store_id, categ_id)',
                                          'This category already has a collection in this store.')


class MirakShopifyCustomer(models.Model):
    """Link between an Odoo contact and a Shopify customer."""
    _name = 'mirak.shopify.customer'
    _description = 'Shopify Customer Link'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', index=True)
    customer_gid = fields.Char(string='Shopify Customer ID', required=True, index=True)
    shopify_updated_at = fields.Char()

    # A contact may match several Shopify customers (e.g. guest checkouts with the same email)
    _gid_uniq = models.Constraint('unique(store_id, customer_gid)', 'This Shopify customer is already linked.')


class MirakShopifyStoreCatalog(models.Model):
    _inherit = 'mirak.shopify.store'

    # Prices
    compare_at_source = fields.Selection([
        ('none', 'Not managed from Odoo'),
        ('list_price', 'Sales price, when higher than the Shopify price'),
        ('pricelist', 'Price of a pricelist, when higher than the Shopify price'),
    ], string='Compare-at Price', default='none', required=True,
        help="Shopify shows the compare-at price crossed out next to the price. 'Sales price' needs the "
             "Shopify price to come from a pricelist (your promotional prices). Sent with the prices.")
    compare_at_pricelist_id = fields.Many2one('product.pricelist', string='Compare-at Pricelist', check_company=True)

    # Tags, product type, collections
    sync_tags = fields.Boolean(
        string='Tags', default=False,
        help="Odoo product tags ↔ Shopify tags. Shopify tags are merged into Odoo before Odoo first "
             "sends the tags of a product, so no tag used by your collections is lost.")
    category_sync = fields.Selection([
        ('none', 'No'),
        ('odoo', 'Odoo category → Shopify product type'),
        ('both', 'Odoo category ↔ Shopify product type'),
    ], string='Product Category', default='none', required=True,
        help="Shopify product types become Odoo categories (created when missing) only in the two-way mode.")
    shopify_categ_parent_id = fields.Many2one(
        'product.category', string='Parent of New Categories',
        help="Categories created from Shopify product types are placed under this category.")
    category_collections = fields.Boolean(
        string='Category Collections', default=False,
        help="Add products to a manual Shopify collection named like their Odoo category (created when "
             "missing); a product changing category moves to the new collection.")

    # Stock of several warehouses published at the main location
    main_extra_warehouse_ids = fields.Many2many(
        related='location_id.extra_warehouse_ids', readonly=False, string='Also Publish Stock Of',
        help="The stock of these warehouses is added to the quantity published at the main location.")

    # Customers
    import_customers = fields.Boolean(
        string='Import Shopify Customers', default=False,
        help="Create and update Odoo contacts from all Shopify customers, not only those who ordered.")
    last_customer_pull = fields.Datetime(readonly=True, copy=False)

    # Point of sale orders
    pos_orders = fields.Selection([
        ('import', 'Import them, already delivered'),
        ('skip', 'Do not import them'),
    ], string='Shopify POS Orders', default='import', required=True,
        help="Orders sold with Shopify POS are imported with their shop location and their delivery is "
             "validated at once (the customer left with the goods).")

    # Orders cancelled in Odoo
    cancel_in_shopify = fields.Boolean(
        string='Cancel in Shopify Orders Cancelled in Odoo', default=False,
        help="An imported order cancelled in Odoo (nothing delivered) is cancelled in Shopify too.")
    cancel_restock = fields.Boolean(string='Restock in Shopify', default=True)
    cancel_refund = fields.Boolean(
        string='Refund the Customer', default=False,
        help="Refund the amount paid to the original payment method when cancelling in Shopify.")
    cancel_notify = fields.Boolean(string='Notify the Customer', default=False)

    @api.constrains('compare_at_source', 'compare_at_pricelist_id', 'price_source')
    def _check_compare_at(self):
        return None

    # ------------------------------------------------------------------
    # Compare-at price
    # ------------------------------------------------------------------

    def _variant_compare_at(self, product, price):
        return False

    # ------------------------------------------------------------------
    # Tags, product type and collections
    # ------------------------------------------------------------------

    def _odoo_tags(self, names):
        return None

    def _tags_for_push(self, template, product_gid=None):
        return None

    def _apply_shopify_tags(self, template, names):
        return None

    def _shopify_product_type(self, template):
        return None

    def _apply_shopify_product_type(self, template, product_type):
        return None

    def _category_collection(self, categ):
        return None

    def _catalog_product_input(self, template, maps, dirty, new):
        return {}

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    def _pull_customers(self):
        return None

    @staticmethod
    def _customer_contact(node):
        email = ((node.get('defaultEmailAddress') or {}).get('emailAddress') or node.get('email') or '').strip().lower()
        phone = ((node.get('defaultPhoneNumber') or {}).get('phoneNumber') or node.get('phone') or '').strip()
        return email, phone

    def _find_partner(self, email, phone):
        Partner = self.env['res.partner']
        partner = Partner
        if email:
            partner = Partner.search([('email', '=ilike', email), ('parent_id', '=', False)], limit=1)
        if not partner and phone:
            partner = Partner.search([('phone', '=', phone), ('parent_id', '=', False)], limit=1)
        return partner

    def _import_customer(self, node):
        return None

    def _partner_for_customer(self, customer_gid):
        if not customer_gid:
            return self.env['res.partner']
        return self.env['mirak.shopify.customer'].search(
            [('store_id', '=', self.id), ('customer_gid', '=', customer_gid)], limit=1).partner_id

    def _link_customer(self, partner, customer_gid):
        Link = self.env['mirak.shopify.customer']
        if not customer_gid or not partner or Link.search_count(
                [('store_id', '=', self.id), ('customer_gid', '=', customer_gid)]):
            return
        Link.create({'store_id': self.id, 'partner_id': partner.id, 'customer_gid': customer_gid})

    def _export_customer(self, partner):
        return None

    # ------------------------------------------------------------------
    # Orders cancelled in Odoo
    # ------------------------------------------------------------------

    def _push_cancellations(self):
        return None


class ResPartner(models.Model):
    _inherit = 'res.partner'

    ms_customer_ids = fields.One2many('mirak.shopify.customer', 'partner_id', string='Shopify Customers')

    def action_ms_send_to_shopify(self):
        raise full_version_error(self.env)
