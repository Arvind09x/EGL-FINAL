"""
Service layer for the ESHTO Games Library.

Contains all business logic for reservations, checkouts, fees, restrictions
and slot scheduling, following the Eeshto Games Library Policy. Keeps views
thin — they call service functions and handle HTTP concerns only.
"""

import zoneinfo
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import EmailValidator
from django.core.exceptions import ValidationError
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
    Vouch,
    WeeklySlot,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

LATE_FEE_PER_SLOT = Decimal("25.00")        # per game, per return slot missed
LOST_AFTER_SLOTS = 3                         # return slots after the due slot
SEMESTER_DAYS = getattr(settings, "EGL_SEMESTER_DAYS", 120)
INSPECTION_WINDOW = timedelta(hours=3)
SPECIAL_NOTICE = timedelta(days=2)
DEPOSIT_REFUND_DAYS = 7
MIN_DAMAGE_FEE = Decimal("50.00")
CLUB_DEPOSIT_CAP = Decimal("1000.00")

PICKUP_LOCATION = (
    "Student Commons, 2nd floor of the mess, "
    "near the couches between the meeting rooms"
)

PolicyType = BorrowerProfile.PolicyType
SPECIAL_TYPES = (PolicyType.CLUB_SOCIETY, PolicyType.RA, PolicyType.TA_TF)
INFRACTION_TYPES = (
    Incident.IncidentType.DAMAGE,
    Incident.IncidentType.MISSING_COMPONENTS,
    Incident.IncidentType.LOST_GAME,
)
LENDABLE = (GameCopy.Condition.GOOD, GameCopy.Condition.FAIR)

# (low %, high %) of MRP for each component category.
COMPONENT_FEE_GUIDE = {
    Incident.ComponentCategory.CRITICAL: (Decimal("100"), Decimal("100")),
    Incident.ComponentCategory.ESSENTIAL: (Decimal("50"), Decimal("75")),
    Incident.ComponentCategory.NECESSARY: (Decimal("25"), Decimal("50")),
    Incident.ComponentCategory.COSMETIC: (Decimal("25"), Decimal("25")),
}


# =====================================================================
# Helpers
# =====================================================================

def _now_ist():
    return timezone.now().astimezone(IST)


def today_ist():
    return _now_ist().date()


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _log(action, actor, target_user=None, **details):
    AuditLog.objects.create(
        action=action, actor=actor, target_user=target_user, details=details
    )


def get_profile(user):
    profile, _ = BorrowerProfile.objects.get_or_create(user=user)
    return profile


def get_borrower_type(user):
    """Policy type that governs this user's limits and deposits."""
    ptype = get_profile(user).policy_type
    if ptype in SPECIAL_TYPES:
        return ptype
    if user.is_member or user.is_senior_admin:
        return PolicyType.EESHTO_MEMBER
    return ptype


# =====================================================================
# Restrictions (probation / ban / blacklist)
# =====================================================================

def _active_restrictions(user):
    today = today_ist()
    return Restriction.objects.filter(user=user).filter(
        Q(start_date__lte=today),
        Q(end_date__isnull=True) | Q(end_date__gte=today),
    )


def get_blocking_restriction(user):
    """Active ban or blacklist (these stop a user from borrowing)."""
    return (
        _active_restrictions(user)
        .exclude(restriction_type=Restriction.RestrictionType.PROBATION)
        .first()
    )


# Kept for existing call sites: "restriction" means something that blocks.
get_active_restriction = get_blocking_restriction


def get_active_probation(user):
    return (
        _active_restrictions(user)
        .filter(restriction_type=Restriction.RestrictionType.PROBATION)
        .first()
    )


def infraction_count(user):
    """Distinct damage/loss incidents (one per checkout) recorded against a user."""
    qs = Incident.objects.filter(user=user, incident_type__in=INFRACTION_TYPES)
    with_checkout = qs.filter(checkout__isnull=False).values("checkout").distinct().count()
    without = qs.filter(checkout__isnull=True).count()
    return with_checkout + without


def _create_restriction(user, rtype, reason, start, end, actor):
    restriction = Restriction.objects.create(
        user=user,
        restriction_type=rtype,
        reason=reason,
        start_date=start,
        end_date=end,
        created_by=actor,
    )
    _log("restriction_created", actor, target_user=user, details={
        "type": rtype,
        "start": str(start),
        "end": str(end) if end else None,
        "reason": reason,
    })
    return restriction


