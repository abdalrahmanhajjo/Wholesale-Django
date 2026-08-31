--
-- PostgreSQL database dump
--

\restrict RSgRzEadAymTcB79xAhv6jIq2boINJibSiFJRSillRAU6qkrk2EYp8mfZ9nis5h

-- Dumped from database version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;


--
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: fn_ap_ageing(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_ap_ageing(p_as_of date) RETURNS TABLE(vendor_id bigint, vendor_name character varying, bill_id bigint, document_number character varying, currency_code character varying, document_date date, due_date date, days_overdue integer, total_txn numeric, settled_txn numeric, open_txn numeric, bucket text)
    LANGUAGE sql STABLE
    AS $$
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
    WHERE pb.status IN ('POSTED','PARTIAL','COMPLETED')
      AND pb.document_date <= p_as_of
      AND pb.total_txn - COALESCE(settled.amt, 0) > 0
    ORDER BY v.name, pb.due_date;
$$;


--
-- Name: fn_ar_ageing(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_ar_ageing(p_as_of date) RETURNS TABLE(customer_id bigint, customer_name character varying, invoice_id bigint, document_number character varying, currency_code character varying, document_date date, due_date date, days_overdue integer, total_txn numeric, settled_txn numeric, open_txn numeric, bucket text)
    LANGUAGE sql STABLE
    AS $$
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
    WHERE si.status IN ('POSTED','PARTIAL','COMPLETED')
      AND si.document_date <= p_as_of
      AND si.total_txn - COALESCE(settled.amt, 0) > 0
    ORDER BY c.name, si.due_date;
$$;


--
-- Name: fn_stock_card(bigint, bigint, date, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_stock_card(p_product_id bigint, p_warehouse_id bigint, p_from date, p_to date) RETURNS TABLE(movement_id bigint, movement_date date, movement_type character varying, source_doc_type character varying, source_doc_number character varying, signed_quantity numeric, unit_cost numeric, total_cost numeric, balance_quantity numeric, balance_value numeric, journal_entry_id bigint)
    LANGUAGE sql STABLE
    AS $$
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
$$;


--
-- Name: fn_trial_balance(date, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_trial_balance(p_from date, p_to date) RETURNS TABLE(account_id bigint, account_code character varying, account_name character varying, account_type character varying, opening_debit numeric, opening_credit numeric, period_debit numeric, period_credit numeric, closing_debit numeric, closing_credit numeric)
    LANGUAGE sql STABLE
    AS $$
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
$$;


--
-- Name: wams_allocation_consistency(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_allocation_consistency() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: wams_journal_balance_check(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_journal_balance_check() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: wams_journal_entry_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_journal_entry_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: wams_journal_line_account_check(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_journal_line_account_check() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: wams_journal_line_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_journal_line_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION
        'BR-004 violated: journal lines cannot be % once written; post a reversal instead',
        lower(TG_OP)
        USING ERRCODE = 'check_violation';
END;
$$;


--
-- Name: wams_journal_period_check(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_journal_period_check() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: wams_stock_movement_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_stock_movement_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


--
-- Name: wams_stock_negative_check(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.wams_stock_negative_check() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(150) NOT NULL,
    account_type character varying(12) NOT NULL,
    subtype character varying(24) NOT NULL,
    normal_balance character varying(6) NOT NULL,
    is_postable boolean NOT NULL,
    is_control boolean NOT NULL,
    control_type character varying(18) NOT NULL,
    is_contra boolean NOT NULL,
    requires_party boolean NOT NULL,
    is_active boolean NOT NULL,
    description text NOT NULL,
    created_by_id bigint,
    currency_id character varying(3),
    parent_id bigint,
    updated_by_id bigint,
    CONSTRAINT account_control_needs_type CHECK (((is_control AND (NOT ((control_type)::text = ''::text))) OR ((NOT is_control) AND ((control_type)::text = ''::text)))),
    CONSTRAINT account_normal_balance_matches_type CHECK ((((NOT is_contra) AND ((((account_type)::text = ANY ((ARRAY['ASSET'::character varying, 'EXPENSE'::character varying])::text[])) AND ((normal_balance)::text = 'DEBIT'::text)) OR (((account_type)::text = ANY ((ARRAY['LIABILITY'::character varying, 'EQUITY'::character varying, 'INCOME'::character varying])::text[])) AND ((normal_balance)::text = 'CREDIT'::text)))) OR (is_contra AND ((((account_type)::text = ANY ((ARRAY['ASSET'::character varying, 'EXPENSE'::character varying])::text[])) AND ((normal_balance)::text = 'CREDIT'::text)) OR (((account_type)::text = ANY ((ARRAY['LIABILITY'::character varying, 'EQUITY'::character varying, 'INCOME'::character varying])::text[])) AND ((normal_balance)::text = 'DEBIT'::text)))))),
    CONSTRAINT account_not_self_parent CHECK ((NOT ((parent_id = id) AND (parent_id IS NOT NULL)))),
    CONSTRAINT account_subtype_matches_type CHECK (((((account_type)::text = 'ASSET'::text) AND ((subtype)::text = ANY ((ARRAY['CURRENT_ASSET'::character varying, 'NONCURRENT_ASSET'::character varying])::text[]))) OR (((account_type)::text = 'LIABILITY'::text) AND ((subtype)::text = ANY ((ARRAY['CURRENT_LIABILITY'::character varying, 'NONCURRENT_LIABILITY'::character varying])::text[]))) OR (((account_type)::text = 'EQUITY'::text) AND ((subtype)::text = 'EQUITY'::text)) OR (((account_type)::text = 'INCOME'::text) AND ((subtype)::text = ANY ((ARRAY['REVENUE'::character varying, 'OTHER_INCOME'::character varying])::text[]))) OR (((account_type)::text = 'EXPENSE'::text) AND ((subtype)::text = ANY ((ARRAY['COGS'::character varying, 'OPERATING_EXPENSE'::character varying, 'OTHER_EXPENSE'::character varying])::text[])))))
);


--
-- Name: account_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.account ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.account_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: account_mapping; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_mapping (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    key character varying(32) NOT NULL,
    notes character varying(255) NOT NULL,
    account_id bigint NOT NULL,
    created_by_id bigint,
    updated_by_id bigint
);


--
-- Name: account_mapping_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.account_mapping ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.account_mapping_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: adjustment_reason; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.adjustment_reason (
    id bigint NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    increases_stock boolean NOT NULL,
    requires_approval boolean NOT NULL,
    is_active boolean NOT NULL,
    gain_loss_account_id bigint
);


--
-- Name: adjustment_reason_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.adjustment_reason ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.adjustment_reason_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: app_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user (
    id bigint NOT NULL,
    password character varying(128) NOT NULL,
    last_login timestamp with time zone,
    is_superuser boolean NOT NULL,
    username character varying(150) NOT NULL,
    first_name character varying(150) NOT NULL,
    last_name character varying(150) NOT NULL,
    is_staff boolean NOT NULL,
    is_active boolean NOT NULL,
    date_joined timestamp with time zone NOT NULL,
    email character varying(254) NOT NULL,
    full_name character varying(150) NOT NULL,
    phone character varying(50) NOT NULL,
    job_title character varying(100) NOT NULL,
    deactivated_at timestamp with time zone,
    deactivated_reason character varying(255) NOT NULL,
    must_change_password boolean NOT NULL,
    last_password_change timestamp with time zone,
    is_read_only boolean NOT NULL,
    default_warehouse_id bigint,
    CONSTRAINT user_inactive_has_timestamp CHECK ((is_active OR (deactivated_at IS NOT NULL)))
);


--
-- Name: app_user_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user_groups (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    group_id integer NOT NULL
);


--
-- Name: app_user_groups_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.app_user_groups ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.app_user_groups_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: app_user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.app_user ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.app_user_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: app_user_user_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user_user_permissions (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: app_user_user_permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.app_user_user_permissions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.app_user_user_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: audit_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_event (
    id bigint NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    action character varying(16) NOT NULL,
    object_id bigint,
    object_repr character varying(255) NOT NULL,
    changes jsonb,
    reason text NOT NULL,
    correlation_id uuid,
    ip_address inet,
    content_type_id integer,
    user_id bigint
);


--
-- Name: audit_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.audit_event ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.audit_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_group (
    id integer NOT NULL,
    name character varying(150) NOT NULL
);


--
-- Name: auth_group_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_group ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_group_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_group_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_group_permissions (
    id bigint NOT NULL,
    group_id integer NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: auth_group_permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_group_permissions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_group_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_permission (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    content_type_id integer NOT NULL,
    codename character varying(100) NOT NULL
);


--
-- Name: auth_permission_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_permission ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_permission_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: company; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    singleton boolean NOT NULL,
    name character varying(200) NOT NULL,
    legal_name character varying(200) NOT NULL,
    tax_id character varying(50) NOT NULL,
    registration_no character varying(50) NOT NULL,
    email character varying(254) NOT NULL,
    phone character varying(50) NOT NULL,
    address_line1 character varying(200) NOT NULL,
    address_line2 character varying(200) NOT NULL,
    city character varying(100) NOT NULL,
    state character varying(100) NOT NULL,
    postal_code character varying(20) NOT NULL,
    country character varying(100) NOT NULL,
    logo character varying(100),
    fiscal_year_start_month smallint NOT NULL,
    timezone character varying(64) NOT NULL,
    language character varying(10) NOT NULL,
    allow_negative_stock boolean NOT NULL,
    rounding_tolerance numeric(18,4) NOT NULL,
    price_decimal_places smallint NOT NULL,
    qty_decimal_places smallint NOT NULL,
    require_po_approval boolean NOT NULL,
    require_so_approval boolean NOT NULL,
    block_duplicate_vendor_invoice boolean NOT NULL,
    warn_duplicate_customer_ref boolean NOT NULL,
    created_by_id bigint,
    updated_by_id bigint,
    base_currency_id character varying(3) NOT NULL,
    CONSTRAINT company_fiscal_year_start_month_check CHECK ((fiscal_year_start_month >= 0)),
    CONSTRAINT company_fy_start_month_range CHECK (((fiscal_year_start_month >= 1) AND (fiscal_year_start_month <= 12))),
    CONSTRAINT company_price_decimal_places_check CHECK ((price_decimal_places >= 0)),
    CONSTRAINT company_qty_decimal_places_check CHECK ((qty_decimal_places >= 0)),
    CONSTRAINT company_rounding_tolerance_nonneg CHECK ((rounding_tolerance >= (0)::numeric))
);


--
-- Name: company_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.company ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.company_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: currency; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.currency (
    code character varying(3) NOT NULL,
    name character varying(64) NOT NULL,
    symbol character varying(8) NOT NULL,
    decimal_places smallint NOT NULL,
    is_base boolean NOT NULL,
    is_active boolean NOT NULL,
    CONSTRAINT currency_decimal_places_check CHECK ((decimal_places >= 0)),
    CONSTRAINT currency_decimal_places_sane CHECK ((decimal_places <= 6))
);


--
-- Name: customer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    legal_name character varying(200) NOT NULL,
    tax_id character varying(50) NOT NULL,
    email character varying(254) NOT NULL,
    phone character varying(50) NOT NULL,
    website character varying(200) NOT NULL,
    notes text NOT NULL,
    is_active boolean NOT NULL,
    deactivated_at timestamp with time zone,
    credit_limit numeric(18,4) NOT NULL,
    credit_hold boolean NOT NULL,
    credit_hold_reason character varying(255) NOT NULL,
    advance_account_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    default_tax_code_id bigint,
    default_warehouse_id bigint,
    payment_term_id bigint,
    receivable_account_id bigint,
    salesperson_id bigint,
    updated_by_id bigint,
    CONSTRAINT customer_credit_limit_nonneg CHECK ((credit_limit >= (0)::numeric))
);


--
-- Name: customer_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.customer ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.customer_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: delivery_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.delivery_note (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    status character varying(10) NOT NULL,
    reference character varying(64) NOT NULL,
    notes text NOT NULL,
    posted_at timestamp with time zone,
    total_cost_base numeric(18,4) NOT NULL,
    shipping_address_text text NOT NULL,
    carrier character varying(100) NOT NULL,
    tracking_reference character varying(64) NOT NULL,
    created_by_id bigint,
    customer_id bigint NOT NULL,
    delivered_by_id bigint,
    journal_entry_id bigint,
    posted_by_id bigint,
    sales_order_id bigint,
    updated_by_id bigint,
    warehouse_id bigint NOT NULL,
    CONSTRAINT delivery_note_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL) OR (total_cost_base = (0)::numeric)))
);


--
-- Name: delivery_note_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.delivery_note ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.delivery_note_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: delivery_note_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.delivery_note_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_cost numeric(18,6) NOT NULL,
    total_cost numeric(18,4) NOT NULL,
    quantity_invoiced numeric(18,4) NOT NULL,
    quantity_returned numeric(18,4) NOT NULL,
    delivery_id bigint NOT NULL,
    product_id bigint NOT NULL,
    sales_order_line_id bigint,
    unit_id bigint NOT NULL,
    CONSTRAINT delivery_note_line_line_no_check CHECK ((line_no >= 0)),
    CONSTRAINT dn_line_invoiced_within_delivered CHECK ((quantity_invoiced <= quantity)),
    CONSTRAINT dn_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT dn_line_returned_within_delivered CHECK ((quantity_returned <= quantity))
);


--
-- Name: delivery_note_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.delivery_note_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.delivery_note_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_admin_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_admin_log (
    id integer NOT NULL,
    action_time timestamp with time zone NOT NULL,
    object_id text,
    object_repr character varying(200) NOT NULL,
    action_flag smallint NOT NULL,
    change_message text NOT NULL,
    content_type_id integer,
    user_id bigint NOT NULL,
    CONSTRAINT django_admin_log_action_flag_check CHECK ((action_flag >= 0))
);


--
-- Name: django_admin_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_admin_log ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_admin_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_content_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_content_type (
    id integer NOT NULL,
    app_label character varying(100) NOT NULL,
    model character varying(100) NOT NULL
);


--
-- Name: django_content_type_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_content_type ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_content_type_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_migrations (
    id bigint NOT NULL,
    app character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    applied timestamp with time zone NOT NULL
);


--
-- Name: django_migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_migrations ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_migrations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_session (
    session_key character varying(40) NOT NULL,
    session_data text NOT NULL,
    expire_date timestamp with time zone NOT NULL
);


--
-- Name: document_sequence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_sequence (
    id bigint NOT NULL,
    document_type character varying(4) NOT NULL,
    series character varying(20) NOT NULL,
    prefix character varying(20) NOT NULL,
    suffix character varying(20) NOT NULL,
    padding smallint NOT NULL,
    next_number bigint NOT NULL,
    reset_policy character varying(8) NOT NULL,
    period_key character varying(10) NOT NULL,
    is_active boolean NOT NULL,
    CONSTRAINT document_sequence_next_positive CHECK ((next_number >= 1)),
    CONSTRAINT document_sequence_padding_check CHECK ((padding >= 0))
);


--
-- Name: document_sequence_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.document_sequence ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.document_sequence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: exchange_rate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_rate (
    id bigint NOT NULL,
    rate_date date NOT NULL,
    rate numeric(18,8) NOT NULL,
    source character varying(64) NOT NULL,
    currency_id character varying(3) NOT NULL,
    CONSTRAINT exchange_rate_positive CHECK ((rate > (0)::numeric))
);


--
-- Name: exchange_rate_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.exchange_rate ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.exchange_rate_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: fiscal_period; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiscal_period (
    id bigint NOT NULL,
    period_no smallint NOT NULL,
    name character varying(50) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status character varying(8) NOT NULL,
    closed_at timestamp with time zone,
    close_reason text NOT NULL,
    reopened_at timestamp with time zone,
    reopen_reason text NOT NULL,
    closed_by_id bigint,
    reopened_by_id bigint,
    fiscal_year_id bigint NOT NULL,
    CONSTRAINT fiscal_period_closed_has_actor CHECK ((((status)::text = 'OPEN'::text) OR (closed_by_id IS NOT NULL))),
    CONSTRAINT fiscal_period_dates_ordered CHECK ((end_date >= start_date)),
    CONSTRAINT fiscal_period_period_no_check CHECK ((period_no >= 0))
);


--
-- Name: fiscal_period_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.fiscal_period ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.fiscal_period_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: fiscal_year; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fiscal_year (
    id bigint NOT NULL,
    code character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status character varying(8) NOT NULL,
    CONSTRAINT fiscal_year_dates_ordered CHECK ((end_date > start_date))
);


--
-- Name: fiscal_year_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.fiscal_year ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.fiscal_year_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: goods_receipt; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goods_receipt (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    status character varying(10) NOT NULL,
    reference character varying(64) NOT NULL,
    notes text NOT NULL,
    posted_at timestamp with time zone,
    total_cost_base numeric(18,4) NOT NULL,
    vendor_delivery_note character varying(64) NOT NULL,
    created_by_id bigint,
    journal_entry_id bigint,
    posted_by_id bigint,
    purchase_order_id bigint,
    received_by_id bigint,
    updated_by_id bigint,
    vendor_id bigint NOT NULL,
    warehouse_id bigint NOT NULL,
    CONSTRAINT goods_receipt_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL) OR (total_cost_base = (0)::numeric)))
);


--
-- Name: goods_receipt_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.goods_receipt ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.goods_receipt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: goods_receipt_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goods_receipt_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity_received numeric(18,4) NOT NULL,
    quantity_accepted numeric(18,4) NOT NULL,
    quantity_rejected numeric(18,4) NOT NULL,
    rejection_reason character varying(255) NOT NULL,
    unit_cost numeric(18,6) NOT NULL,
    total_cost numeric(18,4) NOT NULL,
    quantity_billed numeric(18,4) NOT NULL,
    quantity_returned numeric(18,4) NOT NULL,
    product_id bigint NOT NULL,
    purchase_order_line_id bigint,
    receipt_id bigint NOT NULL,
    unit_id bigint NOT NULL,
    CONSTRAINT goods_receipt_line_line_no_check CHECK ((line_no >= 0)),
    CONSTRAINT gr_line_billed_within_accepted CHECK ((quantity_billed <= quantity_accepted)),
    CONSTRAINT gr_line_qty_positive CHECK ((quantity_received > (0)::numeric)),
    CONSTRAINT gr_line_returned_within_accepted CHECK ((quantity_returned <= quantity_accepted)),
    CONSTRAINT gr_line_split_nonneg CHECK (((quantity_accepted >= (0)::numeric) AND (quantity_rejected >= (0)::numeric))),
    CONSTRAINT gr_line_split_sums CHECK ((quantity_accepted = (quantity_received - quantity_rejected)))
);


--
-- Name: goods_receipt_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.goods_receipt_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.goods_receipt_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: journal_entry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_entry (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    entry_date date NOT NULL,
    journal_type character varying(10) NOT NULL,
    status character varying(8) NOT NULL,
    narration text NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    total_debit_base numeric(18,4) NOT NULL,
    total_credit_base numeric(18,4) NOT NULL,
    source_object_id bigint,
    source_doc_type character varying(4) NOT NULL,
    source_doc_number character varying(32) NOT NULL,
    idempotency_key character varying(120) NOT NULL,
    is_reversal boolean NOT NULL,
    reversal_reason text NOT NULL,
    is_manual boolean NOT NULL,
    posted_at timestamp with time zone,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    fiscal_period_id bigint NOT NULL,
    posted_by_id bigint,
    reverses_id bigint,
    source_content_type_id integer,
    updated_by_id bigint,
    CONSTRAINT journal_entry_balanced CHECK ((total_debit_base = total_credit_base)),
    CONSTRAINT journal_entry_manual_has_no_source CHECK (((NOT is_manual) OR ((source_content_type_id IS NULL) AND ((source_doc_type)::text = ''::text)))),
    CONSTRAINT journal_entry_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT journal_entry_reversal_has_origin CHECK (((NOT is_reversal) OR (reverses_id IS NOT NULL))),
    CONSTRAINT journal_entry_source_pair CHECK ((((source_content_type_id IS NULL) AND (source_object_id IS NULL)) OR ((source_content_type_id IS NOT NULL) AND (source_object_id IS NOT NULL)))),
    CONSTRAINT journal_entry_totals_nonneg CHECK (((total_debit_base >= (0)::numeric) AND (total_credit_base >= (0)::numeric)))
);


--
-- Name: journal_entry_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.journal_entry ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.journal_entry_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: journal_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    debit_base numeric(18,4) NOT NULL,
    credit_base numeric(18,4) NOT NULL,
    debit_txn numeric(18,4) NOT NULL,
    credit_txn numeric(18,4) NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    account_id bigint NOT NULL,
    currency_id character varying(3) NOT NULL,
    customer_id bigint,
    entry_id bigint NOT NULL,
    money_account_id bigint,
    product_id bigint,
    tax_code_id bigint,
    vendor_id bigint,
    warehouse_id bigint,
    CONSTRAINT journal_line_amounts_nonneg CHECK (((debit_base >= (0)::numeric) AND (credit_base >= (0)::numeric) AND (debit_txn >= (0)::numeric) AND (credit_txn >= (0)::numeric))),
    CONSTRAINT journal_line_debit_xor_credit CHECK ((((debit_base > (0)::numeric) AND (credit_base = (0)::numeric)) OR ((credit_base > (0)::numeric) AND (debit_base = (0)::numeric)))),
    CONSTRAINT journal_line_line_no_check CHECK ((line_no >= 0)),
    CONSTRAINT journal_line_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT journal_line_single_party CHECK ((num_nonnulls(customer_id, vendor_id) <= 1)),
    CONSTRAINT journal_line_txn_side_matches_base CHECK ((((debit_base > (0)::numeric) AND (credit_txn = (0)::numeric)) OR ((credit_base > (0)::numeric) AND (debit_txn = (0)::numeric))))
);


--
-- Name: journal_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.journal_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.journal_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: money_account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.money_account (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    account_type character varying(6) NOT NULL,
    bank_name character varying(100) NOT NULL,
    account_number character varying(64) NOT NULL,
    iban character varying(64) NOT NULL,
    swift character varying(20) NOT NULL,
    allow_negative_balance boolean NOT NULL,
    is_active boolean NOT NULL,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    gl_account_id bigint NOT NULL,
    updated_by_id bigint
);


--
-- Name: money_account_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.money_account ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.money_account_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: opening_balance_batch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.opening_balance_batch (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    code character varying(32) NOT NULL,
    as_of_date date NOT NULL,
    description character varying(255) NOT NULL,
    is_posted boolean NOT NULL,
    created_by_id bigint,
    journal_entry_id bigint,
    updated_by_id bigint
);


--
-- Name: opening_balance_batch_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.opening_balance_batch ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.opening_balance_batch_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: party_address; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.party_address (
    id bigint NOT NULL,
    label character varying(100) NOT NULL,
    address_type character varying(8) NOT NULL,
    line1 character varying(200) NOT NULL,
    line2 character varying(200) NOT NULL,
    city character varying(100) NOT NULL,
    state character varying(100) NOT NULL,
    postal_code character varying(20) NOT NULL,
    country character varying(100) NOT NULL,
    is_default boolean NOT NULL,
    customer_id bigint,
    vendor_id bigint,
    CONSTRAINT address_exactly_one_party CHECK ((num_nonnulls(customer_id, vendor_id) = 1))
);


--
-- Name: party_address_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.party_address ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.party_address_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: party_contact; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.party_contact (
    id bigint NOT NULL,
    name character varying(150) NOT NULL,
    job_title character varying(100) NOT NULL,
    email character varying(254) NOT NULL,
    phone character varying(50) NOT NULL,
    is_primary boolean NOT NULL,
    customer_id bigint,
    vendor_id bigint,
    CONSTRAINT contact_exactly_one_party CHECK ((num_nonnulls(customer_id, vendor_id) = 1))
);


--
-- Name: party_contact_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.party_contact ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.party_contact_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: payment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    direction character varying(8) NOT NULL,
    payment_date date NOT NULL,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    amount_txn numeric(18,4) NOT NULL,
    amount_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    unallocated_txn numeric(18,4) NOT NULL,
    reference character varying(64) NOT NULL,
    narration text NOT NULL,
    status character varying(10) NOT NULL,
    posted_at timestamp with time zone,
    is_reversed boolean NOT NULL,
    reversed_at timestamp with time zone,
    reversal_reason text NOT NULL,
    voucher_printed_at timestamp with time zone,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    customer_id bigint,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    money_account_id bigint NOT NULL,
    posted_by_id bigint,
    reversal_journal_id bigint,
    reversed_by_id bigint,
    updated_by_id bigint,
    vendor_id bigint,
    method_id bigint NOT NULL,
    CONSTRAINT payment_allocated_within_amount CHECK (((allocated_txn >= (0)::numeric) AND (allocated_txn <= amount_txn))),
    CONSTRAINT payment_amount_positive CHECK ((amount_txn > (0)::numeric)),
    CONSTRAINT payment_party_matches_direction CHECK (((((direction)::text = 'RECEIPT'::text) AND (customer_id IS NOT NULL) AND (vendor_id IS NULL)) OR (((direction)::text = 'PAYMENT'::text) AND (vendor_id IS NOT NULL) AND (customer_id IS NULL)))),
    CONSTRAINT payment_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying, 'REVERSED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL))),
    CONSTRAINT payment_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT payment_reversal_attributable CHECK (((NOT is_reversed) OR ((reversed_by_id IS NOT NULL) AND (NOT (reversal_reason = ''::text))))),
    CONSTRAINT payment_unallocated_is_derived CHECK ((unallocated_txn = (amount_txn - allocated_txn))),
    CONSTRAINT payment_unallocated_nonneg CHECK ((unallocated_txn >= (0)::numeric))
);


