# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Full-version feature: not included in the Lite edition (fields kept for upgrades)."""
import logging
from datetime import timedelta

from odoo import fields, models

from .shopify_client import ShopifyError

_logger = logging.getLogger(__name__)


class MirakShopifyReturn(models.Model):
    _name = 'mirak.shopify.return'
    _description = 'Shopify Return'
    _order = 'id desc'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    order_id = fields.Many2one('sale.order', ondelete='cascade', index=True)
    gid = fields.Char(string='Shopify Return ID', required=True)
    name = fields.Char()
    origin = fields.Selection([('shopify', 'Shopify'), ('odoo', 'Odoo')], required=True, default='shopify')
    picking_id = fields.Many2one('stock.picking', string='Return Receipt')
    restock_done = fields.Boolean(help="Shopify's restock of this return was taken into account")

    _store_gid_uniq = models.Constraint('unique(store_id, gid)', 'This return is already linked.')


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    ms_return_pending = fields.Boolean(string='Return to Send to Shopify', copy=False, readonly=True)
    ms_return_try = fields.Datetime(copy=False, readonly=True)

    def _action_done(self):
        return super()._action_done()


class MirakShopifyStoreReturns(models.Model):
    _inherit = 'mirak.shopify.store'

    import_returns = fields.Boolean(
        string='Shopify Returns → Odoo', default=False,
        help="When a return is closed in Shopify, the quantities restocked by Shopify come back into the "
             "Odoo warehouse of the restock location (validated return receipt).")
    returns_to_shopify = fields.Boolean(
        string='Odoo Returns → Shopify', default=False,
        help="A return receipt validated in Odoo for a Shopify order opens a return in Shopify.")

    # ------------------------------------------------------------------
    # Shopify -> Odoo
    # ------------------------------------------------------------------

    def _process_returns(self, order):
        return None

    def _expect_restock(self, order, restocked):
        return None

    def _create_odoo_return(self, order, node, restocked):
        return None

    # ------------------------------------------------------------------
    # Odoo -> Shopify
    # ------------------------------------------------------------------

    def _push_returns(self):
        return None

    def _create_shopify_return(self, order, picking):
        return None