def register_infraction(user, actor, reason, incident=None):
    """
    Apply the probation ladder for a lost / damaged / mishandled game.

    * First infraction: 1 semester of probation.
    * Infraction while on probation: 1 semester ban.
    * If the borrower has already been banned once (probation -> ban ->
      probation again), the next ban is permanent.
    * Clubs/societies reach the "on probation" step at their 2nd incident
      and serve a further semester of probation after the ban.

    One checkout produces at most one escalation. Returns a short
    description of what was applied, or None if nothing changed.
    """
    if incident is not None and incident.checkout_id:
        earlier = Incident.objects.filter(
            checkout_id=incident.checkout_id, incident_type__in=INFRACTION_TYPES
        ).exclude(pk=incident.pk)
        if earlier.exists():
            return None

    if get_blocking_restriction(user):
        return None

    today = today_ist()
    semester = timedelta(days=SEMESTER_DAYS)
    is_club = get_borrower_type(user) == PolicyType.CLUB_SOCIETY
    escalate = get_active_probation(user) is not None or (
        is_club and infraction_count(user) >= 2
    )

    if not escalate:
        _create_restriction(
            user, Restriction.RestrictionType.PROBATION, reason,
            today, today + semester, actor,
        )
        return "Borrower placed on probation for 1 semester."

    previously_banned = Restriction.objects.filter(
        user=user, restriction_type=Restriction.RestrictionType.TEMP_BAN
    ).exists()
    if previously_banned:
        _create_restriction(
            user, Restriction.RestrictionType.BLACKLIST, reason, today, None, actor
        )
        return "Borrower permanently blacklisted (repeat offender)."

    ban_end = today + semester
    _create_restriction(
        user, Restriction.RestrictionType.TEMP_BAN, reason, today, ban_end, actor
    )
    if is_club:
        _create_restriction(
            user, Restriction.RestrictionType.PROBATION,
            "Probation following ban.", ban_end + timedelta(days=1),
            ban_end + semester, actor,
        )
        return "Club/society banned for 1 semester, followed by 1 semester of probation."
    return "Borrower banned for 1 semester."


def lift_restriction(restriction, actor):
    restriction.end_date = today_ist() - timedelta(days=1)
    if restriction.start_date > restriction.end_date:
        restriction.start_date = restriction.end_date
    restriction.save()
    _log("restriction_lifted", actor, target_user=restriction.user, details={
        "restriction_id": restriction.pk,
        "type": restriction.restriction_type,
    })


# =====================================================================
# Dues, limits and deposits
# =====================================================================

def unpaid_dues(user):
    return Charge.objects.filter(user=user, status=Charge.ChargeStatus.UNPAID)


def get_max_active_limit(user):
    """Max games a borrower may hold at once; None means unlimited."""
    ptype = get_borrower_type(user)
    if ptype == PolicyType.RA:
        return 4
    if ptype == PolicyType.CLUB_SOCIETY:
        return 6
    if ptype == PolicyType.TA_TF:
        return None
    return 2


def _open_reservations(user):
    """Reservations that still hold a game: not yet picked up and not past."""
    return Reservation.objects.filter(
        user=user,
        status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING],
        checkout__isnull=True,
        pickup_slot__date__gte=today_ist(),
    )


def _active_checkouts(user):
    return Checkout.objects.filter(borrower=user, return_time__isnull=True)


def active_reservation_count(user):
    """Games a user currently holds: open reservations + unreturned checkouts."""
    return _open_reservations(user).count() + _active_checkouts(user).count()


def holds_s_category(user):
    return (
        _open_reservations(user).filter(game_copy__game_title__deposit_category="S").exists()
        or _active_checkouts(user).filter(game_copy__game_title__deposit_category="S").exists()
    )


def club_has_incidents(user):
    return infraction_count(user) >= 1


def required_deposit(user, game_copy):
    """Security deposit in INR this borrower owes for this game."""
    ptype = get_borrower_type(user)
    title = game_copy.game_title
    base = Decimal(title.security_deposit)

    if ptype in (PolicyType.RA, PolicyType.EESHTO_MEMBER):
        return Decimal("0.00")
    if ptype == PolicyType.CLUB_SOCIETY:
        mrp = Decimal(title.mrp)
        if Decimal(title.club_deposit) > 0:
            deposit = max(Decimal(title.club_deposit), base)
        else:
            deposit = max(min(mrp * Decimal("0.25"), CLUB_DEPOSIT_CAP), base)
        if club_has_incidents(user):
            deposit = max(deposit, mrp * Decimal("0.5"))
        return _money(deposit)
    return _money(base)


# =====================================================================
# Vouching (graduating borrowers)
# =====================================================================

def get_active_vouch(user):
    return Vouch.objects.filter(borrower=user, is_active=True).select_related("voucher").first()


def effective_borrower(user):
    """The person responsible for games the user borrows."""
    if get_profile(user).requires_voucher:
        vouch = get_active_vouch(user)
        if vouch:
            return vouch.voucher
    return user


@transaction.atomic
def add_vouch(borrower, voucher, actor):
    if borrower == voucher:
        return None, "A borrower cannot vouch for themselves."
    if get_profile(voucher).requires_voucher:
        return None, "The voucher must be a non-graduating student."
    Vouch.objects.filter(borrower=borrower, is_active=True).update(is_active=False)
    vouch = Vouch.objects.create(borrower=borrower, voucher=voucher, created_by=actor)
    _log("vouch_added", actor, target_user=borrower, details={"voucher": voucher.email})
    return vouch, None


def remove_vouch(vouch, actor):
    vouch.is_active = False
    vouch.save()
    _log("vouch_removed", actor, target_user=vouch.borrower, details={
        "voucher": vouch.voucher.email,
    })


