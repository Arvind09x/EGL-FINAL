"""
Views for the ESHTO Games Library.

Covers: Borrower portal (B3-*), Reservation actions (R4-11, R4-12),
Member desk (D5-*), Fees & incidents (F6-*), Slot management (S7-*),
and Role management (A2-6).
"""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .decorators import borrower_required, member_required, senior_admin_required
from .models import (
    Charge,
    Checkout,
    GameCopy,
    GameTitle,
    Incident,
    Reservation,
    Restriction,
    WeeklySlot,
)

User = get_user_model()

PICKUP_LOCATION = (
    "Student Commons, 2nd floor of the mess, "
    "near the couches between the meeting rooms"
)


# =====================================================================
# Public / Home
# =====================================================================

def home(request):
    """Landing page."""
    game_count = GameTitle.objects.count()
    next_slot = WeeklySlot.objects.filter(
        date__gte=timezone.now().date(), is_cancelled=False
    ).first()
    return render(request, "core/home.html", {
        "game_count": game_count,
        "next_slot": next_slot,
        "location": PICKUP_LOCATION,
    })


def policy(request):
    """Library policy page."""
    return render(request, "core/policy.html")


# =====================================================================
# B3-1  All Games
# =====================================================================

def all_games(request):
    games = GameTitle.objects.prefetch_related("copies").all()
    return render(request, "core/all_games.html", {"games": games})


# =====================================================================
# B3-2  Available Games
# =====================================================================

def available_games(request):
    upcoming_slots = WeeklySlot.objects.filter(
        date__gte=timezone.now().date(), is_cancelled=False
    ).order_by("date", "start_time")[:4]

    games = GameTitle.objects.prefetch_related("copies").filter(
        copies__is_available=True,
        copies__condition__in=["good", "fair"],
    ).distinct()

    return render(request, "core/available_games.html", {
        "games": games,
        "upcoming_slots": upcoming_slots,
    })


# =====================================================================
# B3-3  Game Detail
# =====================================================================

def game_detail(request, pk):
    game = get_object_or_404(GameTitle.objects.prefetch_related("copies"), pk=pk)
    upcoming_slots = WeeklySlot.objects.filter(
        date__gte=timezone.now().date(), is_cancelled=False
    ).order_by("date", "start_time")[:4]

    # Check which slots have open booking windows
    slots_with_status = []
    for slot in upcoming_slots:
        is_open = services.is_booking_window_open(slot) or (
            request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)
        )
        slots_with_status.append({"slot": slot, "is_open": is_open})

    # Build set of (copy_id, slot_id) pairs that are already reserved
    reserved_pairs = set()
    for slot in upcoming_slots:
        reserved_copies = Reservation.objects.filter(
            pickup_slot=slot,
            status=Reservation.Status.CONFIRMED,
            game_copy__game_title=game,
        ).values_list("game_copy_id", flat=True)
        for copy_id in reserved_copies:
            reserved_pairs.add((copy_id, slot.pk))

    user_has_phone = (
        request.user.is_authenticated and bool(request.user.phone)
    )

    return render(request, "core/game_detail.html", {
        "game": game,
        "slots_with_status": slots_with_status,
        "location": PICKUP_LOCATION,
        "reserved_pairs": reserved_pairs,
        "user_has_phone": user_has_phone,
    })


# =====================================================================
# B3-4  My Reservations
# =====================================================================

@borrower_required
def my_reservations(request):
    reservations = Reservation.objects.filter(user=request.user).select_related(
        "game_copy__game_title", "pickup_slot", "return_slot"
    )
    restriction = services.get_active_restriction(request.user)
    return render(request, "core/my_reservations.html", {
        "reservations": reservations,
        "restriction": restriction,
        "location": PICKUP_LOCATION,
    })


# =====================================================================
# B3-5  My Checkouts & Dues
# =====================================================================

