from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from hub.water_dump import import_dump


class Command(BaseCommand):
    help = "Import water history (pump / tank / sump / flow / pH readings) from the platform's MySQL dump."

    def add_arguments(self, parser):
        parser.add_argument("dump", nargs="?", default=settings.WATER_DUMP)

    def handle(self, *args, **o):
        if not o["dump"] or not Path(o["dump"]).exists():
            raise CommandError("Pass the dump path or set WATER_DUMP in .env")
        res = import_dump(Path(o["dump"]), settings.WATER_HISTORY)
        self.stdout.write(self.style.SUCCESS(f"Imported {res['n']} readings from {res['devices']} devices "
                                             f"({res['first']:%d %b %Y} – {res['last']:%d %b %Y})"))
