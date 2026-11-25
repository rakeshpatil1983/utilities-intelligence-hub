from django.core.management.base import BaseCommand

from hub import meters


class Command(BaseCommand):
    help = "Snapshot the predictive meter-health results (meter MHI, topology, DT twin, interval logs) into data/meters."

    def handle(self, *args, **o):
        res = meters.snapshot()
        self.stdout.write(self.style.SUCCESS(f"Snapshotted {res['meters']} smart meters"))
