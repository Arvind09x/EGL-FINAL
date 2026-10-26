"""
Management command to import / update the game catalogue from a CSV.
Covers task I8-2.

One row per physical copy. Columns (header row required):

    name             title (matched case-insensitively against existing titles)
    copy_label       001, 002, ... unique within the title
    location         where the copy is kept (members only)
    status           available | damaged | lost   (damaged/lost are not lendable)
    deposit_category D, C, B, A or S (sets the security deposit)
    club_deposit     deposit for clubs/groups in INR
    mrp              MRP in INR (optional)
    genre            free-text genre (title-level)
    min_players, max_players, description   (title-level)
    notes            public note about this copy (optional)

Title-level columns (genre, players, description, mrp) only need to appear on
one row of a title; blank values never overwrite existing data. Running the
command again updates existing titles and copies in place.

Usage:
    python manage.py import_games data/games.csv
"""

import csv
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from core.models import Checkout, GameCopy, GameTitle

STATUS_TO_CONDITION = {
    "damaged": GameCopy.Condition.DAMAGED,
    "lost": GameCopy.Condition.LOST,
}


def _decimal(raw, line):
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise CommandError(f"Line {line}: '{raw}' is not a number.")


def _int(raw, line):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise CommandError(f"Line {line}: '{raw}' is not a whole number.")


class Command(BaseCommand):
    help = "Import or update the game catalogue (one row per copy) from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the CSV file.")

    def handle(self, *args, **options):
        with open(options["csv_file"], newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            required = {"name", "copy_label", "status", "deposit_category"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise CommandError(f"CSV is missing columns: {', '.join(sorted(missing))}")
            rows = [(i, row) for i, row in enumerate(reader, start=2) if (row.get("name") or "").strip()]

        # Title-level data: first non-empty value across a title's rows.
        titles = {}
        for line, row in rows:
            key = row["name"].strip().lower()
            data = titles.setdefault(key, {"name": row["name"].strip()})
            category = (row.get("deposit_category") or "").strip().upper()
            if category and category not in GameTitle.DEPOSIT_BY_CATEGORY:
                raise CommandError(f"Line {line}: unknown deposit category '{category}'.")
            for field, value in (
                ("deposit_category", category),
                ("category", (row.get("genre") or "").strip()),
                ("description", (row.get("description") or "").strip()),
                ("club_deposit", _decimal(row.get("club_deposit"), line)),
                ("mrp", _decimal(row.get("mrp"), line)),
                ("min_players", _int(row.get("min_players"), line)),
                ("max_players", _int(row.get("max_players"), line)),
            ):
                if value not in (None, "") and field not in data:
                    data[field] = value

        created_titles = updated_titles = created_copies = updated_copies = 0
        title_objs = {}
        for key, data in titles.items():
            fields = {k: v for k, v in data.items() if k != "name"}
            title = GameTitle.objects.filter(name__iexact=data["name"]).first()
            if title is None:
                title = GameTitle(name=data["name"], **fields)
                created_titles += 1
            else:
                for k, v in fields.items():
                    setattr(title, k, v)
                updated_titles += 1
            title.save()  # also derives security_deposit from the category
            title_objs[key] = title

        for line, row in rows:
            title = title_objs[row["name"].strip().lower()]
            label = (row.get("copy_label") or "").strip()
            if not label:
                raise CommandError(f"Line {line}: copy_label is required.")
            status = (row.get("status") or "available").strip().lower()
            if status not in ("available", "damaged", "lost"):
                raise CommandError(f"Line {line}: unknown status '{status}'.")

            copy, created = GameCopy.objects.get_or_create(game_title=title, copy_label=label)
            copy.location = (row.get("location") or "").strip()
            if (row.get("notes") or "").strip():
                copy.notes = row["notes"].strip()

            on_loan = Checkout.objects.filter(game_copy=copy, return_time__isnull=True).exists()
            if status == "available":
                if copy.condition in (GameCopy.Condition.DAMAGED, GameCopy.Condition.LOST):
                    copy.condition = GameCopy.Condition.GOOD
                copy.is_available = not on_loan
            else:
                copy.condition = STATUS_TO_CONDITION[status]
                copy.is_available = False
            copy.save()

            if created:
                created_copies += 1
            else:
                updated_copies += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done: {created_titles} titles created, {updated_titles} updated; "
            f"{created_copies} copies created, {updated_copies} updated."
        ))
