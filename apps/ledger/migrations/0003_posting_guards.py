"""
Database-level posting guards.

Django's `Meta.constraints` can only see one row at a time. These rules span
rows, so they are implemented as PostgreSQL triggers — which means they hold
against the Django ORM, the admin, a management command, a data migration and
`psql` alike.

    BR-006 / SC-01  a posted journal's stored totals equal the sum of its lines,
                    and debits equal credits (deferred to COMMIT so the service
                    may write the header before the lines)
    GL-010          no posting to an inactive or non-postable account
    BR-020 / GL-012 no posting into a closed fiscal period
    BR-004 / SC-06  posted journals and lines are never edited or deleted
    BR-017          stock may not go negative unless policy allows it
    BR-008 / SC-02  an invoice's or bill's allocated amount equals the sum of
                    its live allocations, and a payment's does too
"""

from django.db import migrations

FORWARD = r"""
-- ===========================================================================
-- 1. BR-006  journal entry balance, deferred to commit
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_journal_balance_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_entry_id  bigint;
    v_sum_debit  numeric(18,4);
    v_sum_credit numeric(18,4);
    v_tot_debit  numeric(18,4);
    v_tot_credit numeric(18,4);
    v_number     varchar(32);
BEGIN
    IF TG_TABLE_NAME = 'journal_line' THEN
        v_entry_id := COALESCE(NEW.entry_id, OLD.entry_id);
    ELSE
        v_entry_id := NEW.id;
    END IF;

    SELECT total_debit_base, total_credit_base, number
      INTO v_tot_debit, v_tot_credit, v_number
      FROM journal_entry WHERE id = v_entry_id;

    IF NOT FOUND THEN
        RETURN NULL;  -- entry gone in the same transaction; nothing to check
    END IF;

    SELECT COALESCE(SUM(debit_base), 0), COALESCE(SUM(credit_base), 0)
      INTO v_sum_debit, v_sum_credit
      FROM journal_line WHERE entry_id = v_entry_id;

    IF v_sum_debit <> v_sum_credit THEN
        RAISE EXCEPTION
            'BR-006 violated: journal % lines are unbalanced (debit %, credit %)',
            v_number, v_sum_debit, v_sum_credit
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_sum_debit <> v_tot_debit OR v_sum_credit <> v_tot_credit THEN
        RAISE EXCEPTION
            'BR-006 violated: journal % header totals (% / %) do not match its lines (% / %)',
            v_number, v_tot_debit, v_tot_credit, v_sum_debit, v_sum_credit
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_sum_debit = 0 THEN
        RAISE EXCEPTION 'GL-001 violated: journal % has no lines', v_number
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_journal_entry_balanced
    AFTER INSERT OR UPDATE ON journal_entry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION wams_journal_balance_check();

CREATE CONSTRAINT TRIGGER trg_journal_line_balanced
    AFTER INSERT OR UPDATE OR DELETE ON journal_line
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION wams_journal_balance_check();


-- ===========================================================================
-- 2. GL-010  only active, postable accounts receive journal lines
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_journal_line_account_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_code varchar(20);
    v_postable boolean;
    v_active boolean;
    v_requires_party boolean;
BEGIN
    SELECT code, is_postable, is_active, requires_party
      INTO v_code, v_postable, v_active, v_requires_party
      FROM account WHERE id = NEW.account_id;

    IF NOT v_postable THEN
        RAISE EXCEPTION 'GL-010 violated: account % is a non-postable parent account', v_code
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT v_active THEN
        RAISE EXCEPTION 'GL-010 violated: account % is inactive', v_code
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_requires_party AND NEW.customer_id IS NULL AND NEW.vendor_id IS NULL THEN
        RAISE EXCEPTION
            'GL-011 violated: control account % requires a party on every line', v_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_journal_line_account_check
    BEFORE INSERT ON journal_line
    FOR EACH ROW EXECUTE FUNCTION wams_journal_line_account_check();


-- ===========================================================================
-- 3. BR-020 / GL-012  the target fiscal period must be open, and must contain
--    the entry date
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_journal_period_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_status varchar(8);
    v_start date;
    v_end date;
    v_name varchar(50);
BEGIN
    SELECT status, start_date, end_date, name
      INTO v_status, v_start, v_end, v_name
      FROM fiscal_period WHERE id = NEW.fiscal_period_id;

    IF v_status <> 'OPEN' THEN
        RAISE EXCEPTION 'BR-020 violated: fiscal period % is %', v_name, v_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.entry_date < v_start OR NEW.entry_date > v_end THEN
        RAISE EXCEPTION
            'BR-020 violated: entry date % falls outside period % (% .. %)',
            NEW.entry_date, v_name, v_start, v_end
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_journal_period_check
    BEFORE INSERT ON journal_entry
    FOR EACH ROW EXECUTE FUNCTION wams_journal_period_check();


-- ===========================================================================
-- 4. BR-004 / SC-06 / NFR-017  the ledger is append-only
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_journal_entry_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'BR-004 violated: journal % cannot be deleted; post a reversal instead', OLD.number
            USING ERRCODE = 'check_violation';
    END IF;

    -- Identity and dating are frozen. Totals stay writable so the posting
    -- service can build header-then-lines inside one transaction; the deferred
    -- balance trigger above still proves they agree at COMMIT.
    IF NEW.number         IS DISTINCT FROM OLD.number
    OR NEW.entry_date     IS DISTINCT FROM OLD.entry_date
    OR NEW.fiscal_period_id IS DISTINCT FROM OLD.fiscal_period_id
    OR NEW.currency_id    IS DISTINCT FROM OLD.currency_id
    OR NEW.exchange_rate  IS DISTINCT FROM OLD.exchange_rate
    OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION
            'BR-004 violated: journal % is posted; identity, date, currency and rate are immutable',
            OLD.number
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_journal_entry_immutable
    BEFORE UPDATE OR DELETE ON journal_entry
    FOR EACH ROW EXECUTE FUNCTION wams_journal_entry_immutable();

CREATE OR REPLACE FUNCTION wams_journal_line_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'BR-004 violated: journal lines cannot be % once written; post a reversal instead',
        lower(TG_OP)
        USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER trg_journal_line_immutable
    BEFORE UPDATE OR DELETE ON journal_line
    FOR EACH ROW EXECUTE FUNCTION wams_journal_line_immutable();


-- ===========================================================================
-- 5. BR-017 / INV-010  negative stock policy
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_stock_negative_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_wh_allows boolean;
    v_co_allows boolean;
    v_sku varchar(40);
    v_wh varchar(20);
BEGIN
    IF NEW.quantity_on_hand >= 0 THEN
        RETURN NEW;
    END IF;

    SELECT allow_negative_stock, code INTO v_wh_allows, v_wh
      FROM warehouse WHERE id = NEW.warehouse_id;
    SELECT allow_negative_stock INTO v_co_allows FROM company LIMIT 1;

    IF COALESCE(v_wh_allows, false) OR COALESCE(v_co_allows, false) THEN
        RETURN NEW;
    END IF;

    SELECT sku INTO v_sku FROM product WHERE id = NEW.product_id;
    RAISE EXCEPTION
        'BR-017 violated: % in warehouse % would fall to %; negative stock is not permitted',
        v_sku, v_wh, NEW.quantity_on_hand
        USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER trg_stock_negative_check
    BEFORE INSERT OR UPDATE ON stock_balance
    FOR EACH ROW EXECUTE FUNCTION wams_stock_negative_check();


-- ===========================================================================
-- 6. BR-019  the stock ledger is append-only (only a late journal link may be
--    filled in)
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_stock_movement_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'BR-004 violated: stock movement % cannot be deleted; post a reversing movement', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.product_id     IS DISTINCT FROM OLD.product_id
    OR NEW.warehouse_id   IS DISTINCT FROM OLD.warehouse_id
    OR NEW.movement_date  IS DISTINCT FROM OLD.movement_date
    OR NEW.movement_type  IS DISTINCT FROM OLD.movement_type
    OR NEW.direction      IS DISTINCT FROM OLD.direction
    OR NEW.quantity       IS DISTINCT FROM OLD.quantity
    OR NEW.unit_cost      IS DISTINCT FROM OLD.unit_cost
    OR NEW.total_cost     IS DISTINCT FROM OLD.total_cost THEN
        RAISE EXCEPTION
            'BR-004 violated: stock movement % is posted and immutable', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_stock_movement_immutable
    BEFORE UPDATE OR DELETE ON stock_movement
    FOR EACH ROW EXECUTE FUNCTION wams_stock_movement_immutable();


-- ===========================================================================
-- 7. BR-008 / SC-02  allocation totals agree with the documents they settle
-- ===========================================================================
CREATE OR REPLACE FUNCTION wams_allocation_consistency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    v_sum numeric(18,4);
    v_stored numeric(18,4);
    v_number varchar(32);
BEGIN
    r := COALESCE(NEW, OLD);

    IF r.sales_invoice_id IS NOT NULL THEN
        SELECT COALESCE(SUM(target_amount_txn), 0) INTO v_sum
          FROM payment_allocation
         WHERE sales_invoice_id = r.sales_invoice_id
           AND is_reversed = false
           AND payment_id IS NOT NULL;
        SELECT allocated_txn, number INTO v_stored, v_number
          FROM sales_invoice WHERE id = r.sales_invoice_id;
        IF FOUND AND v_sum <> v_stored THEN
            RAISE EXCEPTION
                'BR-008 violated: invoice % records allocated % but its allocations sum to %',
                v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF r.purchase_bill_id IS NOT NULL THEN
        SELECT COALESCE(SUM(target_amount_txn), 0) INTO v_sum
          FROM payment_allocation
         WHERE purchase_bill_id = r.purchase_bill_id
           AND is_reversed = false
           AND payment_id IS NOT NULL;
        SELECT allocated_txn, number INTO v_stored, v_number
          FROM purchase_bill WHERE id = r.purchase_bill_id;
        IF FOUND AND v_sum <> v_stored THEN
            RAISE EXCEPTION
                'BR-008 violated: bill % records allocated % but its allocations sum to %',
                v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF r.payment_id IS NOT NULL THEN
        SELECT COALESCE(SUM(source_amount_txn), 0) INTO v_sum
          FROM payment_allocation
         WHERE payment_id = r.payment_id AND is_reversed = false;
        SELECT allocated_txn, number INTO v_stored, v_number
          FROM payment WHERE id = r.payment_id;
        IF FOUND AND v_sum <> v_stored THEN
            RAISE EXCEPTION
                'BR-008 violated: payment % records allocated % but its allocations sum to %',
                v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_allocation_consistency
    AFTER INSERT OR UPDATE OR DELETE ON payment_allocation
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION wams_allocation_consistency();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS trg_allocation_consistency ON payment_allocation;
DROP TRIGGER IF EXISTS trg_stock_movement_immutable ON stock_movement;
DROP TRIGGER IF EXISTS trg_stock_negative_check ON stock_balance;
DROP TRIGGER IF EXISTS trg_journal_line_immutable ON journal_line;
DROP TRIGGER IF EXISTS trg_journal_entry_immutable ON journal_entry;
DROP TRIGGER IF EXISTS trg_journal_period_check ON journal_entry;
DROP TRIGGER IF EXISTS trg_journal_line_account_check ON journal_line;
DROP TRIGGER IF EXISTS trg_journal_line_balanced ON journal_line;
DROP TRIGGER IF EXISTS trg_journal_entry_balanced ON journal_entry;
DROP FUNCTION IF EXISTS wams_allocation_consistency();
DROP FUNCTION IF EXISTS wams_stock_movement_immutable();
DROP FUNCTION IF EXISTS wams_stock_negative_check();
DROP FUNCTION IF EXISTS wams_journal_line_immutable();
DROP FUNCTION IF EXISTS wams_journal_entry_immutable();
DROP FUNCTION IF EXISTS wams_journal_period_check();
DROP FUNCTION IF EXISTS wams_journal_line_account_check();
DROP FUNCTION IF EXISTS wams_journal_balance_check();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0002_initial"),
        ("inventory", "0002_initial"),
        ("payments", "0001_initial"),
        ("sales", "0001_initial"),
        ("purchases", "0001_initial"),
        ("core", "0003_initial"),
    ]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
