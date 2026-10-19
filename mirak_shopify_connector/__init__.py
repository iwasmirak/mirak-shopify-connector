# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
from . import controllers
from . import models


def post_init_hook(env):
    # Existing products are not pushed to Shopify automatically: only products
    # linked by the first Shopify import, products created after installation,
    # or products ticked manually are synchronized.
    env.cr.execute("UPDATE product_template SET ms_sync = FALSE, ms_pending_export = FALSE")