@borrower_required
def my_checkouts(request):
    checkouts = Checkout.objects.filter(borrower=request.user).select_related(
        "game_copy__game_title", "due_date_slot"
    )
    charges = Charge.objects.filter(user=request.user)
    restriction = services.get_active_restriction(request.user)

    # Update late fees on access (F6-2)
    for co in checkouts.filter(return_time__isnull=True):
        services.update_late_fee_charge(co)
        services.check_lost_candidate(co)

    return render(request, "core/my_checkouts.html", {
        "checkouts": checkouts,
        "charges": charges,
        "restriction": restriction,
        "location": PICKUP_LOCATION,
    })


# =====================================================================
# R4-11  Create Reservation (HTMX)
# =====================================================================

@borrower_required
@require_POST
def create_reservation(request, copy_id, slot_id):
    copy = get_object_or_404(GameCopy, pk=copy_id)
    slot = get_object_or_404(WeeklySlot, pk=slot_id)

    phone = request.POST.get("phone_number")
    if phone and not request.user.phone:
        request.user.phone = phone.strip()
        request.user.save()

    reservation, error = services.create_reservation(request.user, copy, slot)

    if request.htmx:
        if error:
            return HttpResponse(
                f'<div class="alert alert-error">{error}</div>',
                status=422,
            )
        return HttpResponse(
            '<div class="alert alert-success">Reservation confirmed! '
            'Check <a href="/my/reservations/">My Reservations</a>.</div>'
        )

    if error:
        messages.error(request, error)
        return redirect("core:game_detail", pk=copy.game_title.pk)

    messages.success(request, "Reservation confirmed!")
    return redirect("core:my_reservations")


# =====================================================================
# R4-12  Cancel Reservation (HTMX)
# =====================================================================

@borrower_required
@require_POST
def cancel_reservation(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk, user=request.user)
    ok, error = services.cancel_reservation(reservation, request.user)

    if request.htmx:
        if error:
            return HttpResponse(
                f'<div class="alert alert-error">{error}</div>',
                status=422,
            )
        return HttpResponse(
            '<div class="alert alert-success">Reservation cancelled.</div>'
        )

    if error:
        messages.error(request, error)
    else:
        messages.success(request, "Reservation cancelled.")
    return redirect("core:my_reservations")


# =====================================================================
# D5-1  Member Desk
# =====================================================================

@member_required
def member_desk(request):
    today = timezone.now().date()
    todays_slots = WeeklySlot.objects.filter(date=today, is_cancelled=False)

    reservations = Reservation.objects.filter(
        pickup_slot__in=todays_slots,
        status=Reservation.Status.CONFIRMED,
    ).select_related("user", "game_copy__game_title", "pickup_slot")

    active_checkouts = Checkout.objects.filter(
        return_time__isnull=True
    ).select_related("borrower", "game_copy__game_title", "due_date_slot")

    # Update late fees and lost candidates
    for co in active_checkouts:
        services.update_late_fee_charge(co)
        services.check_lost_candidate(co)

    return render(request, "core/desk/desk.html", {
        "todays_slots": todays_slots,
        "reservations": reservations,
        "active_checkouts": active_checkouts,
        "location": PICKUP_LOCATION,
    })


# =====================================================================
# D5-2 & D5-3  Desk Checkout
# =====================================================================

@member_required
@require_POST
def desk_checkout(request, reservation_id):
    reservation = get_object_or_404(Reservation, pk=reservation_id)
    condition_notes = request.POST.get("condition_notes", "")

    checkout = services.perform_checkout(reservation, request.user, condition_notes)

    if request.htmx:
        return HttpResponse(
            f'<div class="alert alert-success">Checked out to {reservation.user.email}.</div>'
        )
    messages.success(request, f"Checked out to {reservation.user.email}.")
    return redirect("core:member_desk")


# =====================================================================
# D5-4 & D5-5  Desk Return
# =====================================================================

