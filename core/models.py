"""
Core models for the ESHTO Games Library System.

Covers: User, BorrowerProfile, GameTitle, GameCopy, WeeklySlot,
        Reservation, Checkout, Charge, Incident, Restriction, AuditLog,
        and allowlist tables for RA / Club-Society emails.
"""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


# =====================================================================
# M1-1  Custom User
# =====================================================================
class User(AbstractUser):
    """Ashoka user who signs in via Google OAuth."""

    phone = models.CharField(max_length=15, blank=True)
    is_member = models.BooleanField(
        default=False,
        help_text="Designates whether this user is an ESHTO member (desk staff).",
    )
    is_senior_admin = models.BooleanField(
        default=False,
        help_text="Designates whether this user can manage roles and high-risk overrides.",
    )

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.email or self.username


# =====================================================================
# M1-2  Borrower Profile
# =====================================================================
class BorrowerProfile(models.Model):
    """Policy-specific profile linked 1-to-1 to a User."""

    class PolicyType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        CLUB_SOCIETY = "club_society", "Club / Society"
        RA = "ra", "Resident Advisor"
        EESHTO_MEMBER = "eeshto_member", "ESHTO Member"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="borrower_profile",
    )
    policy_type = models.CharField(
        max_length=20,
        choices=PolicyType.choices,
        default=PolicyType.INDIVIDUAL,
    )

    def __str__(self):
        return f"{self.user.email} ({self.get_policy_type_display()})"


# =====================================================================
# M1-3  Game Title
# =====================================================================
class GameTitle(models.Model):
    """A board-game title in the library catalogue."""

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="games/", blank=True, null=True)
    min_players = models.PositiveSmallIntegerField(default=1)
    max_players = models.PositiveSmallIntegerField(default=4)
    category = models.CharField(max_length=100, blank=True)
    security_deposit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Security deposit in INR required to borrow this game.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def available_copies(self):
        return self.copies.filter(is_available=True, condition__in=["good", "fair"])


# =====================================================================
# M1-4  Game Copy
# =====================================================================
class GameCopy(models.Model):
    """A physical copy / box of a GameTitle."""

    class Condition(models.TextChoices):
        GOOD = "good", "Good"
        FAIR = "fair", "Fair"
        DAMAGED = "damaged", "Damaged"
        LOST = "lost", "Lost"

    game_title = models.ForeignKey(
        GameTitle, on_delete=models.CASCADE, related_name="copies"
    )
    copy_label = models.CharField(
        max_length=50,
        help_text="Barcode, shelf label, or human-friendly identifier.",
    )
    condition = models.CharField(
        max_length=10, choices=Condition.choices, default=Condition.GOOD
    )
    is_available = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Game copies"
        ordering = ["game_title", "copy_label"]
        unique_together = [("game_title", "copy_label")]

    def __str__(self):
        return f"{self.game_title.name} [{self.copy_label}]"


# =====================================================================
# M1-5  Weekly Slot
# =====================================================================
class WeeklySlot(models.Model):
    """A specific pickup/return session (usually Tue/Fri 18:30–19:30 IST)."""

    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_cancelled = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_slots",
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["date", "start_time"]
        unique_together = [("date", "start_time")]

    def __str__(self):
        cancelled = " [CANCELLED]" if self.is_cancelled else ""
        return f"{self.date} {self.start_time}–{self.end_time}{cancelled}"

    @property
    def start_datetime(self):
        """Return a timezone-aware datetime combining date + start_time in IST."""
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        from datetime import datetime
        return datetime.combine(self.date, self.start_time, tzinfo=tz)


# =====================================================================
# M1-6  Reservation
# =====================================================================
class Reservation(models.Model):
    """A confirmed booking tying a borrower, copy, and pickup/return slots."""

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No-show"
        COMPLETED = "completed", "Completed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    game_copy = models.ForeignKey(
        GameCopy, on_delete=models.CASCADE, related_name="reservations"
    )
    pickup_slot = models.ForeignKey(
        WeeklySlot, on_delete=models.PROTECT, related_name="pickup_reservations"
    )
    return_slot = models.ForeignKey(
        WeeklySlot,
        on_delete=models.PROTECT,
        related_name="return_reservations",
        help_text="Default = next slot; extended = same weekday next week.",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.CONFIRMED
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"Reservation #{self.pk}: {self.user.email} – "
            f"{self.game_copy} @ {self.pickup_slot.date}"
        )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["game_copy", "pickup_slot"],
                condition=models.Q(status="confirmed"),
                name="unique_confirmed_copy_per_slot",
            )
        ]