--
-- Name: payment_allocation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_allocation (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    allocation_date date NOT NULL,
    party_side character varying(8) NOT NULL,
    source_type character varying(18) NOT NULL,
    target_type character varying(14) NOT NULL,
    source_amount_txn numeric(18,4) NOT NULL,
    target_amount_txn numeric(18,4) NOT NULL,
    amount_base numeric(18,4) NOT NULL,
    settlement_rate numeric(18,8) NOT NULL,
    fx_gain_loss_base numeric(18,4) NOT NULL,
    is_reversed boolean NOT NULL,
    reversal_reason character varying(255) NOT NULL,
    notes character varying(255) NOT NULL,
    created_by_id bigint,
    customer_id bigint,
    fx_journal_entry_id bigint,
    journal_entry_id bigint,
    purchase_bill_id bigint,
    sales_credit_note_id bigint,
    sales_invoice_id bigint,
    updated_by_id bigint,
    vendor_id bigint,
    vendor_debit_note_id bigint,
    payment_id bigint,
    CONSTRAINT allocation_amounts_positive CHECK (((source_amount_txn > (0)::numeric) AND (target_amount_txn > (0)::numeric))),
    CONSTRAINT allocation_exactly_one_party CHECK ((num_nonnulls(customer_id, vendor_id) = 1)),
    CONSTRAINT allocation_exactly_one_source CHECK ((num_nonnulls(payment_id, sales_credit_note_id, vendor_debit_note_id) = 1)),
    CONSTRAINT allocation_exactly_one_target CHECK ((num_nonnulls(sales_invoice_id, purchase_bill_id) = 1)),
    CONSTRAINT allocation_party_matches_side CHECK (((((party_side)::text = 'CUSTOMER'::text) AND (customer_id IS NOT NULL)) OR (((party_side)::text = 'VENDOR'::text) AND (vendor_id IS NOT NULL)))),
    CONSTRAINT allocation_rate_positive CHECK ((settlement_rate > (0)::numeric)),
    CONSTRAINT allocation_side_consistency CHECK (((((party_side)::text = 'CUSTOMER'::text) AND ((target_type)::text = 'SALES_INVOICE'::text) AND ((source_type)::text = ANY ((ARRAY['PAYMENT'::character varying, 'SALES_CREDIT_NOTE'::character varying])::text[]))) OR (((party_side)::text = 'VENDOR'::text) AND ((target_type)::text = 'PURCHASE_BILL'::text) AND ((source_type)::text = ANY ((ARRAY['PAYMENT'::character varying, 'VENDOR_DEBIT_NOTE'::character varying])::text[]))))),
    CONSTRAINT allocation_source_matches_type CHECK (((((source_type)::text = 'PAYMENT'::text) AND (payment_id IS NOT NULL)) OR (((source_type)::text = 'SALES_CREDIT_NOTE'::text) AND (sales_credit_note_id IS NOT NULL)) OR (((source_type)::text = 'VENDOR_DEBIT_NOTE'::text) AND (vendor_debit_note_id IS NOT NULL)))),
    CONSTRAINT allocation_target_matches_type CHECK (((((target_type)::text = 'SALES_INVOICE'::text) AND (sales_invoice_id IS NOT NULL)) OR (((target_type)::text = 'PURCHASE_BILL'::text) AND (purchase_bill_id IS NOT NULL))))
);


--
-- Name: payment_allocation_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.payment_allocation ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.payment_allocation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: payment_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.payment ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.payment_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: payment_method; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_method (
    id bigint NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    requires_reference boolean NOT NULL,
    is_active boolean NOT NULL,
    default_money_account_id bigint
);


--
-- Name: payment_method_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.payment_method ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.payment_method_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: payment_term; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_term (
    id bigint NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    net_days smallint NOT NULL,
    end_of_month boolean NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    discount_days smallint NOT NULL,
    is_active boolean NOT NULL,
    CONSTRAINT payment_term_discount_days_check CHECK ((discount_days >= 0)),
    CONSTRAINT payment_term_discount_range CHECK (((discount_percent >= (0)::numeric) AND (discount_percent <= (100)::numeric))),
    CONSTRAINT payment_term_net_days_check CHECK ((net_days >= 0))
);


--
-- Name: payment_term_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.payment_term ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.payment_term_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: posting_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.posting_link (
    id bigint NOT NULL,
    source_object_id bigint NOT NULL,
    source_doc_type character varying(4) NOT NULL,
    source_doc_number character varying(32) NOT NULL,
    effect_type character varying(8) NOT NULL,
    idempotency_key character varying(120) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    journal_entry_id bigint,
    source_content_type_id integer NOT NULL,
    stock_movement_id bigint,
    CONSTRAINT posting_link_effect_matches_target CHECK (((((effect_type)::text = 'JOURNAL'::text) AND (journal_entry_id IS NOT NULL)) OR (((effect_type)::text = 'STOCK'::text) AND (stock_movement_id IS NOT NULL)))),
    CONSTRAINT posting_link_exactly_one_effect CHECK ((num_nonnulls(journal_entry_id, stock_movement_id) = 1))
);


--
-- Name: posting_link_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.posting_link ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.posting_link_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: product; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    sku character varying(40) NOT NULL,
    name character varying(200) NOT NULL,
    description text NOT NULL,
    barcode character varying(64) NOT NULL,
    product_type character varying(10) NOT NULL,
    is_inventory boolean NOT NULL,
    sales_price numeric(18,4) NOT NULL,
    purchase_price numeric(18,4) NOT NULL,
    reorder_level numeric(18,4) NOT NULL,
    max_discount_percent numeric(9,4) NOT NULL,
    is_active boolean NOT NULL,
    cogs_account_id bigint,
    created_by_id bigint,
    default_purchase_tax_code_id bigint,
    default_sales_tax_code_id bigint,
    expense_account_id bigint,
    inventory_account_id bigint,
    preferred_vendor_id bigint,
    revenue_account_id bigint,
    updated_by_id bigint,
    category_id bigint,
    unit_id bigint NOT NULL,
    CONSTRAINT product_max_discount_range CHECK (((max_discount_percent >= (0)::numeric) AND (max_discount_percent <= (100)::numeric))),
    CONSTRAINT product_nonstock_not_inventory CHECK ((((product_type)::text = 'STOCK'::text) OR (NOT is_inventory))),
    CONSTRAINT product_prices_nonneg CHECK (((sales_price >= (0)::numeric) AND (purchase_price >= (0)::numeric))),
    CONSTRAINT product_reorder_nonneg CHECK ((reorder_level >= (0)::numeric))
);


--
-- Name: product_category; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_category (
    id bigint NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    is_active boolean NOT NULL,
    cogs_account_id bigint,
    inventory_account_id bigint,
    parent_id bigint,
    revenue_account_id bigint,
    CONSTRAINT product_category_not_self_parent CHECK (((parent_id IS NULL) OR (NOT ((parent_id = id) AND (parent_id IS NOT NULL)))))
);


--
-- Name: product_category_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.product_category ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.product_category_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: product_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.product ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.product_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: product_price; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_price (
    id bigint NOT NULL,
    kind character varying(8) NOT NULL,
    price numeric(18,4) NOT NULL,
    min_quantity numeric(18,4) NOT NULL,
    valid_from date NOT NULL,
    valid_to date,
    currency_id character varying(3) NOT NULL,
    product_id bigint NOT NULL,
    CONSTRAINT product_price_dates_ordered CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
    CONSTRAINT product_price_nonneg CHECK ((price >= (0)::numeric))
);


--
-- Name: product_price_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.product_price ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.product_price_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: purchase_bill; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_bill (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    due_date date,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    subtotal_txn numeric(18,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    document_discount_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    rounding_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    subtotal_base numeric(18,4) NOT NULL,
    line_discount_base numeric(18,4) NOT NULL,
    document_discount_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    rounding_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    credited_txn numeric(18,4) NOT NULL,
    open_txn numeric(18,4) NOT NULL,
    open_base numeric(18,4) NOT NULL,
    status character varying(10) NOT NULL,
    notes text NOT NULL,
    internal_notes text NOT NULL,
    posted_at timestamp with time zone,
    submitted_at timestamp with time zone,
    approved_at timestamp with time zone,
    approval_reason character varying(255) NOT NULL,
    vendor_invoice_number character varying(64) NOT NULL,
    vendor_invoice_date date,
    duplicate_override_reason character varying(255) NOT NULL,
    vendor_name_snapshot character varying(200) NOT NULL,
    vendor_tax_id_snapshot character varying(50) NOT NULL,
    billing_address_text text NOT NULL,
    document_discount_kind character varying(8) NOT NULL,
    document_discount_value numeric(9,4) NOT NULL,
    is_reversed boolean NOT NULL,
    approved_by_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    fiscal_period_id bigint,
    goods_receipt_id bigint,
    journal_entry_id bigint,
    payable_account_id bigint,
    payment_term_id bigint,
    posted_by_id bigint,
    updated_by_id bigint,
    vendor_id bigint NOT NULL,
    warehouse_id bigint,
    purchase_order_id bigint,
    CONSTRAINT pb_due_after_document_date CHECK (((due_date IS NULL) OR (due_date >= document_date))),
    CONSTRAINT pb_open_is_derived CHECK ((open_txn = ((total_txn - allocated_txn) - credited_txn))),
    CONSTRAINT pb_open_nonneg CHECK ((open_txn >= (0)::numeric)),
    CONSTRAINT pb_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying, 'REVERSED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL))),
    CONSTRAINT pb_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT pb_settlement_within_total CHECK (((allocated_txn >= (0)::numeric) AND (credited_txn >= (0)::numeric) AND (allocated_txn <= total_txn) AND (credited_txn <= total_txn))),
    CONSTRAINT pb_total_nonneg CHECK (((total_txn >= (0)::numeric) AND (total_base >= (0)::numeric)))
);


--
-- Name: purchase_bill_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.purchase_bill ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.purchase_bill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: purchase_bill_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_bill_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    allocated_document_discount_txn numeric(18,4) NOT NULL,
    tax_rate_percent numeric(9,4) NOT NULL,
    tax_is_inclusive boolean NOT NULL,
    tax_is_recoverable boolean NOT NULL,
    gross_txn numeric(18,4) NOT NULL,
    net_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    net_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    is_stock_line boolean NOT NULL,
    product_sku_snapshot character varying(40) NOT NULL,
    quantity_returned numeric(18,4) NOT NULL,
    bill_id bigint NOT NULL,
    expense_account_id bigint,
    product_id bigint,
    receipt_line_id bigint,
    tax_code_id bigint,
    unit_id bigint,
    warehouse_id bigint,
    purchase_order_line_id bigint,
    CONSTRAINT pb_line_discount_within_gross CHECK ((line_discount_txn <= gross_txn)),
    CONSTRAINT pb_line_price_nonneg CHECK ((unit_price >= (0)::numeric)),
    CONSTRAINT pb_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT pb_line_returned_within_billed CHECK (((quantity_returned >= (0)::numeric) AND (quantity_returned <= quantity))),
    CONSTRAINT pb_line_target_present CHECK (((is_stock_line AND (product_id IS NOT NULL)) OR ((NOT is_stock_line) AND (expense_account_id IS NOT NULL)))),
    CONSTRAINT pb_line_tax_rate_range CHECK (((tax_rate_percent >= (0)::numeric) AND (tax_rate_percent <= (100)::numeric))),
    CONSTRAINT purchase_bill_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: purchase_bill_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.purchase_bill_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.purchase_bill_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: purchase_order; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_order (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    due_date date,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    subtotal_txn numeric(18,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    document_discount_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    rounding_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    subtotal_base numeric(18,4) NOT NULL,
    line_discount_base numeric(18,4) NOT NULL,
    document_discount_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    rounding_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    credited_txn numeric(18,4) NOT NULL,
    open_txn numeric(18,4) NOT NULL,
    open_base numeric(18,4) NOT NULL,
    status character varying(10) NOT NULL,
    notes text NOT NULL,
    internal_notes text NOT NULL,
    posted_at timestamp with time zone,
    submitted_at timestamp with time zone,
    approved_at timestamp with time zone,
    approval_reason character varying(255) NOT NULL,
    expected_date date,
    vendor_reference character varying(64) NOT NULL,
    delivery_address_text text NOT NULL,
    document_discount_kind character varying(8) NOT NULL,
    document_discount_value numeric(9,4) NOT NULL,
    approved_by_id bigint,
    buyer_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    payment_term_id bigint,
    posted_by_id bigint,
    updated_by_id bigint,
    vendor_id bigint NOT NULL,
    warehouse_id bigint NOT NULL,
    CONSTRAINT po_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT po_total_nonneg CHECK (((total_txn >= (0)::numeric) AND (total_base >= (0)::numeric)))
);


--
-- Name: purchase_order_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.purchase_order ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.purchase_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: purchase_order_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_order_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    allocated_document_discount_txn numeric(18,4) NOT NULL,
    tax_rate_percent numeric(9,4) NOT NULL,
    tax_is_inclusive boolean NOT NULL,
    tax_is_recoverable boolean NOT NULL,
    gross_txn numeric(18,4) NOT NULL,
    net_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    net_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    quantity_received numeric(18,4) NOT NULL,
    quantity_billed numeric(18,4) NOT NULL,
    quantity_cancelled numeric(18,4) NOT NULL,
    order_id bigint NOT NULL,
    product_id bigint NOT NULL,
    tax_code_id bigint,
    unit_id bigint NOT NULL,
    warehouse_id bigint,
    CONSTRAINT po_line_discount_range CHECK (((discount_percent >= (0)::numeric) AND (discount_percent <= (100)::numeric))),
    CONSTRAINT po_line_price_nonneg CHECK ((unit_price >= (0)::numeric)),
    CONSTRAINT po_line_progress_nonneg CHECK (((quantity_received >= (0)::numeric) AND (quantity_billed >= (0)::numeric))),
    CONSTRAINT po_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT purchase_order_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: purchase_order_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.purchase_order_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.purchase_order_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: purchase_return; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_return (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    status character varying(10) NOT NULL,
    reason text NOT NULL,
    total_cost_base numeric(18,4) NOT NULL,
    posted_at timestamp with time zone,
    created_by_id bigint,
    journal_entry_id bigint,
    original_bill_id bigint,
    original_receipt_id bigint,
    posted_by_id bigint,
    updated_by_id bigint,
    vendor_id bigint NOT NULL,
    warehouse_id bigint NOT NULL,
    CONSTRAINT purchase_return_has_source CHECK (((original_bill_id IS NOT NULL) OR (original_receipt_id IS NOT NULL))),
    CONSTRAINT purchase_return_reason_required CHECK ((NOT (reason = ''::text)))
);


--
-- Name: purchase_return_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.purchase_return ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.purchase_return_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: purchase_return_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_return_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    quantity numeric(18,4) NOT NULL,
    disposition character varying(16) NOT NULL,
    unit_cost numeric(18,4) NOT NULL,
    total_cost numeric(18,4) NOT NULL,
    note character varying(255) NOT NULL,
    bill_line_id bigint,
    product_id bigint NOT NULL,
    purchase_return_id bigint NOT NULL,
    receipt_line_id bigint,
    CONSTRAINT pr_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT purchase_return_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: purchase_return_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.purchase_return_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.purchase_return_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: refund; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.refund (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    direction character varying(12) NOT NULL,
    refund_date date NOT NULL,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    amount_txn numeric(18,4) NOT NULL,
    amount_base numeric(18,4) NOT NULL,
    reference character varying(64) NOT NULL,
    reason text NOT NULL,
    status character varying(10) NOT NULL,
    posted_at timestamp with time zone,
    approved_by_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    customer_id bigint,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    method_id bigint NOT NULL,
    money_account_id bigint NOT NULL,
    posted_by_id bigint,
    sales_credit_note_id bigint,
    source_payment_id bigint,
    updated_by_id bigint,
    vendor_id bigint,
    vendor_debit_note_id bigint,
    CONSTRAINT refund_amount_positive CHECK ((amount_txn > (0)::numeric)),
    CONSTRAINT refund_exactly_one_source CHECK ((num_nonnulls(sales_credit_note_id, vendor_debit_note_id, source_payment_id) = 1)),
    CONSTRAINT refund_party_matches_direction CHECK (((((direction)::text = 'TO_CUSTOMER'::text) AND (customer_id IS NOT NULL) AND (vendor_id IS NULL)) OR (((direction)::text = 'FROM_VENDOR'::text) AND (vendor_id IS NOT NULL) AND (customer_id IS NULL)))),
    CONSTRAINT refund_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'COMPLETED'::character varying, 'REVERSED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL))),
    CONSTRAINT refund_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT refund_reason_required CHECK ((NOT (reason = ''::text)))
);


--
-- Name: refund_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.refund ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.refund_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: role_profile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_profile (
    id bigint NOT NULL,
    description text NOT NULL,
    is_system boolean NOT NULL,
    can_post boolean NOT NULL,
    can_approve boolean NOT NULL,
    can_reverse boolean NOT NULL,
    can_close_period boolean NOT NULL,
    can_configure boolean NOT NULL,
    group_id integer NOT NULL
);


--
-- Name: role_profile_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.role_profile ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.role_profile_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_credit_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_credit_note (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    due_date date,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    subtotal_txn numeric(18,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    document_discount_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    rounding_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    subtotal_base numeric(18,4) NOT NULL,
    line_discount_base numeric(18,4) NOT NULL,
    document_discount_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    rounding_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    credited_txn numeric(18,4) NOT NULL,
    open_txn numeric(18,4) NOT NULL,
    open_base numeric(18,4) NOT NULL,
    status character varying(10) NOT NULL,
    notes text NOT NULL,
    internal_notes text NOT NULL,
    posted_at timestamp with time zone,
    submitted_at timestamp with time zone,
    approved_at timestamp with time zone,
    approval_reason character varying(255) NOT NULL,
    reason text NOT NULL,
    customer_name_snapshot character varying(200) NOT NULL,
    billing_address_text text NOT NULL,
    refunded_txn numeric(18,4) NOT NULL,
    is_reversed boolean NOT NULL,
    approved_by_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    customer_id bigint NOT NULL,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    posted_by_id bigint,
    updated_by_id bigint,
    original_invoice_id bigint,
    sales_return_id bigint,
    CONSTRAINT cn_open_is_derived CHECK ((open_txn = ((total_txn - allocated_txn) - refunded_txn))),
    CONSTRAINT cn_open_nonneg CHECK ((open_txn >= (0)::numeric)),
    CONSTRAINT cn_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying, 'REVERSED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL))),
    CONSTRAINT cn_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT cn_settlement_within_total CHECK (((allocated_txn >= (0)::numeric) AND (refunded_txn >= (0)::numeric) AND (allocated_txn <= total_txn) AND (refunded_txn <= total_txn))),
    CONSTRAINT cn_total_nonneg CHECK ((total_txn >= (0)::numeric))
);


--
-- Name: sales_credit_note_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_credit_note ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_credit_note_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_credit_note_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_credit_note_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    allocated_document_discount_txn numeric(18,4) NOT NULL,
    tax_rate_percent numeric(9,4) NOT NULL,
    tax_is_inclusive boolean NOT NULL,
    tax_is_recoverable boolean NOT NULL,
    gross_txn numeric(18,4) NOT NULL,
    net_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    net_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    credit_note_id bigint NOT NULL,
    product_id bigint,
    revenue_account_id bigint,
    tax_code_id bigint,
    unit_id bigint,
    invoice_line_id bigint,
    return_line_id bigint,
    CONSTRAINT cn_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT cn_line_tax_rate_range CHECK (((tax_rate_percent >= (0)::numeric) AND (tax_rate_percent <= (100)::numeric))),
    CONSTRAINT sales_credit_note_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: sales_credit_note_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_credit_note_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_credit_note_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_invoice; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_invoice (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    due_date date,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    subtotal_txn numeric(18,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    document_discount_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    rounding_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    subtotal_base numeric(18,4) NOT NULL,
    line_discount_base numeric(18,4) NOT NULL,
    document_discount_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    rounding_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    credited_txn numeric(18,4) NOT NULL,
    open_txn numeric(18,4) NOT NULL,
    open_base numeric(18,4) NOT NULL,
    status character varying(10) NOT NULL,
    notes text NOT NULL,
    internal_notes text NOT NULL,
    posted_at timestamp with time zone,
    submitted_at timestamp with time zone,
    approved_at timestamp with time zone,
    approval_reason character varying(255) NOT NULL,
    customer_reference character varying(64) NOT NULL,
    customer_name_snapshot character varying(200) NOT NULL,
    customer_tax_id_snapshot character varying(50) NOT NULL,
    billing_address_text text NOT NULL,
    shipping_address_text text NOT NULL,
    document_discount_kind character varying(8) NOT NULL,
    document_discount_value numeric(9,4) NOT NULL,
    is_reversed boolean NOT NULL,
    approved_by_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    customer_id bigint NOT NULL,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    payment_term_id bigint,
    posted_by_id bigint,
    receivable_account_id bigint,
    reversed_by_journal_id bigint,
    updated_by_id bigint,
    warehouse_id bigint,
    sales_order_id bigint,
    CONSTRAINT si_due_after_document_date CHECK (((due_date IS NULL) OR (due_date >= document_date))),
    CONSTRAINT si_open_is_derived CHECK ((open_txn = ((total_txn - allocated_txn) - credited_txn))),
    CONSTRAINT si_open_nonneg CHECK ((open_txn >= (0)::numeric)),
    CONSTRAINT si_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying, 'REVERSED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL))),
    CONSTRAINT si_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT si_settlement_within_total CHECK (((allocated_txn >= (0)::numeric) AND (credited_txn >= (0)::numeric) AND (allocated_txn <= total_txn) AND (credited_txn <= total_txn))),
    CONSTRAINT si_total_nonneg CHECK (((total_txn >= (0)::numeric) AND (total_base >= (0)::numeric)))
);


--
-- Name: sales_invoice_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_invoice ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_invoice_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_invoice_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_invoice_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    allocated_document_discount_txn numeric(18,4) NOT NULL,
    tax_rate_percent numeric(9,4) NOT NULL,
    tax_is_inclusive boolean NOT NULL,
    tax_is_recoverable boolean NOT NULL,
    gross_txn numeric(18,4) NOT NULL,
    net_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    net_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    product_sku_snapshot character varying(40) NOT NULL,
    quantity_returned numeric(18,4) NOT NULL,
    delivery_line_id bigint,
    invoice_id bigint NOT NULL,
    product_id bigint NOT NULL,
    revenue_account_id bigint,
    tax_code_id bigint,
    unit_id bigint NOT NULL,
    warehouse_id bigint,
    sales_order_line_id bigint,
    CONSTRAINT sales_invoice_line_line_no_check CHECK ((line_no >= 0)),
    CONSTRAINT si_line_discount_nonneg CHECK (((line_discount_txn >= (0)::numeric) AND (allocated_document_discount_txn >= (0)::numeric))),
    CONSTRAINT si_line_discount_within_gross CHECK ((line_discount_txn <= gross_txn)),
    CONSTRAINT si_line_price_nonneg CHECK ((unit_price >= (0)::numeric)),
    CONSTRAINT si_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT si_line_returned_within_invoiced CHECK (((quantity_returned >= (0)::numeric) AND (quantity_returned <= quantity))),
    CONSTRAINT si_line_tax_rate_range CHECK (((tax_rate_percent >= (0)::numeric) AND (tax_rate_percent <= (100)::numeric)))
);


--
-- Name: sales_invoice_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_invoice_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_invoice_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_order; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_order (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    due_date date,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    subtotal_txn numeric(18,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    document_discount_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    rounding_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    subtotal_base numeric(18,4) NOT NULL,
    line_discount_base numeric(18,4) NOT NULL,
    document_discount_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    rounding_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    credited_txn numeric(18,4) NOT NULL,
    open_txn numeric(18,4) NOT NULL,
    open_base numeric(18,4) NOT NULL,
    status character varying(10) NOT NULL,
    notes text NOT NULL,
    internal_notes text NOT NULL,
    posted_at timestamp with time zone,
    submitted_at timestamp with time zone,
    approved_at timestamp with time zone,
    approval_reason character varying(255) NOT NULL,
    expected_date date,
    customer_reference character varying(64) NOT NULL,
    billing_address_text text NOT NULL,
    shipping_address_text text NOT NULL,
    document_discount_kind character varying(8) NOT NULL,
    document_discount_value numeric(9,4) NOT NULL,
    approved_by_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    customer_id bigint NOT NULL,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    payment_term_id bigint,
    posted_by_id bigint,
    salesperson_id bigint,
    updated_by_id bigint,
    warehouse_id bigint NOT NULL,
    CONSTRAINT so_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT so_total_nonneg CHECK (((total_txn >= (0)::numeric) AND (total_base >= (0)::numeric)))
);


--
-- Name: sales_order_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_order ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_order_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_order_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    allocated_document_discount_txn numeric(18,4) NOT NULL,
    tax_rate_percent numeric(9,4) NOT NULL,
    tax_is_inclusive boolean NOT NULL,
    tax_is_recoverable boolean NOT NULL,
    gross_txn numeric(18,4) NOT NULL,
    net_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    net_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    quantity_delivered numeric(18,4) NOT NULL,
    quantity_invoiced numeric(18,4) NOT NULL,
    quantity_cancelled numeric(18,4) NOT NULL,
    order_id bigint NOT NULL,
    product_id bigint NOT NULL,
    tax_code_id bigint,
    unit_id bigint NOT NULL,
    warehouse_id bigint,
    CONSTRAINT sales_order_line_line_no_check CHECK ((line_no >= 0)),
    CONSTRAINT so_line_discount_range CHECK (((discount_percent >= (0)::numeric) AND (discount_percent <= (100)::numeric))),
    CONSTRAINT so_line_fulfilment_nonneg CHECK (((quantity_delivered >= (0)::numeric) AND (quantity_invoiced >= (0)::numeric))),
    CONSTRAINT so_line_price_nonneg CHECK ((unit_price >= (0)::numeric)),
    CONSTRAINT so_line_qty_positive CHECK ((quantity > (0)::numeric))
);


--
-- Name: sales_order_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_order_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_order_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_return; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_return (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    status character varying(10) NOT NULL,
    reason text NOT NULL,
    total_cost_base numeric(18,4) NOT NULL,
    posted_at timestamp with time zone,
    created_by_id bigint,
    customer_id bigint NOT NULL,
    journal_entry_id bigint,
    original_delivery_id bigint,
    original_invoice_id bigint,
    posted_by_id bigint,
    updated_by_id bigint,
    warehouse_id bigint NOT NULL,
    CONSTRAINT sales_return_has_source CHECK (((original_invoice_id IS NOT NULL) OR (original_delivery_id IS NOT NULL))),
    CONSTRAINT sales_return_reason_required CHECK ((NOT (reason = ''::text)))
);


--
-- Name: sales_return_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_return ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_return_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sales_return_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sales_return_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    quantity numeric(18,4) NOT NULL,
    disposition character varying(16) NOT NULL,
    unit_cost numeric(18,4) NOT NULL,
    total_cost numeric(18,4) NOT NULL,
    note character varying(255) NOT NULL,
    delivery_line_id bigint,
    invoice_line_id bigint,
    product_id bigint NOT NULL,
    sales_return_id bigint NOT NULL,
    CONSTRAINT sales_return_line_line_no_check CHECK ((line_no >= 0)),
    CONSTRAINT sr_line_qty_positive CHECK ((quantity > (0)::numeric))
);


--
-- Name: sales_return_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sales_return_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.sales_return_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stock_adjustment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_adjustment (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    status character varying(10) NOT NULL,
    narration text NOT NULL,
    attachment_reference character varying(255) NOT NULL,
    total_value_base numeric(18,4) NOT NULL,
    approved_at timestamp with time zone,
    posted_at timestamp with time zone,
    approved_by_id bigint,
    created_by_id bigint,
    journal_entry_id bigint,
    posted_by_id bigint,
    reason_id bigint NOT NULL,
    updated_by_id bigint,
    warehouse_id bigint NOT NULL,
    CONSTRAINT stock_adjustment_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'COMPLETED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL)))
);


--
-- Name: stock_adjustment_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.stock_adjustment ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stock_adjustment_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stock_adjustment_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_adjustment_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    quantity_delta numeric(18,4) NOT NULL,
    unit_cost numeric(18,6) NOT NULL,
    value_delta numeric(18,4) NOT NULL,
    note character varying(255) NOT NULL,
    adjustment_id bigint NOT NULL,
    product_id bigint NOT NULL,
    CONSTRAINT sa_line_qty_nonzero CHECK ((NOT (quantity_delta = (0)::numeric))),
    CONSTRAINT stock_adjustment_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: stock_adjustment_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.stock_adjustment_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stock_adjustment_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stock_balance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_balance (
    id bigint NOT NULL,
    quantity_on_hand numeric(18,4) NOT NULL,
    average_cost numeric(18,6) NOT NULL,
    total_value numeric(18,4) NOT NULL,
    quantity_reserved numeric(18,4) NOT NULL,
    last_movement_at timestamp with time zone,
    product_id bigint NOT NULL,
    warehouse_id bigint NOT NULL,
    CONSTRAINT stock_balance_avg_cost_nonneg CHECK ((average_cost >= (0)::numeric)),
    CONSTRAINT stock_balance_reserved_nonneg CHECK ((quantity_reserved >= (0)::numeric))
);


--
-- Name: stock_balance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.stock_balance ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stock_balance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stock_movement; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_movement (
    id bigint NOT NULL,
    movement_date date NOT NULL,
    movement_type character varying(20) NOT NULL,
    direction smallint NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_cost numeric(18,6) NOT NULL,
    total_cost numeric(18,4) NOT NULL,
    balance_quantity_after numeric(18,4) NOT NULL,
    balance_value_after numeric(18,4) NOT NULL,
    average_cost_after numeric(18,6) NOT NULL,
    source_object_id bigint,
    source_doc_type character varying(4) NOT NULL,
    source_doc_number character varying(32) NOT NULL,
    idempotency_key character varying(120) NOT NULL,
    notes character varying(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by_id bigint,
    journal_entry_id bigint,
    product_id bigint NOT NULL,
    reverses_id bigint,
    source_content_type_id integer,
    warehouse_id bigint NOT NULL,
    CONSTRAINT stock_movement_cost_nonneg CHECK (((unit_cost >= (0)::numeric) AND (total_cost >= (0)::numeric))),
    CONSTRAINT stock_movement_direction_matches_type CHECK (((((movement_type)::text = ANY ((ARRAY['GOODS_RECEIPT'::character varying, 'SALES_RETURN_IN'::character varying, 'TRANSFER_IN'::character varying, 'ADJUSTMENT_IN'::character varying, 'OPENING'::character varying])::text[])) AND (direction = 1)) OR (((movement_type)::text = ANY ((ARRAY['DELIVERY'::character varying, 'PURCHASE_RETURN_OUT'::character varying, 'TRANSFER_OUT'::character varying, 'ADJUSTMENT_OUT'::character varying, 'WRITE_OFF'::character varying])::text[])) AND (direction = '-1'::integer)))),
    CONSTRAINT stock_movement_direction_valid CHECK ((direction = ANY (ARRAY['-1'::integer, 1]))),
    CONSTRAINT stock_movement_has_source CHECK (((source_content_type_id IS NOT NULL) OR ((movement_type)::text = ANY ((ARRAY['ADJUSTMENT_IN'::character varying, 'ADJUSTMENT_OUT'::character varying, 'OPENING'::character varying])::text[])))),
    CONSTRAINT stock_movement_quantity_positive CHECK ((quantity > (0)::numeric))
);


--
-- Name: stock_movement_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.stock_movement ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stock_movement_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stock_transfer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_transfer (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    status character varying(10) NOT NULL,
    reason character varying(255) NOT NULL,
    notes text NOT NULL,
    total_cost_base numeric(18,4) NOT NULL,
    posted_at timestamp with time zone,
    created_by_id bigint,
    journal_entry_id bigint,
    posted_by_id bigint,
    updated_by_id bigint,
    from_warehouse_id bigint NOT NULL,
    to_warehouse_id bigint NOT NULL,
    CONSTRAINT stock_transfer_different_warehouses CHECK ((NOT (from_warehouse_id = to_warehouse_id)))
);


--
-- Name: stock_transfer_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.stock_transfer ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stock_transfer_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: stock_transfer_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_transfer_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_cost numeric(18,6) NOT NULL,
    total_cost numeric(18,4) NOT NULL,
    product_id bigint NOT NULL,
    transfer_id bigint NOT NULL,
    CONSTRAINT st_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT stock_transfer_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: stock_transfer_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.stock_transfer_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.stock_transfer_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: tax_code; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_code (
    id bigint NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    rate_percent numeric(9,4) NOT NULL,
    is_inclusive boolean NOT NULL,
    is_recoverable boolean NOT NULL,
    treatment character varying(12) NOT NULL,
    applies_to character varying(8) NOT NULL,
    is_active boolean NOT NULL,
    input_tax_account_id bigint,
    output_tax_account_id bigint,
    CONSTRAINT tax_code_nonstandard_is_zero CHECK ((((treatment)::text = 'STANDARD'::text) OR (rate_percent = (0)::numeric))),
    CONSTRAINT tax_code_rate_range CHECK (((rate_percent >= (0)::numeric) AND (rate_percent <= (100)::numeric)))
);


--
-- Name: tax_code_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.tax_code ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.tax_code_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: unit_of_measure; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.unit_of_measure (
    id bigint NOT NULL,
    code character varying(10) NOT NULL,
    name character varying(50) NOT NULL,
    decimal_places smallint NOT NULL,
    ratio_to_base numeric(18,4) NOT NULL,
    is_active boolean NOT NULL,
    base_unit_id bigint,
    CONSTRAINT unit_of_measure_decimal_places_check CHECK ((decimal_places >= 0)),
    CONSTRAINT uom_not_self_base CHECK (((base_unit_id IS NULL) OR (NOT ((base_unit_id = id) AND (base_unit_id IS NOT NULL))))),
    CONSTRAINT uom_ratio_positive CHECK ((ratio_to_base > (0)::numeric))
);


--
-- Name: unit_of_measure_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.unit_of_measure ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.unit_of_measure_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: v_control_account_balance; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_control_account_balance AS
 SELECT a.control_type,
    a.id AS account_id,
    a.code AS account_code,
    a.name AS account_name,
    sum((jl.debit_base - jl.credit_base)) AS balance_base
   FROM ((public.journal_line jl
     JOIN public.journal_entry je ON ((je.id = jl.entry_id)))
     JOIN public.account a ON ((a.id = jl.account_id)))
  WHERE a.is_control
  GROUP BY a.control_type, a.id, a.code, a.name;


--
-- Name: v_customer_unapplied_credit; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_customer_unapplied_credit AS
 SELECT p.customer_id,
    p.currency_id AS currency_code,
    'PAYMENT'::character varying(20) AS source_type,
    p.id AS source_id,
    p.number AS document_number,
    p.payment_date AS source_date,
    p.unallocated_txn AS available_txn
   FROM public.payment p
  WHERE (((p.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])) AND ((p.direction)::text = 'RECEIPT'::text) AND (p.is_reversed = false) AND (p.unallocated_txn > (0)::numeric))
UNION ALL
 SELECT cn.customer_id,
    cn.currency_id AS currency_code,
    'SALES_CREDIT_NOTE'::character varying(20) AS source_type,
    cn.id AS source_id,
    cn.number AS document_number,
    cn.document_date AS source_date,
    cn.open_txn AS available_txn
   FROM public.sales_credit_note cn
  WHERE (((cn.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])) AND (cn.open_txn > (0)::numeric));


--
-- Name: v_general_ledger; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_general_ledger AS
 SELECT jl.id AS journal_line_id,
    je.id AS journal_entry_id,
    je.number AS entry_number,
    je.entry_date,
    je.journal_type,
    je.status AS entry_status,
    je.is_manual,
    je.source_doc_type,
    je.source_doc_number,
    je.fiscal_period_id,
    a.id AS account_id,
    a.code AS account_code,
    a.name AS account_name,
    a.account_type,
    a.subtype AS account_subtype,
    a.is_contra,
    jl.description,
    jl.debit_base,
    jl.credit_base,
    (jl.debit_base - jl.credit_base) AS signed_amount_base,
    jl.currency_id AS currency_code,
    jl.debit_txn,
    jl.credit_txn,
    jl.customer_id,
    jl.vendor_id,
    jl.product_id,
    jl.warehouse_id,
    jl.tax_code_id,
    jl.money_account_id
   FROM ((public.journal_line jl
     JOIN public.journal_entry je ON ((je.id = jl.entry_id)))
     JOIN public.account a ON ((a.id = jl.account_id)));


--
-- Name: VIEW v_general_ledger; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_general_ledger IS 'RPT-004. One row per journal line with its account and source document.';


--
-- Name: warehouse; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warehouse (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(100) NOT NULL,
    address text NOT NULL,
    allow_negative_stock boolean NOT NULL,
    is_active boolean NOT NULL,
    created_by_id bigint,
    inventory_account_id bigint,
    manager_id bigint,
    updated_by_id bigint
);


--
-- Name: v_inventory_valuation; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_inventory_valuation AS
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
   FROM (((public.stock_balance sb
     JOIN public.product p ON ((p.id = sb.product_id)))
     JOIN public.warehouse w ON ((w.id = sb.warehouse_id)))
     LEFT JOIN public.product_category pc ON ((pc.id = p.category_id)))
  WHERE p.is_inventory;


--
-- Name: v_money_account_activity; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_money_account_activity AS
 SELECT ma.id AS money_account_id,
    ma.code AS money_account_code,
    ma.name AS money_account_name,
    ma.account_type,
    ma.currency_id AS currency_code,
    je.entry_date,
    je.number AS entry_number,
    je.journal_type,
    jl.description,
    jl.debit_base AS money_in_base,
    jl.credit_base AS money_out_base,
    (jl.debit_base - jl.credit_base) AS net_base,
    jl.customer_id,
    jl.vendor_id
   FROM ((public.journal_line jl
     JOIN public.journal_entry je ON ((je.id = jl.entry_id)))
     JOIN public.money_account ma ON ((ma.id = jl.money_account_id)));


--
-- Name: vendor; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    legal_name character varying(200) NOT NULL,
    tax_id character varying(50) NOT NULL,
    email character varying(254) NOT NULL,
    phone character varying(50) NOT NULL,
    website character varying(200) NOT NULL,
    notes text NOT NULL,
    is_active boolean NOT NULL,
    deactivated_at timestamp with time zone,
    advance_account_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    default_expense_account_id bigint,
    default_tax_code_id bigint,
    payable_account_id bigint,
    payment_term_id bigint,
    updated_by_id bigint
);