# =====================================================================
# Slots
# =====================================================================

def _return_slots():
    return WeeklySlot.objects.filter(
        slot_type=WeeklySlot.SlotType.RETURN, is_cancelled=False
    )


def _borrow_slots():
    return WeeklySlot.objects.filter(
        slot_type=WeeklySlot.SlotType.BORROW, is_cancelled=False
    )


def upcoming_borrow_slots(limit=None):
    """Borrow slots that have not started yet."""
    now = _now_ist()
    qs = _borrow_slots().filter(date__gte=now.date()).order_by("date", "start_time")
    slots = [s for s in qs if s.start_datetime > now]
    return slots[:limit] if limit else slots


def upcoming_slots(slot_type=None, limit=None):
    """Non-cancelled slots that have not ended yet, soonest first."""
    now = _now_ist()
    qs = WeeklySlot.objects.filter(is_cancelled=False, date__gte=now.date())
    if slot_type:
        qs = qs.filter(slot_type=slot_type)
    slots = [s for s in qs.order_by("date", "start_time") if s.end_datetime > now]
    return slots[:limit] if limit else slots


def get_return_slot_for(pickup_slot, return_by=None):
    """
    The return slot for a pickup: the next return slot after it. When a
    return-by date is given (clubs, RAs, TAs) it is the first return slot
    on or after that date.
    """
    after = _return_slots().filter(
        Q(date__gt=pickup_slot.date)
        | Q(date=pickup_slot.date, start_time__gt=pickup_slot.start_time)
    ).order_by("date", "start_time")
    if return_by:
        chosen = after.filter(date__gte=return_by).first()
        if chosen:
            return chosen
    return after.first()


def _next_return_slot_after_dt(dt):
    for slot in _return_slots().filter(date__gte=dt.date()).order_by("date", "start_time"):
        if slot.start_datetime > dt:
            return slot
    return None


def effective_due_slot(checkout):
    """The return slot a checkout is currently due at (None if none scheduled)."""
    slot = checkout.due_date_slot
    if slot is None:
        slot = _next_return_slot_after_dt(timezone.localtime(checkout.checkout_time, IST))
        if slot:
            checkout.due_date_slot = slot
            checkout.save(update_fields=["due_date_slot"])
        return slot
    if slot.is_cancelled:
        return get_return_slot_for(slot)
    return slot


def validate_slot_times(start, end):
    if end <= start:
        return "End time must be after the start time."
    return None


@transaction.atomic
def cancel_slot(slot, actor):
    """Cancel a slot and keep reservations consistent."""
    slot.is_cancelled = True
    slot.save()

    cancelled = 0
    if slot.slot_type == WeeklySlot.SlotType.BORROW:
        for res in Reservation.objects.filter(
            pickup_slot=slot,
            status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING],
            checkout__isnull=True,
        ):
            res.status = Reservation.Status.CANCELLED
            res.save()
            cancelled += 1
    else:
        for res in Reservation.objects.filter(return_slot=slot):
            res.return_slot = get_return_slot_for(res.pickup_slot, res.return_by_date)
            res.save()

    _log("slot_cancelled", actor, details={
        "slot_id": slot.pk,
        "date": str(slot.date),
        "type": slot.slot_type,
        "reservations_cancelled": cancelled,
    })
    return cancelled


def slot_in_use(slot):
    return (
        Reservation.objects.filter(Q(pickup_slot=slot) | Q(return_slot=slot)).exists()
        or Checkout.objects.filter(due_date_slot=slot, return_time__isnull=True).exists()
    )


@transaction.atomic
def delete_slot(slot, actor):
    if slot_in_use(slot):
        return False, "This slot has reservations or checkouts. Cancel it instead."
    details = {"slot_id": slot.pk, "date": str(slot.date), "type": slot.slot_type}
    slot.delete()
    _log("slot_deleted", actor, details=details)
    return True, None


# =====================================================================
# Validation
# =====================================================================

def _previous_borrow_slot(pickup_slot):
    if pickup_slot is not None:
        qs = _borrow_slots().filter(
            Q(date__lt=pickup_slot.date)
            | Q(date=pickup_slot.date, start_time__lt=pickup_slot.start_time)
        )
    else:
        qs = _borrow_slots().filter(date__lt=today_ist())
    return qs.order_by("-date", "-start_time").first()


def borrowed_consecutively(user, game_title, pickup_slot=None):
    """True if the user borrowed this title in the previous borrow slot or still has it."""
    if _active_checkouts(user).filter(game_copy__game_title=game_title).exists():
        return True
    prev = _previous_borrow_slot(pickup_slot)
    if prev is None:
        return False
    in_reservations = Reservation.objects.filter(
        user=user,
        pickup_slot=prev,
        game_copy__game_title=game_title,
        status__in=[
            Reservation.Status.CONFIRMED,
            Reservation.Status.PENDING,
            Reservation.Status.COMPLETED,
        ],
    ).exists()
    if in_reservations:
        return True
    return Checkout.objects.filter(
        borrower=user,
        game_copy__game_title=game_title,
        checkout_time__date=prev.date,
    ).exists()


