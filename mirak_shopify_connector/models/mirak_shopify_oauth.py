# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Connection by install link (OAuth authorization code grant): the merchant approves the app in
Shopify and comes back to Odoo with a permanent access token. Needs a public HTTPS address for Odoo."""
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode

import requests

from odoo import fields, models
from odoo.exceptions import UserError

SHOP_RE = re.compile(r'^[a-z0-9][a-z0-9-]*\.myshopify\.com$')

BASE_SCOPES = ['write_products', 'write_inventory', 'read_locations', 'write_files', 'write_orders',
               'read_customers', 'write_fulfillments', 'write_merchant_managed_fulfillment_orders']


class MirakShopifyStoreOAuth(models.Model):
    _inherit = 'mirak.shopify.store'

    auth_method = fields.Selection(
        selection_add=[('oauth', 'Install link (Connect with Shopify)')],
        ondelete={'oauth': 'set default'})
    oauth_state = fields.Char(groups='base.group_system', copy=False)

    def _required_scopes(self):
        """Access scopes needed by the options switched on in this store."""
        scopes = list(BASE_SCOPES)
        if self.import_customers or self.env['mirak.shopify.customer'].search_count([('store_id', '=', self.id)]):
            scopes.append('write_customers')
        if self.import_returns or self.returns_to_shopify:
            scopes += ['read_returns', 'write_returns']
        if self.import_payouts:
            scopes.append('read_shopify_payments_payouts')
        if self.sync_translations:
            scopes += ['read_locales', 'write_translations']
        return scopes

    def _oauth_redirect_uri(self):
        return self._webhook_uri().rsplit('/mirak_shopify/', 1)[0] + '/mirak_shopify/oauth/callback'

    def action_oauth_connect(self):
        """Open Shopify's approval page; Shopify sends the merchant back to the callback."""
        self.ensure_one()
        shop = self._shop_host().lower()
        if not SHOP_RE.match(shop):
            raise UserError(self.env._("Enter the store's address ending with .myshopify.com."))
        if not (self.client_id and self.sudo().client_secret):
            raise UserError(self.env._("Enter the app's Client ID and Client Secret first."))
        state = f"{self.id}-{secrets.token_urlsafe(24)}"
        self.sudo().oauth_state = state
        query = urlencode({
            'client_id': self.client_id.strip(),
            'scope': ','.join(self._required_scopes()),
            'redirect_uri': self._oauth_redirect_uri(),
            'state': state,
        })
        return {'type': 'ir.actions.act_url', 'url': f"https://{shop}/admin/oauth/authorize?{query}", 'target': 'self'}

    @staticmethod
    def _oauth_hmac_valid(params, secret):
        """Shopify signs the callback parameters (all but `hmac`, sorted) with the app secret."""
        signature = params.get('hmac') or ''
        message = '&'.join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ('hmac', 'signature'))
        digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return bool(signature) and hmac.compare_digest(digest, signature)

    def _oauth_finish(self, params):
        """Validate Shopify's callback and exchange the code for an access token. Returns an error or None."""
        self.ensure_one()
        store = self.sudo()
        if not store.oauth_state or params.get('state') != store.oauth_state:
            return self.env._("This connection link expired: click 'Connect with Shopify' again.")
        if not self._oauth_hmac_valid(params, (store.client_secret or '').strip()):
            return self.env._("The answer did not come from Shopify (invalid signature).")
        shop = (params.get('shop') or '').lower()
        if not SHOP_RE.match(shop):
            return self.env._("Invalid store address returned by Shopify.")
        try:
            response = requests.post(f"https://{shop}/admin/oauth/access_token", json={
                'client_id': store.client_id.strip(), 'client_secret': store.client_secret.strip(),
                'code': params.get('code')}, timeout=30)
        except requests.RequestException as e:
            return self.env._("Cannot reach Shopify: %s", e)
        if response.status_code != 200:
            return self.env._("Shopify refused the connection (HTTP %(code)s): %(text)s",
                              code=response.status_code, text=response.text[:200])
        data = response.json()
        store.write({'access_token': data['access_token'], 'token_expires_at': False, 'oauth_state': False})
        granted = set((data.get('scope') or '').split(','))
        missing = [s for s in self._required_scopes() if s not in granted
                   and s.replace('read_', 'write_', 1) not in granted]
        self._log('info', 'connection', f"Connected with the install link ({shop})."
                  + (f" Scopes not granted: {', '.join(missing)}" if missing else ''))
        try:
            self.action_test_connection()
        except UserError as e:
            return str(e)
        return None
