# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
import base64
import logging
import math
import uuid
from datetime import datetime, timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import sql
from odoo.tools.mail import html2plaintext, plaintext2html

from . import mirak_edition, mirak_license
from .mirak_license import LicenseError, check_license
from .shopify_client import ShopifyClient, ShopifyError

_logger = logging.getLogger(__name__)

# Base key for the PostgreSQL advisory lock preventing two syncs of one store at once
SYNC_LOCK_KEY = 742_000_000
SHOPIFY_DT_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

PRODUCT_FRAGMENT = """
fragment MsProduct on Product {
  id
  title
  descriptionHtml
  status
  updatedAt
  tags
  productType
  options { name position values }
  variants(first: 100) {
    pageInfo { hasNextPage }
    nodes {
      id
      title
      sku
      barcode
      price
      selectedOptions { name value }
      inventoryItem { id tracked }
    }
  }
}
"""

PRODUCTS_QUERY = """
query MsProducts($cursor: String, $query: String) {
  products(first: 5, after: $cursor, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { ...MsProduct }
  }
}
""" + PRODUCT_FRAGMENT

PRODUCT_SET_MUTATION = """
mutation MsProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(input: $input, identifier: $identifier, synchronous: true) {
    product { ...MsProduct }
    userErrors { field message code }
  }
}
""" + PRODUCT_FRAGMENT

PRODUCT_UPDATE_MUTATION = """
mutation MsProductUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id status updatedAt }
    userErrors { field message }
  }
}
"""

VARIANTS_UPDATE_MUTATION = """
mutation MsVariantsUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id updatedAt }
    userErrors { field message code }
  }
}
"""

LEVELS_QUERY = """
query MsLevels($ids: [ID!]!, $locationId: ID!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id
      tracked
      inventoryLevel(locationId: $locationId) {
        quantities(names: ["available"]) { name quantity }
      }
    }
  }
}
"""

# Inventory mutations require an idempotency key since API 2026-04
SET_QUANTITIES_MUTATION = """
mutation MsSetQuantities($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    userErrors { field message code }
  }
}
"""

ACTIVATE_MUTATION = """
mutation MsActivate($itemId: ID!, $locationId: ID!, $available: Int, $idempotencyKey: String!) {
  inventoryActivate(inventoryItemId: $itemId, locationId: $locationId, available: $available)
    @idempotent(key: $idempotencyKey) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
"""


PRODUCT_BY_ID_QUERY = """
query MsProductById($id: ID!) {
  product(id: $id) { ...MsProduct }
}
""" + PRODUCT_FRAGMENT

MEDIA_QUERY = """
query MsMedia($id: ID!) {
  product(id: $id) {
    id
    media(first: 50) {
      nodes { id alt mediaContentType status ... on MediaImage { image { url } } }
    }
    variants(first: 100) { nodes { id media(first: 1) { nodes { id } } } }
  }
}
"""

STAGED_UPLOAD_MUTATION = """
mutation MsStagedUpload($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

ADD_MEDIA_MUTATION = """
mutation MsAddMedia($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
  productUpdate(product: $product, media: $media) {
    product { id updatedAt media(first: 50) { nodes { id } } }
    userErrors { field message }
  }
}
"""

DELETE_FILES_MUTATION = """
mutation MsDeleteFiles($ids: [ID!]!) {
  fileDelete(fileIds: $ids) {
    deletedFileIds
    userErrors { field message }
  }
}
"""

DETACH_FILES_MUTATION = """
mutation MsDetachFiles($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files { id }
    userErrors { field message }
  }
}
"""

REORDER_MEDIA_MUTATION = """
mutation MsReorderMedia($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job { id }
    mediaUserErrors { field message }
  }
}
"""

VARIANT_MEDIA_MUTATION = """
mutation MsVariantMedia($productId: ID!, $variantMedia: [ProductVariantAppendMediaInput!]!) {
  productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {
    userErrors { field message }
  }
}
"""

ORDERS_QUERY = """
query MsOrders($cursor: String, $query: String) {
  orders(first: 5, after: $cursor, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      updatedAt
      cancelledAt
      displayFinancialStatus
      displayFulfillmentStatus
      currencyCode
      taxesIncluded
      sourceName
      retailLocation { id }
      email
      phone
      note
      paymentGatewayNames
      currentTotalPriceSet { shopMoney { amount } }
      totalReceivedSet { shopMoney { amount } }
      totalRefundedSet { shopMoney { amount } }
      customer { id firstName lastName email phone }
      billingAddress { ...MsAddress }
      shippingAddress { ...MsAddress }
      shippingLines(first: 5) {
        nodes { title discountedPriceSet { shopMoney { amount } } taxLines { ...MsTaxLine } }
      }
      lineItems(first: 50) {
        pageInfo { hasNextPage }
        nodes {
          id
          name
          sku
          currentQuantity
          variant { id product { id } }
          discountedUnitPriceAfterAllDiscountsSet { shopMoney { amount } }
          taxLines { ...MsTaxLine }
        }
      }
    }
  }
}
fragment MsAddress on MailingAddress {
  name firstName lastName company address1 address2 city zip provinceCode countryCodeV2 phone
}
fragment MsTaxLine on TaxLine {
  title ratePercentage priceSet { shopMoney { amount } }
}
"""

FULFILLMENT_ORDERS_QUERY = """
query MsFulfillmentOrders($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 10) {
      nodes {
        id
        status
        lineItems(first: 50) { nodes { id remainingQuantity lineItem { id } } }
      }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation MsFulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

REFUNDS_QUERY = """
query MsRefunds($id: ID!) {
  order(id: $id) {
    refunds(first: 20) {
      id
      createdAt
      totalRefundedSet { shopMoney { amount } }
      refundLineItems(first: 50) {
        nodes { quantity lineItem { id } subtotalSet { shopMoney { amount } } }
      }
    }
  }
}
"""

PAID_STATUSES = ('PAID', 'PARTIALLY_PAID', 'PARTIALLY_REFUNDED', 'REFUNDED')

FULFILLMENT_SCOPE_HINT = (
    "Add the access scopes write_fulfillments and write_merchant_managed_fulfillment_orders "
    "to the Shopify app, then update the access token.")


class MirakShopifyLocation(models.Model):
    _name = 'mirak.shopify.location'
    _description = 'Shopify Location'
    _order = 'name'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade')
    name = fields.Char(required=True)
    gid = fields.Char(string='Shopify ID', required=True)
    shopify_active = fields.Boolean(string='Active in Shopify')
    is_main = fields.Boolean(string='Main Location', compute='_compute_is_main',
                             help="The store's main location, synchronized with the store warehouse.")
    sync_stock = fields.Boolean(
        string='Synchronize Stock',
        help="Also keep the stock of this Shopify location in line with an Odoo warehouse "
             "(the main location is always synchronized).")
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Odoo Warehouse',
        help="Warehouse of this location: its stock is published here and Shopify changes are applied to it. "
             "Orders fulfilled from this location are delivered from it.")
    extra_warehouse_ids = fields.Many2many(
        'stock.warehouse', 'mirak_shopify_location_extra_wh_rel', 'location_id', 'warehouse_id',
        string='Also Publish Stock Of',
        help="The stock of these warehouses is added to the quantity published at this location.")

    def _compute_is_main(self):
        for location in self:
            location.is_main = location.store_id.location_id == location

    @api.constrains('sync_stock', 'warehouse_id')
    def _check_warehouse(self):
        for location in self:
            if location.sync_stock and not location.is_main and not location.warehouse_id:
                raise ValidationError(self.env._("Select the Odoo warehouse of the location %s.", location.name))


