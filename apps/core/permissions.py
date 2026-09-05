"""
Action permissions and the role matrix.

Django creates add/change/delete/view for every model automatically. That covers
CRUD but not the actions this business actually cares about: approving an order,
posting a document, reversing a posting, closing a period, exporting a report.
ACC-004 requires those to be *separately* grantable, so they are declared here.

Where they live
---------------
All of them hang off one unmanaged model, `core.SystemPermission`, rather than
being scattered across the document models they relate to. Two reasons:

  * Ownership. Members 2, 3 and 4 own sales, purchases, payments and inventory.
    Declaring permissions on their models would mean editing their files and
    generating migrations in their apps — exactly what CONTRIBUTING.md forbids.
  * Auditing. One place to read the whole authorisation surface, instead of
    hunting through ten models to find out what a Cashier can do.

The model is `managed = False`, so it creates no table — only permission rows.

How to use one
--------------
In a view:      PostingPermissionMixin / permission_required(POST_SALES_INVOICE)
In a template:  {% if perms.core.post_sales_invoice %}
In code:        request.user.has_perm(POST_SALES_INVOICE)

BRD coverage: ACC-003, ACC-004, ACC-006, ACC-008, §4.1 access matrix.
"""

# ---------------------------------------------------------------------------
# Permission codenames. Always import these constants — never type the string,
# so a rename is one edit and a typo is an ImportError rather than a silent
# permission check that always returns False.
# ---------------------------------------------------------------------------
APP = "core"


def _p(codename):
    return f"{APP}.{codename}"


# Approval (SAL-004, PUR-002)
APPROVE_SALES_ORDER = _p("approve_sales_order")
APPROVE_PURCHASE_ORDER = _p("approve_purchase_order")
APPROVE_STOCK_ADJUSTMENT = _p("approve_stock_adjustment")
APPROVE_REFUND = _p("approve_refund")
APPROVE_SALES_RETURN = _p("approve_sales_return")
APPROVE_SALES_CREDIT_NOTE = _p("approve_sales_credit_note")
# Posting (GL-001, SAL-009, PUR-008, PAY-001, INV-006, INV-007)
POST_SALES_INVOICE = _p("post_sales_invoice")
POST_PURCHASE_BILL = _p("post_purchase_bill")
POST_DELIVERY = _p("post_delivery")
POST_GOODS_RECEIPT = _p("post_goods_receipt")
POST_PAYMENT = _p("post_payment")
POST_SALES_RETURN = _p("post_sales_return")
POST_CREDIT_NOTE = _p("post_credit_note")
POST_DEBIT_NOTE = _p("post_debit_note")
POST_STOCK_MOVEMENT = _p("post_stock_movement")
POST_JOURNAL = _p("post_journal")

# Money (PAY-003, PAY-005, RET-004)
ALLOCATE_PAYMENT = _p("allocate_payment")
ISSUE_REFUND = _p("issue_refund")

# Correction (BR-004, PAY-010, GL-009)
REVERSE_DOCUMENT = _p("reverse_document")

# Period control (CFG-009, GL-012, ACC-008)
CLOSE_PERIOD = _p("close_period")
REOPEN_PERIOD = _p("reopen_period")

# Configuration and oversight (CFG-001..CFG-010, ACC-005, UX-007)
MANAGE_CONFIGURATION = _p("manage_configuration")
MANAGE_CHART_OF_ACCOUNTS = _p("manage_chart_of_accounts")
VIEW_AUDIT_LOG = _p("view_audit_log")
EXPORT_DATA = _p("export_data")
VIEW_FINANCIAL_REPORTS = _p("view_financial_reports")

# Authorised overrides — each one is a documented exception in the BRD, and each
# needs an audit event with a reason when used (ACC-005, ACC-008).
OVERRIDE_CREDIT_HOLD = _p("override_credit_hold")  # PTY-004
OVERRIDE_NEGATIVE_STOCK = _p("override_negative_stock")  # BR-017, INV-010
OVERRIDE_DUPLICATE_VENDOR_INVOICE = _p("override_duplicate_vendor_invoice")  # PUR-006
OVERRIDE_RETURN_QUANTITY = _p("override_return_quantity")  # BR-015
OVERRIDE_EXCHANGE_RATE = _p("override_exchange_rate")  # FTD-002


