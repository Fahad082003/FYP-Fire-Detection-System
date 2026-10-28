from django.db import connections


class NoMigrationsOnLiveDatabase:
    """`manage.py migrate` only ever touches throwaway test databases (named test_*). The live SentinelDb keeps its EF Core
    schema untouched: Django needs no tables of its own there (sessions are signed cookies)."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return connections[db].settings_dict["NAME"].startswith("test_")