# =====================================================================
# M1-7  Checkout
# =====================================================================
class Checkout(models.Model):
    """Created by a desk member when a borrower picks up a game."""

    reservation = models.OneToOneField(
        Reservation,
        on_delete=models.CASCADE,
        related_name="checkout",
        null=True,
        blank=True,
        help_text="Null for walk-in checkouts.",
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="checkouts",
    )
    game_copy = models.ForeignKey(
        GameCopy, on_delete=models.CASCADE, related_name="checkouts"
    )
    handoff_member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="handled_checkouts",
    )
    pickup_location = models.CharField(
        max_length=255,
        default="Student Commons, 2nd floor of the mess, near the couches between the meeting rooms",
    )
    condition_notes = models.TextField(blank=True)
    deposit_status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("collected", "Collected"),
            ("refunded", "Refunded"),
            ("waived", "Waived"),
        ],
        default="pending",
    )

    due_date_slot = models.ForeignKey(
        WeeklySlot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="due_checkouts",
        help_text="The slot by which the game must be returned.",
    )
    is_lost_candidate = models.BooleanField(
        default=False,
        help_text="Flagged if overdue ≥ 30 days past due date.",
    )

    checkout_time = models.DateTimeField(default=timezone.now)
    return_time = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-checkout_time"]

    def __str__(self):
        status = "returned" if self.return_time else "active"
        return f"Checkout #{self.pk}: {self.borrower.email} – {self.game_copy} ({status})"


# =====================================================================
# M1-8  Charge
# =====================================================================
class Charge(models.Model):
    """Financial record for deposits, fees, and replacement charges."""

    class ChargeType(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        LATE_FEE = "late_fee", "Late Fee"
        DAMAGE_FEE = "damage_fee", "Damage Fee"
        LOST_REPLACEMENT = "lost_replacement", "Lost-Game Replacement"

    class ChargeStatus(models.TextChoices):
        COLLECTED = "collected", "Collected"
        REFUNDED = "refunded", "Refunded"
        PAID = "paid", "Paid"
        UNPAID = "unpaid", "Unpaid"
        WAIVED = "waived", "Waived"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="charges",
    )
    charge_type = models.CharField(max_length=20, choices=ChargeType.choices)
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    status = models.CharField(
        max_length=10, choices=ChargeStatus.choices, default=ChargeStatus.UNPAID
    )
    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
    )
    incident = models.ForeignKey(
        "Incident",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.get_charge_type_display()} ₹{self.amount} – "
            f"{self.user.email} ({self.get_status_display()})"
        )


# =====================================================================
# M1-9  Incident
# =====================================================================
class Incident(models.Model):
    """Record of damage, missing components, lost games, no-shows, or overrides."""

    class IncidentType(models.TextChoices):
        DAMAGE = "damage", "Damage"
        MISSING_COMPONENTS = "missing_components", "Missing Components"
        LOST_GAME = "lost_game", "Lost Game"
        NO_SHOW = "no_show", "No-Show"
        OVERRIDE = "override", "Override"

    incident_type = models.CharField(max_length=20, choices=IncidentType.choices)
    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incidents",
    )
    game_copy = models.ForeignKey(
        GameCopy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incidents",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incidents",
        help_text="The borrower involved in the incident.",
    )
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="incidents_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_incident_type_display()} – {self.created_at:%Y-%m-%d}"


# =====================================================================
# M1-10  Restriction
# =====================================================================
class Restriction(models.Model):
    """Temporary ban or blacklist preventing a user from making reservations."""

    class RestrictionType(models.TextChoices):
        TEMP_BAN = "temp_ban", "Temporary Ban"
        BLACKLIST = "blacklist", "Blacklist"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="restrictions",
    )
    restriction_type = models.CharField(
        max_length=10, choices=RestrictionType.choices
    )
    reason = models.TextField()
    start_date = models.DateField(default=timezone.now)
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Null for permanent blacklist.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="restrictions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.get_restriction_type_display()} on {self.user.email} "
            f"({self.start_date} → {self.end_date or 'permanent'})"
        )

    @property
    def is_active(self):
        today = timezone.now().date()
        if today < self.start_date:
            return False
        if self.end_date and today > self.end_date:
            return False
        return True


# =====================================================================
# Audit Log  (for F6-7)
# =====================================================================
class AuditLog(models.Model):
    """Immutable log of every policy-relevant action."""

    ACTION_CHOICES = [
        ("checkout", "Checkout"),
        ("return", "Return"),
        ("reserve", "Reserve"),
        ("cancel_reservation", "Cancel Reservation"),
        ("no_show", "No-Show"),
        ("walk_in", "Walk-in Checkout"),
        ("deposit_collected", "Deposit Collected"),
        ("deposit_refunded", "Deposit Refunded"),
        ("charge_created", "Charge Created"),
        ("charge_updated", "Charge Updated"),
        ("incident_logged", "Incident Logged"),
        ("override", "Override"),
        ("restriction_created", "Restriction Created"),
        ("restriction_lifted", "Restriction Lifted"),
        ("role_changed", "Role Changed"),
        ("slot_cancelled", "Slot Cancelled"),
        ("lost_finalized", "Lost Game Finalized"),
    ]

    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs_about",
    )
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.action} by {self.actor}"


# =====================================================================
# Email Allowlists  (for A2-3 & I8-3)
# =====================================================================
class RAEmailAllowlist(models.Model):
    """Pre-loaded RA emails that auto-assign policy_type = 'ra'."""
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "RA Email Allowlist Entry"
        verbose_name_plural = "RA Email Allowlist"

    def __str__(self):
        return self.email


class ClubSocietyEmailAllowlist(models.Model):
    """Pre-loaded club/society emails that auto-assign policy_type = 'club_society'."""
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Club/Society Email Allowlist Entry"
        verbose_name_plural = "Club/Society Email Allowlist"

    def __str__(self):
        return f"{self.email} ({self.name})" if self.name else self.email
