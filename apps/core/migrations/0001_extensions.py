"""
PostgreSQL extensions the schema depends on. Must run before any table that
carries an exclusion constraint or a trigram index.

  btree_gist  lets an ExclusionConstraint mix an equality column (fiscal_year)
              with a range operator — required for the no-overlap rules.
  pg_trgm     trigram similarity, for PTY-007 duplicate-party detection.

Both adopted from the colleague's schema.
"""

from django.contrib.postgres.operations import BtreeGistExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [BtreeGistExtension(), TrigramExtension()]
