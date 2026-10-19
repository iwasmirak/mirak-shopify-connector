# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


class MirakShopifyWebhook(http.Controller):

    @http.route('/mirak_shopify/webhook/<int:store_id>', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False)
    def shopify_webhook(self, store_id, **kwargs):
        headers = request.httprequest.headers
        store = request.env['mirak.shopify.store'].sudo().browse(store_id).exists()
        if not store or not store.webhook_enabled:
            return Response(status=404)
        accepted = store._webhook_receive(
            headers.get('X-Shopify-Topic'),
            request.httprequest.get_data(),
            headers.get('X-Shopify-Hmac-Sha256'),
            headers.get('X-Shopify-Webhook-Id'),
        )
        return Response(status=200 if accepted else 401)

    @http.route('/mirak_shopify/oauth/callback', type='http', auth='user', methods=['GET'])
    def shopify_oauth_callback(self, **params):
        """Shopify sends the merchant back here after approving the app (see action_oauth_connect)."""
        if not request.env.user.has_group('base.group_system'):
            return Response("Only an administrator can connect a Shopify store.", status=403)
        try:
            store_id = int((params.get('state') or '').split('-', 1)[0])
        except ValueError:
            return Response("Invalid connection link.", status=400)
        store = request.env['mirak.shopify.store'].browse(store_id).exists()
        if not store:
            return Response("Unknown store.", status=404)
        error = store._oauth_finish(dict(params))
        if error:
            _logger.warning("Shopify [%s]: install link failed: %s", store.name, error)
            store._log('error', 'connection', f"Install link: {error}")
        return request.redirect(f"/odoo/action-mirak_shopify_connector.mirak_shopify_store_action/{store.id}")
