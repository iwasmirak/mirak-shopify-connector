# Copyright 2026 Bouchibane Karim
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
"""Full-version feature: not included in the Lite edition (fields kept for upgrades)."""
import logging

from odoo import api, fields, models

from .mirak_edition import full_version_error
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class MirakShopifyPayout(models.Model):
    _name = 'mirak.shopify.payout'
    _description = 'Shopify Payments Payout'
    _order = 'issued_at desc, id desc'

    store_id = fields.Many2one('mirak.shopify.store', required=True, ondelete='cascade', index=True)
    gid = fields.Char(string='Shopify Payout ID', required=True)
    name = fields.Char(compute='_compute_name')
    issued_at = fields.Datetime(string='Date')
    currency_id = fields.Many2one('res.currency')
    amount_net = fields.Monetary(string='Paid to Bank', currency_field='currency_id')
    amount_gross = fields.Monetary(string='Gross', currency_field='currency_id')
    amount_fees = fields.Monetary(string='Fees', currency_field='currency_id')
    transaction_count = fields.Integer(string='Transactions')
    move_id = fields.Many2one('account.move', string='Journal Entry', readonly=True)
    reconciled_count = fields.Integer(string='Payments Reconciled')

    _store_gid_uniq = models.Constraint('unique(store_id, gid)', 'This payout is already imported.')

    @api.depends('gid', 'issued_at')
    def _compute_name(self):
        for payout in self:
            payout.name = f"Payout {payout.gid.rsplit('/', 1)[-1]}" if payout.gid else ''


class MirakShopifyStoreFinance(models.Model):
    _inherit = 'mirak.shopify.store'

    import_payouts = fields.Boolean(
        string='Import Shopify Payments Payouts', default=False,
        help="Each paid payout becomes a journal entry: bank receives the net amount, fees are booked, and "
             "the card payments of its orders are reconciled.")
    payout_journal_id = fields.Many2one(
        'account.journal', string='Payout Bank Journal', check_company=True, domain="[('type', '=', 'bank')]")
    payout_fee_account_id = fields.Many2one(
        'account.account', string='Shopify Fees Account', check_company=True,
        domain="[('account_type', 'in', ('expense', 'expense_direct_cost'))]")
    payout_clearing_account_id = fields.Many2one(
        'account.account', string='Card Payments Account', check_company=True,
        help="Account holding the Shopify Payments card payments until they are paid out. Empty: the "
             "Outstanding Receipts account of the card payment journal.")
    payout_import_from = fields.Date(string='Payouts From', default=fields.Date.context_today,
                                     help="Older payouts are not imported.")
    payout_ids = fields.One2many('mirak.shopify.payout', 'store_id', string='Payouts')

    @api.constrains('import_payouts', 'payout_journal_id', 'payout_fee_account_id')
    def _check_payout_settings(self):
        return None

    def _payout_clearing_account(self):
        return None

    def _payout_bank_account(self):
        return None

    def _pull_payouts(self):
        return None

    def _payout_transactions(self, client, node):
        return None

    def _import_payout(self, client, node, issued, clearing):
        return None

    def action_view_payouts(self):
        raise full_version_error(self.env)