class MirakShopifyStockLevel(models.Model):
    """Stock state of one linked variant at an additional Shopify location (the main location
    keeps it on the product link itself). Same field names as the link, so both are handled alike."""
    _name = 'mirak.shopify.stock.level'
    _description = 'Shopify Stock at an Additional Location'

    map_id = fields.Many2one('mirak.shopify.product.map', required=True, ondelete='cascade', index=True)
    location_id = fields.Many2one('mirak.shopify.location', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one(related='map_id.store_id')
    product_id = fields.Many2one(related='map_id.product_id')
    product_tmpl_id = fields.Many2one(related='map_id.product_tmpl_id')
    inventory_item_gid = fields.Char(related='map_id.inventory_item_gid')
    origin = fields.Selection(related='map_id.origin')
    shopify_tracked = fields.Boolean(related='map_id.shopify_tracked', readonly=False)
    qty_initialized = fields.Boolean()
    last_qty = fields.Integer(string='Last Synced Quantity')
    last_stock_sync = fields.Datetime()
    drop_since = fields.Datetime()

    _map_location_uniq = models.Constraint('unique(map_id, location_id)',
                                           'This variant already has a stock state at this location.')


class MirakShopifyStore(models.Model):
    _name = 'mirak.shopify.store'
    _description = 'Shopify Store'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    shop_domain = fields.Char(required=True, help="e.g. your-store.myshopify.com")
    auth_method = fields.Selection([
        ('client_credentials', 'Client ID and Secret (automatic token renewal)'),
        ('token', 'Admin API Access Token'),
    ], string='Authentication', default='token', required=True,
        help="Apps created in the Shopify Dev Dashboard give a Client ID and a Client secret: "
             "the connector gets and renews the access token automatically.")
    client_id = fields.Char(string='Client ID', groups='base.group_system', copy=False)
    client_secret = fields.Char(string='Client Secret', groups='base.group_system', copy=False)
    access_token = fields.Char(
        string='Admin API Access Token', groups='base.group_system', copy=False,
        help="Admin API access token (shpat_...) of the Shopify app installed on the store.")
    token_expires_at = fields.Datetime(string='Token Valid Until', groups='base.group_system',
                                       copy=False, readonly=True)
    api_version = fields.Char(string='API Version', required=True, default='2026-07')
    license_key = fields.Char(
        groups='base.group_system', copy=False,
        help="License key of Mirak Connector for Shopify, issued for this Shopify store.")
    license_status = fields.Char(compute='_compute_license_status')
    license_valid = fields.Boolean(compute='_compute_license_status')
    license_required = fields.Boolean(compute='_compute_license_status')
    license_days_left = fields.Integer(compute='_compute_license_status')
    license_expiring = fields.Boolean(
        compute='_compute_license_status', help="The license expires within 14 days.")
    ms_is_lite = fields.Boolean(compute='_compute_license_status', string='Lite Edition')
    ms_contact = fields.Char(compute='_compute_license_status')
    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
    ], default='draft', required=True, readonly=True, copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one(
        'stock.warehouse', required=True, check_company=True,
        help="Odoo warehouse whose free quantity is synchronized with the Shopify location.")
    location_ids = fields.One2many('mirak.shopify.location', 'store_id', string='Shopify Locations')
    location_id = fields.Many2one(
        'mirak.shopify.location', string='Shopify Location',
        domain="[('store_id', '=', id)]",
        help="The only Shopify location whose stock is synchronized with the Odoo warehouse.")

    # Scheduling
    auto_sync = fields.Boolean(string='Automatic Synchronization', default=True)
    sync_interval = fields.Integer(string='Every (minutes)', default=5, required=True)

    # Directions
    product_odoo_to_shopify = fields.Boolean(string='Products: Odoo → Shopify', default=True)
    product_shopify_to_odoo = fields.Boolean(string='Products: Shopify → Odoo', default=True)
    stock_odoo_to_shopify = fields.Boolean(string='Stock: Odoo → Shopify', default=True)
    stock_shopify_to_odoo = fields.Boolean(string='Stock: Shopify → Odoo', default=True)

    # Synchronized product data (updates; new products are always created complete)
    sync_name = fields.Boolean(string='Product Name', default=True)
    sync_description = fields.Boolean(string='Description', default=True)
    sync_price = fields.Boolean(string='Price', default=True)
    sync_sku_barcode = fields.Boolean(string='SKU and Barcode', default=True)
    sync_status = fields.Boolean(string='Status (archive)', default=True)
    sync_images = fields.Boolean(string='Images', default=True)

    # New Odoo products
    export_policy = fields.Selection([
        ('all', 'All new products'),
        ('categories', 'New products of selected categories'),
        ('manual', 'Only products ticked manually'),
    ], string='Send New Odoo Products', default='all', required=True)
    export_categ_ids = fields.Many2many('product.category', string='Categories')
    export_status = fields.Selection([
        ('active', 'Active'),
        ('draft', 'Draft'),
    ], string='Status of New Products in Shopify', default='active', required=True)

    # Price
    price_source = fields.Selection([
        ('list_price', 'Sales Price'),
        ('pricelist', 'Pricelist'),
    ], string='Shopify Price', default='list_price', required=True)
    pricelist_id = fields.Many2one('product.pricelist', check_company=True)

    # Stock
    stock_qty_type = fields.Selection([
        ('free', 'Free Quantity (on hand minus reserved)'),
        ('on_hand', 'On Hand Quantity'),
    ], string='Quantity Sent', default='free', required=True)
    stock_buffer = fields.Integer(
        string='Safety Buffer (units)', default=0,
        help="Units kept aside: this number is subtracted from the quantity sent to Shopify.")
    stock_percent = fields.Float(
        string='Percentage Sent', default=100.0,
        help="Percentage of the Odoo quantity made available in Shopify (rounded down).")
    initial_stock_source = fields.Selection([
        ('shopify', 'Shopify'),
        ('odoo', 'Odoo'),
    ], string='Initial Stock Source', default='shopify', required=True,
        help="When a product that already exists in Shopify is linked for the first time, "
             "which platform's quantity is kept. Products created from Odoo always keep the Odoo quantity.")

    # Conflicts and logs
    conflict_policy = fields.Selection([
        ('latest', 'Most recent change wins'),
        ('odoo', 'Odoo always wins'),
        ('shopify', 'Shopify always wins'),
    ], string='When Changed on Both Sides', default='latest', required=True)
    log_retention_days = fields.Integer(string='Keep Logs (days)', default=30, required=True)

    # Orders
    import_orders = fields.Boolean(string='Import Shopify Orders', default=False)
    order_import_from = fields.Datetime(
        string='Import Orders Created After', default=fields.Datetime.now,
        help="Older orders are never imported (avoids importing the order history twice).")
    order_status_filter = fields.Selection([
        ('all', 'All orders (including unpaid / cash on delivery)'),
        ('paid', 'Paid orders only'),
    ], string='Orders to Import', default='all', required=True)
    order_tax_mode = fields.Selection([
        ('shopify', 'Taxes charged by Shopify (matched to Odoo sales taxes by rate)'),
        ('none', 'No taxes (prices exactly as in Shopify)'),
        ('product', 'Taxes of the Odoo products'),
    ], string='Taxes on Imported Orders', default='shopify', required=True,
        help="Taxes charged by Shopify: each Shopify tax rate uses the Odoo sales tax with the same "
             "percentage (included in price or not, as in Shopify). Orders without taxes stay without taxes.")
    shop_currency_id = fields.Many2one(
        'res.currency', string='Shopify Currency', readonly=True, copy=False,
        help="Currency of the Shopify store, read when the connection is tested. Prices are converted "
             "when it differs from the Odoo currency.")
    order_team_id = fields.Many2one('crm.team', string='Sales Team', check_company=True)
    order_user_id = fields.Many2one('res.users', string='Salesperson')
    shipping_product_id = fields.Many2one(
        'product.product', string='Shipping Product', check_company=True,
        help="Service product used for Shopify shipping charges (created automatically if empty).")
    custom_item_product_id = fields.Many2one(
        'product.product', string='Custom Item Product', check_company=True,
        help="Service product used for Shopify custom items without product (created automatically if empty).")
    cancel_orders = fields.Boolean(string='Cancel Orders Cancelled in Shopify', default=True)
    fulfillment_mode = fields.Selection([
        ('odoo', 'Deliveries validated in Odoo are marked fulfilled in Shopify'),
        ('shopify', 'Orders fulfilled in Shopify validate the Odoo deliveries'),
        ('none', 'No synchronization'),
    ], string='Shipping', default='odoo', required=True)
    fulfillment_notify_customer = fields.Boolean(string='Notify Customer on Fulfillment', default=True)
    last_order_pull = fields.Datetime(readonly=True, copy=False)
    order_count = fields.Integer(compute='_compute_order_count')

    # Invoices and payments
    invoice_trigger = fields.Selection([
        ('none', 'Do not create invoices'),
        ('order', 'When the order is imported'),
        ('paid', 'When the order is paid in Shopify'),
        ('delivery', 'When the delivery is done in Odoo'),
    ], string='Create Invoices', default='none', required=True)
    invoice_send_email = fields.Boolean(string='Email Invoices to Customers', default=False)
    register_payments = fields.Boolean(
        string='Register Shopify Payments', default=True,
        help="Register on the invoices the amounts paid in Shopify, so that they become paid.")
    payment_journal_id = fields.Many2one(
        'account.journal', string='Default Payment Journal', check_company=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
        help="Journal of the Shopify payments whose payment method has no specific journal below.")
    gateway_journal_ids = fields.One2many('mirak.shopify.payment.journal', 'store_id',
                                          string='Journal per Payment Method')
    refund_credit_notes = fields.Boolean(string='Credit Notes for Shopify Refunds', default=True)
    income_account_id = fields.Many2one(
        'account.account', string='Sales Account', check_company=True,
        domain="[('account_type', 'in', ('income', 'income_other'))]",
        help="Revenue account of the Shopify invoice lines (product accounts are used if empty).")
    shipping_account_id = fields.Many2one(
        'account.account', string='Shipping Account', check_company=True,
        domain="[('account_type', 'in', ('income', 'income_other'))]")
    refund_account_id = fields.Many2one(
        'account.account', string='Refunds Account', check_company=True,
        domain="[('account_type', 'in', ('income', 'income_other'))]",
        help="Account of the credit notes created for Shopify refunds (sales account if empty).")

    def _compute_order_count(self):
        for store in self:
            store.order_count = self.env['sale.order'].search_count([('ms_store_id', '=', store.id)])

    def action_view_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Shopify Orders"),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('ms_store_id', '=', self.id)],
        }

    last_product_pull = fields.Datetime(readonly=True, copy=False)
    sync_requested = fields.Boolean(copy=False, help="A synchronization was requested with 'Sync now'")
    last_sync = fields.Datetime(string='Last Synchronization', readonly=True, copy=False)
    map_count = fields.Integer(compute='_compute_counts')
    log_count = fields.Integer(compute='_compute_counts')
    error_count = fields.Integer(compute='_compute_counts')

    def _compute_counts(self):
        Map = self.env['mirak.shopify.product.map']
        Log = self.env['mirak.shopify.log']
        since = fields.Datetime.now() - timedelta(days=1)
        for store in self:
            store.map_count = Map.search_count([('store_id', '=', store.id)])
            store.log_count = Log.search_count([('store_id', '=', store.id)])
            store.error_count = Log.search_count([
                ('store_id', '=', store.id), ('level', '=', 'error'), ('create_date', '>=', since)])

    @api.depends('license_key', 'shop_domain')
    def _compute_license_status(self):
        for store in self:
            store.license_required = mirak_license.LICENSE_KEY_REQUIRED
            store.ms_is_lite = mirak_edition.is_lite()
            store.ms_contact = mirak_edition.CONTACT
            store.license_days_left = 0
            store.license_expiring = False
            if not store.license_required:
                store.license_valid = True
                store.license_status = (self.env._("Lite edition (free)") if store.ms_is_lite
                                        else self.env._("Odoo Apps edition"))
                continue
            try:
                data = check_license(store.sudo().license_key, store.shop_domain)
            except LicenseError as e:
                store.license_valid = False
                store.license_status = str(e)
            else:
                store.license_valid = True
                left = mirak_license.days_left(data)
                if left is not None:
                    store.license_days_left = left
                    store.license_expiring = left <= 14
                expiry = self.env._("until %s", data['expires']) if data.get('expires') else self.env._("no expiry")
                store.license_status = self.env._("Valid for %(customer)s (%(expiry)s)",
                                                  customer=data.get('customer') or '-', expiry=expiry)

    def _check_license(self):
        self.ensure_one()
        if not mirak_license.LICENSE_KEY_REQUIRED:
            return
        try:
            check_license(self.sudo().license_key, self.shop_domain)
        except LicenseError as e:
            raise UserError(self.env._("Mirak Connector for Shopify: %s", e)) from None

    @api.constrains('sync_interval', 'stock_percent', 'stock_buffer', 'log_retention_days',
                    'price_source', 'pricelist_id', 'export_policy', 'export_categ_ids')
    def _check_settings(self):
        for store in self:
            if store.sync_interval < 1:
                raise ValidationError(self.env._("The synchronization interval must be at least 1 minute."))
            if not 0 < store.stock_percent <= 100:
                raise ValidationError(self.env._("The percentage sent must be between 1 and 100."))
            if store.stock_buffer < 0:
                raise ValidationError(self.env._("The safety buffer cannot be negative."))
            if store.log_retention_days < 1:
                raise ValidationError(self.env._("Logs must be kept at least 1 day."))
            if store.price_source == 'pricelist' and not store.pricelist_id:
                raise ValidationError(self.env._("Select the pricelist used for Shopify prices."))
            if store.export_policy == 'categories' and not store.export_categ_ids:
                raise ValidationError(self.env._("Select at least one category of products to send."))

    @api.model
    def _lite_values(self, vals):
        """Lite edition: the options of the full version keep their default value."""
        if not mirak_edition.is_lite():
            return vals
        return dict(vals, **{k: v for k, v in mirak_edition.LITE_LOCKED_VALUES.items() if k in vals})

    @api.model_create_multi
    def create(self, vals_list):
        if mirak_edition.is_lite() and self.with_context(active_test=False).search_count([]) + len(vals_list) > 1:
            raise UserError(self.env._(
                "The Lite edition connects one Shopify store. Several stores are part of the full version: "
                "contact %s.", mirak_edition.CONTACT))
        return super().create([self._lite_values(vals) for vals in vals_list])

    def write(self, vals):
        vals = self._lite_values(vals)
        if {'client_id', 'client_secret', 'auth_method'} & set(vals) and 'access_token' not in vals:
            # New credentials: forget the token obtained with the previous ones
            vals = dict(vals, token_expires_at=False)
            if vals.get('auth_method', self[:1].auth_method) in ('client_credentials', 'oauth'):
                vals['access_token'] = False
        if 'shop_domain' in vals:
            for store in self:
                new_domain = (vals['shop_domain'] or '').strip().lower()
                if store.map_count and new_domain != (store.shop_domain or '').strip().lower():
                    raise UserError(self.env._(
                        "Products are linked to %s. Use 'Disconnect Store' before connecting "
                        "another Shopify store, or create a new store record.", store.shop_domain))
        return super().write(vals)

    @api.model
    def _ms_default_sync(self, categ=None):
        """Whether a new Odoo product must be sent to Shopify, according to the stores' policies."""
        if not sql.table_exists(self.env.cr, self._table):
            return False  # module being installed
        stores = self.sudo().search([('state', '=', 'connected'), ('product_odoo_to_shopify', '=', True)])
        for store in stores:
            if store.export_policy == 'all':
                return True
            if store.export_policy == 'categories' and categ:
                if categ.parent_path and any(
                        f"/{c.id}/" in f"/{categ.parent_path}" for c in store.export_categ_ids):
                    return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _shop_host(self):
        return (self.shop_domain or '').strip().replace('https://', '').replace('http://', '').strip('/')

    def _client(self):
        self.ensure_one()
        self._check_license()
        refresher = None
        if self.auth_method == 'client_credentials':
            refresher = lambda: self._get_access_token(force=True)  # noqa: E731
        return ShopifyClient(self._shop_host(), self._get_access_token(), self.api_version,
                             token_refresher=refresher)

    def _get_access_token(self, force=False):
        store = self.sudo()
        if store.auth_method != 'client_credentials':
            if not store.access_token:
                raise UserError(self.env._("Please set the Admin API access token of %s.", self.name))
            return store.access_token.strip()
        margin = fields.Datetime.now() + timedelta(minutes=10)
        if not force and store.access_token and store.token_expires_at and store.token_expires_at > margin:
            return store.access_token
        return self._refresh_access_token()

    def _refresh_access_token(self):
        """Client credentials grant: exchange the app's Client ID / Secret for an access token."""
        store = self.sudo()
        if not store.client_id or not store.client_secret:
            raise UserError(self.env._("Please set the Client ID and Client Secret of %s.", self.name))
        try:
            response = requests.post(
                f"https://{self._shop_host()}/admin/oauth/access_token",
                json={'client_id': store.client_id.strip(), 'client_secret': store.client_secret.strip(),
                      'grant_type': 'client_credentials'},
                timeout=30)
        except requests.RequestException as e:
            raise UserError(self.env._("Cannot reach Shopify to get an access token: %s", e)) from e
        if response.status_code != 200:
            raise UserError(self.env._(
                "Shopify refused the Client ID / Client Secret (HTTP %(code)s): %(text)s",
                code=response.status_code, text=response.text[:300]))
        data = response.json()
        expires_in = data.get('expires_in')
        expires_at = (fields.Datetime.now() + timedelta(seconds=int(expires_in)) if expires_in
                      else fields.Datetime.now() + timedelta(days=3650))
        store.write({'access_token': data['access_token'], 'token_expires_at': expires_at})
        _logger.info("Shopify [%s]: new access token obtained, valid until %s", self.name, expires_at)
        return data['access_token']

    def _log(self, level, operation, message, template=None, reference=None):
        log_method = _logger.error if level == 'error' else _logger.info
        log_method("Shopify [%s] %s: %s", self.name, operation, message)
        self.env['mirak.shopify.log'].sudo().create({
            'store_id': self.id,
            'level': level,
            'operation': operation,
            'message': message,
            'product_tmpl_id': template.id if template else False,
            'reference': reference,
        })

    def _notify(self, message, notif_type='success'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': self.name, 'message': message, 'type': notif_type},
        }

    @staticmethod
    def _money(amount):
        return f"{amount:.2f}"

    @staticmethod
    def _html_to_text(html):
        return html2plaintext(html).strip() if html else ''

    @staticmethod
    def _text_to_html(text):
        return str(plaintext2html(text)) if text else ''

    @staticmethod
    def _parse_shopify_dt(value):
        return datetime.strptime(value, SHOPIFY_DT_FORMAT)

    def _odoo_qty(self, products, warehouses=None):
        """Odoo quantity (free or on hand, per settings) of each product in the store warehouse
        (or the given warehouses, added up)."""
        field = 'free_qty' if self.stock_qty_type == 'free' else 'qty_available'
        totals = dict.fromkeys(products.ids, 0.0)
        for warehouse in (warehouses or self.warehouse_id):
            for product in products.with_context(warehouse_id=warehouse.id):
                totals[product.id] += product[field]
        return {pid: int(round(qty)) for pid, qty in totals.items()}

    def _shopify_qty(self, products, warehouses=None):
        """Quantity to publish in Shopify: Odoo quantity after percentage and safety buffer."""
        return {pid: self._apply_stock_rules(qty) for pid, qty in self._odoo_qty(products, warehouses).items()}

    def _main_warehouses(self):
        return self.warehouse_id

    def _stock_locations(self):
        result = []
        if self.location_id:
            result.append((self.location_id, self.warehouse_id, self.warehouse_id))
        return result

    def _location_states(self, location, maps):
        """Stock states of these links at a location: the links themselves for the main location,
        stock level records (created when missing) for the others."""
        if location == self.location_id:
            return maps
        Level = self.env['mirak.shopify.stock.level']
        levels = Level.search([('location_id', '=', location.id), ('map_id', 'in', maps.ids)])
        missing = maps - levels.map_id
        if missing:
            levels |= Level.create([{'map_id': m.id, 'location_id': location.id} for m in missing])
        order = {m.id: i for i, m in enumerate(maps)}
        return levels.sorted(lambda l: order[l.map_id.id])

    def _location_for_gid(self, gid):
        """Synchronized location of a Shopify location id (the main location when unknown)."""
        for location, warehouse, _warehouses in self._stock_locations():
            if location.gid == gid:
                return location, warehouse
        return self.location_id, self.warehouse_id

    def _apply_stock_rules(self, qty):
        if self.stock_percent < 100:
            qty = math.floor(qty * self.stock_percent / 100.0)
        return max(0, qty - self.stock_buffer)

    def _shop_currency(self):
        """Currency of the Shopify store (read once from Shopify, then kept on the store)."""
        if not self.shop_currency_id:
            try:
                code = self._client().query('{ shop { currencyCode } }')['shop']['currencyCode']
            except Exception:
                _logger.warning("Shopify [%s]: store currency not available", self.name, exc_info=True)
                return self.company_id.currency_id
            self._set_shop_currency(code)
        return self.shop_currency_id or self.company_id.currency_id

    def _set_shop_currency(self, code):
        currency = self.env['res.currency'].with_context(active_test=False).search([('name', '=', code)], limit=1)
        if currency and currency != self.shop_currency_id:
            if not currency.active:
                currency.active = True
            self.shop_currency_id = currency

    def _price_currency(self):
        """Currency of the Odoo prices sent to Shopify (pricelist or company currency)."""
        if self.price_source == 'pricelist' and self.pricelist_id:
            return self.pricelist_id.currency_id
        return self.company_id.currency_id

    def _convert_price(self, amount, from_currency, to_currency):
        if from_currency == to_currency:
            return amount
        return from_currency._convert(amount, to_currency, self.company_id, fields.Date.context_today(self))

    def _from_shop_price(self, amount):
        """A Shopify price, in the currency of the Odoo prices."""
        return self._convert_price(amount, self._shop_currency(), self._price_currency())

    def _variant_price(self, product):
        """Price of the variant for Shopify, in the Shopify store currency."""
        if self.price_source == 'pricelist' and self.pricelist_id:
            price = self.pricelist_id._get_product_price(product, 1.0)
        else:
            price = product.lst_price
        return self._convert_price(price, self._price_currency(), self._shop_currency())

    def _set_odoo_variant_price(self, product, price):
        """Store a price received from Shopify for one variant, in the pricelist when one is used."""
        # Converted prices are compared with the rounding of the Shopify currency
        tolerance = 0.001 if self._shop_currency() == self._price_currency() else 0.011
        if abs(self._variant_price(product) - price) < tolerance:
            return
        price = self._from_shop_price(price)
        if self.price_source == 'pricelist' and self.pricelist_id:
            Item = self.env['product.pricelist.item'].with_context(ms_no_export=True)
            item = Item.search([
                ('pricelist_id', '=', self.pricelist_id.id),
                ('applied_on', '=', '0_product_variant'),
                ('product_id', '=', product.id),
            ], limit=1)
            if item:
                item.write({'compute_price': 'fixed', 'fixed_price': price})
            else:
                Item.create({
                    'pricelist_id': self.pricelist_id.id,
                    'applied_on': '0_product_variant',
                    'product_id': product.id,
                    'compute_price': 'fixed',
                    'fixed_price': price,
                })
        elif product.product_tmpl_id.product_variant_count == 1:
            product.product_tmpl_id.with_context(ms_no_export=True).write({'list_price': price})

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_test_connection(self):
        self.ensure_one()
        try:
            shop = self._client().query('{ shop { name myshopifyDomain currencyCode } }')['shop']
        except ShopifyError as e:
            self.state = 'draft'
            raise UserError(self.env._("Connection to Shopify failed: %s", e)) from e
        self.state = 'connected'
        if shop.get('currencyCode'):
            self._set_shop_currency(shop['currencyCode'])
        self._fetch_locations()
        self._log('info', 'connection', f"Connected to {shop['name']} ({shop['myshopifyDomain']})")
        try:
            scopes = requests.get(f"https://{self._shop_host()}/admin/oauth/access_scopes.json",
                                  headers={'X-Shopify-Access-Token': self._get_access_token()}, timeout=30).json()
            handles = {s['handle'] for s in scopes.get('access_scopes', [])}
            # A write scope includes the matching read scope
            handles |= {h.replace('write_', 'read_', 1) for h in handles if h.startswith('write_')}
            missing = [s for s in self._required_scopes() if s not in handles]
            if missing:
                self._log('warning', 'connection', "Shopify app access scopes missing: " + ", ".join(missing))
                return self._notify(self.env._(
                    "Connected to %(shop)s, but the Shopify app is missing these access scopes: %(scopes)s",
                    shop=shop['name'], scopes=", ".join(missing)), 'warning')
        except Exception:
            _logger.info("Shopify [%s]: access scopes could not be checked", self.name)
        return self._notify(self.env._("Connected to %s", shop['name']))

    def action_fetch_locations(self):
        self.ensure_one()
        self._fetch_locations()
        return self._notify(self.env._("Locations updated"))

    def _fetch_locations(self):
        data = self._client().query('{ locations(first: 50) { nodes { id name isActive } } }')
        for node in data['locations']['nodes']:
            location = self.location_ids.filtered(lambda l: l.gid == node['id'])
            vals = {'name': node['name'], 'shopify_active': node['isActive']}
            if location:
                location.write(vals)
            else:
                self.env['mirak.shopify.location'].create(dict(vals, store_id=self.id, gid=node['id']))

    def action_sync_now(self):
        """Run a synchronization in the background (scheduled action), so large stores never time out."""
        self.ensure_one()
        if self.state != 'connected':
            raise UserError(self.env._("Test the connection first."))
        if not self.location_id:
            raise UserError(self.env._("Select the Shopify Location (Stock tab) first."))
        self.sync_requested = True
        self.env.ref('mirak_shopify_connector.ir_cron_mirak_shopify_sync').sudo()._trigger()
        return self._notify(self.env._(
            "Synchronization started in the background. Results appear in the dashboard and logs as it progresses."),
            'info')

    def action_resend_all(self):
        """Send again the data of every linked product (e.g. after changing the pricelist)."""
        self.ensure_one()
        templates = self.env['mirak.shopify.product.map'].search([('store_id', '=', self.id)]).product_tmpl_id
        templates._ms_mark_dirty({'all'})
        if self.state == 'connected' and self.product_odoo_to_shopify:
            self._push_products(templates)
        return self._notify(self.env._("%s product(s) sent to Shopify. See the logs for details.", len(templates)),
                            'info')

    def action_disconnect(self):
        """Remove the product links of this store only (no product is deleted anywhere)."""
        self.ensure_one()
        maps = self.env['mirak.shopify.product.map'].search([('store_id', '=', self.id)])
        count = len(maps)
        maps.unlink()
        self.write({'state': 'draft', 'last_product_pull': False, 'last_sync': False})
        self._log('info', 'connection', f"Store disconnected: {count} product link(s) removed.")
        return self._notify(self.env._("Store disconnected, %s product link(s) removed.", count), 'warning')

    def action_view_maps(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Shopify Product Links"),
            'res_model': 'mirak.shopify.product.map',
            'view_mode': 'list,form',
            'domain': [('store_id', '=', self.id)],
            'context': {'default_store_id': self.id},
        }

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Shopify Logs"),
            'res_model': 'mirak.shopify.log',
            'view_mode': 'list,form',
            'domain': [('store_id', '=', self.id)],
        }

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    @api.model
    def _cron_sync(self):
        """Runs every minute; each store synchronizes when its own interval has elapsed."""
        now = fields.Datetime.now()
        for store in self.search([('state', '=', 'connected'), '|', ('auto_sync', '=', True), ('sync_requested', '=', True)]):
            if (not store.sync_requested and store.last_sync
                    and store.last_sync + timedelta(minutes=store.sync_interval, seconds=-30) > now):
                # Not due: only handle the notifications received meanwhile (real-time updates)
                if store.webhook_enabled:
                    store.with_context(ms_cron=True)._run_webhook_sync()
                continue
            if store.sync_requested:
                store.sync_requested = False
                self.env.cr.commit()
            store.with_context(ms_cron=True)._run_sync()

    def _run_sync(self):
        self.ensure_one()
        if not self.location_id:
            self._log('error', 'connection', "No Shopify location selected on the store; synchronization skipped.")
            return
        try:
            self._check_license()
        except UserError as e:
            self._log('error', 'connection', f"{e}; synchronization skipped.")
            self.last_sync = fields.Datetime.now()  # report once per interval, not every minute
            return
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY + self.id,))
        if not cr.fetchone()[0]:
            _logger.info("Shopify [%s]: a synchronization is already running, skipped.", self.name)
            return
        started = fields.Datetime.now()
        try:
            if self.product_shopify_to_odoo:
                self._run_step(self._pull_products, 'product_in')
            if self.import_customers:
                self._run_step(self._pull_customers, 'customer_in')
            # Orders are imported before the stock step so that the stock step already knows them
            if self.import_orders:
                self._run_step(self._pull_orders, 'order_in')
                if self.cancel_in_shopify:
                    self._run_step(self._push_cancellations, 'order_out')
                if self.returns_to_shopify:
                    self._run_step(self._push_returns, 'order_out')
            if self.import_abandoned_checkouts:
                self._run_step(self._pull_abandoned_checkouts, 'order_in')
            if self.product_odoo_to_shopify:
                self._run_step(self._push_products, 'product_out')
            if self.import_orders and self.fulfillment_mode == 'odoo':
                self._run_step(self._push_fulfillments, 'fulfillment_out')
            if self.import_orders and self.invoice_trigger != 'none':
                self._run_step(self._sync_accounting, 'invoice')
            if self.import_orders and self.import_payouts:
                self._run_step(self._pull_payouts, 'invoice')
            if self.stock_odoo_to_shopify or self.stock_shopify_to_odoo:
                self._run_step(self._sync_inventory, 'stock_out')
            # Last, and time-limited: catching up the images of existing products never delays
            # orders, invoices or stock
            if self.product_shopify_to_odoo and self.sync_images:
                self._run_step(self._backfill_images, 'product_in')
            # A full synchronization covers every notification received before it started
            self.env['mirak.shopify.webhook.event'].search(
                [('store_id', '=', self.id), ('processed', '=', False), ('create_date', '<=', started)]).write(
                {'processed': True})
            self.last_sync = fields.Datetime.now()
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY + self.id,))

    def _run_step(self, method, operation):
        if self.env.context.get('ms_cron'):
            # Background run: steps may commit progress themselves, so no savepoint around them;
            # on error only the work since the last commit is lost.
            try:
                method()
                self.env.cr.commit()
            except Exception as e:
                _logger.exception("Shopify [%s]: step %s failed", self.name, method.__name__)
                self.env.cr.rollback()
                self._log('error', operation, f"Step failed: {e}")
                self.env.cr.commit()
            return
        try:
            with self.env.cr.savepoint():
                method()
        except Exception as e:
            _logger.exception("Shopify [%s]: step %s failed", self.name, method.__name__)
            self._log('error', operation, f"Step failed: {e}")
        if self.env.context.get('ms_cron'):
            # Commit each step so that what was already sent to Shopify is also kept in Odoo.
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Products: Shopify -> Odoo
    # ------------------------------------------------------------------

    def _pull_products(self):
        client = self._client()
        started = fields.Datetime.now()
        search = None
        if self.last_product_pull:
            since = self.last_product_pull - timedelta(minutes=2)
            search = f"updated_at:>'{since.strftime(SHOPIFY_DT_FORMAT)}'"
        changed = 0
        first_failure = None
        in_cron = self.env.context.get('ms_cron')
        query = self._products_query(PRODUCTS_QUERY)
        for count, node in enumerate(client.paginate(query, {'query': search}, 'products'), start=1):
            try:
                with self.env.cr.savepoint():
                    if self._import_product(node):
                        changed += 1
            except Exception as e:
                _logger.exception("Shopify import of %s failed", node.get('id'))
                self._log('error', 'product_in', f"{node.get('title')}: {e}", reference=node.get('id'))
                failed_at = self._parse_shopify_dt(node['updatedAt'])
                first_failure = min(first_failure or failed_at, failed_at)
            if in_cron and count % 25 == 0:
                # Large catalogues: save progress (products come oldest change first), so an
                # interrupted import resumes here instead of starting over.
                if not first_failure:
                    self.last_product_pull = self._parse_shopify_dt(node['updatedAt'])
                self.env.cr.commit()
                _logger.info("Shopify [%s]: %s products processed", self.name, count)
        # A failed product must be retried at the next run: don't move the watermark past it
        self.last_product_pull = min(started, first_failure - timedelta(seconds=1)) if first_failure else started
        if changed:
            self._log('info', 'product_in', f"{changed} product(s) created or updated from Shopify")

    def _import_product(self, node):
        """Create/update the Odoo products of one Shopify product. Returns True if something was applied."""
        Map = self.env['mirak.shopify.product.map']
        variants = node['variants']['nodes']
        all_variants_loaded = not node['variants']['pageInfo']['hasNextPage']
        updated_at = node['updatedAt']
        maps = Map.search([('store_id', '=', self.id), ('shopify_product_gid', '=', node['id'])])

        # Already up to date (typically our own export coming back)
        if maps and all(m.shopify_updated_at and m.shopify_updated_at >= updated_at for m in maps):
            return False

        # Changed on both sides since the last sync: apply the conflict policy
        pending = self._pending_rows(maps.product_tmpl_id).product_tmpl_id
        if pending and self.product_odoo_to_shopify:
            if self.conflict_policy == 'latest':
                odoo_wins = max(pending.mapped('write_date')) > self._parse_shopify_dt(updated_at)
            else:
                odoo_wins = self.conflict_policy == 'odoo'
            winner = 'Odoo' if odoo_wins else 'Shopify'
            self._log('warning', 'product_in',
                      f"{node['title']}: changed in Odoo and Shopify; the {winner} version is kept.",
                      template=pending[0], reference=node['id'])
            if odoo_wins:
                maps.write({'shopify_updated_at': updated_at})
                return False

        if not all_variants_loaded:
            self._log('warning', 'product_in',
                      f"{node['title']}: only the first 100 variants are synchronized.", reference=node['id'])

        linked = self.env['mirak.shopify.product.map']
        unmatched = []
        for variant in variants:
            link = maps.filtered(lambda m: m.shopify_variant_gid == variant['id'])
            if not link:
                product = self._match_existing_product(variant)
                if product:
                    link = Map.create(self._map_vals(node, variant, product, origin='shopify'))
            if link:
                self._update_odoo_variant(link.product_id, variant)
                link.write(self._map_item_vals(variant))
                linked |= link
            else:
                unmatched.append(variant)

        if unmatched:
            if linked or maps:
                # Product partly linked (e.g. variants imported as separate products before):
                # each missing variant becomes its own Odoo product.
                for variant in unmatched:
                    linked |= self._create_odoo_product(node, [variant], standalone=True)
            else:
                linked |= self._create_odoo_product(node, unmatched)

        if all_variants_loaded:
            gone = maps.filtered(lambda m: m.shopify_variant_gid not in {v['id'] for v in variants})
            if gone:
                self._log('info', 'product_in',
                          f"{node['title']}: {len(gone)} variant(s) deleted in Shopify, link removed.",
                          reference=node['id'])
                gone.unlink()

        self._update_odoo_templates(node, linked)
        if self.sync_images:
            template = self._image_template_for(linked)
            if template:
                try:
                    with self.env.cr.savepoint():
                        self._pull_images(template, node['id'], linked)
                except Exception as e:
                    self._log('warning', 'product_in', f"{node['title']}: images not synchronized: {e}",
                              template=template, reference=node['id'])
        linked.write({'shopify_updated_at': updated_at})
        templates = linked.product_tmpl_id
        templates.with_context(ms_no_export=True).write({'ms_sync': True})
        self._clear_pending(templates)
        # A change made in this Shopify store reaches the other stores through Odoo
        others = self.search([('id', '!=', self.id), ('state', '=', 'connected'),
                              ('product_odoo_to_shopify', '=', True)])
        if others:
            templates._ms_mark_dirty({'all'}, stores=others)
        return True

    def _map_vals(self, node, variant, product, origin):
        return dict(
            self._map_item_vals(variant),
            store_id=self.id,
            product_id=product.id,
            shopify_product_gid=node['id'],
            shopify_variant_gid=variant['id'],
            shopify_updated_at=node['updatedAt'],
            origin=origin,
        )

    @staticmethod
    def _map_item_vals(variant):
        item = variant.get('inventoryItem') or {}
        return {
            'inventory_item_gid': item.get('id') or False,
            'shopify_tracked': bool(item.get('tracked')),
        }

    def _match_existing_product(self, variant):
        """Find a not yet linked Odoo variant with the same SKU (or the SP-<id> code of the old connector)."""
        Product = self.env['product.product'].with_context(active_test=False)
        Map = self.env['mirak.shopify.product.map']
        codes = [c for c in [(variant.get('sku') or '').strip(), f"SP-{variant['id'].rsplit('/', 1)[-1]}"] if c]
        for code in codes:
            candidates = Product.search([
                ('default_code', '=', code),
                ('company_id', 'in', [False, self.company_id.id]),
            ])
            for product in candidates:
                if not Map.search_count([('store_id', '=', self.id), ('product_id', '=', product.id)]):
                    return product
        return Product

    def _update_odoo_variant(self, product, variant, force=False):
        if not (force or self.sync_sku_barcode):
            return
        vals = {}
        sku = (variant.get('sku') or '').strip()
        if sku and sku != (product.default_code or ''):
            vals['default_code'] = sku
        barcode = (variant.get('barcode') or '').strip()
        if barcode and barcode != (product.barcode or '') and self._barcode_free(barcode, product):
            vals['barcode'] = barcode
        if vals:
            product.with_context(ms_no_export=True).write(vals)

    def _barcode_free(self, barcode, product=None):
        domain = [('barcode', '=', barcode)]
        if product:
            domain.append(('id', '!=', product.id))
        return not self.env['product.product'].with_context(active_test=False).search_count(domain)

    def _update_odoo_templates(self, node, links):
        Map = self.env['mirak.shopify.product.map']
        prices = {v['id']: float(v['price']) for v in node['variants']['nodes']}
        description = self._html_to_text(node['descriptionHtml'])
        for template in links.product_tmpl_id:
            vals = {'ms_shopify_status': node['status'].lower()}
            # Same product linked to other stores is fine; several products of this store is not
            exclusive = len(links.product_tmpl_id) == 1 and not Map.search_count([
                ('store_id', '=', self.id), ('product_tmpl_id', '=', template.id),
                ('shopify_product_gid', '!=', node['id'])])
            if exclusive:
                if self.sync_name and template.name != node['title']:
                    vals['name'] = node['title']
                if self.sync_description and description != (template.description_sale or '').strip():
                    vals['description_sale'] = description or False
            template.with_context(ms_no_export=True).write(vals)
            if exclusive:
                if self.sync_tags and node.get('tags') is not None:
                    self._apply_shopify_tags(template, node['tags'])
                self._apply_shopify_product_type(template, node.get('productType'))
                self._apply_shopify_metafields(template, node)
            if self.sync_price:
                for link in links.filtered(lambda m: m.product_tmpl_id == template):
                    price = prices.get(link.shopify_variant_gid)
                    if price is not None:
                        self._set_odoo_variant_price(link.product_id, price)

    def _create_odoo_product(self, node, variants, standalone=False):
        """Create an Odoo product for Shopify variants and link them. Returns the new links."""
        Map = self.env['mirak.shopify.product.map']
        options = [
            o for o in node['options']
            if not (o['name'] == 'Title' and o['values'] == ['Default Title'])
        ]
        first = variants[0]
        tracked = any((v.get('inventoryItem') or {}).get('tracked') for v in variants)
        name = node['title']
        if standalone and first.get('title') and first['title'] != 'Default Title':
            name = f"{name} - {first['title']}"
        vals = {
            'name': name,
            'type': 'consu',
            'is_storable': tracked,
            'list_price': self._convert_price(float(first['price']), self._shop_currency(),
                                              self.company_id.currency_id),
            'description_sale': self._html_to_text(node['descriptionHtml']) or False,
            'ms_sync': True,
            'ms_shopify_status': node['status'].lower(),
        }
        if options and not standalone:
            vals['attribute_line_ids'] = [
                (0, 0, {'attribute_id': attribute.id, 'value_ids': [(6, 0, values.ids)]})
                for attribute, values in (self._get_attribute(o['name'], o['values']) for o in options)
            ]
        template = self.env['product.template'].with_context(ms_no_export=True).create(vals)

        links = Map
        for variant in variants:
            if options and not standalone:
                product = self._find_template_variant(template, variant)
            else:
                product = template.product_variant_id
            if not product:
                self._log('warning', 'product_in',
                          f"{name}: no Odoo variant matches Shopify variant {variant.get('title')}.",
                          template=template, reference=variant['id'])
                continue
            self._update_odoo_variant(product, variant, force=True)
            links |= Map.create(self._map_vals(node, variant, product, origin='shopify'))

        if len(options) == 1 and not standalone:
            self._set_price_extras(template, variants, float(first['price']))
        self._log('info', 'product_in', f"Product created in Odoo: {name}", template=template, reference=node['id'])
        return links

    def _get_attribute(self, name, value_names):
        Attribute = self.env['product.attribute']
        attribute = Attribute.search([('name', '=ilike', name), ('create_variant', '=', 'always')], limit=1)
        if not attribute:
            attribute = Attribute.create({'name': name, 'create_variant': 'always'})
        values = self.env['product.attribute.value']
        for value_name in value_names:
            value = attribute.value_ids.filtered(lambda v: v.name.lower() == value_name.lower())[:1]
            if not value:
                value = values.create({'name': value_name, 'attribute_id': attribute.id})
            values |= value
        return attribute, values

    @staticmethod
    def _find_template_variant(template, variant):
        wanted = {o['name'].lower(): o['value'].lower() for o in variant['selectedOptions']}
        for product in template.product_variant_ids:
            current = {
                ptav.attribute_id.name.lower(): ptav.name.lower()
                for ptav in product.product_template_attribute_value_ids
            }
            if all(wanted.get(k) == v for k, v in current.items()) and len(current) == len(wanted):
                return product
        return template.env['product.product']

    def _set_price_extras(self, template, variants, base_price):
        """Single option products: express Shopify variant prices as Odoo price extras."""
        for variant in variants:
            price = float(variant['price'])
            if abs(price - base_price) < 0.001:
                continue
            value_name = variant['selectedOptions'][0]['value'].lower()
            ptav = template.attribute_line_ids.product_template_value_ids.filtered(
                lambda v: v.name.lower() == value_name)
            ptav.with_context(ms_no_export=True).write({'price_extra': self._convert_price(
                price - base_price, self._shop_currency(), self.company_id.currency_id)})

    # ------------------------------------------------------------------
    # Products: Odoo -> Shopify
    # ------------------------------------------------------------------

    def _pending_rows(self, templates=None):
        domain = [('store_id', '=', self.id)]
        if templates is not None:
            domain.append(('product_tmpl_id', 'in', templates.ids))
        return self.env['mirak.shopify.pending'].sudo().search(domain)

    def _pending_tags(self, template):
        return self._pending_rows(template).tag_set()

    def _clear_pending(self, templates):
        """The changes of these products reached this store (other stores keep theirs)."""
        rows = self._pending_rows(templates)
        if rows:
            rows.unlink()
            templates._ms_refresh_pending()

    def _push_products(self, templates=None):
        if templates is None:
            rows = self._pending_rows()
            templates = rows.product_tmpl_id.with_context(active_test=False)
            # No longer synchronized: nothing to send
            self._clear_pending(templates.filtered(lambda t: not t.ms_sync))
        templates = templates.with_context(active_test=False).filtered('ms_sync')
        for template in templates:
            try:
                if template.type == 'consu':
                    with self.env.cr.savepoint():
                        self._export_template(template)
                self._clear_pending(template)
            except Exception as e:
                _logger.exception("Shopify export of %s failed", template.display_name)
                self._log('error', 'product_out', f"{template.display_name}: {e}", template=template)

    def _accepts_new_product(self, template):
        """This store's 'send new products' policy (products already in the store are always updated)."""
        if self.export_policy == 'categories':
            return bool(template.categ_id.parent_path) and any(
                f"/{c.id}/" in f"/{template.categ_id.parent_path}" for c in self.export_categ_ids)
        return True

    def _export_template(self, template):
        Map = self.env['mirak.shopify.product.map']
        variants = template.with_context(active_test=True).product_variant_ids
        maps = Map.search([('store_id', '=', self.id), ('product_tmpl_id', '=', template.id)])
        dirty = self._pending_tags(template)

        if not maps:
            if not template.active or not variants or not self._accepts_new_product(template):
                return
            self._create_shopify_product(template, variants)
            new_maps = Map.search([('store_id', '=', self.id), ('product_tmpl_id', '=', template.id)])
            if self.sync_images and template.ms_image_ids and new_maps:
                self._push_images(template, new_maps[0].shopify_product_gid)
            self._push_product_extras(template, new_maps, set(), new=True)
            return

        product_gids = set(maps.mapped('shopify_product_gid'))
        exclusive = len(product_gids) == 1 and not Map.search_count([
            ('store_id', '=', self.id),
            ('shopify_product_gid', '=', maps[0].shopify_product_gid),
            ('product_tmpl_id', '!=', template.id),
        ])
        push_images = exclusive and self.sync_images and bool(dirty & {'images', 'all'})
        if exclusive:
            unmapped = variants - maps.product_id
            removed = maps.filtered(lambda m: m.product_id not in variants)
            if template.active and (unmapped or removed or 'structure' in dirty):
                self._set_shopify_product(template, variants, maps)
                if push_images:
                    self._push_images(template, maps.exists()[:1].shopify_product_gid or maps[0].shopify_product_gid)
                self._push_product_extras(
                    template, Map.search([('store_id', '=', self.id), ('product_tmpl_id', '=', template.id)]),
                    dirty, new=False)
                return
            self._update_shopify_product(template, maps, dirty)
        self._update_shopify_variants(template, maps, dirty)
        if push_images:
            self._push_images(template, maps[0].shopify_product_gid)
        if exclusive:
            self._push_product_extras(template, maps, dirty, new=False)

    def _shopify_status(self, template):
        if not template.active:
            return 'ARCHIVED'
        return 'ACTIVE' if self.export_status == 'active' else 'DRAFT'

    def _product_set_input(self, template, variants, maps, with_description=True, catalog=None):
        lines = template.attribute_line_ids.filtered(lambda l: l.attribute_id.create_variant != 'no_variant')
        if len(lines) > 3:
            raise UserError(self.env._("Shopify supports at most 3 options (attributes) per product."))
        quantities = self._shopify_qty(variants, self._main_warehouses()) if self.stock_odoo_to_shopify else {}

        def variant_options(product):
            if not lines:
                return [{'optionName': 'Title', 'name': 'Default Title'}]
            return [
                {'optionName': ptav.attribute_id.name, 'name': ptav.name}
                for ptav in product.product_template_attribute_value_ids
                if ptav.attribute_id in lines.attribute_id
            ]

        variant_inputs = []
        for product in variants:
            link = maps.filtered(lambda m: m.product_id == product)
            data = {'optionValues': variant_options(product)}
            # New variants are always sent complete; existing ones only with the synchronized data
            if not link or self.sync_price:
                price = self._variant_price(product)
                data['price'] = self._money(price)
                compare_at = self._variant_compare_at(product, price)
                if compare_at is not False:
                    data['compareAtPrice'] = compare_at
            if not link or self.sync_sku_barcode:
                data['sku'] = product.default_code or ''
                data['barcode'] = product.barcode or ''
            if link:
                data['id'] = link.shopify_variant_gid
            else:
                data['inventoryItem'] = {'tracked': bool(template.is_storable)}
                if template.is_storable and product.id in quantities:
                    data['inventoryQuantities'] = [{
                        'locationId': self.location_id.gid,
                        'name': 'available',
                        'quantity': quantities[product.id],
                    }]
            variant_inputs.append(data)

        # Option values are taken from the variants actually sent, in attribute order
        options = []
        if lines:
            for position, line in enumerate(lines, start=1):
                names = []
                for data in variant_inputs:
                    for option in data['optionValues']:
                        if option['optionName'] == line.attribute_id.name and option['name'] not in names:
                            names.append(option['name'])
                options.append({'name': line.attribute_id.name, 'position': position,
                                'values': [{'name': n} for n in names]})
        else:
            options = [{'name': 'Title', 'position': 1, 'values': [{'name': 'Default Title'}]}]

        result = {
            'title': template.name,
            'status': self._shopify_status(template),
            'productOptions': options,
            'variants': variant_inputs,
        }
        if with_description:
            result['descriptionHtml'] = self._text_to_html(template.description_sale)
        if catalog:
            result.update(catalog)
        return result, quantities

    def _create_shopify_product(self, template, variants):
        Map = self.env['mirak.shopify.product.map']
        catalog = self._catalog_product_input(template, Map, set(), new=True)
        collection = catalog.pop('_collection', False)
        product_input, quantities = self._product_set_input(template, variants, Map, catalog=catalog)
        result = self._client().mutate(PRODUCT_SET_MUTATION, {'input': product_input}, 'productSet')
        self._link_shopify_product(template, variants, result['product'], quantities)
        if collection:
            Map.search([('store_id', '=', self.id), ('product_tmpl_id', '=', template.id)]).write(
                {'collection_gid': collection})
        if 'tags' in catalog:
            template.with_context(ms_no_export=True).ms_tags_synced = True
        self._log('info', 'product_out', f"Product created in Shopify: {template.display_name}",
                  template=template, reference=result['product']['id'])

    def _set_shopify_product(self, template, variants, maps):
        """Full synchronization of a product whose variants changed in Odoo."""
        dirty = self._pending_tags(template)
        product_input, quantities = self._product_set_input(
            template, variants, maps,
            with_description=self.sync_description and bool(dirty & {'description', 'all'}))
        if not self.sync_name:
            product_input.pop('title')
        if not self.sync_status or (template.ms_shopify_status in ('draft', 'unlisted') and template.active):
            product_input.pop('status')
        result = self._client().mutate(
            PRODUCT_SET_MUTATION,
            {'input': product_input, 'identifier': {'id': maps[0].shopify_product_gid}},
            'productSet')
        removed = maps.filtered(lambda m: m.product_id not in variants)
        removed.unlink()
        self._link_shopify_product(template, variants, result['product'], quantities)
        self._log('info', 'product_out', f"Product variants updated in Shopify: {template.display_name}",
                  template=template, reference=result['product']['id'])
        # Tags, product type and collection are updated separately (a productSet would replace the
        # product's collections)
        if dirty & {'tags', 'category', 'metafields'}:
            links = self.env['mirak.shopify.product.map'].search(
                [('store_id', '=', self.id), ('product_tmpl_id', '=', template.id)])
            self._update_shopify_product(template, links, dirty & {'tags', 'category', 'metafields'})

    def _link_shopify_product(self, template, variants, node, quantities):
        """Create/refresh links after a productSet. New variants were created with the Odoo quantity."""
        Map = self.env['mirak.shopify.product.map']
        single = len(variants) == 1
        for variant in node['variants']['nodes']:
            if single:
                product = variants
            else:
                product = self._find_template_variant(template, variant)
            if not product:
                continue
            link = Map.search([('store_id', '=', self.id), ('product_id', '=', product.id)], limit=1)
            if link:
                link.write(dict(self._map_item_vals(variant),
                                shopify_variant_gid=variant['id'],
                                shopify_updated_at=node['updatedAt']))
            else:
                vals = self._map_vals(node, variant, product, origin='odoo')
                if template.is_storable and product.id in quantities:
                    vals.update(qty_initialized=True, last_qty=quantities[product.id],
                                last_stock_sync=fields.Datetime.now())
                Map.create(vals)
        template.with_context(ms_no_export=True).write({'ms_shopify_status': node['status'].lower()})

    def _update_shopify_product(self, template, maps, dirty):
        product_input = {'id': maps[0].shopify_product_gid}
        if self.sync_name and dirty & {'name', 'all'}:
            product_input['title'] = template.name
        if self.sync_description and dirty & {'description', 'all'}:
            product_input['descriptionHtml'] = self._text_to_html(template.description_sale)
        if self.sync_status:
            if not template.active and template.ms_shopify_status != 'archived':
                product_input['status'] = 'ARCHIVED'
            elif template.active and template.ms_shopify_status == 'archived':
                product_input['status'] = self._shopify_status(template)
        catalog = self._catalog_product_input(template, maps, dirty, new=False)
        collection = catalog.pop('_collection', 'unchanged')
        product_input.update(catalog)
        if len(product_input) == 1:
            return
        result = self._client().mutate(PRODUCT_UPDATE_MUTATION, {'product': product_input}, 'productUpdate')
        product = result['product']
        maps.write({'shopify_updated_at': product['updatedAt']})
        if collection != 'unchanged':
            maps.write({'collection_gid': collection or False})
        if 'tags' in catalog:
            template.with_context(ms_no_export=True).ms_tags_synced = True
        template.with_context(ms_no_export=True).write({'ms_shopify_status': product['status'].lower()})
        self._log('info', 'product_out', f"Product updated in Shopify: {template.display_name}",
                  template=template, reference=product['id'])

    def _update_shopify_variants(self, template, maps, dirty):
        send_price = self.sync_price and bool(dirty & {'price', 'all'})
        send_codes = self.sync_sku_barcode and bool(dirty & {'variant', 'all'})
        send_tracking = 'tracking' in dirty
        if not (send_price or send_codes or send_tracking):
            return
        client = self._client()
        for product_gid in set(maps.mapped('shopify_product_gid')):
            product_maps = maps.filtered(lambda m: m.shopify_product_gid == product_gid)
            variant_inputs = []
            for link in product_maps:
                product = link.product_id
                data = {'id': link.shopify_variant_gid}
                if send_price:
                    price = self._variant_price(product)
                    data['price'] = self._money(price)
                    compare_at = self._variant_compare_at(product, price)
                    if compare_at is not False:
                        data['compareAtPrice'] = compare_at
                if send_codes:
                    data['barcode'] = product.barcode or ''
                    data['inventoryItem'] = {'sku': product.default_code or ''}
                if send_tracking:
                    data.setdefault('inventoryItem', {})['tracked'] = bool(product.is_storable)
                variant_inputs.append(data)
            result = client.mutate(
                VARIANTS_UPDATE_MUTATION,
                {'productId': product_gid, 'variants': variant_inputs},
                'productVariantsBulkUpdate')
            product_maps.write({'shopify_updated_at': result['product']['updatedAt']})
        self._log('info', 'product_out', f"Variants updated in Shopify: {template.display_name}", template=template)

    # ------------------------------------------------------------------
    # Images (both directions)
    # ------------------------------------------------------------------

    def _image_template_for(self, links):
        """The Odoo product whose gallery mirrors this Shopify product (None when the Shopify
        product is split over several Odoo products)."""
        template = links.product_tmpl_id
        if len(template) != 1:
            return None
        others = self.env['mirak.shopify.product.map'].search_count([
            ('store_id', '=', self.id), ('product_tmpl_id', '=', template.id),
            ('shopify_product_gid', '!=', links[0].shopify_product_gid)])
        return None if others else template

    @staticmethod
    def _image_mime(content):
        if content[:8] == b'\x89PNG\r\n\x1a\n':
            return 'image/png', 'png'
        if content[:3] == b'GIF':
            return 'image/gif', 'gif'
        if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
            return 'image/webp', 'webp'
        return 'image/jpeg', 'jpg'

    def _download_image(self, url):
        response = requests.get(url, timeout=60)
        if response.status_code != 200:
            raise UserError(self.env._("Cannot download the Shopify image (HTTP %s).", response.status_code))
        if len(response.content) > 20 * 1024 * 1024:
            raise UserError(self.env._("Shopify image larger than 20 MB, skipped."))
        return response.content

    def _pull_images(self, template, product_gid, links=None):
        """Shopify media -> Odoo gallery: new images downloaded, removed ones deleted, order kept."""
        Image = self.env['mirak.product.image'].with_context(ms_no_export=True, ms_images_internal=True)
        Link = self.env['mirak.product.image.link']
        data = self._client().query(MEDIA_QUERY, {'id': product_gid}).get('product') or {}
        nodes = [n for n in (data.get('media') or {}).get('nodes', []) if n.get('mediaContentType') == 'IMAGE']
        gids = [n['id'] for n in nodes]
        current = Link.search([('store_id', '=', self.id), ('image_id.product_tmpl_id', '=', template.id)])
        for link in current.filtered(lambda l: l.media_gid not in gids):
            if link.image_id.checksum == link.checksum:
                link.image_id.with_context(ms_no_export=True, ms_images_internal=True).unlink()
            else:
                link.unlink()  # changed in Odoo meanwhile: kept, and sent again at the next export
        added = 0
        for position, node in enumerate(nodes, start=1):
            link = current.exists().filtered(lambda l: l.media_gid == node['id'])
            if link:
                if link.image_id.sequence != position:
                    link.image_id.with_context(ms_no_export=True, ms_images_internal=True).sequence = position
                continue
            url = (node.get('image') or {}).get('url')
            if not url:
                continue  # still being processed by Shopify: taken at the next run
            image = Image.create({
                'product_tmpl_id': template.id,
                'image_1920': base64.b64encode(self._download_image(url)),
                'sequence': position,
                'name': node.get('alt') or False,
            })
            Link.create({'image_id': image.id, 'store_id': self.id, 'media_gid': node['id'], 'checksum': image.checksum})
            added += 1
        # Variant images
        if template.product_variant_count > 1:
            maps = links or self.env['mirak.shopify.product.map'].search(
                [('store_id', '=', self.id), ('shopify_product_gid', '=', product_gid)])
            variants = {m.shopify_variant_gid: m.product_id for m in maps}
            for variant in (data.get('variants') or {}).get('nodes', []):
                media = ((variant.get('media') or {}).get('nodes')) or []
                product = variants.get(variant['id'])
                link = media and Link.search([('store_id', '=', self.id), ('media_gid', '=', media[0]['id']),
                                              ('product_tmpl_id', '=', template.id)], limit=1)
                if product and link and link.image_id.product_id != product:
                    link.image_id.with_context(ms_no_export=True, ms_images_internal=True).product_id = product
        template.with_context(ms_no_export=True).ms_images_synced = True
        return added

    def _stage_upload(self, client, image):
        content = base64.b64decode(image.image_1920)
        mime, ext = self._image_mime(content)
        filename = f"{image.product_tmpl_id.default_code or 'product'}-{image.id}.{ext}".replace(' ', '-')
        result = client.mutate(STAGED_UPLOAD_MUTATION, {'input': [{
            'resource': 'IMAGE', 'filename': filename, 'mimeType': mime, 'httpMethod': 'POST',
            'fileSize': str(len(content)),
        }]}, 'stagedUploadsCreate')
        target = result['stagedTargets'][0]
        response = requests.post(target['url'], data={p['name']: p['value'] for p in target['parameters']},
                                 files={'file': (filename, content, mime)}, timeout=120)
        if response.status_code not in (200, 201, 204):
            raise ShopifyError(f"Image upload refused (HTTP {response.status_code}): {response.text[:200]}")
        return target['resourceUrl']

    def _push_images(self, template, product_gid):
        """Odoo gallery -> Shopify media: deleted/changed images removed, new ones uploaded, order applied."""
        client = self._client()
        Link = self.env['mirak.product.image.link']
        Map = self.env['mirak.shopify.product.map']
        images = template.ms_image_ids.sorted(lambda i: (i.sequence, i.id))
        links = Link.search([('store_id', '=', self.id), ('image_id', 'in', images.ids)])

        # 1. Deleted in Odoo, or content changed (the old version is replaced)
        tombstones = list(filter(None, (template.ms_deleted_media or '').split(',')))
        to_delete = [t.split(':', 1)[1] for t in tombstones if t.split(':', 1)[0] == str(self.id)]
        replaced = links.filtered(lambda l: l.checksum != l.image_id.checksum)
        to_delete += replaced.mapped('media_gid')
        kept_tombstones = [t for t in tombstones if t.split(':', 1)[0] != str(self.id)]
        if to_delete:
            try:
                # A file also used by other products is only detached from this one
                shared = {gid for gid in to_delete if Link.search_count([
                    ('store_id', '=', self.id), ('media_gid', '=', gid), ('product_tmpl_id', '!=', template.id)])}
                if shared:
                    client.mutate(DETACH_FILES_MUTATION, {'files': [
                        {'id': gid, 'referencesToRemove': [product_gid]} for gid in shared]}, 'fileUpdate')
                own = [gid for gid in to_delete if gid not in shared]
                if own:
                    client.mutate(DELETE_FILES_MUTATION, {'ids': own}, 'fileDelete')
            except ShopifyError as e:
                if 'write_files' not in str(e) and 'Access denied' not in str(e):
                    raise
                # Missing permission: keep them to delete later, don't block the rest of the product
                kept_tombstones += [f"{self.id}:{gid}" for gid in to_delete]
                to_delete = []
                self._log('warning', 'product_out',
                          f"{template.display_name}: images deleted in Odoo are still in Shopify. Add the "
                          f"'write_files' access scope to the Shopify app to delete them.", template=template)
        replaced.unlink()
        template.with_context(ms_no_export=True).ms_deleted_media = ','.join(kept_tombstones) or False

        # 2. New images
        data = client.query(MEDIA_QUERY, {'id': product_gid}).get('product') or {}
        media_ids = [n['id'] for n in (data.get('media') or {}).get('nodes', [])]
        new_images = images.filtered(lambda i: i.image_1920 and not i.link_ids.filtered(lambda l: l.store_id == self))
        if new_images:
            sources = [{'originalSource': self._stage_upload(client, image), 'mediaContentType': 'IMAGE',
                        'alt': image.name or template.name} for image in new_images]
            result = client.mutate(ADD_MEDIA_MUTATION, {'product': {'id': product_gid}, 'media': sources},
                                   'productUpdate')
            after = [n['id'] for n in result['product']['media']['nodes']]
            new_ids = [gid for gid in after if gid not in media_ids]
            for image, gid in zip(new_images, new_ids):
                Link.create({'image_id': image.id, 'store_id': self.id, 'media_gid': gid, 'checksum': image.checksum})
            if len(new_ids) < len(new_images):
                self._log('warning', 'product_out', f"{template.display_name}: {len(new_images) - len(new_ids)} "
                                                    f"image(s) not confirmed by Shopify yet.", template=template)
            media_ids = after

        # 3. Order
        desired = [link.media_gid for image in images for link in image.link_ids if link.store_id == self]
        if desired and [g for g in media_ids if g in desired] != desired:
            result = client.query(REORDER_MEDIA_MUTATION, {
                'id': product_gid,
                'moves': [{'id': gid, 'newPosition': str(position)} for position, gid in enumerate(desired)],
            })['productReorderMedia']
            if result.get('mediaUserErrors'):
                self._log('warning', 'product_out', f"{template.display_name}: image order not applied: "
                                                    f"{result['mediaUserErrors'][0]['message']}", template=template)

        # 4. Variant images
        variant_media = []
        for image in images.filtered('product_id'):
            link = image.link_ids.filtered(lambda l: l.store_id == self)[:1]
            variant = Map.search([('store_id', '=', self.id), ('product_id', '=', image.product_id.id)], limit=1)
            if link and variant:
                variant_media.append({'variantId': variant.shopify_variant_gid, 'mediaIds': [link.media_gid]})
        if variant_media:
            try:
                client.mutate(VARIANT_MEDIA_MUTATION, {'productId': product_gid, 'variantMedia': variant_media},
                              'productVariantAppendMedia')
            except ShopifyError as e:
                self._log('warning', 'product_out', f"{template.display_name}: variant images: {e}", template=template)
        template.with_context(ms_no_export=True).ms_images_synced = True
        if new_images or to_delete:
            self._log('info', 'product_out', f"{template.display_name}: {len(new_images)} image(s) sent, "
                                             f"{len(to_delete)} removed in Shopify", template=template,
                      reference=product_gid)

    def _backfill_images(self):
        """Products linked before images were synchronized: take their images (300 products per run)."""
        maps = self.env['mirak.shopify.product.map'].search(
            [('store_id', '=', self.id), ('product_tmpl_id.ms_images_synced', '=', False)])
        if not maps:
            return
        by_product = {}
        for link in maps:
            by_product.setdefault(link.shopify_product_gid, self.env['mirak.shopify.product.map'])
            by_product[link.shopify_product_gid] |= link
        in_cron = self.env.context.get('ms_cron')
        done = images = 0
        deadline = datetime.now() + timedelta(minutes=4)
        for product_gid, links in list(by_product.items())[:300]:
            if datetime.now() > deadline:
                break  # continued at the next run
            template = self._image_template_for(links)
            try:
                with self.env.cr.savepoint():
                    if template:
                        images += self._pull_images(template, product_gid, links)
                        if not template.ms_image_ids and template.image_1920:
                            # Odoo image that never went to Shopify: becomes the gallery and is exported
                            self.env['mirak.product.image'].create(
                                {'product_tmpl_id': template.id, 'image_1920': template.image_1920, 'sequence': 1})
                    else:
                        links.product_tmpl_id.with_context(ms_no_export=True).ms_images_synced = True
            except Exception as e:
                _logger.exception("Shopify images of %s failed", product_gid)
                self._log('warning', 'product_in', f"Images of {links[0].product_tmpl_id.display_name}: {e}",
                          template=links[0].product_tmpl_id, reference=product_gid)
                links.product_tmpl_id.with_context(ms_no_export=True).ms_images_synced = True
            done += 1
            if in_cron and done % 20 == 0:
                self.env.cr.commit()
        left = len(by_product) - done
        self._log('info', 'product_in', f"Images: {done} product(s) checked, {images} image(s) imported"
                                        + (f", {left} product(s) left for the next runs" if left > 0 else ""))

    # ------------------------------------------------------------------
    # Orders: Shopify -> Odoo
    # ------------------------------------------------------------------

    def _pull_orders(self):
        client = self._client()
        started = fields.Datetime.now()
        since = (self.last_order_pull - timedelta(minutes=2)) if self.last_order_pull else self.order_import_from
        search = f"updated_at:>'{since.strftime(SHOPIFY_DT_FORMAT)}'" if since else None
        created = updated = 0
        first_failure = None
        for node in client.paginate(self._orders_query(), {'query': search}, 'orders'):
            try:
                with self.env.cr.savepoint():
                    result = self._process_order(node)
                    created += result == 'created'
                    updated += result == 'updated'
            except Exception as e:
                _logger.exception("Shopify order %s import failed", node.get('name'))
                self._log('error', 'order_in', f"Order {node.get('name')}: {e}", reference=node.get('id'))
                failed_at = self._parse_shopify_dt(node['updatedAt'])
                first_failure = min(first_failure or failed_at, failed_at)
        # A failed order must be retried at the next run: don't move the watermark past it
        self.last_order_pull = min(started, first_failure - timedelta(seconds=1)) if first_failure else started
        if created or updated:
            self._log('info', 'order_in', f"Orders: {created} imported, {updated} updated")

    def _process_order(self, node):
        SaleOrder = self.env['sale.order']
        order = SaleOrder.search([('ms_store_id', '=', self.id), ('ms_order_gid', '=', node['id'])], limit=1)
        if order:
            if order.ms_updated_at and order.ms_updated_at >= node['updatedAt']:
                return None
            self._update_odoo_order(order, node)
            return 'updated'
        if self.order_import_from and self._parse_shopify_dt(node['createdAt']) < self.order_import_from:
            return None
        if node['cancelledAt']:
            return None
        if node.get('sourceName') == 'pos' and self.pos_orders == 'skip':
            return None
        if self.order_status_filter == 'paid' and node['displayFinancialStatus'] not in ('PAID', 'PARTIALLY_PAID'):
            return None  # imported later, when Shopify reports it paid (its updatedAt changes)
        if self._link_legacy_order(node):
            return 'updated'
        self._create_odoo_order(node)
        return 'created'

    def _link_legacy_order(self, node):
        """Orders imported by the previous connector (i8cloud) are linked, not imported twice."""
        if 'i8.shopify.order.mapping' not in self.env:
            return False
        legacy = self.env['i8.shopify.order.mapping'].sudo().search(
            [('external_order_id', '=', node['id'].rsplit('/', 1)[-1])], limit=1)
        order = legacy.sale_order_id
        if not order or order.ms_order_gid:
            return False
        order.write(self._order_status_vals(node) | {
            'ms_store_id': self.id, 'ms_order_gid': node['id'], 'ms_order_name': node['name']})
        self._log('info', 'order_in', f"Order {node['name']} already imported as {order.name}: linked.",
                  reference=node['id'])
        return True

    @staticmethod
    def _order_status_vals(node):
        status = node.get('displayFinancialStatus') or ''

        def money(key):
            return float((((node.get(key) or {}).get('shopMoney')) or {}).get('amount') or 0.0)

        received = money('totalReceivedSet')
        refunded = money('totalRefundedSet')
        if received < 0.01 and status in PAID_STATUSES:
            # Marked as paid without a payment transaction: the whole order is paid
            received = money('currentTotalPriceSet') + refunded
        gateways = node.get('paymentGatewayNames') or []
        return {
            'ms_financial_status': status.lower(),
            'ms_fulfillment_status': (node.get('displayFulfillmentStatus') or '').lower(),
            'ms_updated_at': node['updatedAt'],
            'ms_gateway': gateways[0] if gateways else False,
            'ms_amount_received': received,
            'ms_amount_refunded': refunded,
            'ms_accounting_pending': True,
            'ms_accounting_try': False,
        }

    def _create_odoo_order(self, node):
        if node['lineItems']['pageInfo']['hasNextPage']:
            raise UserError(self.env._("Orders with more than 50 lines are not supported."))
        partner, shipping = self._get_order_partners(node)
        pricelist = self._order_pricelist(node['currencyCode'])
        included = bool(node.get('taxesIncluded'))

        line_vals, line_taxes = [], []
        for item in node['lineItems']['nodes']:
            qty = item.get('currentQuantity') or 0
            if qty <= 0:
                continue  # removed from the order
            line_vals.append({
                'product_id': self._order_line_product(item).id,
                'name': item['name'] + (f" [{item['sku']}]" if item.get('sku') else ''),
                'product_uom_qty': qty,
                'price_unit': float(item['discountedUnitPriceAfterAllDiscountsSet']['shopMoney']['amount']),
                'ms_line_gid': item['id'],
            })
            line_taxes.append(item.get('taxLines') or [])
        for shipping_line in node['shippingLines']['nodes']:
            amount = float(shipping_line['discountedPriceSet']['shopMoney']['amount'])
            line_vals.append({
                'product_id': self._service_product('shipping_product_id', "Shopify Shipping").id,
                'name': self.env._("Shipping: %s", shipping_line['title']),
                'product_uom_qty': 1,
                'price_unit': amount,
            })
            line_taxes.append(shipping_line.get('taxLines') or [])
        if not line_vals:
            return
        if self.order_tax_mode == 'shopify':
            # Before creating anything: a missing Odoo tax stops the import (retried next run)
            line_taxes = [self._shopify_taxes(tax_lines, included) for tax_lines in line_taxes]
        location, warehouse = self._order_location(node)

        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'partner_invoice_id': partner.id,
            'partner_shipping_id': shipping.id,
            'company_id': self.company_id.id,
            'warehouse_id': warehouse.id,
            'ms_location_id': location.id,
            'pricelist_id': pricelist.id or False,
            'date_order': self._parse_shopify_dt(node['createdAt']),
            'client_order_ref': node['name'],
            'origin': f"Shopify {node['name']}",
            'team_id': self.order_team_id.id or False,
            'user_id': self.order_user_id.id or False,
            'note': node.get('note') or False,
            'ms_store_id': self.id,
            'ms_order_gid': node['id'],
            'ms_order_name': node['name'],
            **self._order_status_vals(node),
            'order_line': [(0, 0, vals) for vals in line_vals],
        })
        # Keep Shopify prices (and taxes policy) exactly, whatever the pricelist says
        for line, vals, taxes in zip(order.order_line, line_vals, line_taxes):
            if line.price_unit != vals['price_unit']:
                line.price_unit = vals['price_unit']
            if self.order_tax_mode == 'none' and line.tax_ids:
                line.tax_ids = [(5, 0, 0)]
            elif self.order_tax_mode == 'shopify' and line.tax_ids != taxes:
                line.tax_ids = [(6, 0, taxes.ids)]
        expected = float(node['currentTotalPriceSet']['shopMoney']['amount'])
        if self.order_tax_mode == 'shopify' and abs(order.amount_total - expected) > 0.05:
            self._log('warning', 'order_in',
                      f"Order {node['name']}: Odoo total {order.amount_total:.2f} differs from the Shopify "
                      f"total {expected:.2f} (rounding or tax setup).", reference=node['id'])
        order.action_confirm()
        # Shopify already lowered 'available' for this order: expect it in the stock step
        self._shift_expected_stock(order, -1)
        if node.get('sourceName') == 'pos':
            # Sold over the counter: the goods already left the shop
            self._validate_order_deliveries(order, node)
        self._log('info', 'order_in', f"Order {node['name']} imported as {order.name}", reference=node['id'])
        return order

    def _order_pricelist(self, currency_code):
        """Pricelist in the order currency: none needed in the company currency; created (and the
        currency activated) for another currency."""
        Pricelist = self.env['product.pricelist']
        domain = [('currency_id.name', '=', currency_code), ('company_id', 'in', [False, self.company_id.id])]
        pricelist = Pricelist.search(domain, limit=1)
        if pricelist or currency_code == self.company_id.currency_id.name:
            return pricelist
        currency = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', currency_code)], limit=1)
        if not currency:
            raise UserError(self.env._("Unknown currency %s.", currency_code))
        if not currency.active:
            currency.active = True
        pricelist = Pricelist.create({
            'name': f"Shopify {self.name} ({currency_code})",
            'currency_id': currency.id,
            'company_id': self.company_id.id,
        })
        self._log('info', 'order_in', f"Pricelist {pricelist.name} created for the orders in {currency_code}.")
        return pricelist

    def _shopify_taxes(self, tax_lines, included):
        """Odoo sales taxes matching the Shopify tax lines of one order line (same rate, same
        'included in price' setting)."""
        Tax = self.env['account.tax']
        taxes = Tax
        for tax_line in tax_lines:
            rate = float(tax_line.get('ratePercentage') or 0.0)
            if rate <= 0:
                continue
            candidates = Tax.search([
                ('type_tax_use', '=', 'sale'),
                ('amount_type', '=', 'percent'),
                ('company_id', '=', self.company_id.id),
            ]).filtered(lambda t: abs(t.amount - rate) < 0.0001 and bool(t.price_include) == included
                        and t not in taxes)
            if not candidates:
                raise UserError(self.env._(
                    "No Odoo sales tax of %(rate)s%% %(kind)s for the Shopify tax '%(title)s'. Create it in "
                    "Accounting > Configuration > Taxes; the order is imported at the next synchronization.",
                    rate=f"{rate:g}", title=tax_line.get('title') or '',
                    kind=self.env._("included in price") if included else self.env._("excluded from price")))
            taxes |= candidates[0]
        return taxes

    def _update_odoo_order(self, order, node):
        vals = self._order_status_vals(node)
        if node['cancelledAt'] and order.state != 'cancel' and self.cancel_orders:
            invoices = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
            if order.picking_ids.filtered(lambda p: p.state == 'done'):
                self._log('warning', 'order_in',
                          f"Order {node['name']} cancelled in Shopify, but {order.name} is already delivered: "
                          f"not cancelled in Odoo.", reference=node['id'])
            elif invoices.filtered(lambda m: m.payment_state != 'not_paid'):
                self._log('warning', 'order_in',
                          f"Order {node['name']} cancelled in Shopify, but {order.name} is invoiced and paid: "
                          f"not cancelled in Odoo; Shopify refunds become credit notes.", reference=node['id'])
            else:
                # Unpaid invoices of a cancelled order are cancelled with it
                invoices.button_draft()
                invoices.button_cancel()
                order.with_context(disable_cancel_warning=True, ms_from_shopify=True)._action_cancel()
                # Shopify gives the quantities back to 'available' (or writes them off if not restocked)
                self._shift_expected_stock(order, +1)
                self._log('info', 'order_in', f"Order {node['name']} cancelled in Shopify: {order.name} cancelled.",
                          reference=node['id'])
        else:
            if order.state == 'sale' and not node['cancelledAt']:
                self._apply_order_edits(order, node)
            if (self.fulfillment_mode == 'shopify' and node['displayFulfillmentStatus'] == 'FULFILLED'
                    and order.state == 'sale'):
                self._validate_order_deliveries(order, node)
        if self.import_returns and node.get('returnStatus') in ('RETURNED', 'IN_PROGRESS', 'INSPECTION_COMPLETE'):
            try:
                with self.env.cr.savepoint():
                    self._process_returns(order)
            except Exception as e:
                _logger.exception("Shopify returns of %s failed", order.name)
                message = str(e)
                if 'access' in message.lower() or 'scope' in message.lower():
                    message = f"{message} — Add the access scopes read_returns and write_returns to the Shopify app."
                self._log('error', 'order_in', f"Returns of {node['name']}: {message}", reference=node['id'])
        order.write(vals)

    def _shift_expected_stock(self, order, sign):
        """The order changed Shopify 'available' at its location: expect it in the stock step."""
        changes = {}
        for line in order.order_line.filtered(lambda l: l.ms_line_gid and l.product_id.is_storable):
            changes[line.product_id] = changes.get(line.product_id, 0) + sign * int(line.product_uom_qty)
        self._shift_expected_quantities(order, changes)

    def _shift_expected_quantities(self, order, changes):
        """`changes`: {product: change of Shopify 'available'} at the order's location."""
        changes = {p: q for p, q in changes.items() if q and p.is_storable}
        if not changes:
            return
        location = order.ms_location_id or self.location_id
        if location != self.location_id and not (location.sync_stock and location.warehouse_id):
            return  # location not synchronized: nothing expected
        products = self.env['product.product'].union(*changes)
        links = self.env['mirak.shopify.product.map'].search([
            ('store_id', '=', self.id), ('product_id', 'in', products.ids)])
        states = self._location_states(location, links).filtered('qty_initialized') if links else links
        for product, qty in changes.items():
            state = states.filtered(lambda m: m.product_id == product)[:1]
            if state:
                state.last_qty += qty

    def _apply_order_edits(self, order, node):
        """Order edited in Shopify (items added, removed or quantities changed): same changes in Odoo,
        as long as nothing was delivered or invoiced."""
        lines = {l.ms_line_gid: l for l in order.order_line if l.ms_line_gid}
        edits = []
        for item in node['lineItems']['nodes']:
            qty = item.get('currentQuantity') or 0
            line = lines.get(item['id'])
            if (line and line.product_uom_qty != qty) or (not line and qty > 0):
                edits.append((item, line, qty))
        if not edits:
            return
        delivered = order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state == 'done')
        invoiced = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
        if delivered or invoiced:
            refunded = float((((node.get('totalRefundedSet') or {}).get('shopMoney')) or {}).get('amount') or 0)
            if refunded > 0:
                return  # items removed by a refund: handled by the credit notes
            self._log('warning', 'order_in', f"Order {node['name']} was edited in Shopify, but {order.name} is already "
                                             f"{'delivered' if delivered else 'invoiced'}: change it in Odoo by hand.",
                      reference=node['id'])
            return
        included = bool(node.get('taxesIncluded'))
        changes, summary = {}, []
        for item, line, qty in edits:
            if line:
                changes[line.product_id] = changes.get(line.product_id, 0) - (qty - int(line.product_uom_qty))
                summary.append(f"{line.product_id.display_name}: {int(line.product_uom_qty)} → {qty}")
                line.product_uom_qty = qty
                continue
            product = self._order_line_product(item)
            vals = {
                'order_id': order.id,
                'product_id': product.id,
                'name': item['name'] + (f" [{item['sku']}]" if item.get('sku') else ''),
                'product_uom_qty': qty,
                'price_unit': float(item['discountedUnitPriceAfterAllDiscountsSet']['shopMoney']['amount']),
                'ms_line_gid': item['id'],
            }
            new_line = self.env['sale.order.line'].create(vals)
            new_line.price_unit = vals['price_unit']
            if self.order_tax_mode == 'none':
                new_line.tax_ids = [(5, 0, 0)]
            elif self.order_tax_mode == 'shopify':
                new_line.tax_ids = [(6, 0, self._shopify_taxes(item.get('taxLines') or [], included).ids)]
            changes[product] = changes.get(product, 0) - qty
            summary.append(f"{product.display_name}: added {qty}")
        # Shopify adjusted 'available' for the edit: expect it in the stock step
        self._shift_expected_quantities(order, changes)
        self._log('info', 'order_in', f"Order {node['name']} edited in Shopify: " + "; ".join(summary),
                  reference=node['id'])

    def _orders_query(self):
        """Order query; asks for the fulfilling location when several locations are synchronized and
        for the return status when returns are imported (fields needing extra access scopes)."""
        extra = ''
        if len(self._stock_locations()) > 1:
            extra += "      fulfillmentOrders(first: 5) { nodes { assignedLocation { location { id } } } }\n"
        if self.import_returns:
            extra += "      returnStatus\n"
        if not extra:
            return ORDERS_QUERY
        return ORDERS_QUERY.replace("      paymentGatewayNames\n", "      paymentGatewayNames\n" + extra, 1)

    def _order_location(self, node):
        """(Shopify location, Odoo warehouse) fulfilling the order."""
        retail = (node.get('retailLocation') or {}).get('id')
        if retail:
            return self._location_for_gid(retail)  # point of sale order: the shop that sold it
        for fulfillment_order in ((node.get('fulfillmentOrders') or {}).get('nodes') or []):
            gid = (((fulfillment_order.get('assignedLocation') or {}).get('location')) or {}).get('id')
            if gid:
                return self._location_for_gid(gid)
        return self.location_id, self.warehouse_id

    def _validate_order_deliveries(self, order, node):
        """Shopify fulfilled the order: validate the open Odoo deliveries."""
        for picking in order.picking_ids.filtered(
                lambda p: p.state not in ('done', 'cancel') and p.picking_type_code == 'outgoing'):
            for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                move.quantity = move.product_uom_qty
                move.picked = True
            picking.with_context(skip_backorder=True, skip_sms=True, cancel_backorder=True).button_validate()
            if picking.state == 'done':
                picking.ms_fulfillment_gid = 'shopify'
                self._log('info', 'order_in', f"Order {node['name']} fulfilled in Shopify: {picking.name} validated.",
                          reference=node['id'])
            else:
                self._log('warning', 'order_in', f"Order {node['name']} fulfilled in Shopify, but {picking.name} "
                                                 f"could not be validated automatically.", reference=node['id'])

    def _order_line_product(self, item):
        variant = item.get('variant')
        if not variant:
            return self._service_product('custom_item_product_id', "Shopify Custom Item")
        Map = self.env['mirak.shopify.product.map']
        link = Map.search([('store_id', '=', self.id), ('shopify_variant_gid', '=', variant['id'])], limit=1)
        if not link and variant.get('product'):
            # Product not synchronized yet: import it now
            node = self._client().query(self._products_query(PRODUCT_BY_ID_QUERY),
                                        {'id': variant['product']['id']}).get('product')
            if node:
                self._import_product(node)
                link = Map.search([('store_id', '=', self.id), ('shopify_variant_gid', '=', variant['id'])], limit=1)
        if link:
            return link.product_id
        if item.get('sku'):
            product = self.env['product.product'].search([('default_code', '=', item['sku'])], limit=1)
            if product:
                return product
        return self._service_product('custom_item_product_id', "Shopify Custom Item")

    def _service_product(self, field_name, name):
        product = self[field_name]
        if not product:
            product = self.env['product.product'].with_context(ms_no_export=True).create({
                'name': name, 'type': 'service', 'ms_sync': False, 'sale_ok': True, 'purchase_ok': False,
                'invoice_policy': 'order', 'taxes_id': [(5, 0, 0)],
            })
            self[field_name] = product
        return product

    def _get_order_partners(self, node):
        """Customer (matched by email, then phone) and delivery address of a Shopify order."""
        Partner = self.env['res.partner']
        customer = node.get('customer') or {}
        billing = node.get('billingAddress') or node.get('shippingAddress') or {}
        shipping = node.get('shippingAddress') or billing
        email = (customer.get('email') or node.get('email') or '').strip().lower()
        phone = (customer.get('phone') or node.get('phone') or billing.get('phone') or '').strip()
        name = (f"{customer.get('firstName') or ''} {customer.get('lastName') or ''}".strip()
                or billing.get('name') or email or phone or self.env._("Shopify customer %s", node['name']))

        partner = self._partner_for_customer(customer.get('id')) or self._find_partner(email, phone)
        if not partner:
            partner = Partner.create(dict(self._address_vals(billing), name=name, email=email or False,
                                          phone=phone or False, company_id=False))
        self._link_customer(partner, customer.get('id'))
        if not shipping or not shipping.get('address1'):
            return partner, partner
        address = self._address_vals(shipping)
        if (partner.street or '') == (address.get('street') or '') and (partner.city or '') == (address.get('city') or ''):
            return partner, partner
        delivery = partner.child_ids.filtered(
            lambda c: c.type == 'delivery' and (c.street or '') == (address.get('street') or '')
            and (c.city or '') == (address.get('city') or ''))[:1]
        if not delivery:
            delivery = Partner.create(dict(address, type='delivery', parent_id=partner.id,
                                           name=shipping.get('name') or name))
        return partner, delivery

    def _address_vals(self, address):
        if not address:
            return {}
        country = self.env['res.country'].search([('code', '=', address.get('countryCodeV2'))], limit=1)
        state = country and address.get('provinceCode') and self.env['res.country.state'].search(
            [('country_id', '=', country.id), ('code', '=', address['provinceCode'])], limit=1)
        return {
            'street': address.get('address1') or False,
            'street2': address.get('address2') or False,
            'city': address.get('city') or False,
            'zip': address.get('zip') or False,
            'country_id': country.id or False,
            'state_id': state.id if state else False,
            'phone': address.get('phone') or False,
        }

    # ------------------------------------------------------------------
    # Invoices and payments
    # ------------------------------------------------------------------

    def _sync_accounting(self):
        return None

    def _apply_invoice_accounts(self, moves, refund=False):
        return None

    def _invoice_due(self, order):
        return None

    def _sync_order_accounting(self, order):
        return True

    def _payment_journal(self, order):
        return None

    def _register_payment(self, moves, amount, order, inbound=True):
        return None

    def _send_invoices(self, invoices, order):
        return None

    def _process_refunds(self, order, invoices):
        return None

    # ------------------------------------------------------------------
    # Fulfillments: Odoo -> Shopify
    # ------------------------------------------------------------------

    def _push_fulfillments(self):
        retry_before = fields.Datetime.now() - timedelta(hours=1)
        pickings = self.env['stock.picking'].search([
            ('state', '=', 'done'),
            ('picking_type_code', '=', 'outgoing'),
            ('ms_fulfillment_gid', '=', False),
            ('sale_id.ms_store_id', '=', self.id),
            '|', ('ms_fulfillment_try', '=', False), ('ms_fulfillment_try', '<', retry_before),
        ])
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    self._fulfill_picking(picking)
            except Exception as e:
                message = str(e)
                if 'access' in message.lower() or 'scope' in message.lower():
                    message = f"{message} — {FULFILLMENT_SCOPE_HINT}"
                picking.write({'ms_fulfillment_error': message[:250], 'ms_fulfillment_try': fields.Datetime.now()})
                self._log('error', 'fulfillment_out', f"{picking.name} ({picking.sale_id.ms_order_name}): {message}",
                          reference=picking.sale_id.ms_order_gid)

    def _fulfill_picking(self, picking):
        order = picking.sale_id
        remaining = {}
        for move in picking.move_ids.filtered(lambda m: m.state == 'done' and m.sale_line_id.ms_line_gid):
            gid = move.sale_line_id.ms_line_gid
            remaining[gid] = remaining.get(gid, 0) + int(move.quantity)
        if not remaining:
            picking.ms_fulfillment_gid = 'none'
            return

        client = self._client()
        data = client.query(FULFILLMENT_ORDERS_QUERY, {'id': order.ms_order_gid}).get('order') or {}
        by_fulfillment_order = []
        for fulfillment_order in (data.get('fulfillmentOrders') or {}).get('nodes', []):
            if fulfillment_order['status'] not in ('OPEN', 'IN_PROGRESS'):
                continue
            items = []
            for line in fulfillment_order['lineItems']['nodes']:
                gid = line['lineItem']['id']
                quantity = min(remaining.get(gid, 0), line['remainingQuantity'])
                if quantity > 0:
                    items.append({'id': line['id'], 'quantity': quantity})
                    remaining[gid] -= quantity
            if items:
                by_fulfillment_order.append({'fulfillmentOrderId': fulfillment_order['id'],
                                             'fulfillmentOrderLineItems': items})
        if not by_fulfillment_order:
            picking.write({'ms_fulfillment_gid': 'already', 'ms_fulfillment_error': False})
            self._log('info', 'fulfillment_out', f"{picking.name}: {order.ms_order_name} is already fulfilled in Shopify.",
                      reference=order.ms_order_gid)
            return

        fulfillment = {
            'lineItemsByFulfillmentOrder': by_fulfillment_order,
            'notifyCustomer': self.fulfillment_notify_customer,
        }
        tracking = picking['carrier_tracking_ref'] if 'carrier_tracking_ref' in picking._fields else False
        if tracking:
            fulfillment['trackingInfo'] = {'number': tracking}
            if 'carrier_id' in picking._fields and picking.carrier_id:
                fulfillment['trackingInfo']['company'] = picking.carrier_id.name
        result = client.mutate(FULFILLMENT_CREATE_MUTATION, {'fulfillment': fulfillment}, 'fulfillmentCreate')
        picking.write({'ms_fulfillment_gid': result['fulfillment']['id'], 'ms_fulfillment_error': False})
        self._log('info', 'fulfillment_out', f"{picking.name}: {order.ms_order_name} marked fulfilled in Shopify.",
                  reference=order.ms_order_gid)

    # ------------------------------------------------------------------
    # Stock (both directions)
    # ------------------------------------------------------------------

    def _sync_inventory(self, products=None):
        """Keep Shopify 'available' at each synchronized location equal to the Odoo free quantity
        of its warehouse(s).

        `last_qty` is the value both sides agreed on at the previous run:
        a Shopify quantity different from it is a change made in Shopify (sale,
        manual adjustment) and is applied to Odoo as a stock adjustment first;
        then any Odoo quantity different from it is pushed to Shopify.
        """
        domain = [('store_id', '=', self.id), ('inventory_item_gid', '!=', False)]
        if products is not None:
            domain.append(('product_id', 'in', products.ids))
        maps = self.env['mirak.shopify.product.map'].search(domain).filtered(
            lambda m: m.product_id.is_storable and m.product_id.active)
        if not maps:
            return
        for location, warehouse, warehouses in self._stock_locations():
            self._sync_location_stock(location, warehouse, warehouses, self._location_states(location, maps))

    def _sync_location_stock(self, location, warehouse, warehouses, states):
        levels = self._fetch_levels(states, location)
        main = location == self.location_id
        maps = states  # links (main location) or stock levels: same fields

        inbound = self.stock_shopify_to_odoo
        outbound = self.stock_odoo_to_shopify

        # 1. Shopify -> Odoo
        adjusted = 0
        in_cron = self.env.context.get('ms_cron')
        for position, link in enumerate(maps, start=1):
            if in_cron and position % 100 == 0:
                self.env.cr.commit()  # large catalogues: keep the initial alignment progress
            tracked, shopify_qty = levels.get(link.inventory_item_gid, (False, None))
            if link.shopify_tracked != tracked:
                link.shopify_tracked = tracked
            if not tracked or shopify_qty is None or not inbound:
                continue
            try:
                with self.env.cr.savepoint():
                    if not link.qty_initialized:
                        take_shopify = not outbound or (
                            link.origin == 'shopify' and self.initial_stock_source == 'shopify')
                        if take_shopify:
                            # Repeat: adding stock can reserve it for waiting deliveries,
                            # which lowers the free quantity again.
                            for _attempt in range(3):
                                link.product_id.invalidate_recordset()
                                diff = shopify_qty - self._odoo_qty(link.product_id, warehouses)[link.product_id.id]
                                if not diff:
                                    break
                                self._adjust_odoo_stock(link.product_id, diff,
                                                        f"Shopify initial stock ({self.name})", warehouse)
                            link.write({'qty_initialized': True, 'last_qty': shopify_qty,
                                        'last_stock_sync': fields.Datetime.now()})
                            adjusted += 1
                    elif shopify_qty != link.last_qty:
                        delta = shopify_qty - link.last_qty
                        self._adjust_odoo_stock(link.product_id, delta, f"Shopify stock change ({self.name})",
                                                warehouse)
                        where = '' if main else f" at {location.name}"
                        self._log('info', 'stock_in',
                                  f"{link.product_id.display_name}: {delta:+d} from Shopify{where} "
                                  f"({link.last_qty} → {shopify_qty})",
                                  template=link.product_tmpl_id, reference=link.inventory_item_gid)
                        link.write({'last_qty': shopify_qty, 'last_stock_sync': fields.Datetime.now()})
                        adjusted += 1
            except Exception as e:
                _logger.exception("Shopify stock import failed for %s", link.product_id.display_name)
                self._log('error', 'stock_in', f"{link.product_id.display_name}: {e}",
                          template=link.product_tmpl_id, reference=link.inventory_item_gid)

        # 2. Odoo -> Shopify (when Shopify -> Odoo is off, Shopify changes are overwritten here)
        pushed = 0
        if outbound:
            maps.product_id.invalidate_recordset()
            odoo_qty = self._shopify_qty(maps.product_id, warehouses)
            to_set = []
            for link in maps:
                tracked, shopify_qty = levels.get(link.inventory_item_gid, (False, None))
                if not tracked:
                    continue
                qty = odoo_qty[link.product_id.id]
                if shopify_qty is None:
                    # Not stocked at this location: at the main location only products created from
                    # Odoo are stocked there (Shopify products managed elsewhere are left untouched);
                    # an additional location is stocked as soon as its warehouse has the product.
                    if link.origin == 'odoo' or (not main and qty > 0):
                        self._activate_level(link, qty, location)
                elif not link.qty_initialized or qty != shopify_qty:
                    if (not inbound and link.qty_initialized and shopify_qty < link.last_qty
                            and qty > shopify_qty and self._wait_for_order(link)):
                        continue
                    to_set.append((link, qty, shopify_qty))
                elif link.last_qty != shopify_qty:
                    link.last_qty = shopify_qty
                if link.drop_since and shopify_qty is not None and shopify_qty >= link.last_qty:
                    link.drop_since = False
            pushed = self._push_quantities(to_set, location)
        if adjusted or pushed:
            where = '' if main else f" ({location.name})"
            self._log('info', 'stock_out',
                      f"Stock sync{where}: {adjusted} change(s) from Shopify, {pushed} sent to Shopify")

    def _wait_for_order(self, link):
        """Shopify went down unexpectedly while Odoo is the stock reference: most likely an order
        placed after the order import. Don't raise Shopify back for a while (no overselling);
        after 15 minutes without explanation, Odoo's quantity is applied again."""
        if not self.import_orders:
            return False
        now = fields.Datetime.now()
        if not link.drop_since:
            link.drop_since = now
            return True
        return link.drop_since > now - timedelta(minutes=15)

    def _fetch_levels(self, maps, location=None):
        """Return {inventory_item_gid: (tracked, available or None if not stocked at the location)}."""
        client = self._client()
        location = location or self.location_id
        gids = list(set(maps.mapped('inventory_item_gid')))
        levels = {}
        for start in range(0, len(gids), 50):
            data = client.query(LEVELS_QUERY, {'ids': gids[start:start + 50], 'locationId': location.gid})
            for node in data['nodes']:
                if not node:
                    continue
                level = node.get('inventoryLevel')
                qty = None
                if level:
                    qty = next((q['quantity'] for q in level['quantities'] if q['name'] == 'available'), None)
                levels[node['id']] = (node['tracked'], qty)
        return levels

    def _adjust_odoo_stock(self, product, delta, reason, warehouse=None):
        if not delta:
            return
        if product.tracking != 'none':
            raise UserError(self.env._("Products tracked by lot/serial number are not supported."))
        location = (warehouse or self.warehouse_id).lot_stock_id
        Quant = self.env['stock.quant'].sudo().with_context(inventory_mode=True, inventory_name=reason)
        quant = Quant.search([
            ('product_id', '=', product.id),
            ('location_id', '=', location.id),
            ('lot_id', '=', False),
            ('package_id', '=', False),
            ('owner_id', '=', False),
        ], limit=1)
        if quant:
            quant.inventory_quantity = quant.quantity + delta
        else:
            quant = Quant.create({
                'product_id': product.id,
                'location_id': location.id,
                'inventory_quantity': delta,
            })
        quant._apply_inventory()

    def _activate_level(self, link, qty, location=None):
        location = location or self.location_id
        try:
            self._client().mutate(
                ACTIVATE_MUTATION,
                {'itemId': link.inventory_item_gid, 'locationId': location.gid, 'available': qty,
                 'idempotencyKey': str(uuid.uuid4())},
                'inventoryActivate')
            link.write({'qty_initialized': True, 'last_qty': qty, 'last_stock_sync': fields.Datetime.now()})
        except ShopifyError as e:
            self._log('error', 'stock_out', f"{link.product_id.display_name}: cannot stock it at "
                                            f"{location.name}: {e}",
                      template=link.product_tmpl_id, reference=link.inventory_item_gid)

    def _push_quantities(self, to_set, location=None):
        """Send (link, quantity, current Shopify quantity) tuples. Returns the number sent."""
        if not to_set:
            return 0
        client = self._client()
        location = location or self.location_id

        def payload(items):
            return {'idempotencyKey': str(uuid.uuid4()), 'input': {
                'name': 'available',
                'reason': 'correction',
                'quantities': [{
                    'inventoryItemId': link.inventory_item_gid,
                    'locationId': location.gid,
                    'quantity': qty,
                    'changeFromQuantity': current,
                } for link, qty, current in items],
            }}

        def done(items):
            now = fields.Datetime.now()
            for link, qty, _current in items:
                link.write({'qty_initialized': True, 'last_qty': qty, 'last_stock_sync': now})

        sent = 0
        for start in range(0, len(to_set), 100):
            batch = to_set[start:start + 100]
            try:
                client.mutate(SET_QUANTITIES_MUTATION, payload(batch), 'inventorySetQuantities')
                done(batch)
                sent += len(batch)
                continue
            except ShopifyError as e:
                _logger.info("Shopify batch stock update failed (%s), retrying one by one", e)
            for item in batch:
                link = item[0]
                try:
                    client.mutate(SET_QUANTITIES_MUTATION, payload([item]), 'inventorySetQuantities')
                    done([item])
                    sent += 1
                except ShopifyError as e:
                    # Typically the quantity changed in Shopify meanwhile: next run will reconcile it.
                    self._log('warning', 'stock_out', f"{link.product_id.display_name}: {e}",
                              template=link.product_tmpl_id, reference=link.inventory_item_gid)
        return sent