--
-- Name: v_purchase_bill_open; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_purchase_bill_open AS
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
   FROM (public.purchase_bill pb
     JOIN public.vendor v ON ((v.id = pb.vendor_id)))
  WHERE (((pb.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])) AND (pb.open_txn > (0)::numeric));


--
-- Name: v_sales_invoice_open; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_sales_invoice_open AS
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
   FROM (public.sales_invoice si
     JOIN public.customer c ON ((c.id = si.customer_id)))
  WHERE (((si.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])) AND (si.open_txn > (0)::numeric));


--
-- Name: v_subledger_reconciliation; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_subledger_reconciliation AS
 SELECT 'AR'::character varying(18) AS control_type,
    cab.account_code,
    cab.balance_base AS gl_balance_base,
    COALESCE(sub.total, (0)::numeric) AS subledger_balance_base,
    (cab.balance_base - COALESCE(sub.total, (0)::numeric)) AS difference_base
   FROM (public.v_control_account_balance cab
     LEFT JOIN ( SELECT sum(sales_invoice.open_base) AS total
           FROM public.sales_invoice
          WHERE ((sales_invoice.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[]))) sub ON (true))
  WHERE ((cab.control_type)::text = 'AR'::text)
UNION ALL
 SELECT 'AP'::character varying(18) AS control_type,
    cab.account_code,
    cab.balance_base AS gl_balance_base,
    (- COALESCE(sub.total, (0)::numeric)) AS subledger_balance_base,
    (cab.balance_base + COALESCE(sub.total, (0)::numeric)) AS difference_base
   FROM (public.v_control_account_balance cab
     LEFT JOIN ( SELECT sum(purchase_bill.open_base) AS total
           FROM public.purchase_bill
          WHERE ((purchase_bill.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[]))) sub ON (true))
  WHERE ((cab.control_type)::text = 'AP'::text)
UNION ALL
 SELECT 'INVENTORY'::character varying(18) AS control_type,
    cab.account_code,
    cab.balance_base AS gl_balance_base,
    COALESCE(sub.total, (0)::numeric) AS subledger_balance_base,
    (cab.balance_base - COALESCE(sub.total, (0)::numeric)) AS difference_base
   FROM (public.v_control_account_balance cab
     LEFT JOIN ( SELECT sum(stock_balance.total_value) AS total
           FROM public.stock_balance) sub ON (true))
  WHERE ((cab.control_type)::text = 'INVENTORY'::text);


--
-- Name: v_tax_transaction; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_tax_transaction AS
 SELECT 'SALES'::character varying(10) AS tax_side,
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
   FROM (((public.sales_invoice_line sil
     JOIN public.sales_invoice si ON (((si.id = sil.invoice_id) AND ((si.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])))))
     JOIN public.customer c ON ((c.id = si.customer_id)))
     LEFT JOIN public.tax_code tc ON ((tc.id = sil.tax_code_id)))
UNION ALL
 SELECT 'PURCHASE'::character varying(10) AS tax_side,
    pb.document_date,
    pb.number AS document_number,
    pb.vendor_id AS party_id,
    v.name AS party_name,
    pbl.tax_code_id,
    tc.code AS tax_code,
    tc.treatment AS tax_treatment,
    pbl.tax_rate_percent,
    pbl.tax_is_inclusive,
    pbl.tax_is_recoverable,
    pbl.taxable_base_base AS taxable_base,
    pbl.tax_base AS tax_amount_base
   FROM (((public.purchase_bill_line pbl
     JOIN public.purchase_bill pb ON (((pb.id = pbl.bill_id) AND ((pb.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])))))
     JOIN public.vendor v ON ((v.id = pb.vendor_id)))
     LEFT JOIN public.tax_code tc ON ((tc.id = pbl.tax_code_id)));


--
-- Name: vendor_debit_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_debit_note (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    number character varying(32) NOT NULL,
    document_date date NOT NULL,
    due_date date,
    posting_date date NOT NULL,
    exchange_rate numeric(18,8) NOT NULL,
    subtotal_txn numeric(18,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    document_discount_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    rounding_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    subtotal_base numeric(18,4) NOT NULL,
    line_discount_base numeric(18,4) NOT NULL,
    document_discount_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    rounding_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    allocated_txn numeric(18,4) NOT NULL,
    credited_txn numeric(18,4) NOT NULL,
    open_txn numeric(18,4) NOT NULL,
    open_base numeric(18,4) NOT NULL,
    status character varying(10) NOT NULL,
    notes text NOT NULL,
    internal_notes text NOT NULL,
    posted_at timestamp with time zone,
    submitted_at timestamp with time zone,
    approved_at timestamp with time zone,
    approval_reason character varying(255) NOT NULL,
    vendor_credit_reference character varying(64) NOT NULL,
    reason text NOT NULL,
    vendor_name_snapshot character varying(200) NOT NULL,
    refunded_txn numeric(18,4) NOT NULL,
    is_reversed boolean NOT NULL,
    approved_by_id bigint,
    created_by_id bigint,
    currency_id character varying(3) NOT NULL,
    fiscal_period_id bigint,
    journal_entry_id bigint,
    original_bill_id bigint,
    posted_by_id bigint,
    purchase_return_id bigint,
    updated_by_id bigint,
    vendor_id bigint NOT NULL,
    CONSTRAINT dbn_open_is_derived CHECK ((open_txn = ((total_txn - allocated_txn) - refunded_txn))),
    CONSTRAINT dbn_open_nonneg CHECK ((open_txn >= (0)::numeric)),
    CONSTRAINT dbn_posted_has_journal CHECK (((NOT ((status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying, 'REVERSED'::character varying])::text[]))) OR (journal_entry_id IS NOT NULL))),
    CONSTRAINT dbn_rate_positive CHECK ((exchange_rate > (0)::numeric)),
    CONSTRAINT dbn_settlement_within_total CHECK (((allocated_txn >= (0)::numeric) AND (refunded_txn >= (0)::numeric) AND (allocated_txn <= total_txn) AND (refunded_txn <= total_txn))),
    CONSTRAINT dbn_total_nonneg CHECK ((total_txn >= (0)::numeric))
);


--
-- Name: v_vendor_unapplied_credit; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_vendor_unapplied_credit AS
 SELECT p.vendor_id,
    p.currency_id AS currency_code,
    'PAYMENT'::character varying(20) AS source_type,
    p.id AS source_id,
    p.number AS document_number,
    p.payment_date AS source_date,
    p.unallocated_txn AS available_txn
   FROM public.payment p
  WHERE (((p.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])) AND ((p.direction)::text = 'PAYMENT'::text) AND (p.is_reversed = false) AND (p.unallocated_txn > (0)::numeric))
UNION ALL
 SELECT dn.vendor_id,
    dn.currency_id AS currency_code,
    'VENDOR_DEBIT_NOTE'::character varying(20) AS source_type,
    dn.id AS source_id,
    dn.number AS document_number,
    dn.document_date AS source_date,
    dn.open_txn AS available_txn
   FROM public.vendor_debit_note dn
  WHERE (((dn.status)::text = ANY ((ARRAY['POSTED'::character varying, 'PARTIAL'::character varying, 'COMPLETED'::character varying])::text[])) AND (dn.open_txn > (0)::numeric));


--
-- Name: vendor_debit_note_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.vendor_debit_note ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.vendor_debit_note_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: vendor_debit_note_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_debit_note_line (
    id bigint NOT NULL,
    line_no smallint NOT NULL,
    description character varying(255) NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    discount_percent numeric(9,4) NOT NULL,
    line_discount_txn numeric(18,4) NOT NULL,
    allocated_document_discount_txn numeric(18,4) NOT NULL,
    tax_rate_percent numeric(9,4) NOT NULL,
    tax_is_inclusive boolean NOT NULL,
    tax_is_recoverable boolean NOT NULL,
    gross_txn numeric(18,4) NOT NULL,
    net_txn numeric(18,4) NOT NULL,
    taxable_base_txn numeric(18,4) NOT NULL,
    tax_txn numeric(18,4) NOT NULL,
    total_txn numeric(18,4) NOT NULL,
    net_base numeric(18,4) NOT NULL,
    taxable_base_base numeric(18,4) NOT NULL,
    tax_base numeric(18,4) NOT NULL,
    total_base numeric(18,4) NOT NULL,
    is_stock_line boolean NOT NULL,
    bill_line_id bigint,
    debit_note_id bigint NOT NULL,
    expense_account_id bigint,
    product_id bigint,
    return_line_id bigint,
    tax_code_id bigint,
    unit_id bigint,
    CONSTRAINT dbn_line_qty_positive CHECK ((quantity > (0)::numeric)),
    CONSTRAINT dbn_line_tax_rate_range CHECK (((tax_rate_percent >= (0)::numeric) AND (tax_rate_percent <= (100)::numeric))),
    CONSTRAINT vendor_debit_note_line_line_no_check CHECK ((line_no >= 0))
);


--
-- Name: vendor_debit_note_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.vendor_debit_note_line ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.vendor_debit_note_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: vendor_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.vendor ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.vendor_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: warehouse_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.warehouse ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.warehouse_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: account_mapping account_mapping_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_mapping
    ADD CONSTRAINT account_mapping_key_key UNIQUE (key);


--
-- Name: account_mapping account_mapping_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_mapping
    ADD CONSTRAINT account_mapping_pkey PRIMARY KEY (id);


--
-- Name: account account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_pkey PRIMARY KEY (id);


--
-- Name: adjustment_reason adjustment_reason_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjustment_reason
    ADD CONSTRAINT adjustment_reason_code_key UNIQUE (code);


--
-- Name: adjustment_reason adjustment_reason_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjustment_reason
    ADD CONSTRAINT adjustment_reason_pkey PRIMARY KEY (id);


--
-- Name: app_user app_user_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_email_key UNIQUE (email);


--
-- Name: app_user_groups app_user_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_groups
    ADD CONSTRAINT app_user_groups_pkey PRIMARY KEY (id);


--
-- Name: app_user_groups app_user_groups_user_id_group_id_73b8e940_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_groups
    ADD CONSTRAINT app_user_groups_user_id_group_id_73b8e940_uniq UNIQUE (user_id, group_id);


--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);


--
-- Name: app_user_user_permissions app_user_user_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_user_permissions
    ADD CONSTRAINT app_user_user_permissions_pkey PRIMARY KEY (id);


--
-- Name: app_user_user_permissions app_user_user_permissions_user_id_permission_id_7c8316ce_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_user_permissions
    ADD CONSTRAINT app_user_user_permissions_user_id_permission_id_7c8316ce_uniq UNIQUE (user_id, permission_id);


--
-- Name: app_user app_user_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_username_key UNIQUE (username);


--
-- Name: audit_event audit_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_event
    ADD CONSTRAINT audit_event_pkey PRIMARY KEY (id);


--
-- Name: auth_group auth_group_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group
    ADD CONSTRAINT auth_group_name_key UNIQUE (name);


--
-- Name: auth_group_permissions auth_group_permissions_group_id_permission_id_0cd325b0_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_group_id_permission_id_0cd325b0_uniq UNIQUE (group_id, permission_id);


--
-- Name: auth_group_permissions auth_group_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_pkey PRIMARY KEY (id);


--
-- Name: auth_group auth_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group
    ADD CONSTRAINT auth_group_pkey PRIMARY KEY (id);


--
-- Name: auth_permission auth_permission_content_type_id_codename_01ab375a_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_content_type_id_codename_01ab375a_uniq UNIQUE (content_type_id, codename);


--
-- Name: auth_permission auth_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_pkey PRIMARY KEY (id);


--
-- Name: sales_credit_note_line cn_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT cn_line_unique_no UNIQUE (credit_note_id, line_no);


--
-- Name: company company_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_pkey PRIMARY KEY (id);


--
-- Name: company company_singleton; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_singleton UNIQUE (singleton);


--
-- Name: currency currency_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.currency
    ADD CONSTRAINT currency_pkey PRIMARY KEY (code);


--
-- Name: customer customer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_pkey PRIMARY KEY (id);


--
-- Name: vendor_debit_note_line dbn_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT dbn_line_unique_no UNIQUE (debit_note_id, line_no);


--
-- Name: delivery_note_line delivery_note_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note_line
    ADD CONSTRAINT delivery_note_line_pkey PRIMARY KEY (id);


--
-- Name: delivery_note delivery_note_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_number_key UNIQUE (number);


--
-- Name: delivery_note delivery_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_pkey PRIMARY KEY (id);


--
-- Name: django_admin_log django_admin_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_pkey PRIMARY KEY (id);


--
-- Name: django_content_type django_content_type_app_label_model_76bd3d3b_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_content_type
    ADD CONSTRAINT django_content_type_app_label_model_76bd3d3b_uniq UNIQUE (app_label, model);


--
-- Name: django_content_type django_content_type_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_content_type
    ADD CONSTRAINT django_content_type_pkey PRIMARY KEY (id);


--
-- Name: django_migrations django_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_migrations
    ADD CONSTRAINT django_migrations_pkey PRIMARY KEY (id);


--
-- Name: django_session django_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_session
    ADD CONSTRAINT django_session_pkey PRIMARY KEY (session_key);


--
-- Name: delivery_note_line dn_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note_line
    ADD CONSTRAINT dn_line_unique_no UNIQUE (delivery_id, line_no);


