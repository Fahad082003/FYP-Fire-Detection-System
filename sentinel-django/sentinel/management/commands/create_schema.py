from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from sentinel.schema import SCRIPT, run_schema_script


class Command(BaseCommand):
    help = "Creates the Sentinel tables in an EMPTY database (fresh install). Refuses if they already exist."

    def handle(self, *args, **options):
        name = connection.settings_dict["NAME"]
        if "CameraFeeds" in connection.introspection.table_names():
            raise CommandError(f"Database '{name}' already has the Sentinel tables; nothing was changed.")
        run_schema_script(connection)
        self.stdout.write(self.style.SUCCESS(f"Created the Sentinel tables in '{name}' from {SCRIPT.name}."))
