"""
Management command to import RA and Club/Society email allowlists from CSV.
Covers task I8-3.

CSV format:
    email, name, type

Where type is 'ra' or 'club'.

Usage:
    python manage.py import_allowlists allowlists.csv
"""

import csv

from django.core.management.base import BaseCommand

from core.models import ClubSocietyEmailAllowlist, RAEmailAllowlist


class Command(BaseCommand):
    help = "Import RA and Club/Society email allowlists from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the CSV file.")

    def handle(self, *args, **options):
        path = options["csv_file"]
        ra_count = 0
        club_count = 0

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                email = row["email"].strip().lower()
                name = row.get("name", "").strip()
                entry_type = row.get("type", "").strip().lower()

                if entry_type == "ra":
                    _, created = RAEmailAllowlist.objects.get_or_create(
                        email=email, defaults={"name": name}
                    )
                    if created:
                        ra_count += 1
                elif entry_type == "club":
                    _, created = ClubSocietyEmailAllowlist.objects.get_or_create(
                        email=email, defaults={"name": name}
                    )
                    if created:
                        club_count += 1
                else:
                    self.stderr.write(
                        self.style.WARNING(f"Skipping unknown type '{entry_type}' for {email}")
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {ra_count} RA entries, {club_count} Club/Society entries created."
            )
        )
