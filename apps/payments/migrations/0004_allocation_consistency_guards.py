from django.db import migrations


FORWARD = r"""
CREATE OR REPLACE FUNCTION wams_allocation_consistency() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    v_sum numeric(18,4);
    v_stored numeric(18,4);
    v_secondary numeric(18,4);
    v_number varchar(32);
BEGIN
    r := COALESCE(NEW, OLD);

    IF r.sales_invoice_id IS NOT NULL THEN
        SELECT allocated_txn, credited_txn, number
          INTO v_stored, v_secondary, v_number
          FROM sales_invoice
         WHERE id = r.sales_invoice_id;
        IF FOUND THEN
            SELECT COALESCE(SUM(target_amount_txn), 0) INTO v_sum
              FROM payment_allocation
             WHERE sales_invoice_id = r.sales_invoice_id
               AND is_reversed = false
               AND payment_id IS NOT NULL;
            IF v_sum <> v_stored THEN
                RAISE EXCEPTION
                    'BR-008 violated: invoice % records payment allocations % but rows sum to %',
                    v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
            END IF;

            SELECT COALESCE(SUM(target_amount_txn), 0) INTO v_sum
              FROM payment_allocation
             WHERE sales_invoice_id = r.sales_invoice_id
               AND is_reversed = false
               AND sales_credit_note_id IS NOT NULL;
            IF v_sum <> v_secondary THEN
                RAISE EXCEPTION
                    'BR-008 violated: invoice % records credit allocations % but rows sum to %',
                    v_number, v_secondary, v_sum USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    END IF;

    IF r.purchase_bill_id IS NOT NULL THEN
        SELECT allocated_txn, credited_txn, number
          INTO v_stored, v_secondary, v_number
          FROM purchase_bill
         WHERE id = r.purchase_bill_id;
        IF FOUND THEN
            SELECT COALESCE(SUM(target_amount_txn), 0) INTO v_sum
              FROM payment_allocation
             WHERE purchase_bill_id = r.purchase_bill_id
               AND is_reversed = false
               AND payment_id IS NOT NULL;
            IF v_sum <> v_stored THEN
                RAISE EXCEPTION
                    'BR-008 violated: bill % records payment allocations % but rows sum to %',
                    v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
            END IF;

            SELECT COALESCE(SUM(target_amount_txn), 0) INTO v_sum
              FROM payment_allocation
             WHERE purchase_bill_id = r.purchase_bill_id
               AND is_reversed = false
               AND vendor_debit_note_id IS NOT NULL;
            IF v_sum <> v_secondary THEN
                RAISE EXCEPTION
                    'BR-008 violated: bill % records credit allocations % but rows sum to %',
                    v_number, v_secondary, v_sum USING ERRCODE = 'check_violation';
            END IF;
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
                'BR-008 violated: payment % records allocated % but rows sum to %',
                v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF r.sales_credit_note_id IS NOT NULL THEN
        SELECT COALESCE(SUM(source_amount_txn), 0) INTO v_sum
          FROM payment_allocation
         WHERE sales_credit_note_id = r.sales_credit_note_id AND is_reversed = false;
        SELECT allocated_txn, number INTO v_stored, v_number
          FROM sales_credit_note WHERE id = r.sales_credit_note_id;
        IF FOUND AND v_sum <> v_stored THEN
            RAISE EXCEPTION
                'BR-008 violated: sales credit % records allocated % but rows sum to %',
                v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF r.vendor_debit_note_id IS NOT NULL THEN
        SELECT COALESCE(SUM(source_amount_txn), 0) INTO v_sum
          FROM payment_allocation
         WHERE vendor_debit_note_id = r.vendor_debit_note_id AND is_reversed = false;
        SELECT allocated_txn, number INTO v_stored, v_number
          FROM vendor_debit_note WHERE id = r.vendor_debit_note_id;
        IF FOUND AND v_sum <> v_stored THEN
            RAISE EXCEPTION
                'BR-008 violated: vendor credit % records allocated % but rows sum to %',
                v_number, v_stored, v_sum USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NULL;
END;
$$;
"""


REVERSE = r"""
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
"""


class Migration(migrations.Migration):
    dependencies = [
        (
            "payments",
            "0003_remove_allocation_allocation_unique_payment_invoice_and_more",
        ),
    ]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
