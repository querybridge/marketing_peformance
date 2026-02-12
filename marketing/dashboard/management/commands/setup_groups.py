"""
Management command to create the 6 authorization groups.
Idempotent — safe to re-run.
"""

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from dashboard.auth_config import GROUP_NAMES


class Command(BaseCommand):
    help = "Create the dashboard authorization groups."

    def handle(self, *args, **options):
        for name in GROUP_NAMES:
            _, created = Group.objects.get_or_create(name=name)
            status = "created" if created else "exists"
            self.stdout.write(f"  {name} — {status}")
        self.stdout.write(self.style.SUCCESS("Done."))
