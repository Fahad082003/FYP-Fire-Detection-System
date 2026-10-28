import re

from django.conf import settings
from django.db import transaction

SCRIPT = settings.BASE_DIR / "schema" / "sentinel.sql"


def run_schema_script(connection) -> None:
    """Creates the Sentinel tables (and EF Core's migration history, so the .NET API accepts the database too).
    All or nothing: one transaction (the script's own per-migration transactions span batches, which MARS connections refuse)."""
    batches = re.split(r"^\s*GO\s*$", SCRIPT.read_text(encoding="utf-8-sig"), flags=re.MULTILINE)
    with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
        for batch in batches:
            if batch.strip() not in ("", "BEGIN TRANSACTION;", "COMMIT;"):
                cursor.execute(batch)