def validate_borrow(user, game_copy, pickup_slot=None):
    """
    Policy checks shared by reservations and walk-ins.
    Returns a list of error strings (empty when the borrow is allowed).
    """
    errors = []
    payer = effective_borrower(user)
    profile = get_profile(user)
    title = game_copy.game_title
    ptype = get_borrower_type(user)

    for person in {user.pk: user, payer.pk: payer}.values():
        me = person == user
        if get_blocking_restriction(person):
            errors.append(
                "You are currently barred from borrowing." if me
                else f"{person.email} is currently barred from borrowing."
            )
        if unpaid_dues(person).exists():
            errors.append(
                "You have unresolved dues. Clear them before borrowing again." if me
                else f"{person.email} has unresolved dues."
            )

    if profile.requires_voucher and payer == user:
        errors.append(
            "Graduating borrowers need a non-graduating student to vouch for them. "
            "Email Eeshto with the vouching student's confirmation."
        )

    if game_copy.condition not in LENDABLE:
        errors.append("This copy is not in a lendable condition.")

    limit = get_max_active_limit(user)
    if limit is not None and active_reservation_count(user) >= limit:
        errors.append(f"You already hold the maximum of {limit} games.")

    if title.is_s_category and ptype not in (PolicyType.CLUB_SOCIETY, PolicyType.TA_TF):
        if holds_s_category(user):
            errors.append("Only one S category game can be borrowed at a time.")

    if ptype not in (PolicyType.CLUB_SOCIETY, PolicyType.TA_TF):
        if borrowed_consecutively(user, title, pickup_slot):
            errors.append("The same game cannot be borrowed consecutively.")

    if pickup_slot is not None:
        if pickup_slot.slot_type != WeeklySlot.SlotType.BORROW:
            errors.append("Games can only be picked up during a borrow slot.")
        if pickup_slot.is_cancelled:
            errors.append("This slot has been cancelled.")
        if pickup_slot.start_datetime <= _now_ist():
            errors.append("This slot has already started. Reserve a later slot.")
        if Reservation.objects.filter(
            game_copy=game_copy,
            pickup_slot=pickup_slot,
            status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING],
        ).exists():
            errors.append("This copy is already reserved for the selected slot.")
    return errors


def _validate_special_details(ptype, pickup_slot, details):
    if pickup_slot.start_datetime - _now_ist() < SPECIAL_NOTICE:
        return "Clubs, RAs and TAs/TFs must book at least 2 days before the slot."
    if not details.get("purpose", "").strip():
        return "Please state the purpose the games are being borrowed for."
    if not details.get("poc_name", "").strip() or not details.get("poc_phone", "").strip():
        return "Contact name and phone number are required."
    return_by = details.get("return_by")
    if not return_by:
        return "Please give the date by which the games will be returned."
    if return_by < pickup_slot.date:
        return "The return date cannot be before the pickup date."
    if ptype == PolicyType.RA and not details.get("rh_floor", "").strip():
        return "Please give your RH and floor."
    if ptype == PolicyType.TA_TF:
        email = details.get("authorizing_email", "").strip()
        try:
            EmailValidator()(email)
        except ValidationError:
            return "A valid email for the concerned Professor is required."
        if not email.lower().endswith("@ashoka.edu.in"):
            return "The Professor's email must be an @ashoka.edu.in address."
    return None


# =====================================================================
# Create / Cancel / Approve Reservation
# =====================================================================

@transaction.atomic
def create_reservation(user, game_copy, pickup_slot, details=None):
    """Create a new reservation. Returns (reservation, error_msg)."""
    details = details or {}

    errors = validate_borrow(user, game_copy, pickup_slot)
    if errors:
        return None, errors[0]

    profile = get_profile(user)
    if not profile.tdi_exempt and not details.get("premises_ack"):
        return None, (
            "You must agree not to take the game outside the University premises."
        )

    ptype = get_borrower_type(user)
    special = ptype in SPECIAL_TYPES
    extra = {}
    if special:
        err = _validate_special_details(ptype, pickup_slot, details)
        if err:
            return None, err
        extra = {
            "purpose": details["purpose"].strip(),
            "poc_name": details["poc_name"].strip(),
            "poc_phone": details["poc_phone"].strip(),
            "return_by_date": details["return_by"],
            "rh_floor": details.get("rh_floor", "").strip(),
            "authorizing_email": details.get("authorizing_email", "").strip(),
        }

    reservation = Reservation.objects.create(
        user=user,
        game_copy=game_copy,
        pickup_slot=pickup_slot,
        return_slot=get_return_slot_for(pickup_slot, extra.get("return_by_date")),
        status=Reservation.Status.PENDING if special else Reservation.Status.CONFIRMED,
        **extra,
    )

    _log("reserve", user, details={
        "reservation_id": reservation.pk,
        "game": str(game_copy),
        "pickup_slot": str(pickup_slot),
        "status": reservation.status,
    })
    return reservation, None


