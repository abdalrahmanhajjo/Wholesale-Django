from django.db import migrations


HARDEN_PUBLIC_SCHEMA = r"""
DO $security$
DECLARE
    target record;
    api_role text;
BEGIN
    -- RLS is defense in depth for every application table in Supabase's
    -- Data-API-exposed public schema. The Django connection uses the table
    -- owner and therefore continues to use the ORM normally.
    FOR target IN
        SELECT n.nspname AS schema_name, c.relname AS object_name
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind IN ('r', 'p')
           AND c.relname <> 'spatial_ref_sys'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
            target.schema_name,
            target.object_name
        );
    END LOOP;

    -- These roles are Supabase-specific and are absent from ordinary local
    -- PostgreSQL installations. Harden whichever roles exist independently so
    -- a partially provisioned environment is never left exposed.
    FOR api_role IN
        SELECT rolname
          FROM pg_roles
         WHERE rolname IN ('anon', 'authenticated')
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I',
            api_role
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I',
            api_role
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM %I',
            api_role
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM %I',
            api_role
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON SEQUENCES FROM %I',
            api_role
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM %I',
            api_role
        );
    END LOOP;
END
$security$;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0006_seed_role_permissions")]

    operations = [
        migrations.RunSQL(
            sql=HARDEN_PUBLIC_SCHEMA,
            reverse_sql=migrations.RunSQL.noop,
        )
    ]
