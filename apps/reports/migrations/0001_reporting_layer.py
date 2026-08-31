"""
Reporting layer: views and set-returning functions.

Ported from the colleague's SQL schema, which had the better answer here — the
Django-first schema had no reporting layer at all. Column names, the single-
entity assumption (no company_id) and this schema's status vocabulary have been
adapted; the query logic and the cutoff discipline are theirs.

The `reports` app holds no tables by design (BRD 11.2): report SQL lives here so
it stays separately testable and never leaks into views or templates (BRD 11.3).

BRD coverage: RPT-003..RPT-007, RPT-013, RPT-015..RPT-018, RPT-021, RPT-022,
GL-004, GL-005, GL-011, PTY-005, INV-011, §9.1 report-integrity rules.
"""

from django.db import migrations

# Documents whose financial effects exist. REVERSED is deliberately excluded
# from open-item views: the reversal's own journal already neutralises it.
POSTED = "('POSTED','PARTIAL','COMPLETED')"

FORWARD = f"""
-- ===========================================================================
-- RPT-004 / GL-004  General ledger detail with source drill-down
-- ===========================================================================
CREATE VIEW v_general_ledger AS
SELECT jl.id               AS journal_line_id,
       je.id               AS journal_entry_id,
       je.number           AS entry_number,
       je.entry_date,
       je.journal_type,
       je.status           AS entry_status,
       je.is_manual,
       je.source_doc_type,
       je.source_doc_number,
       je.fiscal_period_id,
       a.id                AS account_id,
       a.code              AS account_code,
       a.name              AS account_name,
       a.account_type,
       a.subtype           AS account_subtype,
       a.is_contra,
       jl.description,
       jl.debit_base,
       jl.credit_base,
       jl.debit_base - jl.credit_base AS signed_amount_base,
       jl.currency_id      AS currency_code,
       jl.debit_txn,
       jl.credit_txn,
       jl.customer_id,
       jl.vendor_id,
       jl.product_id,
       jl.warehouse_id,
       jl.tax_code_id,
       jl.money_account_id
FROM journal_line jl
JOIN journal_entry je ON je.id = jl.entry_id
JOIN account a        ON a.id = jl.account_id;

COMMENT ON VIEW v_general_ledger IS
  'RPT-004. One row per journal line with its account and source document.';


-- ===========================================================================
-- GL-011 / RPT-021  Control-account balances straight from the ledger
-- ===========================================================================
CREATE VIEW v_control_account_balance AS
SELECT a.control_type,
       a.id   AS account_id,
       a.code AS account_code,
       a.name AS account_name,
       SUM(jl.debit_base - jl.credit_base) AS balance_base
FROM journal_line jl
JOIN journal_entry je ON je.id = jl.entry_id
JOIN account a        ON a.id = jl.account_id
WHERE a.is_control
GROUP BY a.control_type, a.id, a.code, a.name;


-- ===========================================================================
-- RPT-022  Open customer invoices — the subledger side of the AR control
-- ===========================================================================
CREATE VIEW v_sales_invoice_open AS
SELECT si.id AS sales_invoice_id,
       si.number AS document_number,
       si.customer_id,
       c.code AS customer_code,
       c.name AS customer_name,
       si.currency_id AS currency_code,
       si.document_date,
       si.due_date,
       si.total_txn,
       si.allocated_txn,
       si.credited_txn,
       si.open_txn,
       si.open_base,
       si.status,
       GREATEST((CURRENT_DATE - si.due_date), 0) AS days_overdue
FROM sales_invoice si
JOIN customer c ON c.id = si.customer_id
WHERE si.status IN {POSTED} AND si.open_txn > 0;


-- ===========================================================================
-- RPT-022  Open vendor bills — the subledger side of the AP control
-- ===========================================================================
CREATE VIEW v_purchase_bill_open AS
SELECT pb.id AS purchase_bill_id,
       pb.number AS document_number,
       pb.vendor_invoice_number,
       pb.vendor_id,
       v.code AS vendor_code,
       v.name AS vendor_name,
       pb.currency_id AS currency_code,
       pb.document_date,
       pb.due_date,
       pb.total_txn,
       pb.allocated_txn,
       pb.credited_txn,
       pb.open_txn,
       pb.open_base,
       pb.status,
       GREATEST((CURRENT_DATE - pb.due_date), 0) AS days_overdue
FROM purchase_bill pb
JOIN vendor v ON v.id = pb.vendor_id
WHERE pb.status IN {POSTED} AND pb.open_txn > 0;


-- ===========================================================================
-- PTY-005 / PAY-004  Unapplied customer credit: advances plus open credit notes
-- ===========================================================================
CREATE VIEW v_customer_unapplied_credit AS
SELECT p.customer_id,
       p.currency_id AS currency_code,
       'PAYMENT'::varchar(20) AS source_type,
       p.id AS source_id,
       p.number AS document_number,
       p.payment_date AS source_date,
       p.unallocated_txn AS available_txn
FROM payment p
WHERE p.status IN {POSTED}
  AND p.direction = 'RECEIPT'
  AND p.is_reversed = false
  AND p.unallocated_txn > 0
UNION ALL
SELECT cn.customer_id,
       cn.currency_id,
       'SALES_CREDIT_NOTE'::varchar(20),
       cn.id,
       cn.number,
       cn.document_date,
       cn.open_txn
FROM sales_credit_note cn
WHERE cn.status IN {POSTED} AND cn.open_txn > 0;


-- ===========================================================================
-- PTY-005  Unapplied vendor credit: advances paid plus open debit notes
-- ===========================================================================
CREATE VIEW v_vendor_unapplied_credit AS
SELECT p.vendor_id,
       p.currency_id AS currency_code,
       'PAYMENT'::varchar(20) AS source_type,
       p.id AS source_id,
       p.number AS document_number,
       p.payment_date AS source_date,
       p.unallocated_txn AS available_txn
FROM payment p
WHERE p.status IN {POSTED}
  AND p.direction = 'PAYMENT'
  AND p.is_reversed = false
  AND p.unallocated_txn > 0
UNION ALL
SELECT dn.vendor_id,
       dn.currency_id,
       'VENDOR_DEBIT_NOTE'::varchar(20),
       dn.id,
       dn.number,
       dn.document_date,
       dn.open_txn
FROM vendor_debit_note dn
WHERE dn.status IN {POSTED} AND dn.open_txn > 0;


-- ===========================================================================
-- INV-003 / INV-011 / RPT-016 / RPT-018  Stock on hand, valuation, low stock
-- ===========================================================================
CREATE VIEW v_inventory_valuation AS
SELECT sb.product_id,
       p.sku,
       p.name AS product_name,
       pc.name AS category_name,
       sb.warehouse_id,
       w.code AS warehouse_code,
       sb.quantity_on_hand,
       sb.quantity_reserved,
       sb.average_cost,
       sb.total_value,
       p.reorder_level,
       (sb.quantity_on_hand <= p.reorder_level) AS is_below_reorder_level
FROM stock_balance sb
JOIN product p   ON p.id = sb.product_id
JOIN warehouse w ON w.id = sb.warehouse_id
LEFT JOIN product_category pc ON pc.id = p.category_id
WHERE p.is_inventory;


-- ===========================================================================
-- RPT-014 / RPT-015  Tax transaction detail, both sides combined
-- ===========================================================================
CREATE VIEW v_tax_transaction AS
SELECT 'SALES'::varchar(10) AS tax_side,
       si.document_date,
       si.number AS document_number,
       si.customer_id AS party_id,
       c.name AS party_name,
       sil.tax_code_id,
       tc.code AS tax_code,
       tc.treatment AS tax_treatment,
       sil.tax_rate_percent,
       sil.tax_is_inclusive,
       sil.tax_is_recoverable,
       sil.taxable_base_base AS taxable_base,
       sil.tax_base AS tax_amount_base
FROM sales_invoice_line sil
JOIN sales_invoice si ON si.id = sil.invoice_id AND si.status IN {POSTED}
JOIN customer c       ON c.id = si.customer_id
LEFT JOIN tax_code tc ON tc.id = sil.tax_code_id
UNION ALL
SELECT 'PURCHASE'::varchar(10),
       pb.document_date,
       pb.number,
       pb.vendor_id,
       v.name,
       pbl.tax_code_id,
       tc.code,
       tc.treatment,
       pbl.tax_rate_percent,
       pbl.tax_is_inclusive,
       pbl.tax_is_recoverable,
       pbl.taxable_base_base,
       pbl.tax_base
FROM purchase_bill_line pbl
JOIN purchase_bill pb ON pb.id = pbl.bill_id AND pb.status IN {POSTED}
JOIN vendor v         ON v.id = pb.vendor_id
LEFT JOIN tax_code tc ON tc.id = pbl.tax_code_id;


-- ===========================================================================
-- RPT-021 / SC-02  Subledger reconciliation
-- Sign convention: AR and inventory are debit-natured, so the GL balance is
-- positive; AP and customer advances are credit-natured, so the subledger
-- total is negated before comparison.
-- ===========================================================================
CREATE VIEW v_subledger_reconciliation AS
SELECT 'AR'::varchar(18) AS control_type,
       cab.account_code,
       cab.balance_base AS gl_balance_base,
       COALESCE(sub.total, 0) AS subledger_balance_base,
       cab.balance_base - COALESCE(sub.total, 0) AS difference_base
FROM v_control_account_balance cab
LEFT JOIN (SELECT SUM(open_base) AS total FROM sales_invoice
            WHERE status IN {POSTED}) sub ON true
WHERE cab.control_type = 'AR'
UNION ALL
SELECT 'AP'::varchar(18),
       cab.account_code,
       cab.balance_base,
       -COALESCE(sub.total, 0),
       cab.balance_base + COALESCE(sub.total, 0)
FROM v_control_account_balance cab
LEFT JOIN (SELECT SUM(open_base) AS total FROM purchase_bill
            WHERE status IN {POSTED}) sub ON true
WHERE cab.control_type = 'AP'
UNION ALL
SELECT 'INVENTORY'::varchar(18),
       cab.account_code,
       cab.balance_base,
       COALESCE(sub.total, 0),
       cab.balance_base - COALESCE(sub.total, 0)
FROM v_control_account_balance cab
LEFT JOIN (SELECT SUM(total_value) AS total FROM stock_balance) sub ON true
WHERE cab.control_type = 'INVENTORY';


-- ===========================================================================
-- RPT-003 / GL-005  Trial balance: opening, period movement, closing
-- ===========================================================================
CREATE OR REPLACE FUNCTION fn_trial_balance(p_from date, p_to date)
RETURNS TABLE (
    account_id     bigint,
    account_code   varchar,
    account_name   varchar,
    account_type   varchar,
    opening_debit  numeric,
    opening_credit numeric,
    period_debit   numeric,
    period_credit  numeric,
    closing_debit  numeric,
    closing_credit numeric)
LANGUAGE sql STABLE AS $fn$
    WITH agg AS (
        SELECT jl.account_id AS acc_id,
               SUM(CASE WHEN je.entry_date < p_from
                        THEN jl.debit_base - jl.credit_base ELSE 0 END) AS opening_net,
               SUM(CASE WHEN je.entry_date >= p_from THEN jl.debit_base  ELSE 0 END) AS per_debit,
               SUM(CASE WHEN je.entry_date >= p_from THEN jl.credit_base ELSE 0 END) AS per_credit,
               SUM(jl.debit_base - jl.credit_base) AS closing_net
        FROM journal_line jl
        JOIN journal_entry je ON je.id = jl.entry_id
        WHERE je.entry_date <= p_to
        GROUP BY jl.account_id
    )
    SELECT a.id, a.code, a.name, a.account_type,
           GREATEST(agg.opening_net, 0),
           GREATEST(-agg.opening_net, 0),
           agg.per_debit,
           agg.per_credit,
           GREATEST(agg.closing_net, 0),
           GREATEST(-agg.closing_net, 0)
    FROM agg
    JOIN account a ON a.id = agg.acc_id
    ORDER BY a.code;
$fn$;


-- ===========================================================================
-- RPT-006  AR ageing, recomputed at an arbitrary cutoff from the allocations
-- rather than from the cached open_txn column (§9.1 cutoff rule).
-- ===========================================================================
CREATE OR REPLACE FUNCTION fn_ar_ageing(p_as_of date)
RETURNS TABLE (
    customer_id     bigint,
    customer_name   varchar,
    invoice_id      bigint,
    document_number varchar,
    currency_code   varchar,
    document_date   date,
    due_date        date,
    days_overdue    integer,
    total_txn       numeric,
    settled_txn     numeric,
    open_txn        numeric,
    bucket          text)
LANGUAGE sql STABLE AS $fn$
    WITH settled AS (
        SELECT al.sales_invoice_id AS inv_id, SUM(al.target_amount_txn) AS amt
        FROM payment_allocation al
        WHERE al.is_reversed = false
          AND al.sales_invoice_id IS NOT NULL
          AND al.allocation_date <= p_as_of
        GROUP BY al.sales_invoice_id
    )
    SELECT si.customer_id, c.name, si.id, si.number, si.currency_id,
           si.document_date, si.due_date,
           GREATEST((p_as_of - si.due_date), 0)::integer,
           si.total_txn,
           COALESCE(settled.amt, 0),
           si.total_txn - COALESCE(settled.amt, 0),
           CASE
               WHEN p_as_of <= si.due_date      THEN 'CURRENT'
               WHEN p_as_of - si.due_date <= 30 THEN '1-30'
               WHEN p_as_of - si.due_date <= 60 THEN '31-60'
               WHEN p_as_of - si.due_date <= 90 THEN '61-90'
               ELSE '90+'
           END
    FROM sales_invoice si
    JOIN customer c ON c.id = si.customer_id
    LEFT JOIN settled ON settled.inv_id = si.id
    WHERE si.status IN {POSTED}
      AND si.document_date <= p_as_of
      AND si.total_txn - COALESCE(settled.amt, 0) > 0
    ORDER BY c.name, si.due_date;
$fn$;


-- ===========================================================================
-- RPT-007  AP ageing, the same rules on the vendor side
-- ===========================================================================
CREATE OR REPLACE FUNCTION fn_ap_ageing(p_as_of date)
RETURNS TABLE (
    vendor_id       bigint,
    vendor_name     varchar,
    bill_id         bigint,
    document_number varchar,
    currency_code   varchar,
    document_date   date,
    due_date        date,
    days_overdue    integer,
    total_txn       numeric,
    settled_txn     numeric,
    open_txn        numeric,
    bucket          text)
LANGUAGE sql STABLE AS $fn$
    WITH settled AS (
        SELECT al.purchase_bill_id AS bill_id, SUM(al.target_amount_txn) AS amt
        FROM payment_allocation al
        WHERE al.is_reversed = false
          AND al.purchase_bill_id IS NOT NULL
          AND al.allocation_date <= p_as_of
        GROUP BY al.purchase_bill_id
    )
    SELECT pb.vendor_id, v.name, pb.id, pb.number, pb.currency_id,
           pb.document_date, pb.due_date,
           GREATEST((p_as_of - pb.due_date), 0)::integer,
           pb.total_txn,
           COALESCE(settled.amt, 0),
           pb.total_txn - COALESCE(settled.amt, 0),
           CASE
               WHEN p_as_of <= pb.due_date      THEN 'CURRENT'
               WHEN p_as_of - pb.due_date <= 30 THEN '1-30'
               WHEN p_as_of - pb.due_date <= 60 THEN '31-60'
               WHEN p_as_of - pb.due_date <= 90 THEN '61-90'
               ELSE '90+'
           END
    FROM purchase_bill pb
    JOIN vendor v ON v.id = pb.vendor_id
    LEFT JOIN settled ON settled.bill_id = pb.id
    WHERE pb.status IN {POSTED}
      AND pb.document_date <= p_as_of
      AND pb.total_txn - COALESCE(settled.amt, 0) > 0
    ORDER BY v.name, pb.due_date;
$fn$;


-- ===========================================================================
-- RPT-017 / INV-004  Stock card for one product in one warehouse
-- Quantity is signed here (direction * quantity) so the column reads like a
-- ledger, even though the underlying movement stores it positive.
-- ===========================================================================
CREATE OR REPLACE FUNCTION fn_stock_card(p_product_id bigint, p_warehouse_id bigint,
                                         p_from date, p_to date)
RETURNS TABLE (
    movement_id       bigint,
    movement_date     date,
    movement_type     varchar,
    source_doc_type   varchar,
    source_doc_number varchar,
    signed_quantity   numeric,
    unit_cost         numeric,
    total_cost        numeric,
    balance_quantity  numeric,
    balance_value     numeric,
    journal_entry_id  bigint)
LANGUAGE sql STABLE AS $fn$
    SELECT sm.id, sm.movement_date, sm.movement_type,
           sm.source_doc_type, sm.source_doc_number,
           sm.direction * sm.quantity,
           sm.unit_cost, sm.total_cost,
           sm.balance_quantity_after, sm.balance_value_after,
           sm.journal_entry_id
    FROM stock_movement sm
    WHERE sm.product_id = p_product_id
      AND sm.warehouse_id = p_warehouse_id
      AND sm.movement_date BETWEEN p_from AND p_to
    ORDER BY sm.movement_date, sm.id;
$fn$;


-- ===========================================================================
-- RPT-013  Cash and bank activity per money account
-- ===========================================================================
CREATE VIEW v_money_account_activity AS
SELECT ma.id AS money_account_id,
       ma.code AS money_account_code,
       ma.name AS money_account_name,
       ma.account_type,
       ma.currency_id AS currency_code,
       je.entry_date,
       je.number AS entry_number,
       je.journal_type,
       jl.description,
       jl.debit_base  AS money_in_base,
       jl.credit_base AS money_out_base,
       jl.debit_base - jl.credit_base AS net_base,
       jl.customer_id,
       jl.vendor_id
FROM journal_line jl
JOIN journal_entry je   ON je.id = jl.entry_id
JOIN money_account ma   ON ma.id = jl.money_account_id;
"""

REVERSE = """
DROP FUNCTION IF EXISTS fn_stock_card(bigint, bigint, date, date);
DROP FUNCTION IF EXISTS fn_ap_ageing(date);
DROP FUNCTION IF EXISTS fn_ar_ageing(date);
DROP FUNCTION IF EXISTS fn_trial_balance(date, date);
DROP VIEW IF EXISTS v_money_account_activity;
DROP VIEW IF EXISTS v_subledger_reconciliation;
DROP VIEW IF EXISTS v_tax_transaction;
DROP VIEW IF EXISTS v_inventory_valuation;
DROP VIEW IF EXISTS v_vendor_unapplied_credit;
DROP VIEW IF EXISTS v_customer_unapplied_credit;
DROP VIEW IF EXISTS v_purchase_bill_open;
DROP VIEW IF EXISTS v_sales_invoice_open;
DROP VIEW IF EXISTS v_control_account_balance;
DROP VIEW IF EXISTS v_general_ledger;
"""


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("ledger", "0003_posting_guards"),
        ("sales", "0001_initial"),
        ("purchases", "0001_initial"),
        ("payments", "0001_initial"),
        ("inventory", "0002_initial"),
        ("catalog", "0002_initial"),
    ]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