@transaction.atomic
def cancel_reservation(reservation, user):
    """Cancel a reservation before its pickup slot starts."""
    if reservation.status not in (
        Reservation.Status.CONFIRMED, Reservation.Status.PENDING
    ):
        return False, "Only active reservations can be cancelled."

    if _now_ist() > reservation.pickup_slot.start_datetime:
        return False, "Cannot cancel within or after the pickup slot starts."

    reservation.status = Reservation.Status.CANCELLED
    reservation.save()

    _log("cancel_reservation", user, details={"reservation_id": reservation.pk})
    return True, None


@transaction.atomic
def approve_reservation(reservation, member):
    if reservation.status != Reservation.Status.PENDING:
        return False, "Only pending requests can be approved."
    if reservation.pickup_slot.is_cancelled or reservation.pickup_slot.start_datetime <= _now_ist():
        return False, "The pickup slot is no longer available."
    if Reservation.objects.filter(
        game_copy=reservation.game_copy,
        pickup_slot=reservation.pickup_slot,
        status=Reservation.Status.CONFIRMED,
    ).exists():
        return False, "This copy is already confirmed for the slot."
    reservation.status = Reservation.Status.CONFIRMED
    reservation.save()
    _log("reservation_approved", member, target_user=reservation.user, details={
        "reservation_id": reservation.pk,
    })
    return True, None


@transaction.atomic
def reject_reservation(reservation, member, reason=""):
    if reservation.status != Reservation.Status.PENDING:
        return False, "Only pending requests can be rejected."
    reservation.status = Reservation.Status.REJECTED
    reservation.save()
    _log("reservation_rejected", member, target_user=reservation.user, details={
        "reservation_id": reservation.pk,
        "reason": reason,
    })
    return True, None


# =====================================================================
# Desk: Checkout / Return / No-show / Walk-in
# =====================================================================

def checkout_blockers(reservation):
    """Problems that should stop a desk checkout unless a member overrides."""
    errors = []
    copy = reservation.game_copy
    payer = effective_borrower(reservation.user)

    if reservation.status != Reservation.Status.CONFIRMED:
        errors.append("The reservation is not confirmed.")
    if reservation.pickup_slot.is_cancelled:
        errors.append("The pickup slot was cancelled.")
    if copy.condition not in LENDABLE:
        errors.append("This copy is not in a lendable condition.")
    elif not copy.is_available:
        errors.append("This copy has not been returned yet.")
    if get_profile(reservation.user).requires_voucher and payer == reservation.user:
        errors.append("Borrower needs a voucher before borrowing.")
    if get_blocking_restriction(payer):
        errors.append(f"{payer.email} is barred from borrowing.")
    if unpaid_dues(payer).exists():
        errors.append(f"{payer.email} has unresolved dues.")
    return errors


def _create_deposit(payer, copy, checkout):
    deposit = required_deposit(payer, copy)
    if deposit > 0:
        Charge.objects.create(
            user=payer,
            charge_type=Charge.ChargeType.DEPOSIT,
            amount=deposit,
            status=Charge.ChargeStatus.COLLECTED,
            checkout=checkout,
        )
    return deposit


@transaction.atomic
def perform_checkout(reservation, member, condition_notes="", override_reason=""):
    """D5-3: Member checks out a game to a borrower. Returns (checkout, errors)."""
    if hasattr(reservation, "checkout"):
        return reservation.checkout, []

    errors = checkout_blockers(reservation)
    if errors and not override_reason.strip():
        return None, errors

    payer = effective_borrower(reservation.user)
    copy = reservation.game_copy

    due_slot = reservation.return_slot
    if due_slot is None or due_slot.is_cancelled:
        due_slot = get_return_slot_for(reservation.pickup_slot, reservation.return_by_date)
        reservation.return_slot = due_slot
        reservation.save()

    checkout = Checkout.objects.create(
        reservation=reservation,
        borrower=payer,
        game_copy=copy,
        handoff_member=member,
        condition_notes=condition_notes,
        due_date_slot=due_slot,
    )

    copy.is_available = False
    if condition_notes:
        copy.notes = condition_notes
    copy.save()

    deposit = _create_deposit(payer, copy, checkout)

    if errors:
        _log("override", member, target_user=payer, details={
            "override_type": "checkout",
            "reason": override_reason,
            "blocked_by": errors,
        })
    _log("checkout", member, target_user=payer, details={
        "checkout_id": checkout.pk,
        "game_copy": str(copy),
        "deposit_amount": str(deposit),
        "requested_by": reservation.user.email,
    })
    return checkout, []


def refund_deposit(checkout, member):
    """Mark a checkout's collected deposit as refunded."""
    deposits = Charge.objects.filter(
        checkout=checkout,
        charge_type=Charge.ChargeType.DEPOSIT,
        status=Charge.ChargeStatus.COLLECTED,
    )
    count = 0
    for charge in deposits:
        charge.status = Charge.ChargeStatus.REFUNDED
        charge.save()
        count += 1
    if count:
        checkout.deposit_status = "refunded"
        checkout.refund_member = member
        checkout.save()
        _log("deposit_refunded", member, target_user=checkout.borrower, details={
            "checkout_id": checkout.pk,
        })
    return count


