"""Tests run on a throwaway SQL Server database built from schema/sentinel.sql, the exact EF Core schema
(the models are unmanaged, so Django itself would create no tables for them). The database is dropped afterwards."""
from django.test.runner import DiscoverRunner

from .schema import run_schema_script


class SchemaTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        config = super().setup_databases(**kwargs)
        # Only the databases Django just created for this run; never a connection that still points at a real database
        # (a run with no database tests creates none, and the default connection is then the live one).
        for connection, _old_name, created in config:
            name = connection.settings_dict["NAME"]
            if not (created and name.startswith("test_")):
                raise RuntimeError(f"Refusing to build the schema in {name!r}: not a throwaway test database.")
            run_schema_script(connection)
        return config
