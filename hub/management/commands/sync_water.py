from django.core.management.base import BaseCommand, CommandError

from hub import water_live


class Command(BaseCommand):
    help = "Pull water readings from the live platform API (credentials from .env)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Backfill window for devices seen for the first time")

    def handle(self, *args, **o):
        try:
            res = water_live.sync(backfill_days=o["days"])
        except water_live.NotConfigured as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"{res['devices']} water devices, {res['updated']} updated, {res['rows']} new readings"))
