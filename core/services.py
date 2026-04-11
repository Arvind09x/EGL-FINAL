"""
Service layer for the ESHTO Games Library.

Contains all business logic for reservations, checkouts, fees, and restrictions.
Keeps views thin — they call service functions and handle HTTP concerns only.
"""

import zoneinfo
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AuditLog,
    BorrowerProfile,
    Charge,
    Checkout,
    GameCopy,
    Incident,
    Reservation,
    Restriction,
    WeeklySlot,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
LATE_FEE_PER_SLOT = Decimal("50.00")
LOST_CANDIDATE_DAYS = 30
PICKUP_LOCATION = (
    "Student Commons, 2nd floor of the mess, "
    "near the couches between the meeting rooms"
)


# =====================================================================
# Helpers
# =====================================================================

def _now_ist():
    return timezone.now().astimezone(IST)


def _log(action, actor, target_user=None, **details):
    AuditLog.objects.create(
        action=action, actor=actor, target_user=target_user, details=details
    )


def get_active_restriction(user):
    """Return the first active restriction for a user, or None."""
    today = timezone.now().date()
    return (
        Restriction.objects.filter(user=user)
        .filter(
            Q(start_date__lte=today),
            Q(end_date__isnull=True) | Q(end_date__gte=today),
        )
        .first()
    )


def active_reservation_count(user):
    """Count active reservations + checkouts (not returned) for a user."""
    reserved = Reservation.objects.filter(
        user=user, status=Reservation.Status.CONFIRMED
    ).count()
    checked_out = Checkout.objects.filter(
        borrower=user, return_time__isnull=True
    ).count()
    return reserved + checked_out


def get_max_active_limit(user):
    """Return the max number of active games allowed for this user."""
    try:
        profile = user.borrower_profile
    except BorrowerProfile.DoesNotExist:
        return 2
    if profile.policy_type == BorrowerProfile.PolicyType.RA:
        return 4
    return 2


def get_next_slot(after_slot):
    """Return the next non-cancelled WeeklySlot after the given slot."""
    return (
        WeeklySlot.objects.filter(
            date__gt=after_slot.date, is_cancelled=False
        )
        .order_by("date", "start_time")
        .first()
    )


def get_same_weekday_next_week(slot):
    """Return the slot on the same weekday one week later."""
    target_date = slot.date + timedelta(days=7)
    return WeeklySlot.objects.filter(
        date=target_date, is_cancelled=False
    ).first()


# =====================================================================
# R4-1  Booking window check
# =====================================================================

def is_booking_window_open(pickup_slot):
    """
    Reservations can only be created between Monday 08:00 IST of the
    week of the pickup slot and 1 hour before the slot starts.
    """
    now = _now_ist()
    # Monday of the pickup slot's week (weekday 0 = Monday)
    slot_date = pickup_slot.date
    monday = slot_date - timedelta(days=slot_date.weekday())
    window_open = datetime.combine(monday, datetime.min.time().replace(hour=8), tzinfo=IST)
    window_close = pickup_slot.start_datetime - timedelta(hours=1)
    return window_open <= now <= window_close


# =====================================================================
# R4-2 – R4-5  Validation
# =====================================================================

def validate_reservation(user, game_copy, pickup_slot):
    """
    Run all pre-reservation checks. Returns (ok: bool, error: str | None).
    """
    # Restriction check (R4-10)
    restriction = get_active_restriction(user)
    if restriction:
        return False, "You are currently restricted from making reservations."

    # Booking window (R4-1)
    if not is_booking_window_open(pickup_slot) and not user.is_staff:
        return False, "The booking window for this slot is not open."

    # Active limit (R4-2, R4-3)
    limit = get_max_active_limit(user)
    if active_reservation_count(user) >= limit:
        return False, f"You already have {limit} active reservations/checkouts."

    # Copy already booked for this slot (R4-8)
    if Reservation.objects.filter(
        game_copy=game_copy,
        pickup_slot=pickup_slot,
        status=Reservation.Status.CONFIRMED,
    ).exists():
        return False, "This copy is already reserved for the selected slot."

    # Slot cancelled check
    if pickup_slot.is_cancelled:
        return False, "This slot has been cancelled."

    return True, None


# =====================================================================
# R4-6, R4-7  Return slot computation
# =====================================================================

def compute_return_slot(user, pickup_slot):
    """
    Default return slot = next slot.
    Two-slot extension: if the borrower has a confirmed reservation for both
    Tuesday and Friday of the same week, return = same weekday next week.
    """
    next_slot = get_next_slot(pickup_slot)
    if not next_slot:
        return pickup_slot  # fallback

    # Check for two-slot extension
    week_start = pickup_slot.date - timedelta(days=pickup_slot.date.weekday())
    week_end = week_start + timedelta(days=6)
    week_slots = WeeklySlot.objects.filter(
        date__gte=week_start, date__lte=week_end, is_cancelled=False
    )
    user_reservations_this_week = Reservation.objects.filter(
        user=user,
        pickup_slot__in=week_slots,
        status=Reservation.Status.CONFIRMED,
    )
    booked_dates = set(
        user_reservations_this_week.values_list("pickup_slot__date", flat=True)
    )
    booked_dates.add(pickup_slot.date)  # include current reservation

    # If both a Tue and Fri are booked this week → extended return
    weekdays_booked = {d.weekday() for d in booked_dates}
    if {1, 4}.issubset(weekdays_booked):  # 1=Tue, 4=Fri
        extended = get_same_weekday_next_week(pickup_slot)
        if extended:
            return extended

    return next_slot