#: (codename, human-readable name) — consumed by SystemPermission.Meta.permissions.
ACTION_PERMISSIONS = [
    ("approve_sales_order", "Can approve or reject a sales order"),
    ("approve_purchase_order", "Can approve or reject a purchase order"),
    ("approve_sales_return", "Can approve a customer return"),
    ("approve_sales_credit_note", "Can approve a sales credit note"),
    ("approve_stock_adjustment", "Can approve a stock adjustment"),
    ("approve_refund", "Can approve a refund"),
    ("post_sales_invoice", "Can post a sales invoice"),
    ("post_purchase_bill", "Can post a purchase bill"),
    ("post_delivery", "Can post a delivery note"),
    ("post_sales_return", "Can post a customer return"),
    ("post_goods_receipt", "Can post a goods receipt"),
    ("post_payment", "Can post a receipt or payment"),
    ("post_credit_note", "Can post a sales credit note"),
    ("post_debit_note", "Can post a vendor debit note"),
    ("post_stock_movement", "Can post a stock transfer or adjustment"),
    ("post_journal", "Can post a manual journal entry"),
    ("allocate_payment", "Can allocate a payment or credit"),
    ("issue_refund", "Can issue a refund"),
    ("reverse_document", "Can reverse a posted document"),
    ("close_period", "Can close a fiscal period"),
    ("reopen_period", "Can reopen a closed fiscal period"),
    ("manage_configuration", "Can change company and financial configuration"),
    ("manage_chart_of_accounts", "Can change the chart of accounts and mappings"),
    ("view_audit_log", "Can view the audit history"),
    ("export_data", "Can export reports and lists"),
    ("view_financial_reports", "Can view financial statements"),
    ("override_credit_hold", "Can sell to a customer on credit hold"),
    ("override_negative_stock", "Can post a movement that takes stock negative"),
    ("override_duplicate_vendor_invoice", "Can accept a duplicate vendor invoice number"),
    ("override_return_quantity", "Can return more than the eligible quantity"),
    ("override_exchange_rate", "Can override the exchange rate for a transaction"),
]


# ---------------------------------------------------------------------------
# Role definitions (BRD §4.1 baseline access matrix)
#
# These are a *baseline*, seeded once. An administrator can change any group's
# permissions afterwards through the admin — the matrix is data, not code, which
# is what BRD §4.1's "Control principle" asks for.
# ---------------------------------------------------------------------------
OWNER_ADMIN = "Owner/Admin"
ACCOUNTANT = "Accountant"
SALES = "Sales"
PURCHASING = "Purchasing"
WAREHOUSE = "Warehouse"
CASHIER = "Cashier"
AUDITOR = "Auditor"

#: Every domain app whose models a role might be granted CRUD on.
ALL_APPS = [
    "core",
    "accounts",
    "ledger",
    "parties",
    "catalog",
    "inventory",
    "sales",
    "purchases",
    "payments",
]

ROLE_MATRIX = {
    OWNER_ADMIN: {
        "description": "Full business access. Approves, posts, reverses, closes periods, configures.",
        "all_permissions": True,
        "can_post": True,
        "can_approve": True,
        "can_reverse": True,
        "can_close_period": True,
        "can_configure": True,
    },
    ACCOUNTANT: {
        "description": "Owns the ledger: posts, allocates, journals, reconciles, closes periods.",
        "crud_apps": ["ledger", "core", "payments"],
        "view_apps": ALL_APPS,
        "actions": [
            "post_sales_return",
            "approve_sales_return",
            "post_sales_invoice",
            "post_purchase_bill",
            "post_payment",
            "post_credit_note",
            "approve_sales_credit_note",
            "post_debit_note",
            "post_journal",
            "post_stock_movement",
            "allocate_payment",
            "issue_refund",
            "reverse_document",
            "close_period",
            "reopen_period",
            "manage_configuration",
            "manage_chart_of_accounts",
            "view_audit_log",
            "export_data",
            "view_financial_reports",
            "override_exchange_rate",
            "approve_refund",
        ],
        "can_post": True,
        "can_approve": True,
        "can_reverse": True,
        "can_close_period": True,
        "can_configure": True,
    },
    SALES: {
        "description": "Maintains customers and creates sales documents within permission limits.",
        "crud_apps": ["sales", "parties"],
        "view_apps": ["catalog", "inventory", "core"],
        # No posting by default. BRD 4.1 says "limited post if granted" — an
        # administrator grants post_sales_invoice to individual users.
        "actions": ["export_data"],
    },
    PURCHASING: {
        "description": "Maintains vendors and creates purchase documents.",
        "crud_apps": ["purchases", "parties"],
        "view_apps": ["catalog", "inventory", "core"],
        "actions": ["export_data"],
    },
    WAREHOUSE: {
        "description": "Confirms physical stock movements.",
        "crud_apps": ["inventory"],
        "view_apps": ["catalog", "sales", "purchases", "core"],
        "actions": [
            "post_delivery",
            "post_goods_receipt",
            "post_stock_movement",
            "export_data",
        ],
        "can_post": True,
    },
    CASHIER: {
        "description": "Records and allocates money and prints receipts. No configuration access.",
        "crud_apps": ["payments"],
        "view_apps": ["parties", "sales", "purchases", "core"],
        "actions": ["post_payment", "allocate_payment", "export_data"],
        "can_post": True,
    },
    AUDITOR: {
        "description": "Read-only across all approved records. May export, may not mutate.",
        "view_apps": ALL_APPS,
        "actions": ["view_audit_log", "export_data", "view_financial_reports"],
        "read_only": True,
    },
}