@transaction.atomic
def perform_return(checkout, member, condition_notes="", refund=False):
    """D5-4: Member marks a game as returned. Deposit is refunded now or within a week."""
    update_late_fee_charge(checkout)  # settle fees while the checkout is still open

    checkout.return_time = timezone.now()
    checkout.return_member = member
    checkout.refund_due_by = today_ist() + timedelta(days=DEPOSIT_REFUND_DAYS)
    if condition_notes:
        checkout.condition_notes = condition_notes
        checkout.game_copy.notes = condition_notes
    checkout.save()

    copy = checkout.game_copy
    if copy.condition == GameCopy.Condition.LOST:
        copy.condition = GameCopy.Condition.GOOD
    copy.is_available = copy.condition in LENDABLE
    copy.save()

    # A game found/returned after being declared lost no longer owes replacement.
    Charge.objects.filter(
        checkout=checkout,
        charge_type=Charge.ChargeType.LOST_REPLACEMENT,
        status=Charge.ChargeStatus.UNPAID,
    ).update(status=Charge.ChargeStatus.WAIVED, notes="Game returned.")

    if checkout.reservation:
        checkout.reservation.status = Reservation.Status.COMPLETED
        checkout.reservation.save()

    if refund:
        refund_deposit(checkout, member)

    _log("return", member, target_user=checkout.borrower, details={
        "checkout_id": checkout.pk,
        "game_copy": str(copy),
        "refund_due_by": str(checkout.refund_due_by),
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


def walk_in_blockers(borrower, game_copy):
    """Policy problems for a first-come, first-served walk-in checkout."""
    errors = validate_borrow(borrower, game_copy, None)
    if not game_copy.is_available:
        errors.append("This copy is currently checked out.")
    today = today_ist()
    if not _borrow_slots().filter(date=today).exists():
        errors.append("There is no borrow slot today.")
    if Reservation.objects.filter(
        game_copy=game_copy,
        pickup_slot__date=today,
        pickup_slot__slot_type=WeeklySlot.SlotType.BORROW,
        status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING],
        checkout__isnull=True,
    ).exists():
        errors.append(
            "This copy is reserved for today. Walk-ins are only allowed when "
            "there is no reservation (mark a no-show first)."
        )
    return errors


@transaction.atomic
def perform_walk_in_checkout(borrower, game_copy, member, condition_notes="",
                             override_reason=""):
    """D5-7: Walk-in checkout without a reservation. Returns (checkout, errors)."""
    errors = walk_in_blockers(borrower, game_copy)
    if errors and not override_reason.strip():
        return None, errors

    payer = effective_borrower(borrower)
    due_slot = _next_return_slot_after_dt(_now_ist())

    checkout = Checkout.objects.create(
        borrower=payer,
        game_copy=game_copy,
        handoff_member=member,
        condition_notes=condition_notes,
        due_date_slot=due_slot,
    )
    game_copy.is_available = False
    if condition_notes:
        game_copy.notes = condition_notes
    game_copy.save()

    deposit = _create_deposit(payer, game_copy, checkout)

    if errors:
        _log("override", member, target_user=payer, details={
            "override_type": "walk_in",
            "reason": override_reason,
            "blocked_by": errors,
        })
    _log("walk_in", member, target_user=payer, details={
        "checkout_id": checkout.pk,
        "game_copy": str(game_copy),
        "deposit_amount": str(deposit),
    })
    return checkout, []


# =====================================================================
# Fees & Incidents
# =====================================================================

def missed_return_slots(checkout, as_of=None):
    """Return slots, from the due slot onward, that ended without the game being back."""
    due = effective_due_slot(checkout)
    if due is None:
        return []
    end = checkout.return_time or as_of or timezone.now()
    slots = _return_slots().filter(date__gte=due.date).order_by("date", "start_time")
    due_key = (due.date, due.start_time)
    return [
        s for s in slots
        if (s.date, s.start_time) >= due_key and s.end_datetime <= end
    ]


def calculate_late_fee(checkout):
    """F6-1: INR 25 per game per return slot missed."""
    return LATE_FEE_PER_SLOT * len(missed_return_slots(checkout))


def update_late_fee_charge(checkout):
    """F6-2: Keep the checkout's late fee charges in step with slots missed."""
    if checkout.return_time:
        return None
    fee = calculate_late_fee(checkout)
    existing = list(Charge.objects.filter(
        checkout=checkout, charge_type=Charge.ChargeType.LATE_FEE
    ))
    covered = sum((c.amount for c in existing), Decimal("0"))
    diff = fee - covered
    if diff <= 0:
        return existing[0] if existing else None

    unpaid = next((c for c in existing if c.status == Charge.ChargeStatus.UNPAID), None)
    if unpaid:
        unpaid.amount += diff
        unpaid.save()
        return unpaid
    return Charge.objects.create(
        user=checkout.borrower,
        charge_type=Charge.ChargeType.LATE_FEE,
        amount=diff,
        status=Charge.ChargeStatus.UNPAID,
        checkout=checkout,
        notes="Late fee: INR 25 per return slot missed.",
    )


def check_lost_candidate(checkout):
    """F6-3: Flag checkouts still out 3 return slots after the due slot."""
    if checkout.return_time:
        return False
    due = effective_due_slot(checkout)
    if due is None:
        return False
    due_key = (due.date, due.start_time)
    after_due = [
        s for s in missed_return_slots(checkout) if (s.date, s.start_time) > due_key
    ]
    if len(after_due) >= LOST_AFTER_SLOTS:
        if not checkout.is_lost_candidate:
            checkout.is_lost_candidate = True
            checkout.save()
        return True
    return False


@transaction.atomic
def finalize_lost_game(checkout, member, replacement_amount=None):
    """
    F6-4: Finalize a lost game. Charges 100% of MRP (or a replacement with the
    same/later edition) as one unpaid charge. Late fees keep accruing separately.
    Returns (ok, message).
    """
    if checkout.return_time:
        return False, "This game has already been returned."
    if Charge.objects.filter(
        checkout=checkout, charge_type=Charge.ChargeType.LOST_REPLACEMENT
    ).exists():
        return False, "This game has already been finalized as lost."

    amount = replacement_amount
    if amount is None:
        amount = checkout.game_copy.game_title.mrp
    if amount is None or Decimal(amount) <= 0:
        return False, "The MRP for this game is not set. Enter the replacement amount."
    amount = _money(amount)

    update_late_fee_charge(checkout)

    Charge.objects.create(
        user=checkout.borrower,
        charge_type=Charge.ChargeType.LOST_REPLACEMENT,
        amount=amount,
        checkout=checkout,
        status=Charge.ChargeStatus.UNPAID,
        notes="100% of MRP, or replace with the same or a later edition. "
              "Must be paid in full; late fees continue to accrue until resolved.",
    )

    incident = Incident.objects.create(
        incident_type=Incident.IncidentType.LOST_GAME,
        checkout=checkout,
        game_copy=checkout.game_copy,
        user=checkout.borrower,
        description=f"Game finalized as lost. Replacement charge: INR {amount}",
        created_by=member,
    )

    checkout.is_lost_candidate = True
    checkout.save()
    checkout.game_copy.condition = GameCopy.Condition.LOST
    checkout.game_copy.is_available = False
    checkout.game_copy.save()

    outcome = register_infraction(
        checkout.borrower, member, "Lost game: " + str(checkout.game_copy), incident
    )

    _log("lost_finalized", member, target_user=checkout.borrower, details={
        "checkout_id": checkout.pk,
        "replacement_charge": str(amount),
        "infraction": outcome,
    })
    return True, outcome or ""


@transaction.atomic
def resolve_lost(checkout, member, method):
    """
    Close out a lost game. method: 'paid' (MRP paid), 'replaced' (new copy of the
    same or later edition supplied) or 'waived' (society discretion).
    """
    charge = Charge.objects.filter(
        checkout=checkout,
        charge_type=Charge.ChargeType.LOST_REPLACEMENT,
        status=Charge.ChargeStatus.UNPAID,
    ).first()
    if not charge:
        return False, "No unpaid replacement charge for this checkout."
    if method not in ("paid", "replaced", "waived"):
        return False, "Unknown resolution."

    charge.status = (
        Charge.ChargeStatus.PAID if method == "paid" else Charge.ChargeStatus.WAIVED
    )
    charge.notes = f"Resolved: {method}."
    charge.save()

    update_late_fee_charge(checkout)  # settle fees up to resolution
    checkout.return_time = timezone.now()
    checkout.return_member = member
    checkout.refund_due_by = today_ist() + timedelta(days=DEPOSIT_REFUND_DAYS)
    checkout.save()

    if method == "replaced":
        copy = checkout.game_copy
        copy.condition = GameCopy.Condition.GOOD
        copy.is_available = True
        copy.save()

    _log("lost_resolved", member, target_user=checkout.borrower, details={
        "checkout_id": checkout.pk,
        "method": method,
    })
    return True, None


def suggested_fee_range(category, mrp):
    """(low, high) INR guidance for a component category, or None if unknown."""
    if category not in COMPONENT_FEE_GUIDE or not mrp or Decimal(mrp) <= 0:
        return None
    low_pct, high_pct = COMPONENT_FEE_GUIDE[category]
    low = max(MIN_DAMAGE_FEE, _money(Decimal(mrp) * low_pct / 100))
    high = max(MIN_DAMAGE_FEE, _money(Decimal(mrp) * high_pct / 100))
    return low, high


@transaction.atomic
def log_damage_incident(checkout, member, description, damage_fee=None,
                        component_category="", unplayable=False,
                        incident_type=Incident.IncidentType.DAMAGE):
    """
    F6-5: Log damage / missing components with an optional replacement fee.
    Puts the borrower on probation. Returns (incident, warnings, infraction_msg).
    """
    copy = checkout.game_copy
    mrp = Decimal(copy.game_title.mrp)

    incident = Incident.objects.create(
        incident_type=incident_type,
        checkout=checkout,
        game_copy=copy,
        user=checkout.borrower,
        description=description,
        component_category=component_category,
        unplayable=unplayable,
        created_by=member,
    )

    warnings = []
    fee = Decimal(damage_fee) if damage_fee else Decimal("0")
    if unplayable and mrp > 0 and fee < mrp:
        warnings.append(
            f"Game is unplayable: policy requires 100% of MRP (INR {mrp}) "
            "or a replacement copy of the same or later edition."
        )
    if fee > 0:
        if fee < MIN_DAMAGE_FEE:
            warnings.append(f"Fee is below the INR {MIN_DAMAGE_FEE} minimum.")
        if mrp > 0 and fee > mrp:
            warnings.append(f"Fee INR {fee} exceeds the MRP (INR {mrp}).")
        Charge.objects.create(
            user=checkout.borrower,
            charge_type=Charge.ChargeType.DAMAGE_FEE,
            amount=fee,
            checkout=checkout,
            incident=incident,
            status=Charge.ChargeStatus.UNPAID,
            notes=description[:200],
        )

    if unplayable:
        copy.condition = GameCopy.Condition.DAMAGED
        copy.is_available = False
    elif copy.condition == GameCopy.Condition.GOOD:
        copy.condition = GameCopy.Condition.FAIR
    copy.notes = (copy.notes + "\n" if copy.notes else "") + description
    copy.save()

    outcome = register_infraction(
        checkout.borrower, member, f"Damage/missing components: {copy}", incident
    )

    _log("incident_logged", member, target_user=checkout.borrower, details={
        "incident_type": incident_type,
        "checkout_id": checkout.pk,
        "component_category": component_category,
        "fee": str(fee),
        "infraction": outcome,
    })
    return incident, warnings, outcome


@transaction.atomic
def report_existing_damage(checkout, user, description):
    """
    Borrower reports damage they did not cause, within 3 hours of pickup.
    Returns (ok, error).
    """
    if checkout.borrower != user:
        return False, "This is not your checkout."
    if checkout.return_time:
        return False, "This game has already been returned."
    if timezone.now() - checkout.checkout_time > INSPECTION_WINDOW:
        return False, (
            "The 3 hour inspection window has passed. You are now responsible "
            "for the game and all its components."
        )
    if not description.strip():
        return False, "Please describe the damage."

    Incident.objects.create(
        incident_type=Incident.IncidentType.PRE_EXISTING_DAMAGE,
        checkout=checkout,
        game_copy=checkout.game_copy,
        user=user,
        description=description.strip(),
        created_by=user,
    )
    _log("preexisting_damage", user, target_user=user, details={
        "checkout_id": checkout.pk,
        "description": description.strip(),
    })
    return True, None


# =====================================================================
# Borrower administration
# =====================================================================

@transaction.atomic
def update_borrower_flags(user, actor, requires_voucher, tdi_exempt, policy_type):
    profile = get_profile(user)
    profile.requires_voucher = requires_voucher
    profile.tdi_exempt = tdi_exempt
    if policy_type in dict(PolicyType.choices):
        profile.policy_type = policy_type
    profile.save()
    _log("borrower_updated", actor, target_user=user, details={
        "requires_voucher": requires_voucher,
        "tdi_exempt": tdi_exempt,
        "policy_type": profile.policy_type,
    })


@transaction.atomic
def record_cadi_escalation(user, actor, reason):
    Incident.objects.create(
        incident_type=Incident.IncidentType.CADI_ESCALATION,
        user=user,
        description=reason,
        created_by=actor,
    )
    _log("cadi_escalation", actor, target_user=user, details={"reason": reason})


@transaction.atomic
def record_premises_violation(user, actor, description, fine=None, probation=False):
    incident = Incident.objects.create(
        incident_type=Incident.IncidentType.PREMISES_VIOLATION,
        user=user,
        description=description,
        created_by=actor,
    )
    if fine and Decimal(fine) > 0:
        Charge.objects.create(
            user=user,
            charge_type=Charge.ChargeType.FINE,
            amount=fine,
            incident=incident,
            status=Charge.ChargeStatus.UNPAID,
            notes="Took a game outside University premises.",
        )
    outcome = None
    if probation:
        outcome = register_infraction(user, actor, "Took a game outside premises.")
    _log("premises_violation", actor, target_user=user, details={
        "description": description,
        "fine": str(fine) if fine else None,
        "infraction": outcome,
    })
    return outcome


@transaction.atomic
def add_manual_restriction(user, actor, rtype, reason, days=None):
    today = today_ist()
    end = None if rtype == Restriction.RestrictionType.BLACKLIST else today + timedelta(
        days=days or SEMESTER_DAYS
    )
    return _create_restriction(user, rtype, reason, today, end, actor)
