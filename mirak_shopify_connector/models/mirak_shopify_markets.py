# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Full-version feature: not included in the Lite edition (fields kept for upgrades)."""
import logging

from odoo import api, fields, models

from .mirak_edition import full_version_error
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


METAFIELD_TYPES = [
    ('single_line_text_field', 'Text (single line)'),
    ('multi_line_text_field', 'Text (multi-line)'),
    ('number_integer', 'Integer'),
    ('number_decimal', 'Decimal'),
    ('boolean', 'True / False'),
]
FIELD_TYPES = ('char', 'text', 'html', 'integer', 'float', 'monetary', 'boolean', 'selection')


class MirakShopifyPriceList(models.Model):
    """A Shopify Markets price list and the Odoo pricelist giving its prices."""
    _name = 'mirak.shopify.price.list'
    _description = 'Shopify Market Price List'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    gid = fields.Char(string='Shopify Price List ID', required=True)
    name = fields.Char(readonly=True)
    catalog = fields.Char(readonly=True)
    currency = fields.Char(readonly=True)
    pricelist_id = fields.Many2one(
        'product.pricelist', string='Odoo Pricelist',
        help="Prices of this pricelist (converted to the price list currency) become the fixed prices of the "
             "Shopify price list. Empty: Shopify keeps its own rules for this market.")

    _store_gid_uniq = models.Constraint('unique(store_id, gid)', 'This price list is already listed.')


class MirakShopifyMetafield(models.Model):
    """An Odoo product field kept in a Shopify product metafield."""
    _name = 'mirak.shopify.metafield'
    _description = 'Shopify Metafield Mapping'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    field_id = fields.Many2one(
        'ir.model.fields', string='Odoo Field', required=True, ondelete='cascade',
        domain=[('model', '=', 'product.template'), ('ttype', 'in', FIELD_TYPES), ('store', '=', True)])
    namespace = fields.Char(required=True, default='custom')
    key = fields.Char(required=True)
    metafield_type = fields.Selection(METAFIELD_TYPES, string='Shopify Type', required=True,
                                      default='single_line_text_field')
    direction = fields.Selection([
        ('both', 'Both directions'),
        ('odoo', 'Odoo → Shopify'),
        ('shopify', 'Shopify → Odoo'),
    ], required=True, default='both')

    @api.constrains('field_id')
    def _check_field(self):
        return None

    def _to_shopify(self, template):
        return None

    def _to_odoo(self, value):
        return None


class MirakShopifyStoreMarkets(models.Model):
    _inherit = 'mirak.shopify.store'

    price_list_ids = fields.One2many('mirak.shopify.price.list', 'store_id', string='Market Price Lists')
    metafield_ids = fields.One2many('mirak.shopify.metafield', 'store_id', string='Metafields')
    sync_translations = fields.Boolean(
        string='Translations', default=False,
        help="Product names and descriptions in the other languages, both ways. Shopify keeps the lead: "
             "Odoo only fills translations Shopify does not have yet (or that Shopify marked as outdated), "
             "and the translations made in Shopify are copied into Odoo every day.")

    # ------------------------------------------------------------------
    # Market prices
    # ------------------------------------------------------------------

    def action_fetch_price_lists(self):
        raise full_version_error(self.env)

    def _push_market_prices(self, template, maps):
        return None

    # ------------------------------------------------------------------
    # Metafields
    # ------------------------------------------------------------------

    def _metafields_input(self, template, dirty, new):
        return None

    def _apply_shopify_metafields(self, template, node):
        return None

    def _products_query(self, query):
        return query

    # ------------------------------------------------------------------
    # Translations
    # ------------------------------------------------------------------

    def _shop_languages(self):
        return None

    def _push_translations(self, template, product_gid):
        return None

    def _import_translations(self):
        return None

    def action_import_translations(self):
        raise full_version_error(self.env)

    @api.model
    def _cron_import_translations(self):
        return None

    def _push_product_extras(self, template, maps, dirty, new):
        return None


class ProductTemplateMetafields(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        return super().write(vals)