@member_required
@require_POST
def desk_return(request, checkout_id):
    checkout = get_object_or_404(Checkout, pk=checkout_id)
    condition_notes = request.POST.get("condition_notes", "")

    services.perform_return(checkout, request.user, condition_notes)

    # Refund deposit (D5-5)
    deposit_charges = Charge.objects.filter(
        checkout=checkout,
        charge_type=Charge.ChargeType.DEPOSIT,
        status=Charge.ChargeStatus.COLLECTED,
    )
    for charge in deposit_charges:
        charge.status = Charge.ChargeStatus.REFUNDED
        charge.save()

    if request.htmx:
        return HttpResponse(
            '<div class="alert alert-success">Game returned and deposit refunded.</div>'
        )
    messages.success(request, "Game returned and deposit refunded.")
    return redirect("core:member_desk")


# =====================================================================
# D5-6  No-Show
# =====================================================================

@member_required
@require_POST
def desk_no_show(request, reservation_id):
    reservation = get_object_or_404(Reservation, pk=reservation_id)
    services.perform_no_show(reservation, request.user)

    if request.htmx:
        return HttpResponse(
            '<div class="alert alert-warning">Marked as no-show. Copy is now available for walk-in.</div>'
        )
    messages.warning(request, "Marked as no-show.")
    return redirect("core:member_desk")


# =====================================================================
# D5-7  Walk-in Checkout
# =====================================================================

@member_required
def desk_walk_in(request):
    if request.method == "POST":
        borrower_email = request.POST.get("borrower_email", "")
        phone_number = request.POST.get("phone_number", "").strip()
        copy_id = request.POST.get("copy_id", "")
        condition_notes = request.POST.get("condition_notes", "")

        try:
            borrower = User.objects.get(email=borrower_email)
        except User.DoesNotExist:
            messages.error(request, f"No user found with email {borrower_email}.")
            return redirect("core:desk_walk_in")

        if phone_number and not borrower.phone:
            borrower.phone = phone_number
            borrower.save()

        # Check if phone number is present now
        if not borrower.phone:
             messages.error(request, "A contact number is mandatory to perform a walk-in checkout.")
             return redirect("core:desk_walk_in")

        copy = get_object_or_404(GameCopy, pk=copy_id)

        # Find next slot for due date
        due_slot = WeeklySlot.objects.filter(
            date__gte=timezone.now().date(), is_cancelled=False
        ).order_by("date", "start_time").first()

        checkout = services.perform_walk_in_checkout(
            borrower, copy, request.user, condition_notes, due_slot
        )
        messages.success(request, f"Walk-in checkout created for {borrower.email}.")
        return redirect("core:member_desk")

    available_copies = GameCopy.objects.filter(
        is_available=True, condition__in=["good", "fair"]
    ).select_related("game_title")
    return render(request, "core/desk/walk_in.html", {
        "available_copies": available_copies,
    })


# =====================================================================
# D5-8  Override
# =====================================================================

@member_required
@require_POST
def desk_override(request):
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Override reason is mandatory.")
        return redirect("core:member_desk")

    override_type = request.POST.get("override_type", "")
    target_email = request.POST.get("target_email", "")

    try:
        target_user = User.objects.get(email=target_email) if target_email else None
    except User.DoesNotExist:
        target_user = None

    Incident.objects.create(
        incident_type=Incident.IncidentType.OVERRIDE,
        user=target_user,
        description=f"Override ({override_type}): {reason}",
        created_by=request.user,
    )

    services._log("override", request.user, target_user=target_user, details={
        "override_type": override_type,
        "reason": reason,
    })

    messages.success(request, "Override applied and logged.")
    return redirect("core:member_desk")


# =====================================================================
# D5-2  Deposit actions
# =====================================================================

@member_required
@require_POST
def desk_deposit(request, charge_id, action):
    charge = get_object_or_404(Charge, pk=charge_id, charge_type=Charge.ChargeType.DEPOSIT)

    if action == "collect":
        charge.status = Charge.ChargeStatus.COLLECTED
        services._log("deposit_collected", request.user, target_user=charge.user)
    elif action == "refund":
        charge.status = Charge.ChargeStatus.REFUNDED
        services._log("deposit_refunded", request.user, target_user=charge.user)
    elif action == "waive":
        charge.status = Charge.ChargeStatus.WAIVED

    charge.save()

    if request.htmx:
        return HttpResponse(
            f'<span class="badge badge-{action}">{charge.get_status_display()}</span>'
        )
    return redirect("core:member_desk")


