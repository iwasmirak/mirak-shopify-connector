# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Full-version feature: not included in the Lite edition (fields kept for upgrades)."""
import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)


class MirakShopifyCheckout(models.Model):
    _name = 'mirak.shopify.checkout'
    _description = 'Shopify Abandoned Checkout'
    _order = 'id desc'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    gid = fields.Char(string='Shopify Checkout ID', required=True)
    name = fields.Char()
    partner_id = fields.Many2one('res.partner', ondelete='set null')
    amount = fields.Float()
    url = fields.Char(string='Recovery Link')
    lead_res_id = fields.Integer(string='Opportunity ID')
    completed = fields.Boolean()

    _store_gid_uniq = models.Constraint('unique(store_id, gid)', 'This checkout is already imported.')


class MirakShopifyStoreCheckout(models.Model):
    _inherit = 'mirak.shopify.store'

    import_abandoned_checkouts = fields.Boolean(
        string='Abandoned Checkouts → CRM', default=False,
        help="Each abandoned Shopify checkout of a known customer becomes a CRM opportunity with the recovery "
             "link; it is marked won if the customer completes the order. Needs the CRM app.")
    checkout_team_id = fields.Many2one('crm.team', string='Sales Team of the Opportunities', check_company=True)
    last_checkout_pull = fields.Datetime(readonly=True, copy=False)

    def _pull_abandoned_checkouts(self):
        return None

    def _import_checkout(self, node):
        return None
