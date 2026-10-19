# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""License keys of Mirak Connector for Shopify.

A key is ``MIRAK1.<payload>.<signature>`` where the payload is a JSON document
(shop domain, customer, expiry date) signed with the author's Ed25519 private key.
Only the public key is shipped with the module, so keys cannot be forged, and
each key only works for the Shopify store it was issued for.
"""
import base64
import json
from datetime import date

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .mirak_edition import CONTACT

_PUBLIC_KEY = base64.b64decode('DHn65L2ABPfBDcZFzWNRISxnj3UF1Ixf0O1XR3GdtRk=')
_PREFIX = 'MIRAK1'

# Direct-sale edition: a key per Shopify store. The Odoo Apps edition is built with False
# (bought on apps.odoo.com, it works without key).
LICENSE_KEY_REQUIRED = False


class LicenseError(Exception):
    pass


def normalize_shop(shop):
    return (shop or '').strip().lower().replace('https://', '').replace('http://', '').strip('/')


def _b64decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def decode_license(key):
    """Return the license data if the signature is valid, else raise LicenseError."""
    parts = (key or '').strip().split('.')
    if len(parts) != 3 or parts[0] != _PREFIX:
        raise LicenseError("Invalid license key format.")
    try:
        payload = _b64decode(parts[1])
        Ed25519PublicKey.from_public_bytes(_PUBLIC_KEY).verify(_b64decode(parts[2]), payload)
        return json.loads(payload)
    except (InvalidSignature, ValueError):
        raise LicenseError("Invalid license key.") from None


def check_license(key, shop_domain, today=None):
    """Validate the key for this Shopify store. Returns the license data or raises LicenseError."""
    if not key:
        raise LicenseError("No license key. Please enter the license key you received.")
    data = decode_license(key)
    licensed_shop = normalize_shop(data.get('shop'))
    if licensed_shop != normalize_shop(shop_domain):
        raise LicenseError(f"This license key is for the store {licensed_shop}, not {normalize_shop(shop_domain)}.")
    expires = data.get('expires')
    if expires and date.fromisoformat(expires) < (today or date.today()):
        raise LicenseError(f"This license key expired on {expires}. To renew it, contact {CONTACT}.")
    return data


def days_left(data, today=None):
    """Days until the license expires (None for a license without expiry)."""
    expires = data.get('expires')
    if not expires:
        return None
    return (date.fromisoformat(expires) - (today or date.today())).days
