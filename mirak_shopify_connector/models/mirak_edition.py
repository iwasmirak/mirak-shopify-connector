# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Edition of the module: 'full' (sold with a license key) or 'lite' (free, built by build-lite.sh,
without the full-version features)."""
from odoo.exceptions import UserError

EDITION = 'lite'

CONTACT_EMAIL = 'iwasmirak@gmail.com'
CONTACT_PHONE = '+974 66999180'
CONTACT = f"{CONTACT_EMAIL} or {CONTACT_PHONE} (WhatsApp)"

# Options of the full version and the value they keep in the Lite edition
LITE_LOCKED_VALUES = {
    'import_customers': False,
    'cancel_in_shopify': False,
    'import_returns': False,
    'returns_to_shopify': False,
    'import_abandoned_checkouts': False,
    'import_payouts': False,
    'invoice_trigger': 'none',
    'sync_tags': False,
    'category_sync': 'none',
    'category_collections': False,
    'sync_translations': False,
    'compare_at_source': 'none',
}


def is_lite():
    return EDITION == 'lite'


def full_version_error(env):
    return UserError(env._(
        "This feature is part of the full version of Mirak Connector for Shopify. "
        "Contact %s to get it.", CONTACT))
