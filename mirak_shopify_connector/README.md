# Mirak Connector for Shopify (Lite) — Setup Guide

Free edition: products and variants in both directions, images, stock of the main Shopify location,
order import and fulfillments between Odoo 19 (Community or Enterprise) and one Shopify store.

> **Full version** — several stores, several locations and warehouses, real-time updates (webhooks),
> invoices, payments and Shopify Payments payouts, returns, cancellations from Odoo, customers in both
> directions, abandoned checkouts, tags, product types, collections, compare-at and Markets prices,
> metafields and translations.
> Contact **iwasmirak@gmail.com** or **+974 66999180** (WhatsApp). Upgrading keeps all your settings and data.

---

## 1. Install the module

1. Copy the `mirak_shopify_connector` folder into your Odoo addons path.
2. Restart Odoo, then in **Apps** click **Update Apps List**, search for *Mirak Connector* and click **Install**.
   Sales and Inventory are installed automatically if needed.

A new menu **Mirak Connector** appears. It opens on the dashboard.

## 2. Create the Shopify app

1. Go to <https://dev.shopify.com/dashboard> (log in with the account that owns your store) and click **Create app**.
2. Give it a name (for example *Odoo*).
3. Create a **version** of the app with these **Admin API access scopes**:

   | Scope | Used for |
   |---|---|
   | `write_products` (includes `read_products`) | products, variants, prices, images |
   | `write_inventory` (includes `read_inventory`) | stock quantities |
   | `read_locations` | choosing the Shopify location |
   | `write_files` (includes `read_files`) | deleting images removed in Odoo |
   | `write_orders` (includes `read_orders`) | importing orders |
   | `read_customers` | customer name, email and phone of the orders |
   | `write_fulfillments` | marking orders as fulfilled |
   | `write_merchant_managed_fulfillment_orders` | marking orders as fulfilled, order location |

4. **Release** the version, then **Install** the app on your store and approve the permissions.
5. In the app's **Settings**, copy the **Client ID** and the **Client secret**.

> Keep the Client secret private. If it leaks, rotate it in the Dev Dashboard and enter the new one in Odoo.

## 3. Connect the store in Odoo

1. **Mirak Connector ▸ Stores ▸ New**.
2. **Shopify domain**: your `xxxx.myshopify.com` address (Shopify admin ▸ Settings ▸ Domains).
3. **Authentication**: *Client ID and Secret*, paste the two values. The access token is obtained and renewed automatically.
4. Click **Test Connection**. Missing permissions are listed if any.
5. **Stock** tab: choose the **Shopify location** and the **Odoo warehouse** to synchronize.

## 4. Choose the options

| Tab | Options |
|---|---|
| **Products** | directions, synchronized data (name, description, price, SKU/barcode, status, images), which new Odoo products are sent (all, categories, ticked only), status of new products, price from the sales price or a pricelist, conflict policy |
| **Stock** | directions, free or on-hand quantity, safety buffer, percentage published, reference for the first link |
| **Orders** | import from a date, all or paid orders, taxes, sales team, cancellations from Shopify, POS orders, shipping (fulfillment either way) |

## 5. First synchronization

Click **Sync Now**. It runs in the background; follow the progress on the dashboard and in the logs.

* Shopify products are matched to Odoo products by **SKU** (Odoo *Internal Reference*), so an existing
  catalogue is not duplicated. Products without a match are created.
* Large catalogues are imported in several passes; images are caught up progressively.

## 6. Troubleshooting

| Symptom | What to do |
|---|---|
| *Connection to Shopify failed* | Check the domain, the Client ID and Secret, and that the app is installed on the store. |
| *Access scopes missing* | Add the scopes in a new app version, release it and approve it in the store. |
| An order is not imported | Look at **Mirak Connector ▸ Logs**: the reason is shown (for example a missing tax). The order is retried at every synchronization. |

## Limits

* One Shopify store and its main location (full version: several stores and locations).
* Up to 3 options and 100 variants per product (Shopify limits). Orders of up to 50 lines.
* Products tracked by lot or serial number are not supported for stock.

---

Author: Bouchibane Karim · iwasmirak@gmail.com · License OPL-1.
Shopify is a trademark of Shopify Inc. This module is not affiliated with or endorsed by Shopify Inc.