# =====================================================================
# R4-4, R4-5  Club/Society deposit + ban logic
# =====================================================================

def compute_deposit_amount(user, game_copy):
    """
    Determine the deposit amount for this reservation.
    - RAs: 0
    - Clubs with prior incidents: replacement cost
    - Others: 0 (default, unless member overrides)
    """
    try:
        profile = user.borrower_profile
    except BorrowerProfile.DoesNotExist:
        return Decimal("0")

    if profile.policy_type == BorrowerProfile.PolicyType.RA:
        return Decimal("0")

    if profile.policy_type == BorrowerProfile.PolicyType.CLUB_SOCIETY:
        incident_count = Incident.objects.filter(
            user=user,
            incident_type__in=[
                Incident.IncidentType.DAMAGE,
                Incident.IncidentType.LOST_GAME,
            ],
        ).count()
        if incident_count > 0:
            return game_copy.game_title.replacement_cost

    return Decimal("0")


def check_club_ban(user):
    """
    R4-5: If a club has ≥ 2 damage/loss incidents, auto-create a 2-semester
    restriction (~10 months).
    """
    try:
        profile = user.borrower_profile
    except BorrowerProfile.DoesNotExist:
        return
    if profile.policy_type != BorrowerProfile.PolicyType.CLUB_SOCIETY:
        return

    incident_count = Incident.objects.filter(
        user=user,
        incident_type__in=[
            Incident.IncidentType.DAMAGE,
            Incident.IncidentType.LOST_GAME,
        ],
    ).count()

    if incident_count >= 2:
        already_banned = get_active_restriction(user)
        if not already_banned:
            Restriction.objects.create(
                user=user,
                restriction_type=Restriction.RestrictionType.TEMP_BAN,
                reason=f"Auto-ban: {incident_count} damage/loss incidents.",
                start_date=timezone.now().date(),
                end_date=timezone.now().date() + timedelta(days=300),  # ~2 semesters
            )


# =====================================================================
# Create / Cancel Reservation
# =====================================================================

@transaction.atomic
def create_reservation(user, game_copy, pickup_slot):
    """Create a new reservation. Returns (reservation, error_msg)."""
    ok, err = validate_reservation(user, game_copy, pickup_slot)
    if not ok:
        return None, err

    return_slot = compute_return_slot(user, pickup_slot)

    reservation = Reservation.objects.create(
        user=user,
        game_copy=game_copy,
        pickup_slot=pickup_slot,
        return_slot=return_slot,
        status=Reservation.Status.CONFIRMED,
    )

    # Create deposit charge if applicable
    deposit = compute_deposit_amount(user, game_copy)
    if deposit > 0:
        Charge.objects.create(
            user=user,
            charge_type=Charge.ChargeType.DEPOSIT,
            amount=deposit,
            status=Charge.ChargeStatus.UNPAID,
        )

    _log("reserve", user, details={
        "reservation_id": reservation.pk,
        "game": str(game_copy),
        "pickup_slot": str(pickup_slot),
    })
    return reservation, None


# R4-9  Cancellation
@transaction.atomic
def cancel_reservation(reservation, user):
    """Cancel a reservation if within the allowed window."""
    now = _now_ist()
    cutoff = reservation.pickup_slot.start_datetime - timedelta(hours=1)
    if now > cutoff:
        return False, "Cannot cancel within 1 hour of the pickup slot."

    if reservation.status != Reservation.Status.CONFIRMED:
        return False, "Only confirmed reservations can be cancelled."

    reservation.status = Reservation.Status.CANCELLED
    reservation.save()

    _log("cancel_reservation", user, details={
        "reservation_id": reservation.pk,
    })
    return True, None


# =====================================================================
# Desk: Checkout / Return / No-show / Walk-in
# =====================================================================

@transaction.atomic
def perform_checkout(reservation, member, condition_notes=""):
    """D5-3: Member checks out a game to a borrower."""
    checkout = Checkout.objects.create(
        reservation=reservation,
        borrower=reservation.user,
        game_copy=reservation.game_copy,
        handoff_member=member,
        condition_notes=condition_notes,
        due_date_slot=reservation.return_slot,
    )
    reservation.game_copy.is_available = False
    reservation.game_copy.save()

    _log("checkout", member, target_user=reservation.user, details={
        "checkout_id": checkout.pk,
        "game_copy": str(reservation.game_copy),
    })
    return checkout


@transaction.atomic
def perform_return(checkout, member, condition_notes=""):
    """D5-4: Member marks a game as returned."""
    checkout.return_time = timezone.now()
    checkout.condition_notes = condition_notes or checkout.condition_notes
    checkout.save()

    checkout.game_copy.is_available = True
    checkout.game_copy.save()

    if checkout.reservation:
        checkout.reservation.status = Reservation.Status.COMPLETED
        checkout.reservation.save()

    _log("return", member, target_user=checkout.borrower, details={
        "checkout_id": checkout.pk,
        "game_copy": str(checkout.game_copy),
    })
    return checkout


