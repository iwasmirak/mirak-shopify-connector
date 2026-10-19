# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
import logging
import time

import requests

_logger = logging.getLogger(__name__)


class ShopifyError(Exception):
    """Error returned by the Shopify Admin API (transport, GraphQL or userErrors)."""

    def __init__(self, message, codes=None):
        super().__init__(message)
        self.codes = set(codes or [])


class ShopifyClient:
    """Minimal Shopify Admin GraphQL client with throttling and retries."""

    MAX_RETRIES = 5

    def __init__(self, shop_domain, access_token, api_version, timeout=40, token_refresher=None):
        """`token_refresher`: optional callable returning a new access token, used once on HTTP 401."""
        self.url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self.timeout = timeout
        self.token_refresher = token_refresher
        self.session = requests.Session()
        self.session.headers.update({
            'X-Shopify-Access-Token': access_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'Mirak-Odoo-Connector',
        })

    def query(self, query, variables=None):
        payload = {'query': query, 'variables': variables or {}}
        token_refreshed = False
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.session.post(self.url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == self.MAX_RETRIES - 1:
                    raise ShopifyError(f"Network error: {e}") from e
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 401 and self.token_refresher and not token_refreshed:
                # Access token expired or revoked: get a new one and retry once
                self.session.headers['X-Shopify-Access-Token'] = self.token_refresher()
                token_refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                wait = float(response.headers.get('Retry-After') or 2 ** attempt)
                _logger.info("Shopify HTTP %s, retrying in %ss", response.status_code, wait)
                time.sleep(wait)
                continue
            if response.status_code != 200:
                raise ShopifyError(f"HTTP {response.status_code}: {response.text[:500]}")

            data = response.json()
            errors = data.get('errors')
            if errors:
                if any((e.get('extensions') or {}).get('code') == 'THROTTLED' for e in errors):
                    time.sleep(self._throttle_wait(data))
                    continue
                raise ShopifyError('; '.join(e.get('message', str(e)) for e in errors))
            return data.get('data') or {}
        raise ShopifyError("Shopify API is still throttling after several retries")

    def mutate(self, query, variables, key):
        """Run a mutation and raise on userErrors. Returns the payload of `key`."""
        result = self.query(query, variables).get(key) or {}
        # Most mutations use `userErrors`; orderCancel has its own list
        user_errors = result.get('userErrors') or result.get('orderCancelUserErrors') or []
        if user_errors:
            message = '; '.join(
                f"{'.'.join(str(f) for f in (e.get('field') or []))}: {e.get('message')}".lstrip(': ')
                for e in user_errors
            )
            raise ShopifyError(message, codes=[e.get('code') for e in user_errors if e.get('code')])
        return result

    def paginate(self, query, variables, connection):
        """Yield every node of a top-level connection (query must accept $cursor)."""
        cursor = None
        while True:
            data = self.query(query, dict(variables or {}, cursor=cursor))[connection]
            yield from data['nodes']
            if not data['pageInfo']['hasNextPage']:
                return
            cursor = data['pageInfo']['endCursor']

    @staticmethod
    def _throttle_wait(data):
        cost = (data.get('extensions') or {}).get('cost') or {}
        status = cost.get('throttleStatus') or {}
        missing = (cost.get('requestedQueryCost') or 0) - (status.get('currentlyAvailable') or 0)
        rate = status.get('restoreRate') or 50
        return min(max(missing / rate, 1.0), 20.0)