--
-- Name: document_sequence document_sequence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_sequence
    ADD CONSTRAINT document_sequence_pkey PRIMARY KEY (id);


--
-- Name: document_sequence document_sequence_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_sequence
    ADD CONSTRAINT document_sequence_unique UNIQUE (document_type, series);


--
-- Name: exchange_rate exchange_rate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rate
    ADD CONSTRAINT exchange_rate_pkey PRIMARY KEY (id);


--
-- Name: exchange_rate exchange_rate_unique_per_day; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rate
    ADD CONSTRAINT exchange_rate_unique_per_day UNIQUE (currency_id, rate_date);


--
-- Name: fiscal_period fiscal_period_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_period
    ADD CONSTRAINT fiscal_period_no_overlap EXCLUDE USING gist (fiscal_year_id WITH =, daterange(start_date, end_date, '[]'::text) WITH &&);


--
-- Name: fiscal_period fiscal_period_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_period
    ADD CONSTRAINT fiscal_period_pkey PRIMARY KEY (id);


--
-- Name: fiscal_period fiscal_period_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_period
    ADD CONSTRAINT fiscal_period_unique_no UNIQUE (fiscal_year_id, period_no);


--
-- Name: fiscal_year fiscal_year_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_year
    ADD CONSTRAINT fiscal_year_code_key UNIQUE (code);


--
-- Name: fiscal_year fiscal_year_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_year
    ADD CONSTRAINT fiscal_year_no_overlap EXCLUDE USING gist (daterange(start_date, end_date, '[]'::text) WITH &&);


--
-- Name: fiscal_year fiscal_year_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_year
    ADD CONSTRAINT fiscal_year_pkey PRIMARY KEY (id);


--
-- Name: goods_receipt_line goods_receipt_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt_line
    ADD CONSTRAINT goods_receipt_line_pkey PRIMARY KEY (id);


--
-- Name: goods_receipt goods_receipt_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_number_key UNIQUE (number);


--
-- Name: goods_receipt goods_receipt_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_pkey PRIMARY KEY (id);


--
-- Name: goods_receipt_line gr_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt_line
    ADD CONSTRAINT gr_line_unique_no UNIQUE (receipt_id, line_no);


--
-- Name: journal_entry journal_entry_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: journal_entry journal_entry_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_number_key UNIQUE (number);


--
-- Name: journal_entry journal_entry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_pkey PRIMARY KEY (id);


--
-- Name: journal_entry journal_entry_reverses_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_reverses_id_key UNIQUE (reverses_id);


--
-- Name: journal_line journal_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_pkey PRIMARY KEY (id);


--
-- Name: journal_line journal_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_unique_no UNIQUE (entry_id, line_no);


--
-- Name: money_account money_account_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_code_key UNIQUE (code);


--
-- Name: money_account money_account_gl_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_gl_unique UNIQUE (gl_account_id);


--
-- Name: money_account money_account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_pkey PRIMARY KEY (id);


--
-- Name: opening_balance_batch opening_balance_batch_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opening_balance_batch
    ADD CONSTRAINT opening_balance_batch_code_key UNIQUE (code);


--
-- Name: opening_balance_batch opening_balance_batch_journal_entry_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opening_balance_batch
    ADD CONSTRAINT opening_balance_batch_journal_entry_id_key UNIQUE (journal_entry_id);


--
-- Name: opening_balance_batch opening_balance_batch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opening_balance_batch
    ADD CONSTRAINT opening_balance_batch_pkey PRIMARY KEY (id);


--
-- Name: party_address party_address_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.party_address
    ADD CONSTRAINT party_address_pkey PRIMARY KEY (id);


--
-- Name: party_contact party_contact_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.party_contact
    ADD CONSTRAINT party_contact_pkey PRIMARY KEY (id);


--
-- Name: payment_allocation payment_allocation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_pkey PRIMARY KEY (id);


--
-- Name: payment_method payment_method_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_method
    ADD CONSTRAINT payment_method_code_key UNIQUE (code);


--
-- Name: payment_method payment_method_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_method
    ADD CONSTRAINT payment_method_pkey PRIMARY KEY (id);


--
-- Name: payment payment_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_number_key UNIQUE (number);


--
-- Name: payment payment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_pkey PRIMARY KEY (id);


--
-- Name: payment_term payment_term_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_term
    ADD CONSTRAINT payment_term_code_key UNIQUE (code);


--
-- Name: payment_term payment_term_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_term
    ADD CONSTRAINT payment_term_pkey PRIMARY KEY (id);


--
-- Name: purchase_bill_line pb_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT pb_line_unique_no UNIQUE (bill_id, line_no);


--
-- Name: purchase_bill pb_vendor_invoice_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT pb_vendor_invoice_unique UNIQUE (vendor_id, vendor_invoice_number);


--
-- Name: purchase_order_line po_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT po_line_unique_no UNIQUE (order_id, line_no);


--
-- Name: posting_link posting_link_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.posting_link
    ADD CONSTRAINT posting_link_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: posting_link posting_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.posting_link
    ADD CONSTRAINT posting_link_pkey PRIMARY KEY (id);


--
-- Name: purchase_return_line pr_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return_line
    ADD CONSTRAINT pr_line_unique_no UNIQUE (purchase_return_id, line_no);


--
-- Name: product_category product_category_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_category
    ADD CONSTRAINT product_category_code_key UNIQUE (code);


--
-- Name: product_category product_category_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_category
    ADD CONSTRAINT product_category_pkey PRIMARY KEY (id);


--
-- Name: product product_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_pkey PRIMARY KEY (id);


--
-- Name: product_price product_price_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_price
    ADD CONSTRAINT product_price_pkey PRIMARY KEY (id);


--
-- Name: product_price product_price_unique_point; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_price
    ADD CONSTRAINT product_price_unique_point UNIQUE (product_id, kind, currency_id, min_quantity, valid_from);


--
-- Name: product product_sku_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_sku_key UNIQUE (sku);


--
-- Name: purchase_bill_line purchase_bill_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_pkey PRIMARY KEY (id);


--
-- Name: purchase_bill purchase_bill_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_number_key UNIQUE (number);


--
-- Name: purchase_bill purchase_bill_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_pkey PRIMARY KEY (id);


--
-- Name: purchase_order_line purchase_order_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT purchase_order_line_pkey PRIMARY KEY (id);


--
-- Name: purchase_order purchase_order_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_number_key UNIQUE (number);


--
-- Name: purchase_order purchase_order_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_pkey PRIMARY KEY (id);


--
-- Name: purchase_return_line purchase_return_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return_line
    ADD CONSTRAINT purchase_return_line_pkey PRIMARY KEY (id);


--
-- Name: purchase_return purchase_return_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_number_key UNIQUE (number);


--
-- Name: purchase_return purchase_return_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_pkey PRIMARY KEY (id);


--
-- Name: refund refund_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_number_key UNIQUE (number);


--
-- Name: refund refund_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_pkey PRIMARY KEY (id);


--
-- Name: role_profile role_profile_group_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_profile
    ADD CONSTRAINT role_profile_group_id_key UNIQUE (group_id);


--
-- Name: role_profile role_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_profile
    ADD CONSTRAINT role_profile_pkey PRIMARY KEY (id);


--
-- Name: stock_adjustment_line sa_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment_line
    ADD CONSTRAINT sa_line_unique_no UNIQUE (adjustment_id, line_no);


--
-- Name: sales_credit_note_line sales_credit_note_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_line_pkey PRIMARY KEY (id);


--
-- Name: sales_credit_note sales_credit_note_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_number_key UNIQUE (number);


--
-- Name: sales_credit_note sales_credit_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_pkey PRIMARY KEY (id);


--
-- Name: sales_invoice_line sales_invoice_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_pkey PRIMARY KEY (id);


--
-- Name: sales_invoice sales_invoice_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_number_key UNIQUE (number);


--
-- Name: sales_invoice sales_invoice_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_pkey PRIMARY KEY (id);


--
-- Name: sales_order_line sales_order_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT sales_order_line_pkey PRIMARY KEY (id);


--
-- Name: sales_order sales_order_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_number_key UNIQUE (number);


--
-- Name: sales_order sales_order_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_pkey PRIMARY KEY (id);


--
-- Name: sales_return_line sales_return_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return_line
    ADD CONSTRAINT sales_return_line_pkey PRIMARY KEY (id);


--
-- Name: sales_return sales_return_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_number_key UNIQUE (number);


--
-- Name: sales_return sales_return_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_pkey PRIMARY KEY (id);


--
-- Name: sales_invoice_line si_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT si_line_unique_no UNIQUE (invoice_id, line_no);


--
-- Name: sales_order_line so_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT so_line_unique_no UNIQUE (order_id, line_no);


--
-- Name: sales_return_line sr_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return_line
    ADD CONSTRAINT sr_line_unique_no UNIQUE (sales_return_id, line_no);


--
-- Name: stock_transfer_line st_line_unique_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer_line
    ADD CONSTRAINT st_line_unique_no UNIQUE (transfer_id, line_no);


--
-- Name: stock_adjustment_line stock_adjustment_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment_line
    ADD CONSTRAINT stock_adjustment_line_pkey PRIMARY KEY (id);


--
-- Name: stock_adjustment stock_adjustment_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_number_key UNIQUE (number);


--
-- Name: stock_adjustment stock_adjustment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_pkey PRIMARY KEY (id);


--
-- Name: stock_balance stock_balance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_balance
    ADD CONSTRAINT stock_balance_pkey PRIMARY KEY (id);


--
-- Name: stock_balance stock_balance_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_balance
    ADD CONSTRAINT stock_balance_unique UNIQUE (product_id, warehouse_id);


--
-- Name: stock_movement stock_movement_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: stock_movement stock_movement_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_pkey PRIMARY KEY (id);


--
-- Name: stock_movement stock_movement_reverses_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_reverses_id_key UNIQUE (reverses_id);


--
-- Name: stock_transfer_line stock_transfer_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer_line
    ADD CONSTRAINT stock_transfer_line_pkey PRIMARY KEY (id);


--
-- Name: stock_transfer stock_transfer_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_number_key UNIQUE (number);


--
-- Name: stock_transfer stock_transfer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_pkey PRIMARY KEY (id);


--
-- Name: tax_code tax_code_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_code
    ADD CONSTRAINT tax_code_code_key UNIQUE (code);


--
-- Name: tax_code tax_code_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_code
    ADD CONSTRAINT tax_code_pkey PRIMARY KEY (id);


--
-- Name: unit_of_measure unit_of_measure_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.unit_of_measure
    ADD CONSTRAINT unit_of_measure_code_key UNIQUE (code);


--
-- Name: unit_of_measure unit_of_measure_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.unit_of_measure
    ADD CONSTRAINT unit_of_measure_pkey PRIMARY KEY (id);


--
-- Name: vendor_debit_note_line vendor_debit_note_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_line_pkey PRIMARY KEY (id);


--
-- Name: vendor_debit_note vendor_debit_note_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_number_key UNIQUE (number);


--
-- Name: vendor_debit_note vendor_debit_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_pkey PRIMARY KEY (id);


--
-- Name: vendor vendor_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_pkey PRIMARY KEY (id);


--
-- Name: warehouse warehouse_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse
    ADD CONSTRAINT warehouse_code_key UNIQUE (code);


--
-- Name: warehouse warehouse_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse
    ADD CONSTRAINT warehouse_pkey PRIMARY KEY (id);


--
-- Name: account_code_unique_ci; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX account_code_unique_ci ON public.account USING btree (upper((code)::text));


--
-- Name: account_created_by_id_9c481c1a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_created_by_id_9c481c1a ON public.account USING btree (created_by_id);


--
-- Name: account_currency_id_061676bb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_currency_id_061676bb ON public.account USING btree (currency_id);


--
-- Name: account_currency_id_061676bb_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_currency_id_061676bb_like ON public.account USING btree (currency_id varchar_pattern_ops);


--
-- Name: account_mapping_account_id_e13a06f4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_mapping_account_id_e13a06f4 ON public.account_mapping USING btree (account_id);


--
-- Name: account_mapping_created_by_id_c985f2e3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_mapping_created_by_id_c985f2e3 ON public.account_mapping USING btree (created_by_id);


--
-- Name: account_mapping_key_ca7354b1_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_mapping_key_ca7354b1_like ON public.account_mapping USING btree (key varchar_pattern_ops);


--
-- Name: account_mapping_updated_by_id_4e106374; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_mapping_updated_by_id_4e106374 ON public.account_mapping USING btree (updated_by_id);


--
-- Name: account_parent_id_5f9163e3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_parent_id_5f9163e3 ON public.account USING btree (parent_id);


--
-- Name: account_updated_by_id_cc9a8d93; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_updated_by_id_cc9a8d93 ON public.account USING btree (updated_by_id);


--
-- Name: address_one_default_per_customer_type; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX address_one_default_per_customer_type ON public.party_address USING btree (customer_id, address_type) WHERE ((customer_id IS NOT NULL) AND is_default);


--
-- Name: address_one_default_per_vendor_type; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX address_one_default_per_vendor_type ON public.party_address USING btree (vendor_id, address_type) WHERE (is_default AND (vendor_id IS NOT NULL));


--
-- Name: adjustment_reason_code_1441a051_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX adjustment_reason_code_1441a051_like ON public.adjustment_reason USING btree (code varchar_pattern_ops);


--
-- Name: adjustment_reason_gain_loss_account_id_faaf2ac8; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX adjustment_reason_gain_loss_account_id_faaf2ac8 ON public.adjustment_reason USING btree (gain_loss_account_id);


--
-- Name: allocation_unique_credit_invoice; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX allocation_unique_credit_invoice ON public.payment_allocation USING btree (sales_credit_note_id, sales_invoice_id) WHERE ((NOT is_reversed) AND (sales_credit_note_id IS NOT NULL));


--
-- Name: allocation_unique_debit_bill; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX allocation_unique_debit_bill ON public.payment_allocation USING btree (vendor_debit_note_id, purchase_bill_id) WHERE ((NOT is_reversed) AND (vendor_debit_note_id IS NOT NULL));


--
-- Name: allocation_unique_payment_bill; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX allocation_unique_payment_bill ON public.payment_allocation USING btree (payment_id, purchase_bill_id) WHERE ((NOT is_reversed) AND (payment_id IS NOT NULL) AND (purchase_bill_id IS NOT NULL));


--
-- Name: allocation_unique_payment_invoice; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX allocation_unique_payment_invoice ON public.payment_allocation USING btree (payment_id, sales_invoice_id) WHERE ((NOT is_reversed) AND (payment_id IS NOT NULL) AND (sales_invoice_id IS NOT NULL));


--
-- Name: app_user_default_warehouse_id_43fb9731; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_default_warehouse_id_43fb9731 ON public.app_user USING btree (default_warehouse_id);


--
-- Name: app_user_email_efde8896_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_email_efde8896_like ON public.app_user USING btree (email varchar_pattern_ops);


--
-- Name: app_user_groups_group_id_e774d92c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_groups_group_id_e774d92c ON public.app_user_groups USING btree (group_id);


--
-- Name: app_user_groups_user_id_e6f878f6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_groups_user_id_e6f878f6 ON public.app_user_groups USING btree (user_id);


--
-- Name: app_user_user_permissions_permission_id_4ef8e133; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_user_permissions_permission_id_4ef8e133 ON public.app_user_user_permissions USING btree (permission_id);


--
-- Name: app_user_user_permissions_user_id_24780b52; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_user_permissions_user_id_24780b52 ON public.app_user_user_permissions USING btree (user_id);


--
-- Name: app_user_username_9d6296ff_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX app_user_username_9d6296ff_like ON public.app_user USING btree (username varchar_pattern_ops);


--
-- Name: audit_event_content_type_id_72949bd3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_event_content_type_id_72949bd3 ON public.audit_event USING btree (content_type_id);


--
-- Name: audit_event_correlation_id_df230e48; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_event_correlation_id_df230e48 ON public.audit_event USING btree (correlation_id);


--
-- Name: audit_event_occurred_at_daece6a4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_event_occurred_at_daece6a4 ON public.audit_event USING btree (occurred_at);


--
-- Name: audit_event_user_id_05228691; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_event_user_id_05228691 ON public.audit_event USING btree (user_id);


--
-- Name: auth_group_name_a6ea08ec_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_name_a6ea08ec_like ON public.auth_group USING btree (name varchar_pattern_ops);


--
-- Name: auth_group_permissions_group_id_b120cbf9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_permissions_group_id_b120cbf9 ON public.auth_group_permissions USING btree (group_id);


--
-- Name: auth_group_permissions_permission_id_84c5c92e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_permissions_permission_id_84c5c92e ON public.auth_group_permissions USING btree (permission_id);


--
-- Name: auth_permission_content_type_id_2f476e4b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_permission_content_type_id_2f476e4b ON public.auth_permission USING btree (content_type_id);


--
-- Name: company_base_currency_id_dc725f40; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX company_base_currency_id_dc725f40 ON public.company USING btree (base_currency_id);


--
-- Name: company_base_currency_id_dc725f40_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX company_base_currency_id_dc725f40_like ON public.company USING btree (base_currency_id varchar_pattern_ops);


--
-- Name: company_created_by_id_6d8b6b91; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX company_created_by_id_6d8b6b91 ON public.company USING btree (created_by_id);


--
-- Name: company_updated_by_id_feec3d9b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX company_updated_by_id_feec3d9b ON public.company USING btree (updated_by_id);


--
-- Name: contact_one_primary_per_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX contact_one_primary_per_customer ON public.party_contact USING btree (customer_id) WHERE ((customer_id IS NOT NULL) AND is_primary);


--
-- Name: contact_one_primary_per_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX contact_one_primary_per_vendor ON public.party_contact USING btree (vendor_id) WHERE (is_primary AND (vendor_id IS NOT NULL));


--
-- Name: currency_code_5628b440_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX currency_code_5628b440_like ON public.currency USING btree (code varchar_pattern_ops);


--
-- Name: currency_single_base; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX currency_single_base ON public.currency USING btree (is_base) WHERE is_base;


--
-- Name: customer_advance_account_id_86ea7fb2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_advance_account_id_86ea7fb2 ON public.customer USING btree (advance_account_id);


--
-- Name: customer_code_unique_ci; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX customer_code_unique_ci ON public.customer USING btree (upper((code)::text));


--
-- Name: customer_created_by_id_6fc5ba53; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_created_by_id_6fc5ba53 ON public.customer USING btree (created_by_id);


--
-- Name: customer_currency_id_1890d9ef; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_currency_id_1890d9ef ON public.customer USING btree (currency_id);


--
-- Name: customer_currency_id_1890d9ef_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_currency_id_1890d9ef_like ON public.customer USING btree (currency_id varchar_pattern_ops);


--
-- Name: customer_default_tax_code_id_8e287fcf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_default_tax_code_id_8e287fcf ON public.customer USING btree (default_tax_code_id);


--
-- Name: customer_default_warehouse_id_bec5c600; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_default_warehouse_id_bec5c600 ON public.customer USING btree (default_warehouse_id);


--
-- Name: customer_payment_term_id_66e0c0da; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_payment_term_id_66e0c0da ON public.customer USING btree (payment_term_id);


--
-- Name: customer_receivable_account_id_2b154cc9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_receivable_account_id_2b154cc9 ON public.customer USING btree (receivable_account_id);


--
-- Name: customer_salesperson_id_702bb6ad; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_salesperson_id_702bb6ad ON public.customer USING btree (salesperson_id);


--
-- Name: customer_tax_id_529af153; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_tax_id_529af153 ON public.customer USING btree (tax_id);


--
-- Name: customer_tax_id_529af153_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_tax_id_529af153_like ON public.customer USING btree (tax_id varchar_pattern_ops);


--
-- Name: customer_updated_by_id_0592dd0a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customer_updated_by_id_0592dd0a ON public.customer USING btree (updated_by_id);


--
-- Name: delivery_note_created_by_id_46f25ae5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_created_by_id_46f25ae5 ON public.delivery_note USING btree (created_by_id);


--
-- Name: delivery_note_customer_id_4f138130; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_customer_id_4f138130 ON public.delivery_note USING btree (customer_id);


--
-- Name: delivery_note_delivered_by_id_26592bb2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_delivered_by_id_26592bb2 ON public.delivery_note USING btree (delivered_by_id);


--
-- Name: delivery_note_document_date_c7afa1d7; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_document_date_c7afa1d7 ON public.delivery_note USING btree (document_date);


--
-- Name: delivery_note_journal_entry_id_baabbbba; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_journal_entry_id_baabbbba ON public.delivery_note USING btree (journal_entry_id);


--
-- Name: delivery_note_line_delivery_id_8408ade4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_line_delivery_id_8408ade4 ON public.delivery_note_line USING btree (delivery_id);


--
-- Name: delivery_note_line_product_id_d8c4a71b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_line_product_id_d8c4a71b ON public.delivery_note_line USING btree (product_id);


--
-- Name: delivery_note_line_sales_order_line_id_c02a6e8b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_line_sales_order_line_id_c02a6e8b ON public.delivery_note_line USING btree (sales_order_line_id);


--
-- Name: delivery_note_line_unit_id_981f6abf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_line_unit_id_981f6abf ON public.delivery_note_line USING btree (unit_id);


--
-- Name: delivery_note_number_21081340_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_number_21081340_like ON public.delivery_note USING btree (number varchar_pattern_ops);


--
-- Name: delivery_note_posted_by_id_daf4c829; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_posted_by_id_daf4c829 ON public.delivery_note USING btree (posted_by_id);


--
-- Name: delivery_note_sales_order_id_484d58f5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_sales_order_id_484d58f5 ON public.delivery_note USING btree (sales_order_id);


--
-- Name: delivery_note_updated_by_id_e84b6007; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_updated_by_id_e84b6007 ON public.delivery_note USING btree (updated_by_id);


--
-- Name: delivery_note_warehouse_id_34823705; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX delivery_note_warehouse_id_34823705 ON public.delivery_note USING btree (warehouse_id);


--
-- Name: django_admin_log_content_type_id_c4bce8eb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_admin_log_content_type_id_c4bce8eb ON public.django_admin_log USING btree (content_type_id);


--
-- Name: django_admin_log_user_id_c564eba6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_admin_log_user_id_c564eba6 ON public.django_admin_log USING btree (user_id);


--
-- Name: django_session_expire_date_a5c62663; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_session_expire_date_a5c62663 ON public.django_session USING btree (expire_date);


--
-- Name: django_session_session_key_c0390e0f_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_session_session_key_c0390e0f_like ON public.django_session USING btree (session_key varchar_pattern_ops);


--
-- Name: exchange_rate_currency_id_9e574674; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX exchange_rate_currency_id_9e574674 ON public.exchange_rate USING btree (currency_id);


--
-- Name: exchange_rate_currency_id_9e574674_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX exchange_rate_currency_id_9e574674_like ON public.exchange_rate USING btree (currency_id varchar_pattern_ops);


--
-- Name: fiscal_period_closed_by_id_e168f4f2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fiscal_period_closed_by_id_e168f4f2 ON public.fiscal_period USING btree (closed_by_id);


--
-- Name: fiscal_period_fiscal_year_id_9f495ae5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fiscal_period_fiscal_year_id_9f495ae5 ON public.fiscal_period USING btree (fiscal_year_id);


--
-- Name: fiscal_period_reopened_by_id_67b69bb7; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fiscal_period_reopened_by_id_67b69bb7 ON public.fiscal_period USING btree (reopened_by_id);


--
-- Name: fiscal_year_code_184131e0_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fiscal_year_code_184131e0_like ON public.fiscal_year USING btree (code varchar_pattern_ops);


--
-- Name: goods_receipt_created_by_id_60396763; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_created_by_id_60396763 ON public.goods_receipt USING btree (created_by_id);


--
-- Name: goods_receipt_document_date_fbee992b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_document_date_fbee992b ON public.goods_receipt USING btree (document_date);


--
-- Name: goods_receipt_journal_entry_id_992fb570; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_journal_entry_id_992fb570 ON public.goods_receipt USING btree (journal_entry_id);


--
-- Name: goods_receipt_line_product_id_a498892c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_line_product_id_a498892c ON public.goods_receipt_line USING btree (product_id);


--
-- Name: goods_receipt_line_purchase_order_line_id_665e1d3f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_line_purchase_order_line_id_665e1d3f ON public.goods_receipt_line USING btree (purchase_order_line_id);


--
-- Name: goods_receipt_line_receipt_id_a0a4f990; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_line_receipt_id_a0a4f990 ON public.goods_receipt_line USING btree (receipt_id);


--
-- Name: goods_receipt_line_unit_id_6c10ded0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_line_unit_id_6c10ded0 ON public.goods_receipt_line USING btree (unit_id);


--
-- Name: goods_receipt_number_cc13169f_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_number_cc13169f_like ON public.goods_receipt USING btree (number varchar_pattern_ops);


--
-- Name: goods_receipt_posted_by_id_94723af2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_posted_by_id_94723af2 ON public.goods_receipt USING btree (posted_by_id);


--
-- Name: goods_receipt_purchase_order_id_aedf9d62; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_purchase_order_id_aedf9d62 ON public.goods_receipt USING btree (purchase_order_id);


--
-- Name: goods_receipt_received_by_id_91c25e9c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_received_by_id_91c25e9c ON public.goods_receipt USING btree (received_by_id);


--
-- Name: goods_receipt_updated_by_id_6d8eba7f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_updated_by_id_6d8eba7f ON public.goods_receipt USING btree (updated_by_id);


--
-- Name: goods_receipt_vendor_id_aba03709; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_vendor_id_aba03709 ON public.goods_receipt USING btree (vendor_id);


--
-- Name: goods_receipt_warehouse_id_f305c6f5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goods_receipt_warehouse_id_f305c6f5 ON public.goods_receipt USING btree (warehouse_id);


--
-- Name: ix_account_selectable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_account_selectable ON public.account USING btree (is_postable, is_active);


--
-- Name: ix_account_type_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_account_type_code ON public.account USING btree (account_type, code);


