# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Full-version feature: not included in the Lite edition (fields kept for upgrades)."""
import base64
import hashlib
import hmac
import json
import logging
from datetime import timedelta

from odoo import api, fields, models

from .mirak_edition import full_version_error
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# Shopify topic -> kind of work for the quick synchronization


class MirakShopifyWebhookEvent(models.Model):
    """A notification received from Shopify, waiting for (or done by) the quick synchronization."""
    _name = 'mirak.shopify.webhook.event'
    _description = 'Shopify Webhook Event'
    _order = 'id desc'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    topic = fields.Char(required=True)
    kind = fields.Selection([('orders', 'Orders'), ('products', 'Products'), ('stock', 'Stock'),
                             ('customers', 'Customers'), ('other', 'Other')], required=True, default='other')
    webhook_id = fields.Char(index=True, help="X-Shopify-Webhook-Id, used to ignore duplicates")
    resource_gid = fields.Char(string='Shopify ID')
    processed = fields.Boolean(index=True)

    @api.autovacuum
    def _gc_processed(self):
        return None


class MirakShopifyStoreWebhook(models.Model):
    _inherit = 'mirak.shopify.store'

    webhook_enabled = fields.Boolean(string='Real-time Updates (Webhooks)', readonly=True, copy=False)
    webhook_base_url = fields.Char(
        string='Public Odoo URL',
        help="HTTPS address of this Odoo as seen from the internet (e.g. https://odoo.example.com). "
             "Shopify sends its notifications to it.")
    webhook_secret = fields.Char(
        string='Webhook Signing Secret', groups='base.group_system', copy=False,
        help="Secret used by Shopify to sign notifications: the app's Client secret (used when empty).")
    webhook_event_count = fields.Integer(compute='_compute_webhook_event_count')

    def _compute_webhook_event_count(self):
        for store in self:
            store.webhook_event_count = 0

    def _webhook_uri(self):
        return None

    def action_register_webhooks(self):
        raise full_version_error(self.env)

    def action_unregister_webhooks(self):
        raise full_version_error(self.env)

    # ------------------------------------------------------------------
    # Reception (called by the controller, as superuser)
    # ------------------------------------------------------------------

    def _webhook_signature_valid(self, body, signature):
        return None

    def _webhook_receive(self, topic, body, signature, webhook_id=None):
        return False

    # ------------------------------------------------------------------
    # Quick synchronization of the received events
    # ------------------------------------------------------------------

    def _run_webhook_sync(self):
        return None