@transaction.atomic
def perform_no_show(reservation, member):
    """D5-6: Mark a reservation as no-show and release the copy."""
    reservation.status = Reservation.Status.NO_SHOW
    reservation.save()

    Incident.objects.create(
        incident_type=Incident.IncidentType.NO_SHOW,
        user=reservation.user,
        game_copy=reservation.game_copy,
        description="Borrower did not show up for pickup.",
        created_by=member,
    )

    _log("no_show", member, target_user=reservation.user, details={
        "reservation_id": reservation.pk,
    })


@transaction.atomic
def perform_walk_in_checkout(borrower, game_copy, member, condition_notes="", due_date_slot=None):
    """D5-7: Walk-in checkout without a reservation."""
    checkout = Checkout.objects.create(
        borrower=borrower,
        game_copy=game_copy,
        handoff_member=member,
        condition_notes=condition_notes,
        due_date_slot=due_date_slot,
    )
    game_copy.is_available = False
    game_copy.save()

    _log("walk_in", member, target_user=borrower, details={
        "checkout_id": checkout.pk,
        "game_copy": str(game_copy),
    })
    return checkout


# =====================================================================
# Fees & Incidents
# =====================================================================

def calculate_late_fee(checkout):
    """F6-1: INR 50 × number of slots missed since the due date."""
    if not checkout.due_date_slot or checkout.return_time:
        return Decimal("0")
    now = timezone.now()
    missed = WeeklySlot.objects.filter(
        date__gt=checkout.due_date_slot.date,
        date__lte=now.date(),
        is_cancelled=False,
    ).count()
    return LATE_FEE_PER_SLOT * missed


def update_late_fee_charge(checkout):
    """F6-2: Create or update the late fee charge for a checkout."""
    fee = calculate_late_fee(checkout)
    if fee <= 0:
        return None
    charge, created = Charge.objects.get_or_create(
        checkout=checkout,
        charge_type=Charge.ChargeType.LATE_FEE,
        defaults={"user": checkout.borrower, "amount": fee},
    )
    if not created and charge.amount != fee:
        charge.amount = fee
        charge.save()
    return charge


def check_lost_candidate(checkout):
    """F6-3: Flag checkouts overdue by ≥ 30 days."""
    if checkout.return_time or not checkout.due_date_slot:
        return False
    due = checkout.due_date_slot.date
    if (timezone.now().date() - due).days >= LOST_CANDIDATE_DAYS:
        if not checkout.is_lost_candidate:
            checkout.is_lost_candidate = True
            checkout.save()
        return True
    return False


@transaction.atomic
def finalize_lost_game(checkout, member):
    """F6-4: Finalize a lost game — charge replacement + accumulated late fees."""
    replacement_cost = checkout.game_copy.game_title.replacement_cost
    late_fee = calculate_late_fee(checkout)
    total = replacement_cost + late_fee

    Charge.objects.create(
        user=checkout.borrower,
        charge_type=Charge.ChargeType.LOST_REPLACEMENT,
        amount=total,
        checkout=checkout,
        notes=f"Replacement cost ₹{replacement_cost} + late fees ₹{late_fee}",
    )

    Incident.objects.create(
        incident_type=Incident.IncidentType.LOST_GAME,
        checkout=checkout,
        game_copy=checkout.game_copy,
        user=checkout.borrower,
        description=f"Game finalized as lost. Total charge: ₹{total}",
        created_by=member,
    )

    checkout.game_copy.condition = GameCopy.Condition.LOST
    checkout.game_copy.is_available = False
    checkout.game_copy.save()

    check_club_ban(checkout.borrower)

    _log("lost_finalized", member, target_user=checkout.borrower, details={
        "checkout_id": checkout.pk,
        "total_charge": str(total),
    })


@transaction.atomic
def log_damage_incident(checkout, member, description, damage_fee=None):
    """F6-5: Log damage / missing components and optional charge."""
    incident = Incident.objects.create(
        incident_type=Incident.IncidentType.DAMAGE,
        checkout=checkout,
        game_copy=checkout.game_copy,
        user=checkout.borrower,
        description=description,
        created_by=member,
    )

    warning = None
    if damage_fee and damage_fee > 0:
        if damage_fee > checkout.game_copy.game_title.replacement_cost:
            warning = (
                f"Warning: damage fee ₹{damage_fee} exceeds replacement "
                f"cost ₹{checkout.game_copy.game_title.replacement_cost}"
            )
        Charge.objects.create(
            user=checkout.borrower,
            charge_type=Charge.ChargeType.DAMAGE_FEE,
            amount=damage_fee,
            checkout=checkout,
            incident=incident,
        )

    check_club_ban(checkout.borrower)

    _log("incident_logged", member, target_user=checkout.borrower, details={
        "incident_type": "damage",
        "checkout_id": checkout.pk,
    })
    return incident, warning