--
-- Name: ix_alloc_bill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_alloc_bill ON public.payment_allocation USING btree (purchase_bill_id);


--
-- Name: ix_alloc_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_alloc_customer ON public.payment_allocation USING btree (customer_id, allocation_date);


--
-- Name: ix_alloc_invoice; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_alloc_invoice ON public.payment_allocation USING btree (sales_invoice_id);


--
-- Name: ix_alloc_payment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_alloc_payment ON public.payment_allocation USING btree (payment_id);


--
-- Name: ix_alloc_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_alloc_vendor ON public.payment_allocation USING btree (vendor_id, allocation_date);


--
-- Name: ix_audit_action_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_action_time ON public.audit_event USING btree (action, occurred_at DESC);


--
-- Name: ix_audit_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_target ON public.audit_event USING btree (content_type_id, object_id);


--
-- Name: ix_audit_user_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_user_time ON public.audit_event USING btree (user_id, occurred_at DESC);


--
-- Name: ix_cn_customer_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_customer_date ON public.sales_credit_note USING btree (customer_id, document_date DESC);


--
-- Name: ix_cn_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_line_product ON public.sales_credit_note_line USING btree (product_id);


--
-- Name: ix_cn_open_credit; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_open_credit ON public.sales_credit_note USING btree (customer_id) WHERE (open_txn > (0)::numeric);


--
-- Name: ix_customer_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_name ON public.customer USING btree (name);


--
-- Name: ix_customer_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_name_trgm ON public.customer USING gin (upper((name)::text) public.gin_trgm_ops);


--
-- Name: ix_customer_selectable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_selectable ON public.customer USING btree (is_active, code);


--
-- Name: ix_customer_taxid_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_customer_taxid_trgm ON public.customer USING gin (upper((tax_id)::text) public.gin_trgm_ops);


--
-- Name: ix_dbn_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dbn_line_product ON public.vendor_debit_note_line USING btree (product_id);


--
-- Name: ix_dbn_open_credit; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dbn_open_credit ON public.vendor_debit_note USING btree (vendor_id) WHERE (open_txn > (0)::numeric);


--
-- Name: ix_dbn_vendor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dbn_vendor_date ON public.vendor_debit_note USING btree (vendor_id, document_date DESC);


--
-- Name: ix_dn_customer_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dn_customer_date ON public.delivery_note USING btree (customer_id, document_date DESC);


--
-- Name: ix_dn_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dn_line_product ON public.delivery_note_line USING btree (product_id);


--
-- Name: ix_dn_status_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dn_status_date ON public.delivery_note USING btree (status, document_date DESC);


--
-- Name: ix_fx_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fx_lookup ON public.exchange_rate USING btree (currency_id, rate_date DESC);


--
-- Name: ix_gr_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gr_line_product ON public.goods_receipt_line USING btree (product_id);


--
-- Name: ix_gr_status_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gr_status_date ON public.goods_receipt USING btree (status, document_date DESC);


--
-- Name: ix_gr_vendor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gr_vendor_date ON public.goods_receipt USING btree (vendor_id, document_date DESC);


--
-- Name: ix_je_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_je_date ON public.journal_entry USING btree (entry_date);


--
-- Name: ix_je_doc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_je_doc ON public.journal_entry USING btree (source_doc_type, source_doc_number);


--
-- Name: ix_je_period_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_je_period_date ON public.journal_entry USING btree (fiscal_period_id, entry_date);


--
-- Name: ix_je_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_je_source ON public.journal_entry USING btree (source_content_type_id, source_object_id);


--
-- Name: ix_je_type_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_je_type_date ON public.journal_entry USING btree (journal_type, entry_date);


--
-- Name: ix_jl_account_entry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jl_account_entry ON public.journal_line USING btree (account_id, entry_id);


--
-- Name: ix_jl_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jl_customer ON public.journal_line USING btree (customer_id);


--
-- Name: ix_jl_money_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jl_money_account ON public.journal_line USING btree (money_account_id);


--
-- Name: ix_jl_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jl_product ON public.journal_line USING btree (product_id);


--
-- Name: ix_jl_tax_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jl_tax_code ON public.journal_line USING btree (tax_code_id);


--
-- Name: ix_jl_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jl_vendor ON public.journal_line USING btree (vendor_id);


--
-- Name: ix_movement_card; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_movement_card ON public.stock_movement USING btree (product_id, warehouse_id, movement_date);


--
-- Name: ix_movement_journal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_movement_journal ON public.stock_movement USING btree (journal_entry_id);


--
-- Name: ix_movement_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_movement_source ON public.stock_movement USING btree (source_content_type_id, source_object_id);


--
-- Name: ix_movement_type_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_movement_type_date ON public.stock_movement USING btree (movement_type, movement_date);


--
-- Name: ix_pay_customer_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pay_customer_date ON public.payment USING btree (customer_id, payment_date DESC);


--
-- Name: ix_pay_money_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pay_money_date ON public.payment USING btree (money_account_id, payment_date DESC);


--
-- Name: ix_pay_status_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pay_status_date ON public.payment USING btree (status, payment_date DESC);


--
-- Name: ix_pay_unapplied; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pay_unapplied ON public.payment USING btree (customer_id, vendor_id) WHERE (unallocated_txn > (0)::numeric);


--
-- Name: ix_pay_vendor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pay_vendor_date ON public.payment USING btree (vendor_id, payment_date DESC);


--
-- Name: ix_pb_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pb_line_product ON public.purchase_bill_line USING btree (product_id);


--
-- Name: ix_pb_line_tax; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pb_line_tax ON public.purchase_bill_line USING btree (tax_code_id);


--
-- Name: ix_pb_open_items; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pb_open_items ON public.purchase_bill USING btree (vendor_id, due_date) WHERE (open_txn > (0)::numeric);


--
-- Name: ix_pb_posting_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pb_posting_date ON public.purchase_bill USING btree (posting_date);


--
-- Name: ix_pb_status_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pb_status_due ON public.purchase_bill USING btree (status, due_date);


--
-- Name: ix_pb_vendor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pb_vendor_date ON public.purchase_bill USING btree (vendor_id, document_date DESC);


--
-- Name: ix_period_daterange; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_period_daterange ON public.fiscal_period USING btree (start_date, end_date);


--
-- Name: ix_period_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_period_status ON public.fiscal_period USING btree (status);


--
-- Name: ix_po_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_po_line_product ON public.purchase_order_line USING btree (product_id);


--
-- Name: ix_po_status_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_po_status_date ON public.purchase_order USING btree (status, document_date DESC);


--
-- Name: ix_po_vendor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_po_vendor_date ON public.purchase_order USING btree (vendor_id, document_date DESC);


--
-- Name: ix_posting_link_doc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_posting_link_doc ON public.posting_link USING btree (source_doc_type, source_doc_number);


--
-- Name: ix_posting_link_journal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_posting_link_journal ON public.posting_link USING btree (journal_entry_id);


--
-- Name: ix_posting_link_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_posting_link_source ON public.posting_link USING btree (source_content_type_id, source_object_id);


--
-- Name: ix_posting_link_stock; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_posting_link_stock ON public.posting_link USING btree (stock_movement_id);


--
-- Name: ix_pr_vendor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pr_vendor_date ON public.purchase_return USING btree (vendor_id, document_date DESC);


--
-- Name: ix_product_barcode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_barcode ON public.product USING btree (barcode);


--
-- Name: ix_product_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_category ON public.product USING btree (category_id, is_active);


--
-- Name: ix_product_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_name ON public.product USING btree (name);


--
-- Name: ix_product_price_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_price_lookup ON public.product_price USING btree (product_id, kind, currency_id, valid_from DESC);


--
-- Name: ix_product_selectable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_selectable ON public.product USING btree (is_active, sku);


--
-- Name: ix_refund_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_refund_customer ON public.refund USING btree (customer_id, refund_date DESC);


--
-- Name: ix_refund_money; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_refund_money ON public.refund USING btree (money_account_id, refund_date DESC);


--
-- Name: ix_refund_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_refund_vendor ON public.refund USING btree (vendor_id, refund_date DESC);


--
-- Name: ix_si_currency_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_currency_status ON public.sales_invoice USING btree (currency_id, status);


--
-- Name: ix_si_customer_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_customer_date ON public.sales_invoice USING btree (customer_id, document_date DESC);


--
-- Name: ix_si_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_line_product ON public.sales_invoice_line USING btree (product_id);


--
-- Name: ix_si_line_tax; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_line_tax ON public.sales_invoice_line USING btree (tax_code_id);


--
-- Name: ix_si_open_items; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_open_items ON public.sales_invoice USING btree (customer_id, due_date) WHERE (open_txn > (0)::numeric);


--
-- Name: ix_si_posting_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_posting_date ON public.sales_invoice USING btree (posting_date);


--
-- Name: ix_si_status_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_si_status_due ON public.sales_invoice USING btree (status, due_date);


--
-- Name: ix_so_customer_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_so_customer_date ON public.sales_order USING btree (customer_id, document_date DESC);


--
-- Name: ix_so_customer_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_so_customer_ref ON public.sales_order USING btree (customer_reference);


--
-- Name: ix_so_line_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_so_line_product ON public.sales_order_line USING btree (product_id);


--
-- Name: ix_so_status_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_so_status_date ON public.sales_order USING btree (status, document_date DESC);


--
-- Name: ix_sr_customer_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sr_customer_date ON public.sales_return USING btree (customer_id, document_date DESC);


--
-- Name: ix_stock_balance_wh; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stock_balance_wh ON public.stock_balance USING btree (warehouse_id, product_id);


--
-- Name: ix_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_active ON public.app_user USING btree (is_active);


--
-- Name: ix_vendor_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_name ON public.vendor USING btree (name);


--
-- Name: ix_vendor_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_name_trgm ON public.vendor USING gin (upper((name)::text) public.gin_trgm_ops);


--
-- Name: ix_vendor_selectable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_selectable ON public.vendor USING btree (is_active, code);


--
-- Name: ix_vendor_taxid_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vendor_taxid_trgm ON public.vendor USING gin (upper((tax_id)::text) public.gin_trgm_ops);


--
-- Name: journal_entry_created_by_id_0ae13889; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_created_by_id_0ae13889 ON public.journal_entry USING btree (created_by_id);


--
-- Name: journal_entry_currency_id_fd3ea6bd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_currency_id_fd3ea6bd ON public.journal_entry USING btree (currency_id);


--
-- Name: journal_entry_currency_id_fd3ea6bd_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_currency_id_fd3ea6bd_like ON public.journal_entry USING btree (currency_id varchar_pattern_ops);


--
-- Name: journal_entry_fiscal_period_id_72e01511; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_fiscal_period_id_72e01511 ON public.journal_entry USING btree (fiscal_period_id);


--
-- Name: journal_entry_idempotency_key_b0cde079_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_idempotency_key_b0cde079_like ON public.journal_entry USING btree (idempotency_key varchar_pattern_ops);


--
-- Name: journal_entry_number_604c3f83_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_number_604c3f83_like ON public.journal_entry USING btree (number varchar_pattern_ops);


--
-- Name: journal_entry_posted_by_id_84fe2fa1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_posted_by_id_84fe2fa1 ON public.journal_entry USING btree (posted_by_id);


--
-- Name: journal_entry_source_content_type_id_b908d235; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_source_content_type_id_b908d235 ON public.journal_entry USING btree (source_content_type_id);


--
-- Name: journal_entry_updated_by_id_09c7c3a4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_entry_updated_by_id_09c7c3a4 ON public.journal_entry USING btree (updated_by_id);


--
-- Name: journal_line_account_id_f42b4cb5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_account_id_f42b4cb5 ON public.journal_line USING btree (account_id);


--
-- Name: journal_line_currency_id_a27db2f6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_currency_id_a27db2f6 ON public.journal_line USING btree (currency_id);


--
-- Name: journal_line_currency_id_a27db2f6_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_currency_id_a27db2f6_like ON public.journal_line USING btree (currency_id varchar_pattern_ops);


--
-- Name: journal_line_customer_id_907dd46d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_customer_id_907dd46d ON public.journal_line USING btree (customer_id);


--
-- Name: journal_line_entry_id_90f26900; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_entry_id_90f26900 ON public.journal_line USING btree (entry_id);


--
-- Name: journal_line_money_account_id_ddb8b77b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_money_account_id_ddb8b77b ON public.journal_line USING btree (money_account_id);


--
-- Name: journal_line_product_id_8654113b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_product_id_8654113b ON public.journal_line USING btree (product_id);


--
-- Name: journal_line_tax_code_id_579a0c95; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_tax_code_id_579a0c95 ON public.journal_line USING btree (tax_code_id);


--
-- Name: journal_line_vendor_id_c2b290e5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_vendor_id_c2b290e5 ON public.journal_line USING btree (vendor_id);


--
-- Name: journal_line_warehouse_id_4f101383; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX journal_line_warehouse_id_4f101383 ON public.journal_line USING btree (warehouse_id);


--
-- Name: money_account_code_3aac6894_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX money_account_code_3aac6894_like ON public.money_account USING btree (code varchar_pattern_ops);


--
-- Name: money_account_created_by_id_72951b7e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX money_account_created_by_id_72951b7e ON public.money_account USING btree (created_by_id);


--
-- Name: money_account_currency_id_f2ea24b3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX money_account_currency_id_f2ea24b3 ON public.money_account USING btree (currency_id);


--
-- Name: money_account_currency_id_f2ea24b3_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX money_account_currency_id_f2ea24b3_like ON public.money_account USING btree (currency_id varchar_pattern_ops);


--
-- Name: money_account_gl_account_id_bc425b4d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX money_account_gl_account_id_bc425b4d ON public.money_account USING btree (gl_account_id);


--
-- Name: money_account_updated_by_id_0715c7d4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX money_account_updated_by_id_0715c7d4 ON public.money_account USING btree (updated_by_id);


--
-- Name: opening_balance_batch_code_208e5fad_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX opening_balance_batch_code_208e5fad_like ON public.opening_balance_batch USING btree (code varchar_pattern_ops);


--
-- Name: opening_balance_batch_created_by_id_c37090e1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX opening_balance_batch_created_by_id_c37090e1 ON public.opening_balance_batch USING btree (created_by_id);


--
-- Name: opening_balance_batch_updated_by_id_f22fbbe0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX opening_balance_batch_updated_by_id_f22fbbe0 ON public.opening_balance_batch USING btree (updated_by_id);


--
-- Name: party_address_customer_id_a0b58139; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX party_address_customer_id_a0b58139 ON public.party_address USING btree (customer_id);


--
-- Name: party_address_vendor_id_f6eb3eac; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX party_address_vendor_id_f6eb3eac ON public.party_address USING btree (vendor_id);


--
-- Name: party_contact_customer_id_6ff47470; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX party_contact_customer_id_6ff47470 ON public.party_contact USING btree (customer_id);


--
-- Name: party_contact_vendor_id_de937ded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX party_contact_vendor_id_de937ded ON public.party_contact USING btree (vendor_id);


--
-- Name: payment_allocation_allocation_date_94f70b29; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_allocation_date_94f70b29 ON public.payment_allocation USING btree (allocation_date);


--
-- Name: payment_allocation_created_by_id_ed2804c0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_created_by_id_ed2804c0 ON public.payment_allocation USING btree (created_by_id);


--
-- Name: payment_allocation_customer_id_a35451e8; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_customer_id_a35451e8 ON public.payment_allocation USING btree (customer_id);


--
-- Name: payment_allocation_fx_journal_entry_id_7bb33a22; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_fx_journal_entry_id_7bb33a22 ON public.payment_allocation USING btree (fx_journal_entry_id);


--
-- Name: payment_allocation_journal_entry_id_38826ae6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_journal_entry_id_38826ae6 ON public.payment_allocation USING btree (journal_entry_id);


--
-- Name: payment_allocation_payment_id_11a2910b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_payment_id_11a2910b ON public.payment_allocation USING btree (payment_id);


--
-- Name: payment_allocation_purchase_bill_id_659248eb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_purchase_bill_id_659248eb ON public.payment_allocation USING btree (purchase_bill_id);


--
-- Name: payment_allocation_sales_credit_note_id_dfc35563; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_sales_credit_note_id_dfc35563 ON public.payment_allocation USING btree (sales_credit_note_id);


--
-- Name: payment_allocation_sales_invoice_id_830ef636; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_sales_invoice_id_830ef636 ON public.payment_allocation USING btree (sales_invoice_id);


--
-- Name: payment_allocation_updated_by_id_42ac0fdf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_updated_by_id_42ac0fdf ON public.payment_allocation USING btree (updated_by_id);


--
-- Name: payment_allocation_vendor_debit_note_id_af99f886; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_vendor_debit_note_id_af99f886 ON public.payment_allocation USING btree (vendor_debit_note_id);


--
-- Name: payment_allocation_vendor_id_82bb9be1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_allocation_vendor_id_82bb9be1 ON public.payment_allocation USING btree (vendor_id);


--
-- Name: payment_created_by_id_0a1fb28a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_created_by_id_0a1fb28a ON public.payment USING btree (created_by_id);


--
-- Name: payment_currency_id_d55dcf3a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_currency_id_d55dcf3a ON public.payment USING btree (currency_id);


--
-- Name: payment_currency_id_d55dcf3a_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_currency_id_d55dcf3a_like ON public.payment USING btree (currency_id varchar_pattern_ops);


--
-- Name: payment_customer_id_cfa68abe; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_customer_id_cfa68abe ON public.payment USING btree (customer_id);


--
-- Name: payment_fiscal_period_id_c1765b7a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_fiscal_period_id_c1765b7a ON public.payment USING btree (fiscal_period_id);


--
-- Name: payment_journal_entry_id_755d7b67; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_journal_entry_id_755d7b67 ON public.payment USING btree (journal_entry_id);


--
-- Name: payment_method_code_e4a3f43d_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_method_code_e4a3f43d_like ON public.payment_method USING btree (code varchar_pattern_ops);


--
-- Name: payment_method_default_money_account_id_562588dc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_method_default_money_account_id_562588dc ON public.payment_method USING btree (default_money_account_id);


--
-- Name: payment_method_id_e2ad63d9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_method_id_e2ad63d9 ON public.payment USING btree (method_id);


--
-- Name: payment_money_account_id_92d00b15; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_money_account_id_92d00b15 ON public.payment USING btree (money_account_id);


--
-- Name: payment_number_02c3721d_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_number_02c3721d_like ON public.payment USING btree (number varchar_pattern_ops);


--
-- Name: payment_payment_date_17fc0f98; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_payment_date_17fc0f98 ON public.payment USING btree (payment_date);


--
-- Name: payment_posted_by_id_7f760a84; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_posted_by_id_7f760a84 ON public.payment USING btree (posted_by_id);


--
-- Name: payment_reversal_journal_id_43d69685; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_reversal_journal_id_43d69685 ON public.payment USING btree (reversal_journal_id);


--
-- Name: payment_reversed_by_id_8baf6c2d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_reversed_by_id_8baf6c2d ON public.payment USING btree (reversed_by_id);


--
-- Name: payment_term_code_585ca5ca_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_term_code_585ca5ca_like ON public.payment_term USING btree (code varchar_pattern_ops);


--
-- Name: payment_updated_by_id_5c13b476; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_updated_by_id_5c13b476 ON public.payment USING btree (updated_by_id);


--
-- Name: payment_vendor_id_292bb080; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payment_vendor_id_292bb080 ON public.payment USING btree (vendor_id);


--
-- Name: posting_link_idempotency_key_dd819856_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX posting_link_idempotency_key_dd819856_like ON public.posting_link USING btree (idempotency_key varchar_pattern_ops);


--
-- Name: posting_link_journal_entry_id_0ffc3384; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX posting_link_journal_entry_id_0ffc3384 ON public.posting_link USING btree (journal_entry_id);


--
-- Name: posting_link_source_content_type_id_109464e6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX posting_link_source_content_type_id_109464e6 ON public.posting_link USING btree (source_content_type_id);


--
-- Name: posting_link_stock_movement_id_7148d9fb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX posting_link_stock_movement_id_7148d9fb ON public.posting_link USING btree (stock_movement_id);


--
-- Name: product_category_code_5ada3694_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_category_code_5ada3694_like ON public.product_category USING btree (code varchar_pattern_ops);


--
-- Name: product_category_cogs_account_id_3b739406; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_category_cogs_account_id_3b739406 ON public.product_category USING btree (cogs_account_id);


--
-- Name: product_category_id_640030a0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_category_id_640030a0 ON public.product USING btree (category_id);


--
-- Name: product_category_inventory_account_id_0047a328; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_category_inventory_account_id_0047a328 ON public.product_category USING btree (inventory_account_id);


--
-- Name: product_category_parent_id_f6860923; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_category_parent_id_f6860923 ON public.product_category USING btree (parent_id);


--
-- Name: product_category_revenue_account_id_271774c9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_category_revenue_account_id_271774c9 ON public.product_category USING btree (revenue_account_id);


--
-- Name: product_cogs_account_id_4f1075ec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_cogs_account_id_4f1075ec ON public.product USING btree (cogs_account_id);


--
-- Name: product_created_by_id_0baf418a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_created_by_id_0baf418a ON public.product USING btree (created_by_id);


--
-- Name: product_default_purchase_tax_code_id_d75e0f83; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_default_purchase_tax_code_id_d75e0f83 ON public.product USING btree (default_purchase_tax_code_id);


--
-- Name: product_default_sales_tax_code_id_caf14afd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_default_sales_tax_code_id_caf14afd ON public.product USING btree (default_sales_tax_code_id);


--
-- Name: product_expense_account_id_497bbca9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_expense_account_id_497bbca9 ON public.product USING btree (expense_account_id);


--
-- Name: product_inventory_account_id_573d8fa2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_inventory_account_id_573d8fa2 ON public.product USING btree (inventory_account_id);


--
-- Name: product_preferred_vendor_id_62e57074; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_preferred_vendor_id_62e57074 ON public.product USING btree (preferred_vendor_id);


--
-- Name: product_price_currency_id_0e71f85c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_price_currency_id_0e71f85c ON public.product_price USING btree (currency_id);


--
-- Name: product_price_currency_id_0e71f85c_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_price_currency_id_0e71f85c_like ON public.product_price USING btree (currency_id varchar_pattern_ops);


--
-- Name: product_price_product_id_831af532; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_price_product_id_831af532 ON public.product_price USING btree (product_id);


--
-- Name: product_revenue_account_id_775acf59; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_revenue_account_id_775acf59 ON public.product USING btree (revenue_account_id);


--
-- Name: product_sku_1e15f61e_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_sku_1e15f61e_like ON public.product USING btree (sku varchar_pattern_ops);


--
-- Name: product_unit_id_9b7abe5c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_unit_id_9b7abe5c ON public.product USING btree (unit_id);


--
-- Name: product_updated_by_id_f384b7dc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX product_updated_by_id_f384b7dc ON public.product USING btree (updated_by_id);


--
-- Name: purchase_bill_approved_by_id_6598b560; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_approved_by_id_6598b560 ON public.purchase_bill USING btree (approved_by_id);


--
-- Name: purchase_bill_created_by_id_8233ddb0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_created_by_id_8233ddb0 ON public.purchase_bill USING btree (created_by_id);


--
-- Name: purchase_bill_currency_id_fe5f18d1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_currency_id_fe5f18d1 ON public.purchase_bill USING btree (currency_id);


--
-- Name: purchase_bill_currency_id_fe5f18d1_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_currency_id_fe5f18d1_like ON public.purchase_bill USING btree (currency_id varchar_pattern_ops);


--
-- Name: purchase_bill_document_date_8d4d3a60; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_document_date_8d4d3a60 ON public.purchase_bill USING btree (document_date);


--
-- Name: purchase_bill_due_date_c4e5b5ef; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_due_date_c4e5b5ef ON public.purchase_bill USING btree (due_date);


--
-- Name: purchase_bill_fiscal_period_id_a1924ac7; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_fiscal_period_id_a1924ac7 ON public.purchase_bill USING btree (fiscal_period_id);


--
-- Name: purchase_bill_goods_receipt_id_297060ef; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_goods_receipt_id_297060ef ON public.purchase_bill USING btree (goods_receipt_id);


--
-- Name: purchase_bill_journal_entry_id_a5acc538; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_journal_entry_id_a5acc538 ON public.purchase_bill USING btree (journal_entry_id);


--
-- Name: purchase_bill_line_bill_id_558fef46; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_bill_id_558fef46 ON public.purchase_bill_line USING btree (bill_id);


--
-- Name: purchase_bill_line_expense_account_id_83382ac1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_expense_account_id_83382ac1 ON public.purchase_bill_line USING btree (expense_account_id);


--
-- Name: purchase_bill_line_product_id_9cbca758; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_product_id_9cbca758 ON public.purchase_bill_line USING btree (product_id);


--
-- Name: purchase_bill_line_purchase_order_line_id_682e3b42; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_purchase_order_line_id_682e3b42 ON public.purchase_bill_line USING btree (purchase_order_line_id);


--
-- Name: purchase_bill_line_receipt_line_id_52a4acb8; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_receipt_line_id_52a4acb8 ON public.purchase_bill_line USING btree (receipt_line_id);


--
-- Name: purchase_bill_line_tax_code_id_e9eebf28; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_tax_code_id_e9eebf28 ON public.purchase_bill_line USING btree (tax_code_id);


--
-- Name: purchase_bill_line_unit_id_75136d87; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_unit_id_75136d87 ON public.purchase_bill_line USING btree (unit_id);


--
-- Name: purchase_bill_line_warehouse_id_03a867e3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_line_warehouse_id_03a867e3 ON public.purchase_bill_line USING btree (warehouse_id);


--
-- Name: purchase_bill_number_05306e5c_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_number_05306e5c_like ON public.purchase_bill USING btree (number varchar_pattern_ops);


--
-- Name: purchase_bill_payable_account_id_70e01447; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_payable_account_id_70e01447 ON public.purchase_bill USING btree (payable_account_id);


