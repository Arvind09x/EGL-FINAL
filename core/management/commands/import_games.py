"""
Management command to import game catalog from CSV.
Covers task I8-2.

CSV format (with header row):
    name, copy_count, security_deposit, category, min_players, max_players, description

Optional columns: mrp, deposit_category (D/C/B/A/S; overrides security_deposit).

Usage:
    python manage.py import_games games.csv
"""

import csv

from django.core.management.base import BaseCommand

from core.models import GameCopy, GameTitle


class Command(BaseCommand):
    help = "Import game catalog from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the CSV file.")

    def handle(self, *args, **options):
        path = options["csv_file"]
        created_titles = 0
        created_copies = 0

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row["name"].strip()
                copy_count = int(row.get("copy_count", 1))
                security_deposit = float(row.get("security_deposit", 0))
                category = row.get("category", "").strip()
                min_players = int(row.get("min_players", 1))
                max_players = int(row.get("max_players", 4))
                description = row.get("description", "").strip()
                mrp = float(row.get("mrp") or 0)
                deposit_category = (row.get("deposit_category") or "").strip().upper()

                title, t_created = GameTitle.objects.get_or_create(
                    name=name,
                    defaults={
                        "security_deposit": security_deposit,
                        "category": category,
                        "min_players": min_players,
                        "max_players": max_players,
                        "description": description,
                        "mrp": mrp,
                        "deposit_category": deposit_category,
                    },
                )
                if t_created:
                    created_titles += 1

                existing_copies = title.copies.count()
                for i in range(existing_copies + 1, existing_copies + copy_count + 1):
                    GameCopy.objects.get_or_create(
                        game_title=title,
                        copy_label=f"{i:03d}",
                    )
                    created_copies += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {created_titles} titles, {created_copies} copies created."
            )
        )