# =====================================================================
# F6-5  Log Incident
# =====================================================================

@member_required
def log_incident(request, checkout_id):
    checkout = get_object_or_404(Checkout, pk=checkout_id)

    if request.method == "POST":
        description = request.POST.get("description", "")
        damage_fee = request.POST.get("damage_fee", "")
        fee = Decimal(damage_fee) if damage_fee else None

        incident, warning = services.log_damage_incident(
            checkout, request.user, description, fee
        )
        if warning:
            messages.warning(request, warning)
        messages.success(request, "Incident logged.")
        return redirect("core:member_desk")

    return render(request, "core/desk/log_incident.html", {"checkout": checkout})


# =====================================================================
# F6-4  Finalize Lost Game
# =====================================================================

@member_required
@require_POST
def finalize_lost(request, checkout_id):
    checkout = get_object_or_404(Checkout, pk=checkout_id)
    services.finalize_lost_game(checkout, request.user)

    if request.htmx:
        return HttpResponse(
            '<div class="alert alert-error">Game finalized as lost. Charges created.</div>'
        )
    messages.warning(request, "Game finalized as lost. Charges created.")
    return redirect("core:member_desk")


# =====================================================================
# F6-6  Update Charge Status
# =====================================================================

@member_required
@require_POST
def update_charge_status(request, charge_id):
    charge = get_object_or_404(Charge, pk=charge_id)
    new_status = request.POST.get("status", "")
    if new_status in dict(Charge.ChargeStatus.choices):
        charge.status = new_status
        charge.save()
        services._log("charge_updated", request.user, target_user=charge.user, details={
            "charge_id": charge.pk,
            "new_status": new_status,
        })

    if request.htmx:
        return HttpResponse(
            f'<span class="badge">{charge.get_status_display()}</span>'
        )
    return redirect("core:member_desk")


# =====================================================================
# S7-2  Slot management
# =====================================================================

@member_required
def slot_list(request):
    slots = WeeklySlot.objects.filter(
        date__gte=timezone.now().date()
    ).order_by("date", "start_time")
    return render(request, "core/slots/slot_list.html", {"slots": slots})


@member_required
@require_POST
def cancel_slot(request, pk):
    slot = get_object_or_404(WeeklySlot, pk=pk)
    slot.is_cancelled = True
    slot.save()

    services._log("slot_cancelled", request.user, details={
        "slot_id": slot.pk,
        "date": str(slot.date),
    })

    if request.htmx:
        return HttpResponse(
            '<span class="badge badge-cancelled">Cancelled</span>'
        )
    messages.success(request, f"Slot on {slot.date} cancelled.")
    return redirect("core:slot_list")


# =====================================================================
# A2-6  Manage Roles (senior_admin only)
# =====================================================================

@senior_admin_required
def manage_roles(request):
    if request.method == "POST":
        target_email = request.POST.get("email", "")
        action = request.POST.get("action", "")

        try:
            target_user = User.objects.get(email=target_email)
        except User.DoesNotExist:
            messages.error(request, f"No user found with email {target_email}.")
            return redirect("core:manage_roles")

        if action == "grant_member":
            target_user.is_member = True
            target_user.save()
            messages.success(request, f"{target_email} is now a member.")
        elif action == "revoke_member":
            target_user.is_member = False
            target_user.save()
            messages.success(request, f"{target_email} is no longer a member.")
        elif action == "grant_admin":
            target_user.is_senior_admin = True
            target_user.save()
            messages.success(request, f"{target_email} is now a senior admin.")
        elif action == "revoke_admin":
            target_user.is_senior_admin = False
            target_user.save()
            messages.success(request, f"{target_email} is no longer a senior admin.")

        services._log("role_changed", request.user, target_user=target_user, details={
            "action": action,
        })
        return redirect("core:manage_roles")

    users = User.objects.all().order_by("email")
    return render(request, "core/manage_roles.html", {"users": users})
