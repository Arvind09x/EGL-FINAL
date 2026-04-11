"""
Management command to generate recurring WeeklySlot records.
Covers task S7-1: Tuesday/Friday 18:30–19:30 Asia/Kolkata slots.

Usage:
    python manage.py generate_slots --start 2026-01-06 --end 2026-05-31
"""

from datetime import date, time, timedelta

from django.core.management.base import BaseCommand

from core.models import WeeklySlot


class Command(BaseCommand):
    help = "Generate recurring Tuesday/Friday WeeklySlot records for a date range."

    def add_arguments(self, parser):
        parser.add_argument(
            "--start",
            type=str,
            required=True,
            help="Start date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--end",
            type=str,
            required=True,
            help="End date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--start-time",
            type=str,
            default="18:30",
            help="Slot start time (HH:MM). Default: 18:30.",
        )
        parser.add_argument(
            "--end-time",
            type=str,
            default="19:30",
            help="Slot end time (HH:MM). Default: 19:30.",
        )
        parser.add_argument(
            "--days",
            type=str,
            default="1,4",
            help="Comma-separated weekday numbers (0=Mon, 1=Tue, ..., 4=Fri). Default: 1,4 (Tue/Fri).",
        )

    def handle(self, *args, **options):
        start_date = date.fromisoformat(options["start"])
        end_date = date.fromisoformat(options["end"])
        start_h, start_m = map(int, options["start_time"].split(":"))
        end_h, end_m = map(int, options["end_time"].split(":"))
        slot_start = time(start_h, start_m)
        slot_end = time(end_h, end_m)
        target_days = set(int(d) for d in options["days"].split(","))

        created = 0
        skipped = 0
        current = start_date

        while current <= end_date:
            if current.weekday() in target_days:
                _, was_created = WeeklySlot.objects.get_or_create(
                    date=current,
                    start_time=slot_start,
                    defaults={"end_time": slot_end},
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1
            current += timedelta(days=1)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {created} slots created, {skipped} already existed."
            )
        )