--
-- Name: purchase_bill_payment_term_id_163154fc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_payment_term_id_163154fc ON public.purchase_bill USING btree (payment_term_id);


--
-- Name: purchase_bill_posted_by_id_4384731a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_posted_by_id_4384731a ON public.purchase_bill USING btree (posted_by_id);


--
-- Name: purchase_bill_purchase_order_id_dbe35a02; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_purchase_order_id_dbe35a02 ON public.purchase_bill USING btree (purchase_order_id);


--
-- Name: purchase_bill_updated_by_id_40d90996; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_updated_by_id_40d90996 ON public.purchase_bill USING btree (updated_by_id);


--
-- Name: purchase_bill_vendor_id_cd3b6808; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_vendor_id_cd3b6808 ON public.purchase_bill USING btree (vendor_id);


--
-- Name: purchase_bill_warehouse_id_ba5b3965; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_bill_warehouse_id_ba5b3965 ON public.purchase_bill USING btree (warehouse_id);


--
-- Name: purchase_order_approved_by_id_b622b3b4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_approved_by_id_b622b3b4 ON public.purchase_order USING btree (approved_by_id);


--
-- Name: purchase_order_buyer_id_721f6871; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_buyer_id_721f6871 ON public.purchase_order USING btree (buyer_id);


--
-- Name: purchase_order_created_by_id_112332e4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_created_by_id_112332e4 ON public.purchase_order USING btree (created_by_id);


--
-- Name: purchase_order_currency_id_c06f147b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_currency_id_c06f147b ON public.purchase_order USING btree (currency_id);


--
-- Name: purchase_order_currency_id_c06f147b_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_currency_id_c06f147b_like ON public.purchase_order USING btree (currency_id varchar_pattern_ops);


--
-- Name: purchase_order_document_date_1ea8376f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_document_date_1ea8376f ON public.purchase_order USING btree (document_date);


--
-- Name: purchase_order_due_date_b4cf015d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_due_date_b4cf015d ON public.purchase_order USING btree (due_date);


--
-- Name: purchase_order_fiscal_period_id_12a78e00; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_fiscal_period_id_12a78e00 ON public.purchase_order USING btree (fiscal_period_id);


--
-- Name: purchase_order_journal_entry_id_4ec5351f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_journal_entry_id_4ec5351f ON public.purchase_order USING btree (journal_entry_id);


--
-- Name: purchase_order_line_order_id_de705a26; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_line_order_id_de705a26 ON public.purchase_order_line USING btree (order_id);


--
-- Name: purchase_order_line_product_id_f1e43442; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_line_product_id_f1e43442 ON public.purchase_order_line USING btree (product_id);


--
-- Name: purchase_order_line_tax_code_id_32505185; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_line_tax_code_id_32505185 ON public.purchase_order_line USING btree (tax_code_id);


--
-- Name: purchase_order_line_unit_id_eaa3bd21; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_line_unit_id_eaa3bd21 ON public.purchase_order_line USING btree (unit_id);


--
-- Name: purchase_order_line_warehouse_id_071f6114; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_line_warehouse_id_071f6114 ON public.purchase_order_line USING btree (warehouse_id);


--
-- Name: purchase_order_number_c90f7411_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_number_c90f7411_like ON public.purchase_order USING btree (number varchar_pattern_ops);


--
-- Name: purchase_order_payment_term_id_d8987ec5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_payment_term_id_d8987ec5 ON public.purchase_order USING btree (payment_term_id);


--
-- Name: purchase_order_posted_by_id_515332d4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_posted_by_id_515332d4 ON public.purchase_order USING btree (posted_by_id);


--
-- Name: purchase_order_updated_by_id_8805b754; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_updated_by_id_8805b754 ON public.purchase_order USING btree (updated_by_id);


--
-- Name: purchase_order_vendor_id_eb91ea9b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_vendor_id_eb91ea9b ON public.purchase_order USING btree (vendor_id);


--
-- Name: purchase_order_warehouse_id_2bb9b357; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_order_warehouse_id_2bb9b357 ON public.purchase_order USING btree (warehouse_id);


--
-- Name: purchase_return_created_by_id_eba17aab; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_created_by_id_eba17aab ON public.purchase_return USING btree (created_by_id);


--
-- Name: purchase_return_document_date_c341a299; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_document_date_c341a299 ON public.purchase_return USING btree (document_date);


--
-- Name: purchase_return_journal_entry_id_514ab2d7; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_journal_entry_id_514ab2d7 ON public.purchase_return USING btree (journal_entry_id);


--
-- Name: purchase_return_line_bill_line_id_d399e005; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_line_bill_line_id_d399e005 ON public.purchase_return_line USING btree (bill_line_id);


--
-- Name: purchase_return_line_product_id_47bccc27; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_line_product_id_47bccc27 ON public.purchase_return_line USING btree (product_id);


--
-- Name: purchase_return_line_purchase_return_id_059e0f8a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_line_purchase_return_id_059e0f8a ON public.purchase_return_line USING btree (purchase_return_id);


--
-- Name: purchase_return_line_receipt_line_id_11efd351; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_line_receipt_line_id_11efd351 ON public.purchase_return_line USING btree (receipt_line_id);


--
-- Name: purchase_return_number_91ce3485_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_number_91ce3485_like ON public.purchase_return USING btree (number varchar_pattern_ops);


--
-- Name: purchase_return_original_bill_id_ecdaeb83; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_original_bill_id_ecdaeb83 ON public.purchase_return USING btree (original_bill_id);


--
-- Name: purchase_return_original_receipt_id_8399b520; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_original_receipt_id_8399b520 ON public.purchase_return USING btree (original_receipt_id);


--
-- Name: purchase_return_posted_by_id_63b21bcb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_posted_by_id_63b21bcb ON public.purchase_return USING btree (posted_by_id);


--
-- Name: purchase_return_updated_by_id_232694ec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_updated_by_id_232694ec ON public.purchase_return USING btree (updated_by_id);


--
-- Name: purchase_return_vendor_id_dff5a1a5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_vendor_id_dff5a1a5 ON public.purchase_return USING btree (vendor_id);


--
-- Name: purchase_return_warehouse_id_2bb8986b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX purchase_return_warehouse_id_2bb8986b ON public.purchase_return USING btree (warehouse_id);


--
-- Name: refund_approved_by_id_e3fdffe0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_approved_by_id_e3fdffe0 ON public.refund USING btree (approved_by_id);


--
-- Name: refund_created_by_id_f59404bc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_created_by_id_f59404bc ON public.refund USING btree (created_by_id);


--
-- Name: refund_currency_id_759036a3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_currency_id_759036a3 ON public.refund USING btree (currency_id);


--
-- Name: refund_currency_id_759036a3_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_currency_id_759036a3_like ON public.refund USING btree (currency_id varchar_pattern_ops);


--
-- Name: refund_customer_id_1144f495; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_customer_id_1144f495 ON public.refund USING btree (customer_id);


--
-- Name: refund_fiscal_period_id_56b8e021; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_fiscal_period_id_56b8e021 ON public.refund USING btree (fiscal_period_id);


--
-- Name: refund_journal_entry_id_32ef8464; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_journal_entry_id_32ef8464 ON public.refund USING btree (journal_entry_id);


--
-- Name: refund_method_id_a914e860; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_method_id_a914e860 ON public.refund USING btree (method_id);


--
-- Name: refund_money_account_id_6e9dd60d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_money_account_id_6e9dd60d ON public.refund USING btree (money_account_id);


--
-- Name: refund_number_94961bb4_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_number_94961bb4_like ON public.refund USING btree (number varchar_pattern_ops);


--
-- Name: refund_posted_by_id_79536c86; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_posted_by_id_79536c86 ON public.refund USING btree (posted_by_id);


--
-- Name: refund_refund_date_adb529d8; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_refund_date_adb529d8 ON public.refund USING btree (refund_date);


--
-- Name: refund_sales_credit_note_id_9fa231b7; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_sales_credit_note_id_9fa231b7 ON public.refund USING btree (sales_credit_note_id);


--
-- Name: refund_source_payment_id_7b6a8112; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_source_payment_id_7b6a8112 ON public.refund USING btree (source_payment_id);


--
-- Name: refund_updated_by_id_94eddd47; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_updated_by_id_94eddd47 ON public.refund USING btree (updated_by_id);


--
-- Name: refund_vendor_debit_note_id_83c9112e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_vendor_debit_note_id_83c9112e ON public.refund USING btree (vendor_debit_note_id);


--
-- Name: refund_vendor_id_d3aa90d3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX refund_vendor_id_d3aa90d3 ON public.refund USING btree (vendor_id);


--
-- Name: sales_credit_note_approved_by_id_601f1979; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_approved_by_id_601f1979 ON public.sales_credit_note USING btree (approved_by_id);


--
-- Name: sales_credit_note_created_by_id_03b1ad73; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_created_by_id_03b1ad73 ON public.sales_credit_note USING btree (created_by_id);


--
-- Name: sales_credit_note_currency_id_c06504b7; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_currency_id_c06504b7 ON public.sales_credit_note USING btree (currency_id);


--
-- Name: sales_credit_note_currency_id_c06504b7_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_currency_id_c06504b7_like ON public.sales_credit_note USING btree (currency_id varchar_pattern_ops);


--
-- Name: sales_credit_note_customer_id_e293da96; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_customer_id_e293da96 ON public.sales_credit_note USING btree (customer_id);


--
-- Name: sales_credit_note_document_date_1762593b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_document_date_1762593b ON public.sales_credit_note USING btree (document_date);


--
-- Name: sales_credit_note_due_date_2b933d78; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_due_date_2b933d78 ON public.sales_credit_note USING btree (due_date);


--
-- Name: sales_credit_note_fiscal_period_id_a6039790; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_fiscal_period_id_a6039790 ON public.sales_credit_note USING btree (fiscal_period_id);


--
-- Name: sales_credit_note_journal_entry_id_81b06aaf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_journal_entry_id_81b06aaf ON public.sales_credit_note USING btree (journal_entry_id);


--
-- Name: sales_credit_note_line_credit_note_id_7310afcc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_credit_note_id_7310afcc ON public.sales_credit_note_line USING btree (credit_note_id);


--
-- Name: sales_credit_note_line_invoice_line_id_99441b18; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_invoice_line_id_99441b18 ON public.sales_credit_note_line USING btree (invoice_line_id);


--
-- Name: sales_credit_note_line_product_id_49739ab4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_product_id_49739ab4 ON public.sales_credit_note_line USING btree (product_id);


--
-- Name: sales_credit_note_line_return_line_id_5f590314; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_return_line_id_5f590314 ON public.sales_credit_note_line USING btree (return_line_id);


--
-- Name: sales_credit_note_line_revenue_account_id_c630909a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_revenue_account_id_c630909a ON public.sales_credit_note_line USING btree (revenue_account_id);


--
-- Name: sales_credit_note_line_tax_code_id_5f16fddf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_tax_code_id_5f16fddf ON public.sales_credit_note_line USING btree (tax_code_id);


--
-- Name: sales_credit_note_line_unit_id_6876950a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_line_unit_id_6876950a ON public.sales_credit_note_line USING btree (unit_id);


--
-- Name: sales_credit_note_number_0c36b8f9_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_number_0c36b8f9_like ON public.sales_credit_note USING btree (number varchar_pattern_ops);


--
-- Name: sales_credit_note_original_invoice_id_31015ee0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_original_invoice_id_31015ee0 ON public.sales_credit_note USING btree (original_invoice_id);


--
-- Name: sales_credit_note_posted_by_id_eebf965a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_posted_by_id_eebf965a ON public.sales_credit_note USING btree (posted_by_id);


--
-- Name: sales_credit_note_sales_return_id_586c3bac; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_sales_return_id_586c3bac ON public.sales_credit_note USING btree (sales_return_id);


--
-- Name: sales_credit_note_updated_by_id_e927d005; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_credit_note_updated_by_id_e927d005 ON public.sales_credit_note USING btree (updated_by_id);


--
-- Name: sales_invoice_approved_by_id_51492162; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_approved_by_id_51492162 ON public.sales_invoice USING btree (approved_by_id);


--
-- Name: sales_invoice_created_by_id_c9aad729; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_created_by_id_c9aad729 ON public.sales_invoice USING btree (created_by_id);


--
-- Name: sales_invoice_currency_id_a7a68a61; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_currency_id_a7a68a61 ON public.sales_invoice USING btree (currency_id);


--
-- Name: sales_invoice_currency_id_a7a68a61_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_currency_id_a7a68a61_like ON public.sales_invoice USING btree (currency_id varchar_pattern_ops);


--
-- Name: sales_invoice_customer_id_339082a2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_customer_id_339082a2 ON public.sales_invoice USING btree (customer_id);


--
-- Name: sales_invoice_document_date_28946e45; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_document_date_28946e45 ON public.sales_invoice USING btree (document_date);


--
-- Name: sales_invoice_due_date_035091f5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_due_date_035091f5 ON public.sales_invoice USING btree (due_date);


--
-- Name: sales_invoice_fiscal_period_id_c6884593; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_fiscal_period_id_c6884593 ON public.sales_invoice USING btree (fiscal_period_id);


--
-- Name: sales_invoice_journal_entry_id_bb34c51f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_journal_entry_id_bb34c51f ON public.sales_invoice USING btree (journal_entry_id);


--
-- Name: sales_invoice_line_delivery_line_id_61bcb9f2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_delivery_line_id_61bcb9f2 ON public.sales_invoice_line USING btree (delivery_line_id);


--
-- Name: sales_invoice_line_invoice_id_bb18924e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_invoice_id_bb18924e ON public.sales_invoice_line USING btree (invoice_id);


--
-- Name: sales_invoice_line_product_id_46d5812c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_product_id_46d5812c ON public.sales_invoice_line USING btree (product_id);


--
-- Name: sales_invoice_line_revenue_account_id_7e2544af; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_revenue_account_id_7e2544af ON public.sales_invoice_line USING btree (revenue_account_id);


--
-- Name: sales_invoice_line_sales_order_line_id_85c7f654; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_sales_order_line_id_85c7f654 ON public.sales_invoice_line USING btree (sales_order_line_id);


--
-- Name: sales_invoice_line_tax_code_id_8a942e17; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_tax_code_id_8a942e17 ON public.sales_invoice_line USING btree (tax_code_id);


--
-- Name: sales_invoice_line_unit_id_848122a9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_unit_id_848122a9 ON public.sales_invoice_line USING btree (unit_id);


--
-- Name: sales_invoice_line_warehouse_id_ac9c8ab2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_line_warehouse_id_ac9c8ab2 ON public.sales_invoice_line USING btree (warehouse_id);


--
-- Name: sales_invoice_number_be53c67c_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_number_be53c67c_like ON public.sales_invoice USING btree (number varchar_pattern_ops);


--
-- Name: sales_invoice_payment_term_id_0c1ff60e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_payment_term_id_0c1ff60e ON public.sales_invoice USING btree (payment_term_id);


--
-- Name: sales_invoice_posted_by_id_6da46b3f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_posted_by_id_6da46b3f ON public.sales_invoice USING btree (posted_by_id);


--
-- Name: sales_invoice_receivable_account_id_db2ee1cc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_receivable_account_id_db2ee1cc ON public.sales_invoice USING btree (receivable_account_id);


--
-- Name: sales_invoice_reversed_by_journal_id_d7b68300; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_reversed_by_journal_id_d7b68300 ON public.sales_invoice USING btree (reversed_by_journal_id);


--
-- Name: sales_invoice_sales_order_id_11be25e2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_sales_order_id_11be25e2 ON public.sales_invoice USING btree (sales_order_id);


--
-- Name: sales_invoice_updated_by_id_59776dfa; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_updated_by_id_59776dfa ON public.sales_invoice USING btree (updated_by_id);


--
-- Name: sales_invoice_warehouse_id_b3873f38; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_invoice_warehouse_id_b3873f38 ON public.sales_invoice USING btree (warehouse_id);


--
-- Name: sales_order_approved_by_id_946a87c3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_approved_by_id_946a87c3 ON public.sales_order USING btree (approved_by_id);


--
-- Name: sales_order_created_by_id_1b611bc2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_created_by_id_1b611bc2 ON public.sales_order USING btree (created_by_id);


--
-- Name: sales_order_currency_id_6468265f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_currency_id_6468265f ON public.sales_order USING btree (currency_id);


--
-- Name: sales_order_currency_id_6468265f_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_currency_id_6468265f_like ON public.sales_order USING btree (currency_id varchar_pattern_ops);


--
-- Name: sales_order_customer_id_798c889f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_customer_id_798c889f ON public.sales_order USING btree (customer_id);


--
-- Name: sales_order_document_date_7c9b054e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_document_date_7c9b054e ON public.sales_order USING btree (document_date);


--
-- Name: sales_order_due_date_ff5ed1c2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_due_date_ff5ed1c2 ON public.sales_order USING btree (due_date);


--
-- Name: sales_order_fiscal_period_id_62bd2dfe; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_fiscal_period_id_62bd2dfe ON public.sales_order USING btree (fiscal_period_id);


--
-- Name: sales_order_journal_entry_id_30112851; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_journal_entry_id_30112851 ON public.sales_order USING btree (journal_entry_id);


--
-- Name: sales_order_line_order_id_558b9683; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_line_order_id_558b9683 ON public.sales_order_line USING btree (order_id);


--
-- Name: sales_order_line_product_id_69b5b691; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_line_product_id_69b5b691 ON public.sales_order_line USING btree (product_id);


--
-- Name: sales_order_line_tax_code_id_e7eb3515; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_line_tax_code_id_e7eb3515 ON public.sales_order_line USING btree (tax_code_id);


--
-- Name: sales_order_line_unit_id_78e7109e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_line_unit_id_78e7109e ON public.sales_order_line USING btree (unit_id);


--
-- Name: sales_order_line_warehouse_id_74ac00e3; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_line_warehouse_id_74ac00e3 ON public.sales_order_line USING btree (warehouse_id);


--
-- Name: sales_order_number_48f6507b_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_number_48f6507b_like ON public.sales_order USING btree (number varchar_pattern_ops);


--
-- Name: sales_order_payment_term_id_286c63b2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_payment_term_id_286c63b2 ON public.sales_order USING btree (payment_term_id);


--
-- Name: sales_order_posted_by_id_5cce5439; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_posted_by_id_5cce5439 ON public.sales_order USING btree (posted_by_id);


--
-- Name: sales_order_salesperson_id_60448194; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_salesperson_id_60448194 ON public.sales_order USING btree (salesperson_id);


--
-- Name: sales_order_updated_by_id_7927d1d4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_updated_by_id_7927d1d4 ON public.sales_order USING btree (updated_by_id);


--
-- Name: sales_order_warehouse_id_a7eb0089; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_order_warehouse_id_a7eb0089 ON public.sales_order USING btree (warehouse_id);


--
-- Name: sales_return_created_by_id_cefeee3c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_created_by_id_cefeee3c ON public.sales_return USING btree (created_by_id);


--
-- Name: sales_return_customer_id_a731a0d8; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_customer_id_a731a0d8 ON public.sales_return USING btree (customer_id);


--
-- Name: sales_return_document_date_6a22f396; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_document_date_6a22f396 ON public.sales_return USING btree (document_date);


--
-- Name: sales_return_journal_entry_id_95826a9b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_journal_entry_id_95826a9b ON public.sales_return USING btree (journal_entry_id);


--
-- Name: sales_return_line_delivery_line_id_64549345; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_line_delivery_line_id_64549345 ON public.sales_return_line USING btree (delivery_line_id);


--
-- Name: sales_return_line_invoice_line_id_a01eed9e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_line_invoice_line_id_a01eed9e ON public.sales_return_line USING btree (invoice_line_id);


--
-- Name: sales_return_line_product_id_f1721143; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_line_product_id_f1721143 ON public.sales_return_line USING btree (product_id);


--
-- Name: sales_return_line_sales_return_id_d017c92e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_line_sales_return_id_d017c92e ON public.sales_return_line USING btree (sales_return_id);


--
-- Name: sales_return_number_063371ac_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_number_063371ac_like ON public.sales_return USING btree (number varchar_pattern_ops);


--
-- Name: sales_return_original_delivery_id_e823de8e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_original_delivery_id_e823de8e ON public.sales_return USING btree (original_delivery_id);


--
-- Name: sales_return_original_invoice_id_6b20840b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_original_invoice_id_6b20840b ON public.sales_return USING btree (original_invoice_id);


--
-- Name: sales_return_posted_by_id_8e92fcc8; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_posted_by_id_8e92fcc8 ON public.sales_return USING btree (posted_by_id);


--
-- Name: sales_return_updated_by_id_2673e656; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_updated_by_id_2673e656 ON public.sales_return USING btree (updated_by_id);


--
-- Name: sales_return_warehouse_id_389e6f5c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sales_return_warehouse_id_389e6f5c ON public.sales_return USING btree (warehouse_id);


--
-- Name: stock_adjustment_approved_by_id_f865f5f1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_approved_by_id_f865f5f1 ON public.stock_adjustment USING btree (approved_by_id);


--
-- Name: stock_adjustment_created_by_id_e2f683f6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_created_by_id_e2f683f6 ON public.stock_adjustment USING btree (created_by_id);


--
-- Name: stock_adjustment_document_date_3d8ca162; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_document_date_3d8ca162 ON public.stock_adjustment USING btree (document_date);


--
-- Name: stock_adjustment_journal_entry_id_ebf7dd5e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_journal_entry_id_ebf7dd5e ON public.stock_adjustment USING btree (journal_entry_id);


--
-- Name: stock_adjustment_line_adjustment_id_211381eb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_line_adjustment_id_211381eb ON public.stock_adjustment_line USING btree (adjustment_id);


--
-- Name: stock_adjustment_line_product_id_78420274; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_line_product_id_78420274 ON public.stock_adjustment_line USING btree (product_id);


--
-- Name: stock_adjustment_number_78e35608_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_number_78e35608_like ON public.stock_adjustment USING btree (number varchar_pattern_ops);


--
-- Name: stock_adjustment_posted_by_id_29b953b5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_posted_by_id_29b953b5 ON public.stock_adjustment USING btree (posted_by_id);


--
-- Name: stock_adjustment_reason_id_961df6a2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_reason_id_961df6a2 ON public.stock_adjustment USING btree (reason_id);


--
-- Name: stock_adjustment_updated_by_id_f7e6f104; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_updated_by_id_f7e6f104 ON public.stock_adjustment USING btree (updated_by_id);


--
-- Name: stock_adjustment_warehouse_id_2ecf196d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_adjustment_warehouse_id_2ecf196d ON public.stock_adjustment USING btree (warehouse_id);


--
-- Name: stock_balance_product_id_488344c4; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_balance_product_id_488344c4 ON public.stock_balance USING btree (product_id);


--
-- Name: stock_balance_warehouse_id_0fdcf6d6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_balance_warehouse_id_0fdcf6d6 ON public.stock_balance USING btree (warehouse_id);


--
-- Name: stock_movement_created_by_id_4d0216fc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_created_by_id_4d0216fc ON public.stock_movement USING btree (created_by_id);


--
-- Name: stock_movement_idempotency_key_62a2a8d3_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_idempotency_key_62a2a8d3_like ON public.stock_movement USING btree (idempotency_key varchar_pattern_ops);


--
-- Name: stock_movement_journal_entry_id_d9d4bc80; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_journal_entry_id_d9d4bc80 ON public.stock_movement USING btree (journal_entry_id);


--
-- Name: stock_movement_movement_date_a4b0bd89; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_movement_date_a4b0bd89 ON public.stock_movement USING btree (movement_date);


--
-- Name: stock_movement_product_id_126b1c0d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_product_id_126b1c0d ON public.stock_movement USING btree (product_id);


--
-- Name: stock_movement_source_content_type_id_19e5e186; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_source_content_type_id_19e5e186 ON public.stock_movement USING btree (source_content_type_id);


--
-- Name: stock_movement_warehouse_id_806239d6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_movement_warehouse_id_806239d6 ON public.stock_movement USING btree (warehouse_id);


--
-- Name: stock_transfer_created_by_id_c5517845; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_created_by_id_c5517845 ON public.stock_transfer USING btree (created_by_id);


--
-- Name: stock_transfer_document_date_ed20d90a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_document_date_ed20d90a ON public.stock_transfer USING btree (document_date);


--
-- Name: stock_transfer_from_warehouse_id_d1e866ae; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_from_warehouse_id_d1e866ae ON public.stock_transfer USING btree (from_warehouse_id);


--
-- Name: stock_transfer_journal_entry_id_b2dcf187; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_journal_entry_id_b2dcf187 ON public.stock_transfer USING btree (journal_entry_id);


--
-- Name: stock_transfer_line_product_id_1ce71084; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_line_product_id_1ce71084 ON public.stock_transfer_line USING btree (product_id);


--
-- Name: stock_transfer_line_transfer_id_4403a83b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_line_transfer_id_4403a83b ON public.stock_transfer_line USING btree (transfer_id);


--
-- Name: stock_transfer_number_8c5fd44e_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_number_8c5fd44e_like ON public.stock_transfer USING btree (number varchar_pattern_ops);


--
-- Name: stock_transfer_posted_by_id_53dca9ac; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_posted_by_id_53dca9ac ON public.stock_transfer USING btree (posted_by_id);


--
-- Name: stock_transfer_to_warehouse_id_9c565097; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_to_warehouse_id_9c565097 ON public.stock_transfer USING btree (to_warehouse_id);


--
-- Name: stock_transfer_updated_by_id_8439895a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX stock_transfer_updated_by_id_8439895a ON public.stock_transfer USING btree (updated_by_id);


--
-- Name: tax_code_code_dcb27a42_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tax_code_code_dcb27a42_like ON public.tax_code USING btree (code varchar_pattern_ops);


