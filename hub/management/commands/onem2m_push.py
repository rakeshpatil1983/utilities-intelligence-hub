from django.core.management.base import BaseCommand, CommandError

from hub import onem2m


class Command(BaseCommand):
    help = "Register the hub as a oneM2M AE in ONEM2M_CSE and publish every device's status as contentInstances."

    def add_arguments(self, parser):
        parser.add_argument("--subscribe", help="also subscribe the hub to this CSE path (e.g. a field gateway's container)")

    def handle(self, *args, **o):
        try:
            n = onem2m.push(log=self.stdout.write)
            if o.get("subscribe"):
                onem2m.subscribe(o["subscribe"], log=self.stdout.write)
        except Exception as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"published {n} contentInstances"))