--
-- Name: tax_code_input_tax_account_id_1b9fdc38; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tax_code_input_tax_account_id_1b9fdc38 ON public.tax_code USING btree (input_tax_account_id);


--
-- Name: tax_code_output_tax_account_id_2b416174; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tax_code_output_tax_account_id_2b416174 ON public.tax_code USING btree (output_tax_account_id);


--
-- Name: unit_of_measure_base_unit_id_46d6f302; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX unit_of_measure_base_unit_id_46d6f302 ON public.unit_of_measure USING btree (base_unit_id);


--
-- Name: unit_of_measure_code_71bff5cb_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX unit_of_measure_code_71bff5cb_like ON public.unit_of_measure USING btree (code varchar_pattern_ops);


--
-- Name: vendor_advance_account_id_da8e6198; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_advance_account_id_da8e6198 ON public.vendor USING btree (advance_account_id);


--
-- Name: vendor_code_unique_ci; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX vendor_code_unique_ci ON public.vendor USING btree (upper((code)::text));


--
-- Name: vendor_created_by_id_6eff9955; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_created_by_id_6eff9955 ON public.vendor USING btree (created_by_id);


--
-- Name: vendor_currency_id_2d9d42db; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_currency_id_2d9d42db ON public.vendor USING btree (currency_id);


--
-- Name: vendor_currency_id_2d9d42db_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_currency_id_2d9d42db_like ON public.vendor USING btree (currency_id varchar_pattern_ops);


--
-- Name: vendor_debit_note_approved_by_id_e08ab371; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_approved_by_id_e08ab371 ON public.vendor_debit_note USING btree (approved_by_id);


--
-- Name: vendor_debit_note_created_by_id_c60f5762; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_created_by_id_c60f5762 ON public.vendor_debit_note USING btree (created_by_id);


--
-- Name: vendor_debit_note_currency_id_97a652a9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_currency_id_97a652a9 ON public.vendor_debit_note USING btree (currency_id);


--
-- Name: vendor_debit_note_currency_id_97a652a9_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_currency_id_97a652a9_like ON public.vendor_debit_note USING btree (currency_id varchar_pattern_ops);


--
-- Name: vendor_debit_note_document_date_fb4a443e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_document_date_fb4a443e ON public.vendor_debit_note USING btree (document_date);


--
-- Name: vendor_debit_note_due_date_7d84d5b5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_due_date_7d84d5b5 ON public.vendor_debit_note USING btree (due_date);


--
-- Name: vendor_debit_note_fiscal_period_id_88a93af0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_fiscal_period_id_88a93af0 ON public.vendor_debit_note USING btree (fiscal_period_id);


--
-- Name: vendor_debit_note_journal_entry_id_ea2b9361; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_journal_entry_id_ea2b9361 ON public.vendor_debit_note USING btree (journal_entry_id);


--
-- Name: vendor_debit_note_line_bill_line_id_c55d41dd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_bill_line_id_c55d41dd ON public.vendor_debit_note_line USING btree (bill_line_id);


--
-- Name: vendor_debit_note_line_debit_note_id_55b38a5b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_debit_note_id_55b38a5b ON public.vendor_debit_note_line USING btree (debit_note_id);


--
-- Name: vendor_debit_note_line_expense_account_id_77caaa0a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_expense_account_id_77caaa0a ON public.vendor_debit_note_line USING btree (expense_account_id);


--
-- Name: vendor_debit_note_line_product_id_b3c85f4f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_product_id_b3c85f4f ON public.vendor_debit_note_line USING btree (product_id);


--
-- Name: vendor_debit_note_line_return_line_id_81ab4a1a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_return_line_id_81ab4a1a ON public.vendor_debit_note_line USING btree (return_line_id);


--
-- Name: vendor_debit_note_line_tax_code_id_1aee6796; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_tax_code_id_1aee6796 ON public.vendor_debit_note_line USING btree (tax_code_id);


--
-- Name: vendor_debit_note_line_unit_id_c94ece3f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_line_unit_id_c94ece3f ON public.vendor_debit_note_line USING btree (unit_id);


--
-- Name: vendor_debit_note_number_597e64e0_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_number_597e64e0_like ON public.vendor_debit_note USING btree (number varchar_pattern_ops);


--
-- Name: vendor_debit_note_original_bill_id_bc039eb6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_original_bill_id_bc039eb6 ON public.vendor_debit_note USING btree (original_bill_id);


--
-- Name: vendor_debit_note_posted_by_id_c90ff54d; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_posted_by_id_c90ff54d ON public.vendor_debit_note USING btree (posted_by_id);


--
-- Name: vendor_debit_note_purchase_return_id_7e50ee88; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_purchase_return_id_7e50ee88 ON public.vendor_debit_note USING btree (purchase_return_id);


--
-- Name: vendor_debit_note_updated_by_id_c5bfa453; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_updated_by_id_c5bfa453 ON public.vendor_debit_note USING btree (updated_by_id);


--
-- Name: vendor_debit_note_vendor_id_14767e87; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_debit_note_vendor_id_14767e87 ON public.vendor_debit_note USING btree (vendor_id);


--
-- Name: vendor_default_expense_account_id_5cab5f2f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_default_expense_account_id_5cab5f2f ON public.vendor USING btree (default_expense_account_id);


--
-- Name: vendor_default_tax_code_id_8c6c2b43; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_default_tax_code_id_8c6c2b43 ON public.vendor USING btree (default_tax_code_id);


--
-- Name: vendor_payable_account_id_45bbd262; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_payable_account_id_45bbd262 ON public.vendor USING btree (payable_account_id);


--
-- Name: vendor_payment_term_id_e18b894b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_payment_term_id_e18b894b ON public.vendor USING btree (payment_term_id);


--
-- Name: vendor_tax_id_cb612863; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_tax_id_cb612863 ON public.vendor USING btree (tax_id);


--
-- Name: vendor_tax_id_cb612863_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_tax_id_cb612863_like ON public.vendor USING btree (tax_id varchar_pattern_ops);


--
-- Name: vendor_updated_by_id_1ef1c2d5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX vendor_updated_by_id_1ef1c2d5 ON public.vendor USING btree (updated_by_id);


--
-- Name: warehouse_code_446a59c0_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX warehouse_code_446a59c0_like ON public.warehouse USING btree (code varchar_pattern_ops);


--
-- Name: warehouse_created_by_id_0081bffd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX warehouse_created_by_id_0081bffd ON public.warehouse USING btree (created_by_id);


--
-- Name: warehouse_inventory_account_id_cae197de; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX warehouse_inventory_account_id_cae197de ON public.warehouse USING btree (inventory_account_id);


--
-- Name: warehouse_manager_id_88a15ae0; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX warehouse_manager_id_88a15ae0 ON public.warehouse USING btree (manager_id);


--
-- Name: warehouse_updated_by_id_1abb8a8f; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX warehouse_updated_by_id_1abb8a8f ON public.warehouse USING btree (updated_by_id);


--
-- Name: payment_allocation trg_allocation_consistency; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_allocation_consistency AFTER INSERT OR DELETE OR UPDATE ON public.payment_allocation DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wams_allocation_consistency();


--
-- Name: journal_entry trg_journal_entry_balanced; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_journal_entry_balanced AFTER INSERT OR UPDATE ON public.journal_entry DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wams_journal_balance_check();


--
-- Name: journal_entry trg_journal_entry_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_journal_entry_immutable BEFORE DELETE OR UPDATE ON public.journal_entry FOR EACH ROW EXECUTE FUNCTION public.wams_journal_entry_immutable();


--
-- Name: journal_line trg_journal_line_account_check; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_journal_line_account_check BEFORE INSERT ON public.journal_line FOR EACH ROW EXECUTE FUNCTION public.wams_journal_line_account_check();


--
-- Name: journal_line trg_journal_line_balanced; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_journal_line_balanced AFTER INSERT OR DELETE OR UPDATE ON public.journal_line DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.wams_journal_balance_check();


--
-- Name: journal_line trg_journal_line_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_journal_line_immutable BEFORE DELETE OR UPDATE ON public.journal_line FOR EACH ROW EXECUTE FUNCTION public.wams_journal_line_immutable();


--
-- Name: journal_entry trg_journal_period_check; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_journal_period_check BEFORE INSERT ON public.journal_entry FOR EACH ROW EXECUTE FUNCTION public.wams_journal_period_check();


--
-- Name: stock_movement trg_stock_movement_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_stock_movement_immutable BEFORE DELETE OR UPDATE ON public.stock_movement FOR EACH ROW EXECUTE FUNCTION public.wams_stock_movement_immutable();


--
-- Name: stock_balance trg_stock_negative_check; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_stock_negative_check BEFORE INSERT OR UPDATE ON public.stock_balance FOR EACH ROW EXECUTE FUNCTION public.wams_stock_negative_check();


--
-- Name: account account_created_by_id_9c481c1a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_created_by_id_9c481c1a_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account account_currency_id_061676bb_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_currency_id_061676bb_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account_mapping account_mapping_account_id_e13a06f4_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_mapping
    ADD CONSTRAINT account_mapping_account_id_e13a06f4_fk_account_id FOREIGN KEY (account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account_mapping account_mapping_created_by_id_c985f2e3_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_mapping
    ADD CONSTRAINT account_mapping_created_by_id_c985f2e3_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account_mapping account_mapping_updated_by_id_4e106374_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_mapping
    ADD CONSTRAINT account_mapping_updated_by_id_4e106374_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account account_parent_id_5f9163e3_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_parent_id_5f9163e3_fk_account_id FOREIGN KEY (parent_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account account_updated_by_id_cc9a8d93_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_updated_by_id_cc9a8d93_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: adjustment_reason adjustment_reason_gain_loss_account_id_faaf2ac8_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjustment_reason
    ADD CONSTRAINT adjustment_reason_gain_loss_account_id_faaf2ac8_fk_account_id FOREIGN KEY (gain_loss_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: app_user app_user_default_warehouse_id_43fb9731_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_default_warehouse_id_43fb9731_fk_warehouse_id FOREIGN KEY (default_warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: app_user_groups app_user_groups_group_id_e774d92c_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_groups
    ADD CONSTRAINT app_user_groups_group_id_e774d92c_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: app_user_groups app_user_groups_user_id_e6f878f6_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_groups
    ADD CONSTRAINT app_user_groups_user_id_e6f878f6_fk_app_user_id FOREIGN KEY (user_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: app_user_user_permissions app_user_user_permis_permission_id_4ef8e133_fk_auth_perm; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_user_permissions
    ADD CONSTRAINT app_user_user_permis_permission_id_4ef8e133_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES public.auth_permission(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: app_user_user_permissions app_user_user_permissions_user_id_24780b52_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user_user_permissions
    ADD CONSTRAINT app_user_user_permissions_user_id_24780b52_fk_app_user_id FOREIGN KEY (user_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: audit_event audit_event_content_type_id_72949bd3_fk_django_content_type_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_event
    ADD CONSTRAINT audit_event_content_type_id_72949bd3_fk_django_content_type_id FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: audit_event audit_event_user_id_05228691_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_event
    ADD CONSTRAINT audit_event_user_id_05228691_fk_app_user_id FOREIGN KEY (user_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_group_permissions auth_group_permissio_permission_id_84c5c92e_fk_auth_perm; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissio_permission_id_84c5c92e_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES public.auth_permission(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_group_permissions auth_group_permissions_group_id_b120cbf9_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_group_id_b120cbf9_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_permission auth_permission_content_type_id_2f476e4b_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_content_type_id_2f476e4b_fk_django_co FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: company company_base_currency_id_dc725f40_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_base_currency_id_dc725f40_fk_currency_code FOREIGN KEY (base_currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: company company_created_by_id_6d8b6b91_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_created_by_id_6d8b6b91_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: company company_updated_by_id_feec3d9b_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company
    ADD CONSTRAINT company_updated_by_id_feec3d9b_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_advance_account_id_86ea7fb2_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_advance_account_id_86ea7fb2_fk_account_id FOREIGN KEY (advance_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_created_by_id_6fc5ba53_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_created_by_id_6fc5ba53_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_currency_id_1890d9ef_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_currency_id_1890d9ef_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_default_tax_code_id_8e287fcf_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_default_tax_code_id_8e287fcf_fk_tax_code_id FOREIGN KEY (default_tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_default_warehouse_id_bec5c600_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_default_warehouse_id_bec5c600_fk_warehouse_id FOREIGN KEY (default_warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_payment_term_id_66e0c0da_fk_payment_term_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_payment_term_id_66e0c0da_fk_payment_term_id FOREIGN KEY (payment_term_id) REFERENCES public.payment_term(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_receivable_account_id_2b154cc9_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_receivable_account_id_2b154cc9_fk_account_id FOREIGN KEY (receivable_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_salesperson_id_702bb6ad_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_salesperson_id_702bb6ad_fk_app_user_id FOREIGN KEY (salesperson_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: customer customer_updated_by_id_0592dd0a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer
    ADD CONSTRAINT customer_updated_by_id_0592dd0a_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_created_by_id_46f25ae5_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_created_by_id_46f25ae5_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_customer_id_4f138130_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_customer_id_4f138130_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_delivered_by_id_26592bb2_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_delivered_by_id_26592bb2_fk_app_user_id FOREIGN KEY (delivered_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_journal_entry_id_baabbbba_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_journal_entry_id_baabbbba_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note_line delivery_note_line_delivery_id_8408ade4_fk_delivery_note_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note_line
    ADD CONSTRAINT delivery_note_line_delivery_id_8408ade4_fk_delivery_note_id FOREIGN KEY (delivery_id) REFERENCES public.delivery_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note_line delivery_note_line_product_id_d8c4a71b_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note_line
    ADD CONSTRAINT delivery_note_line_product_id_d8c4a71b_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note_line delivery_note_line_sales_order_line_id_c02a6e8b_fk_sales_ord; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note_line
    ADD CONSTRAINT delivery_note_line_sales_order_line_id_c02a6e8b_fk_sales_ord FOREIGN KEY (sales_order_line_id) REFERENCES public.sales_order_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note_line delivery_note_line_unit_id_981f6abf_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note_line
    ADD CONSTRAINT delivery_note_line_unit_id_981f6abf_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_posted_by_id_daf4c829_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_posted_by_id_daf4c829_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_sales_order_id_484d58f5_fk_sales_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_sales_order_id_484d58f5_fk_sales_order_id FOREIGN KEY (sales_order_id) REFERENCES public.sales_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_updated_by_id_e84b6007_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_updated_by_id_e84b6007_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: delivery_note delivery_note_warehouse_id_34823705_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.delivery_note
    ADD CONSTRAINT delivery_note_warehouse_id_34823705_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: django_admin_log django_admin_log_content_type_id_c4bce8eb_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_content_type_id_c4bce8eb_fk_django_co FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: django_admin_log django_admin_log_user_id_c564eba6_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_user_id_c564eba6_fk_app_user_id FOREIGN KEY (user_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: exchange_rate exchange_rate_currency_id_9e574674_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rate
    ADD CONSTRAINT exchange_rate_currency_id_9e574674_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: fiscal_period fiscal_period_closed_by_id_e168f4f2_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_period
    ADD CONSTRAINT fiscal_period_closed_by_id_e168f4f2_fk_app_user_id FOREIGN KEY (closed_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: fiscal_period fiscal_period_fiscal_year_id_9f495ae5_fk_fiscal_year_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_period
    ADD CONSTRAINT fiscal_period_fiscal_year_id_9f495ae5_fk_fiscal_year_id FOREIGN KEY (fiscal_year_id) REFERENCES public.fiscal_year(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: fiscal_period fiscal_period_reopened_by_id_67b69bb7_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fiscal_period
    ADD CONSTRAINT fiscal_period_reopened_by_id_67b69bb7_fk_app_user_id FOREIGN KEY (reopened_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_created_by_id_60396763_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_created_by_id_60396763_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_journal_entry_id_992fb570_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_journal_entry_id_992fb570_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt_line goods_receipt_line_product_id_a498892c_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt_line
    ADD CONSTRAINT goods_receipt_line_product_id_a498892c_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt_line goods_receipt_line_purchase_order_line__665e1d3f_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt_line
    ADD CONSTRAINT goods_receipt_line_purchase_order_line__665e1d3f_fk_purchase_ FOREIGN KEY (purchase_order_line_id) REFERENCES public.purchase_order_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt_line goods_receipt_line_receipt_id_a0a4f990_fk_goods_receipt_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt_line
    ADD CONSTRAINT goods_receipt_line_receipt_id_a0a4f990_fk_goods_receipt_id FOREIGN KEY (receipt_id) REFERENCES public.goods_receipt(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt_line goods_receipt_line_unit_id_6c10ded0_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt_line
    ADD CONSTRAINT goods_receipt_line_unit_id_6c10ded0_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_posted_by_id_94723af2_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_posted_by_id_94723af2_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_purchase_order_id_aedf9d62_fk_purchase_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_purchase_order_id_aedf9d62_fk_purchase_order_id FOREIGN KEY (purchase_order_id) REFERENCES public.purchase_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_received_by_id_91c25e9c_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_received_by_id_91c25e9c_fk_app_user_id FOREIGN KEY (received_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_updated_by_id_6d8eba7f_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_updated_by_id_6d8eba7f_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_vendor_id_aba03709_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_vendor_id_aba03709_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: goods_receipt goods_receipt_warehouse_id_f305c6f5_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goods_receipt
    ADD CONSTRAINT goods_receipt_warehouse_id_f305c6f5_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_created_by_id_0ae13889_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_created_by_id_0ae13889_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_currency_id_fd3ea6bd_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_currency_id_fd3ea6bd_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_fiscal_period_id_72e01511_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_fiscal_period_id_72e01511_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_posted_by_id_84fe2fa1_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_posted_by_id_84fe2fa1_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_reverses_id_deea2828_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_reverses_id_deea2828_fk_journal_entry_id FOREIGN KEY (reverses_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_source_content_type__b908d235_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_source_content_type__b908d235_fk_django_co FOREIGN KEY (source_content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_entry journal_entry_updated_by_id_09c7c3a4_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_entry
    ADD CONSTRAINT journal_entry_updated_by_id_09c7c3a4_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_account_id_f42b4cb5_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_account_id_f42b4cb5_fk_account_id FOREIGN KEY (account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_currency_id_a27db2f6_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_currency_id_a27db2f6_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_customer_id_907dd46d_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_customer_id_907dd46d_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_entry_id_90f26900_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_entry_id_90f26900_fk_journal_entry_id FOREIGN KEY (entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_money_account_id_ddb8b77b_fk_money_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_money_account_id_ddb8b77b_fk_money_account_id FOREIGN KEY (money_account_id) REFERENCES public.money_account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_product_id_8654113b_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_product_id_8654113b_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_tax_code_id_579a0c95_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_tax_code_id_579a0c95_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_vendor_id_c2b290e5_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_vendor_id_c2b290e5_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: journal_line journal_line_warehouse_id_4f101383_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_line
    ADD CONSTRAINT journal_line_warehouse_id_4f101383_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: money_account money_account_created_by_id_72951b7e_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_created_by_id_72951b7e_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: money_account money_account_currency_id_f2ea24b3_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_currency_id_f2ea24b3_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: money_account money_account_gl_account_id_bc425b4d_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_gl_account_id_bc425b4d_fk_account_id FOREIGN KEY (gl_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: money_account money_account_updated_by_id_0715c7d4_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.money_account
    ADD CONSTRAINT money_account_updated_by_id_0715c7d4_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: opening_balance_batch opening_balance_batc_journal_entry_id_4b1fe038_fk_journal_e; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opening_balance_batch
    ADD CONSTRAINT opening_balance_batc_journal_entry_id_4b1fe038_fk_journal_e FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: opening_balance_batch opening_balance_batch_created_by_id_c37090e1_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opening_balance_batch
    ADD CONSTRAINT opening_balance_batch_created_by_id_c37090e1_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: opening_balance_batch opening_balance_batch_updated_by_id_f22fbbe0_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opening_balance_batch
    ADD CONSTRAINT opening_balance_batch_updated_by_id_f22fbbe0_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: party_address party_address_customer_id_a0b58139_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.party_address
    ADD CONSTRAINT party_address_customer_id_a0b58139_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: party_address party_address_vendor_id_f6eb3eac_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.party_address
    ADD CONSTRAINT party_address_vendor_id_f6eb3eac_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: party_contact party_contact_customer_id_6ff47470_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.party_contact
    ADD CONSTRAINT party_contact_customer_id_6ff47470_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: party_contact party_contact_vendor_id_de937ded_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.party_contact
    ADD CONSTRAINT party_contact_vendor_id_de937ded_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_created_by_id_ed2804c0_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_created_by_id_ed2804c0_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_customer_id_a35451e8_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_customer_id_a35451e8_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_fx_journal_entry_id_7bb33a22_fk_journal_e; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_fx_journal_entry_id_7bb33a22_fk_journal_e FOREIGN KEY (fx_journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_journal_entry_id_38826ae6_fk_journal_e; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_journal_entry_id_38826ae6_fk_journal_e FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_payment_id_11a2910b_fk_payment_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_payment_id_11a2910b_fk_payment_id FOREIGN KEY (payment_id) REFERENCES public.payment(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_purchase_bill_id_659248eb_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_purchase_bill_id_659248eb_fk_purchase_ FOREIGN KEY (purchase_bill_id) REFERENCES public.purchase_bill(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_sales_credit_note_id_dfc35563_fk_sales_cre; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_sales_credit_note_id_dfc35563_fk_sales_cre FOREIGN KEY (sales_credit_note_id) REFERENCES public.sales_credit_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_sales_invoice_id_830ef636_fk_sales_inv; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_sales_invoice_id_830ef636_fk_sales_inv FOREIGN KEY (sales_invoice_id) REFERENCES public.sales_invoice(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_updated_by_id_42ac0fdf_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_updated_by_id_42ac0fdf_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_vendor_debit_note_id_af99f886_fk_vendor_de; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_vendor_debit_note_id_af99f886_fk_vendor_de FOREIGN KEY (vendor_debit_note_id) REFERENCES public.vendor_debit_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_allocation payment_allocation_vendor_id_82bb9be1_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_allocation
    ADD CONSTRAINT payment_allocation_vendor_id_82bb9be1_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_created_by_id_0a1fb28a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_created_by_id_0a1fb28a_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_currency_id_d55dcf3a_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_currency_id_d55dcf3a_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_customer_id_cfa68abe_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_customer_id_cfa68abe_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_fiscal_period_id_c1765b7a_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_fiscal_period_id_c1765b7a_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_journal_entry_id_755d7b67_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_journal_entry_id_755d7b67_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment_method payment_method_default_money_accoun_562588dc_fk_money_acc; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_method
    ADD CONSTRAINT payment_method_default_money_accoun_562588dc_fk_money_acc FOREIGN KEY (default_money_account_id) REFERENCES public.money_account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_method_id_e2ad63d9_fk_payment_method_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_method_id_e2ad63d9_fk_payment_method_id FOREIGN KEY (method_id) REFERENCES public.payment_method(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_money_account_id_92d00b15_fk_money_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_money_account_id_92d00b15_fk_money_account_id FOREIGN KEY (money_account_id) REFERENCES public.money_account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_posted_by_id_7f760a84_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_posted_by_id_7f760a84_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_reversal_journal_id_43d69685_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_reversal_journal_id_43d69685_fk_journal_entry_id FOREIGN KEY (reversal_journal_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_reversed_by_id_8baf6c2d_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_reversed_by_id_8baf6c2d_fk_app_user_id FOREIGN KEY (reversed_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_updated_by_id_5c13b476_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_updated_by_id_5c13b476_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: payment payment_vendor_id_292bb080_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_vendor_id_292bb080_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: posting_link posting_link_journal_entry_id_0ffc3384_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.posting_link
    ADD CONSTRAINT posting_link_journal_entry_id_0ffc3384_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: posting_link posting_link_source_content_type__109464e6_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.posting_link
    ADD CONSTRAINT posting_link_source_content_type__109464e6_fk_django_co FOREIGN KEY (source_content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: posting_link posting_link_stock_movement_id_7148d9fb_fk_stock_movement_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.posting_link
    ADD CONSTRAINT posting_link_stock_movement_id_7148d9fb_fk_stock_movement_id FOREIGN KEY (stock_movement_id) REFERENCES public.stock_movement(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product_category product_category_cogs_account_id_3b739406_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_category
    ADD CONSTRAINT product_category_cogs_account_id_3b739406_fk_account_id FOREIGN KEY (cogs_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_category_id_640030a0_fk_product_category_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_category_id_640030a0_fk_product_category_id FOREIGN KEY (category_id) REFERENCES public.product_category(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product_category product_category_inventory_account_id_0047a328_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_category
    ADD CONSTRAINT product_category_inventory_account_id_0047a328_fk_account_id FOREIGN KEY (inventory_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product_category product_category_parent_id_f6860923_fk_product_category_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_category
    ADD CONSTRAINT product_category_parent_id_f6860923_fk_product_category_id FOREIGN KEY (parent_id) REFERENCES public.product_category(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product_category product_category_revenue_account_id_271774c9_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_category
    ADD CONSTRAINT product_category_revenue_account_id_271774c9_fk_account_id FOREIGN KEY (revenue_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_cogs_account_id_4f1075ec_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_cogs_account_id_4f1075ec_fk_account_id FOREIGN KEY (cogs_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_created_by_id_0baf418a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_created_by_id_0baf418a_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_default_purchase_tax_code_id_d75e0f83_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_default_purchase_tax_code_id_d75e0f83_fk_tax_code_id FOREIGN KEY (default_purchase_tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_default_sales_tax_code_id_caf14afd_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_default_sales_tax_code_id_caf14afd_fk_tax_code_id FOREIGN KEY (default_sales_tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_expense_account_id_497bbca9_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_expense_account_id_497bbca9_fk_account_id FOREIGN KEY (expense_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_inventory_account_id_573d8fa2_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_inventory_account_id_573d8fa2_fk_account_id FOREIGN KEY (inventory_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_preferred_vendor_id_62e57074_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_preferred_vendor_id_62e57074_fk_vendor_id FOREIGN KEY (preferred_vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product_price product_price_currency_id_0e71f85c_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_price
    ADD CONSTRAINT product_price_currency_id_0e71f85c_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product_price product_price_product_id_831af532_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_price
    ADD CONSTRAINT product_price_product_id_831af532_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_revenue_account_id_775acf59_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_revenue_account_id_775acf59_fk_account_id FOREIGN KEY (revenue_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_unit_id_9b7abe5c_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_unit_id_9b7abe5c_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: product product_updated_by_id_f384b7dc_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_updated_by_id_f384b7dc_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_approved_by_id_6598b560_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_approved_by_id_6598b560_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_created_by_id_8233ddb0_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_created_by_id_8233ddb0_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_currency_id_fe5f18d1_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_currency_id_fe5f18d1_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_fiscal_period_id_a1924ac7_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_fiscal_period_id_a1924ac7_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_goods_receipt_id_297060ef_fk_goods_receipt_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_goods_receipt_id_297060ef_fk_goods_receipt_id FOREIGN KEY (goods_receipt_id) REFERENCES public.goods_receipt(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_journal_entry_id_a5acc538_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_journal_entry_id_a5acc538_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_bill_id_558fef46_fk_purchase_bill_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_bill_id_558fef46_fk_purchase_bill_id FOREIGN KEY (bill_id) REFERENCES public.purchase_bill(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_expense_account_id_83382ac1_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_expense_account_id_83382ac1_fk_account_id FOREIGN KEY (expense_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_product_id_9cbca758_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_product_id_9cbca758_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_purchase_order_line__682e3b42_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_purchase_order_line__682e3b42_fk_purchase_ FOREIGN KEY (purchase_order_line_id) REFERENCES public.purchase_order_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_receipt_line_id_52a4acb8_fk_goods_rec; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_receipt_line_id_52a4acb8_fk_goods_rec FOREIGN KEY (receipt_line_id) REFERENCES public.goods_receipt_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_tax_code_id_e9eebf28_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_tax_code_id_e9eebf28_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_unit_id_75136d87_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_unit_id_75136d87_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill_line purchase_bill_line_warehouse_id_03a867e3_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill_line
    ADD CONSTRAINT purchase_bill_line_warehouse_id_03a867e3_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_payable_account_id_70e01447_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_payable_account_id_70e01447_fk_account_id FOREIGN KEY (payable_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_payment_term_id_163154fc_fk_payment_term_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_payment_term_id_163154fc_fk_payment_term_id FOREIGN KEY (payment_term_id) REFERENCES public.payment_term(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_posted_by_id_4384731a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_posted_by_id_4384731a_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_purchase_order_id_dbe35a02_fk_purchase_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_purchase_order_id_dbe35a02_fk_purchase_order_id FOREIGN KEY (purchase_order_id) REFERENCES public.purchase_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_updated_by_id_40d90996_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_updated_by_id_40d90996_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_vendor_id_cd3b6808_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_vendor_id_cd3b6808_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_bill purchase_bill_warehouse_id_ba5b3965_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_bill
    ADD CONSTRAINT purchase_bill_warehouse_id_ba5b3965_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_approved_by_id_b622b3b4_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_approved_by_id_b622b3b4_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_buyer_id_721f6871_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_buyer_id_721f6871_fk_app_user_id FOREIGN KEY (buyer_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_created_by_id_112332e4_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_created_by_id_112332e4_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_currency_id_c06f147b_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_currency_id_c06f147b_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_fiscal_period_id_12a78e00_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_fiscal_period_id_12a78e00_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_journal_entry_id_4ec5351f_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_journal_entry_id_4ec5351f_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order_line purchase_order_line_order_id_de705a26_fk_purchase_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT purchase_order_line_order_id_de705a26_fk_purchase_order_id FOREIGN KEY (order_id) REFERENCES public.purchase_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order_line purchase_order_line_product_id_f1e43442_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT purchase_order_line_product_id_f1e43442_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order_line purchase_order_line_tax_code_id_32505185_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT purchase_order_line_tax_code_id_32505185_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order_line purchase_order_line_unit_id_eaa3bd21_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT purchase_order_line_unit_id_eaa3bd21_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order_line purchase_order_line_warehouse_id_071f6114_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order_line
    ADD CONSTRAINT purchase_order_line_warehouse_id_071f6114_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_payment_term_id_d8987ec5_fk_payment_term_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_payment_term_id_d8987ec5_fk_payment_term_id FOREIGN KEY (payment_term_id) REFERENCES public.payment_term(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_posted_by_id_515332d4_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_posted_by_id_515332d4_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_updated_by_id_8805b754_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_updated_by_id_8805b754_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_vendor_id_eb91ea9b_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_vendor_id_eb91ea9b_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_order purchase_order_warehouse_id_2bb9b357_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_warehouse_id_2bb9b357_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_created_by_id_eba17aab_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_created_by_id_eba17aab_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_journal_entry_id_514ab2d7_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_journal_entry_id_514ab2d7_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return_line purchase_return_line_bill_line_id_d399e005_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return_line
    ADD CONSTRAINT purchase_return_line_bill_line_id_d399e005_fk_purchase_ FOREIGN KEY (bill_line_id) REFERENCES public.purchase_bill_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return_line purchase_return_line_product_id_47bccc27_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return_line
    ADD CONSTRAINT purchase_return_line_product_id_47bccc27_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return_line purchase_return_line_purchase_return_id_059e0f8a_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return_line
    ADD CONSTRAINT purchase_return_line_purchase_return_id_059e0f8a_fk_purchase_ FOREIGN KEY (purchase_return_id) REFERENCES public.purchase_return(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return_line purchase_return_line_receipt_line_id_11efd351_fk_goods_rec; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return_line
    ADD CONSTRAINT purchase_return_line_receipt_line_id_11efd351_fk_goods_rec FOREIGN KEY (receipt_line_id) REFERENCES public.goods_receipt_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_original_bill_id_ecdaeb83_fk_purchase_bill_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_original_bill_id_ecdaeb83_fk_purchase_bill_id FOREIGN KEY (original_bill_id) REFERENCES public.purchase_bill(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_original_receipt_id_8399b520_fk_goods_rec; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_original_receipt_id_8399b520_fk_goods_rec FOREIGN KEY (original_receipt_id) REFERENCES public.goods_receipt(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_posted_by_id_63b21bcb_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_posted_by_id_63b21bcb_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_updated_by_id_232694ec_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_updated_by_id_232694ec_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_vendor_id_dff5a1a5_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_vendor_id_dff5a1a5_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: purchase_return purchase_return_warehouse_id_2bb8986b_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_return
    ADD CONSTRAINT purchase_return_warehouse_id_2bb8986b_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_approved_by_id_e3fdffe0_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_approved_by_id_e3fdffe0_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_created_by_id_f59404bc_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_created_by_id_f59404bc_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_currency_id_759036a3_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_currency_id_759036a3_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_customer_id_1144f495_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_customer_id_1144f495_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_fiscal_period_id_56b8e021_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_fiscal_period_id_56b8e021_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_journal_entry_id_32ef8464_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_journal_entry_id_32ef8464_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_method_id_a914e860_fk_payment_method_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_method_id_a914e860_fk_payment_method_id FOREIGN KEY (method_id) REFERENCES public.payment_method(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_money_account_id_6e9dd60d_fk_money_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_money_account_id_6e9dd60d_fk_money_account_id FOREIGN KEY (money_account_id) REFERENCES public.money_account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_posted_by_id_79536c86_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_posted_by_id_79536c86_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_sales_credit_note_id_9fa231b7_fk_sales_credit_note_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_sales_credit_note_id_9fa231b7_fk_sales_credit_note_id FOREIGN KEY (sales_credit_note_id) REFERENCES public.sales_credit_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_source_payment_id_7b6a8112_fk_payment_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_source_payment_id_7b6a8112_fk_payment_id FOREIGN KEY (source_payment_id) REFERENCES public.payment(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_updated_by_id_94eddd47_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_updated_by_id_94eddd47_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_vendor_debit_note_id_83c9112e_fk_vendor_debit_note_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_vendor_debit_note_id_83c9112e_fk_vendor_debit_note_id FOREIGN KEY (vendor_debit_note_id) REFERENCES public.vendor_debit_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: refund refund_vendor_id_d3aa90d3_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refund
    ADD CONSTRAINT refund_vendor_id_d3aa90d3_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: role_profile role_profile_group_id_99125240_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_profile
    ADD CONSTRAINT role_profile_group_id_99125240_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_approved_by_id_601f1979_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_approved_by_id_601f1979_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_created_by_id_03b1ad73_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_created_by_id_03b1ad73_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_currency_id_c06504b7_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_currency_id_c06504b7_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_customer_id_e293da96_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_customer_id_e293da96_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_fiscal_period_id_a6039790_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_fiscal_period_id_a6039790_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_journal_entry_id_81b06aaf_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_journal_entry_id_81b06aaf_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_li_credit_note_id_7310afcc_fk_sales_cre; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_li_credit_note_id_7310afcc_fk_sales_cre FOREIGN KEY (credit_note_id) REFERENCES public.sales_credit_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_li_invoice_line_id_99441b18_fk_sales_inv; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_li_invoice_line_id_99441b18_fk_sales_inv FOREIGN KEY (invoice_line_id) REFERENCES public.sales_invoice_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_li_return_line_id_5f590314_fk_sales_ret; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_li_return_line_id_5f590314_fk_sales_ret FOREIGN KEY (return_line_id) REFERENCES public.sales_return_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_li_revenue_account_id_c630909a_fk_account_i; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_li_revenue_account_id_c630909a_fk_account_i FOREIGN KEY (revenue_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_line_product_id_49739ab4_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_line_product_id_49739ab4_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_line_tax_code_id_5f16fddf_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_line_tax_code_id_5f16fddf_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note_line sales_credit_note_line_unit_id_6876950a_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note_line
    ADD CONSTRAINT sales_credit_note_line_unit_id_6876950a_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_original_invoice_id_31015ee0_fk_sales_inv; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_original_invoice_id_31015ee0_fk_sales_inv FOREIGN KEY (original_invoice_id) REFERENCES public.sales_invoice(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_posted_by_id_eebf965a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_posted_by_id_eebf965a_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_sales_return_id_586c3bac_fk_sales_return_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_sales_return_id_586c3bac_fk_sales_return_id FOREIGN KEY (sales_return_id) REFERENCES public.sales_return(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_credit_note sales_credit_note_updated_by_id_e927d005_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_credit_note
    ADD CONSTRAINT sales_credit_note_updated_by_id_e927d005_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_approved_by_id_51492162_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_approved_by_id_51492162_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_created_by_id_c9aad729_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_created_by_id_c9aad729_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_currency_id_a7a68a61_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_currency_id_a7a68a61_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_customer_id_339082a2_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_customer_id_339082a2_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_fiscal_period_id_c6884593_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_fiscal_period_id_c6884593_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_journal_entry_id_bb34c51f_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_journal_entry_id_bb34c51f_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_delivery_line_id_61bcb9f2_fk_delivery_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_delivery_line_id_61bcb9f2_fk_delivery_ FOREIGN KEY (delivery_line_id) REFERENCES public.delivery_note_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_invoice_id_bb18924e_fk_sales_invoice_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_invoice_id_bb18924e_fk_sales_invoice_id FOREIGN KEY (invoice_id) REFERENCES public.sales_invoice(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_product_id_46d5812c_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_product_id_46d5812c_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_revenue_account_id_7e2544af_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_revenue_account_id_7e2544af_fk_account_id FOREIGN KEY (revenue_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_sales_order_line_id_85c7f654_fk_sales_ord; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_sales_order_line_id_85c7f654_fk_sales_ord FOREIGN KEY (sales_order_line_id) REFERENCES public.sales_order_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_tax_code_id_8a942e17_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_tax_code_id_8a942e17_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_unit_id_848122a9_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_unit_id_848122a9_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice_line sales_invoice_line_warehouse_id_ac9c8ab2_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice_line
    ADD CONSTRAINT sales_invoice_line_warehouse_id_ac9c8ab2_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_payment_term_id_0c1ff60e_fk_payment_term_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_payment_term_id_0c1ff60e_fk_payment_term_id FOREIGN KEY (payment_term_id) REFERENCES public.payment_term(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_posted_by_id_6da46b3f_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_posted_by_id_6da46b3f_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_receivable_account_id_db2ee1cc_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_receivable_account_id_db2ee1cc_fk_account_id FOREIGN KEY (receivable_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_reversed_by_journal__d7b68300_fk_journal_e; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_reversed_by_journal__d7b68300_fk_journal_e FOREIGN KEY (reversed_by_journal_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_sales_order_id_11be25e2_fk_sales_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_sales_order_id_11be25e2_fk_sales_order_id FOREIGN KEY (sales_order_id) REFERENCES public.sales_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_updated_by_id_59776dfa_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_updated_by_id_59776dfa_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_invoice sales_invoice_warehouse_id_b3873f38_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_invoice
    ADD CONSTRAINT sales_invoice_warehouse_id_b3873f38_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_approved_by_id_946a87c3_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_approved_by_id_946a87c3_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_created_by_id_1b611bc2_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_created_by_id_1b611bc2_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_currency_id_6468265f_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_currency_id_6468265f_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_customer_id_798c889f_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_customer_id_798c889f_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_fiscal_period_id_62bd2dfe_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_fiscal_period_id_62bd2dfe_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_journal_entry_id_30112851_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_journal_entry_id_30112851_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order_line sales_order_line_order_id_558b9683_fk_sales_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT sales_order_line_order_id_558b9683_fk_sales_order_id FOREIGN KEY (order_id) REFERENCES public.sales_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order_line sales_order_line_product_id_69b5b691_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT sales_order_line_product_id_69b5b691_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order_line sales_order_line_tax_code_id_e7eb3515_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT sales_order_line_tax_code_id_e7eb3515_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order_line sales_order_line_unit_id_78e7109e_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT sales_order_line_unit_id_78e7109e_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order_line sales_order_line_warehouse_id_74ac00e3_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order_line
    ADD CONSTRAINT sales_order_line_warehouse_id_74ac00e3_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_payment_term_id_286c63b2_fk_payment_term_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_payment_term_id_286c63b2_fk_payment_term_id FOREIGN KEY (payment_term_id) REFERENCES public.payment_term(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_posted_by_id_5cce5439_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_posted_by_id_5cce5439_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_salesperson_id_60448194_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_salesperson_id_60448194_fk_app_user_id FOREIGN KEY (salesperson_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_updated_by_id_7927d1d4_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_updated_by_id_7927d1d4_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_order sales_order_warehouse_id_a7eb0089_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_order
    ADD CONSTRAINT sales_order_warehouse_id_a7eb0089_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_created_by_id_cefeee3c_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_created_by_id_cefeee3c_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_customer_id_a731a0d8_fk_customer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_customer_id_a731a0d8_fk_customer_id FOREIGN KEY (customer_id) REFERENCES public.customer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_journal_entry_id_95826a9b_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_journal_entry_id_95826a9b_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return_line sales_return_line_delivery_line_id_64549345_fk_delivery_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return_line
    ADD CONSTRAINT sales_return_line_delivery_line_id_64549345_fk_delivery_ FOREIGN KEY (delivery_line_id) REFERENCES public.delivery_note_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return_line sales_return_line_invoice_line_id_a01eed9e_fk_sales_inv; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return_line
    ADD CONSTRAINT sales_return_line_invoice_line_id_a01eed9e_fk_sales_inv FOREIGN KEY (invoice_line_id) REFERENCES public.sales_invoice_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return_line sales_return_line_product_id_f1721143_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return_line
    ADD CONSTRAINT sales_return_line_product_id_f1721143_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return_line sales_return_line_sales_return_id_d017c92e_fk_sales_return_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return_line
    ADD CONSTRAINT sales_return_line_sales_return_id_d017c92e_fk_sales_return_id FOREIGN KEY (sales_return_id) REFERENCES public.sales_return(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_original_delivery_id_e823de8e_fk_delivery_note_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_original_delivery_id_e823de8e_fk_delivery_note_id FOREIGN KEY (original_delivery_id) REFERENCES public.delivery_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_original_invoice_id_6b20840b_fk_sales_invoice_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_original_invoice_id_6b20840b_fk_sales_invoice_id FOREIGN KEY (original_invoice_id) REFERENCES public.sales_invoice(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_posted_by_id_8e92fcc8_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_posted_by_id_8e92fcc8_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_updated_by_id_2673e656_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_updated_by_id_2673e656_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: sales_return sales_return_warehouse_id_389e6f5c_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sales_return
    ADD CONSTRAINT sales_return_warehouse_id_389e6f5c_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_approved_by_id_f865f5f1_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_approved_by_id_f865f5f1_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_created_by_id_e2f683f6_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_created_by_id_e2f683f6_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_journal_entry_id_ebf7dd5e_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_journal_entry_id_ebf7dd5e_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment_line stock_adjustment_lin_adjustment_id_211381eb_fk_stock_adj; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment_line
    ADD CONSTRAINT stock_adjustment_lin_adjustment_id_211381eb_fk_stock_adj FOREIGN KEY (adjustment_id) REFERENCES public.stock_adjustment(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment_line stock_adjustment_line_product_id_78420274_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment_line
    ADD CONSTRAINT stock_adjustment_line_product_id_78420274_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_posted_by_id_29b953b5_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_posted_by_id_29b953b5_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_reason_id_961df6a2_fk_adjustment_reason_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_reason_id_961df6a2_fk_adjustment_reason_id FOREIGN KEY (reason_id) REFERENCES public.adjustment_reason(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_updated_by_id_f7e6f104_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_updated_by_id_f7e6f104_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_adjustment stock_adjustment_warehouse_id_2ecf196d_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_adjustment
    ADD CONSTRAINT stock_adjustment_warehouse_id_2ecf196d_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_balance stock_balance_product_id_488344c4_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_balance
    ADD CONSTRAINT stock_balance_product_id_488344c4_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_balance stock_balance_warehouse_id_0fdcf6d6_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_balance
    ADD CONSTRAINT stock_balance_warehouse_id_0fdcf6d6_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_movement stock_movement_created_by_id_4d0216fc_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_created_by_id_4d0216fc_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_movement stock_movement_journal_entry_id_d9d4bc80_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_journal_entry_id_d9d4bc80_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_movement stock_movement_product_id_126b1c0d_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_product_id_126b1c0d_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_movement stock_movement_reverses_id_b4bc039d_fk_stock_movement_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_reverses_id_b4bc039d_fk_stock_movement_id FOREIGN KEY (reverses_id) REFERENCES public.stock_movement(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_movement stock_movement_source_content_type__19e5e186_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_source_content_type__19e5e186_fk_django_co FOREIGN KEY (source_content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_movement stock_movement_warehouse_id_806239d6_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_movement
    ADD CONSTRAINT stock_movement_warehouse_id_806239d6_fk_warehouse_id FOREIGN KEY (warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer stock_transfer_created_by_id_c5517845_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_created_by_id_c5517845_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer stock_transfer_from_warehouse_id_d1e866ae_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_from_warehouse_id_d1e866ae_fk_warehouse_id FOREIGN KEY (from_warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer stock_transfer_journal_entry_id_b2dcf187_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_journal_entry_id_b2dcf187_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer_line stock_transfer_line_product_id_1ce71084_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer_line
    ADD CONSTRAINT stock_transfer_line_product_id_1ce71084_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer_line stock_transfer_line_transfer_id_4403a83b_fk_stock_transfer_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer_line
    ADD CONSTRAINT stock_transfer_line_transfer_id_4403a83b_fk_stock_transfer_id FOREIGN KEY (transfer_id) REFERENCES public.stock_transfer(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer stock_transfer_posted_by_id_53dca9ac_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_posted_by_id_53dca9ac_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer stock_transfer_to_warehouse_id_9c565097_fk_warehouse_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_to_warehouse_id_9c565097_fk_warehouse_id FOREIGN KEY (to_warehouse_id) REFERENCES public.warehouse(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: stock_transfer stock_transfer_updated_by_id_8439895a_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_transfer
    ADD CONSTRAINT stock_transfer_updated_by_id_8439895a_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: tax_code tax_code_input_tax_account_id_1b9fdc38_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_code
    ADD CONSTRAINT tax_code_input_tax_account_id_1b9fdc38_fk_account_id FOREIGN KEY (input_tax_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: tax_code tax_code_output_tax_account_id_2b416174_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_code
    ADD CONSTRAINT tax_code_output_tax_account_id_2b416174_fk_account_id FOREIGN KEY (output_tax_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: unit_of_measure unit_of_measure_base_unit_id_46d6f302_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.unit_of_measure
    ADD CONSTRAINT unit_of_measure_base_unit_id_46d6f302_fk_unit_of_measure_id FOREIGN KEY (base_unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_advance_account_id_da8e6198_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_advance_account_id_da8e6198_fk_account_id FOREIGN KEY (advance_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_created_by_id_6eff9955_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_created_by_id_6eff9955_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_currency_id_2d9d42db_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_currency_id_2d9d42db_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_approved_by_id_e08ab371_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_approved_by_id_e08ab371_fk_app_user_id FOREIGN KEY (approved_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_created_by_id_c60f5762_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_created_by_id_c60f5762_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_currency_id_97a652a9_fk_currency_code; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_currency_id_97a652a9_fk_currency_code FOREIGN KEY (currency_id) REFERENCES public.currency(code) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_fiscal_period_id_88a93af0_fk_fiscal_period_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_fiscal_period_id_88a93af0_fk_fiscal_period_id FOREIGN KEY (fiscal_period_id) REFERENCES public.fiscal_period(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_journal_entry_id_ea2b9361_fk_journal_entry_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_journal_entry_id_ea2b9361_fk_journal_entry_id FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entry(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_li_bill_line_id_c55d41dd_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_li_bill_line_id_c55d41dd_fk_purchase_ FOREIGN KEY (bill_line_id) REFERENCES public.purchase_bill_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_li_debit_note_id_55b38a5b_fk_vendor_de; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_li_debit_note_id_55b38a5b_fk_vendor_de FOREIGN KEY (debit_note_id) REFERENCES public.vendor_debit_note(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_li_expense_account_id_77caaa0a_fk_account_i; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_li_expense_account_id_77caaa0a_fk_account_i FOREIGN KEY (expense_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_li_return_line_id_81ab4a1a_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_li_return_line_id_81ab4a1a_fk_purchase_ FOREIGN KEY (return_line_id) REFERENCES public.purchase_return_line(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_line_product_id_b3c85f4f_fk_product_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_line_product_id_b3c85f4f_fk_product_id FOREIGN KEY (product_id) REFERENCES public.product(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_line_tax_code_id_1aee6796_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_line_tax_code_id_1aee6796_fk_tax_code_id FOREIGN KEY (tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note_line vendor_debit_note_line_unit_id_c94ece3f_fk_unit_of_measure_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note_line
    ADD CONSTRAINT vendor_debit_note_line_unit_id_c94ece3f_fk_unit_of_measure_id FOREIGN KEY (unit_id) REFERENCES public.unit_of_measure(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_original_bill_id_bc039eb6_fk_purchase_bill_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_original_bill_id_bc039eb6_fk_purchase_bill_id FOREIGN KEY (original_bill_id) REFERENCES public.purchase_bill(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_posted_by_id_c90ff54d_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_posted_by_id_c90ff54d_fk_app_user_id FOREIGN KEY (posted_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_purchase_return_id_7e50ee88_fk_purchase_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_purchase_return_id_7e50ee88_fk_purchase_ FOREIGN KEY (purchase_return_id) REFERENCES public.purchase_return(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_updated_by_id_c5bfa453_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_updated_by_id_c5bfa453_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor_debit_note vendor_debit_note_vendor_id_14767e87_fk_vendor_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_debit_note
    ADD CONSTRAINT vendor_debit_note_vendor_id_14767e87_fk_vendor_id FOREIGN KEY (vendor_id) REFERENCES public.vendor(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_default_expense_account_id_5cab5f2f_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_default_expense_account_id_5cab5f2f_fk_account_id FOREIGN KEY (default_expense_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_default_tax_code_id_8c6c2b43_fk_tax_code_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_default_tax_code_id_8c6c2b43_fk_tax_code_id FOREIGN KEY (default_tax_code_id) REFERENCES public.tax_code(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_payable_account_id_45bbd262_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_payable_account_id_45bbd262_fk_account_id FOREIGN KEY (payable_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_payment_term_id_e18b894b_fk_payment_term_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_payment_term_id_e18b894b_fk_payment_term_id FOREIGN KEY (payment_term_id) REFERENCES public.payment_term(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: vendor vendor_updated_by_id_1ef1c2d5_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor
    ADD CONSTRAINT vendor_updated_by_id_1ef1c2d5_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: warehouse warehouse_created_by_id_0081bffd_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse
    ADD CONSTRAINT warehouse_created_by_id_0081bffd_fk_app_user_id FOREIGN KEY (created_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: warehouse warehouse_inventory_account_id_cae197de_fk_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse
    ADD CONSTRAINT warehouse_inventory_account_id_cae197de_fk_account_id FOREIGN KEY (inventory_account_id) REFERENCES public.account(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: warehouse warehouse_manager_id_88a15ae0_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse
    ADD CONSTRAINT warehouse_manager_id_88a15ae0_fk_app_user_id FOREIGN KEY (manager_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: warehouse warehouse_updated_by_id_1abb8a8f_fk_app_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse
    ADD CONSTRAINT warehouse_updated_by_id_1abb8a8f_fk_app_user_id FOREIGN KEY (updated_by_id) REFERENCES public.app_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- PostgreSQL database dump complete
--

\unrestrict RSgRzEadAymTcB79xAhv6jIq2boINJibSiFJRSillRAU6qkrk2EYp8mfZ9nis5h

