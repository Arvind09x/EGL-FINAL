"""
Views for the ESHTO Games Library.

Covers: Borrower portal (B3-*), Reservation actions (R4-11, R4-12),
Member desk (D5-*), Fees & incidents (F6-*), Slot management (S7-*),
Borrower administration, and Role management (A2-6).
"""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q, Exists, OuterRef
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .decorators import borrower_required, member_required, senior_admin_required
from .forms import SlotForm
from .models import (
    BorrowerProfile,
    Charge,
    Checkout,
    GameCopy,
    GameTitle,
    Incident,
    Reservation,
    Restriction,
    Vouch,
    WeeklySlot,
)

User = get_user_model()

PICKUP_LOCATION = services.PICKUP_LOCATION


def _alert(kind, text):
    """Small HTMX response fragment. Always HTTP 200 so HTMX swaps it in."""
    return HttpResponse(f'<div class="alert alert-{kind}">{text}</div>')


def _parse_decimal(raw):
    try:
        return Decimal(raw) if raw not in (None, "") else None
    except InvalidOperation:
        return None


# =====================================================================
# Public / Home
# =====================================================================

def home(request):
    """Landing page."""
    game_count = GameTitle.objects.count()
    next_slot = next(iter(services.upcoming_slots(limit=1)), None)
    return render(request, "core/home.html", {
        "game_count": game_count,
        "next_slot": next_slot,
        "location": PICKUP_LOCATION,
    })


def policy(request):
    """Library policy page."""
    return render(request, "core/policy.html")


def pixel_reveal(request):
    """Secret unlinked traitor reveal page."""
    return render(request, "core/pixel.html")


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
    games = GameTitle.objects.prefetch_related("copies").filter(
        copies__condition__in=["good", "fair"],
    ).distinct()

    return render(request, "core/available_games.html", {
        "games": games,
        "upcoming_slots": services.upcoming_slots(limit=6),
    })


# =====================================================================
# B3-3  Game Detail
# =====================================================================

def game_detail(request, pk):
    game = get_object_or_404(GameTitle.objects.prefetch_related("copies"), pk=pk)

    special_type = ""
    my_deposit = None
    tdi_exempt = False
    if request.user.is_authenticated:
        ptype = services.get_borrower_type(request.user)
        if ptype in services.SPECIAL_TYPES:
            special_type = ptype
        tdi_exempt = services.get_profile(request.user).tdi_exempt
        first_copy = game.copies.first()
        if first_copy:
            my_deposit = services.required_deposit(request.user, first_copy)

    now = timezone.now()
    slots_with_status = []
    for slot in services.upcoming_borrow_slots(limit=4):
        is_open = True
        closed_reason = ""
        if special_type and slot.start_datetime - now < services.SPECIAL_NOTICE:
            is_open = False
            closed_reason = "needs 2 days notice"
        slots_with_status.append({
            "slot": slot, "is_open": is_open, "closed_reason": closed_reason,
        })

    # (copy_id, slot_id) pairs that are already reserved
    reserved_pairs = set()
    for info in slots_with_status:
        reserved_copies = Reservation.objects.filter(
            pickup_slot=info["slot"],
            status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING],
            game_copy__game_title=game,
        ).values_list("game_copy_id", flat=True)
        for copy_id in reserved_copies:
            reserved_pairs.add((copy_id, info["slot"].pk))

    return render(request, "core/game_detail.html", {
        "game": game,
        "slots_with_status": slots_with_status,
        "location": PICKUP_LOCATION,
        "reserved_pairs": reserved_pairs,
        "user_has_phone": request.user.is_authenticated and bool(request.user.phone),
        "special_type": special_type,
        "my_deposit": my_deposit,
        "tdi_exempt": tdi_exempt,
        "return_slots": services.upcoming_slots(WeeklySlot.SlotType.RETURN, limit=2),
    })


# =====================================================================
# B3-4  My Reservations
# =====================================================================

@borrower_required
def my_reservations(request):
    reservations = Reservation.objects.filter(user=request.user).select_related(
        "game_copy__game_title", "pickup_slot", "return_slot"
    ).annotate(
        has_checkout=Exists(Checkout.objects.filter(reservation=OuterRef("pk")))
    ).order_by("-created_at")

    return render(request, "core/my_reservations.html", {
        "reservations": reservations,
        "restriction": services.get_blocking_restriction(request.user),
        "probation": services.get_active_probation(request.user),
        "location": PICKUP_LOCATION,
    })


# =====================================================================
# B3-5  My Checkouts & Dues
# =====================================================================

@borrower_required
def my_checkouts(request):
    checkouts = list(Checkout.objects.filter(borrower=request.user).select_related(
        "game_copy__game_title", "due_date_slot"
    ))
    now = timezone.now()

    # Update late fees on access (F6-2)
    for co in checkouts:
        if co.return_time is None:
            services.update_late_fee_charge(co)
            services.check_lost_candidate(co)
            deadline = co.checkout_time + services.INSPECTION_WINDOW
            co.can_report_damage = now <= deadline
            co.report_deadline = deadline
            co.due_slot = services.effective_due_slot(co)
        else:
            co.can_report_damage = False

    charges = Charge.objects.filter(user=request.user)
    return render(request, "core/my_checkouts.html", {
        "checkouts": checkouts,
        "charges": charges,
        "total_due": sum(
            (c.amount for c in charges if c.status == Charge.ChargeStatus.UNPAID),
            Decimal("0"),
        ),
        "restriction": services.get_blocking_restriction(request.user),
        "probation": services.get_active_probation(request.user),
        "location": PICKUP_LOCATION,
    })


@borrower_required
@require_POST
def report_damage(request, checkout_id):
    """Borrower reports damage they did not cause (within 3 hours of pickup)."""
    checkout = get_object_or_404(Checkout, pk=checkout_id, borrower=request.user)
    ok, error = services.report_existing_damage(
        checkout, request.user, request.POST.get("description", "")
    )
    if ok:
        messages.success(
            request,
            "Damage report recorded. You will not be charged for it. "
            "Please also email eeshto@ashoka.edu.in.",
        )
    else:
        messages.error(request, error)
    return redirect("core:my_checkouts")


# =====================================================================
# R4-11  Create Reservation
# =====================================================================

def _parse_date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@borrower_required
@require_POST
def create_reservation(request, copy_id, slot_id):
    copy = get_object_or_404(GameCopy, pk=copy_id)
    slot = get_object_or_404(WeeklySlot, pk=slot_id)

    phone = request.POST.get("phone_number", "").strip()
    if phone and not request.user.phone:
        request.user.phone = phone
        request.user.save()

    if not request.user.phone:
        error = "A contact number is required to reserve a game."
        reservation = None
    else:
        details = {
            "premises_ack": request.POST.get("premises_ack") == "on",
            "purpose": request.POST.get("purpose", ""),
            "poc_name": request.POST.get("poc_name", ""),
            "poc_phone": request.POST.get("poc_phone", ""),
            "return_by": _parse_date(request.POST.get("return_by", "")),
            "rh_floor": request.POST.get("rh_floor", ""),
            "authorizing_email": request.POST.get("authorizing_email", ""),
        }
        reservation, error = services.create_reservation(
            request.user, copy, slot, details
        )

    if request.htmx:
        if error:
            return HttpResponse(f'<div class="alert alert-error">{error}</div>', status=422)
        return HttpResponse(
            '<div class="alert alert-success">Reservation confirmed! '
            'Check <a href="/my/reservations/">My Reservations</a>.</div>'
        )

    if error:
        messages.error(request, error)
        return redirect("core:game_detail", pk=copy.game_title.pk)

    if reservation.status == Reservation.Status.PENDING:
        messages.success(
            request, "Request submitted. An Eeshto member will confirm it before the slot."
        )
    else:
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
            return HttpResponse(f'<div class="alert alert-error">{error}</div>', status=422)
        return HttpResponse('<div class="alert alert-success">Reservation cancelled.</div>')

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
    today = services.today_ist()
    todays_slots = WeeklySlot.objects.filter(date=today, is_cancelled=False)

    reservations = list(Reservation.objects.filter(
        pickup_slot__in=todays_slots.filter(slot_type=WeeklySlot.SlotType.BORROW),
        status=Reservation.Status.CONFIRMED,
        checkout__isnull=True,
    ).select_related("user", "game_copy__game_title", "pickup_slot", "return_slot"))
    for res in reservations:
        res.on_probation = services.get_active_probation(res.user) is not None
        res.blockers = services.checkout_blockers(res)

    pending_requests = Reservation.objects.filter(
        status=Reservation.Status.PENDING, pickup_slot__date__gte=today,
    ).select_related("user", "game_copy__game_title", "pickup_slot", "return_slot")

    active_checkouts = list(Checkout.objects.filter(
        return_time__isnull=True
    ).select_related("borrower", "game_copy__game_title", "due_date_slot"))
    for co in active_checkouts:
        services.update_late_fee_charge(co)
        services.check_lost_candidate(co)
        co.due_slot = services.effective_due_slot(co)
        co.on_probation = services.get_active_probation(co.borrower) is not None
        co.lost_charged = Charge.objects.filter(
            checkout=co, charge_type=Charge.ChargeType.LOST_REPLACEMENT,
            status=Charge.ChargeStatus.UNPAID,
        ).exists()
        co.late_fee_total = sum(
            (c.amount for c in Charge.objects.filter(
                checkout=co, charge_type=Charge.ChargeType.LATE_FEE
            ).exclude(status=Charge.ChargeStatus.WAIVED)),
            Decimal("0"),
        )

    pending_refunds = Charge.objects.filter(
        charge_type=Charge.ChargeType.DEPOSIT,
        status=Charge.ChargeStatus.COLLECTED,
        checkout__return_time__isnull=False,
    ).select_related("user", "checkout__game_copy__game_title")
    for refund in pending_refunds:
        due = refund.checkout.refund_due_by
        refund.overdue = bool(due and due < today)

    open_dues = Charge.objects.filter(
        status=Charge.ChargeStatus.UNPAID
    ).select_related("user", "checkout__game_copy__game_title")

    return render(request, "core/desk/desk.html", {
        "todays_slots": todays_slots,
        "reservations": reservations,
        "pending_requests": pending_requests,
        "active_checkouts": active_checkouts,
        "pending_refunds": pending_refunds,
        "open_dues": open_dues,
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
    override_reason = request.POST.get("override_reason", "")

    checkout, errors = services.perform_checkout(
        reservation, request.user, condition_notes, override_reason
    )
    if errors:
        text = " ".join(errors) + " Enter an override reason to proceed anyway."
        if request.htmx:
            return _alert("error", text)
        messages.error(request, text)
        return redirect("core:member_desk")

    who = checkout.borrower.email
    if request.htmx:
        return _alert("success", f"Checked out to {who}.")
    messages.success(request, f"Checked out to {who}.")
    return redirect("core:member_desk")


# =====================================================================
# D5-4 & D5-5  Desk Return
# =====================================================================

@member_required
@require_POST
def desk_return(request, checkout_id):
    checkout = get_object_or_404(Checkout, pk=checkout_id)
    condition_notes = request.POST.get("condition_notes", "")
    refund = request.POST.get("refund") == "on"

    services.perform_return(checkout, request.user, condition_notes, refund=refund)

    text = (
        "Game returned and deposit refunded." if refund
        else "Game returned. Deposit to be refunded within one week."
    )
    if request.htmx:
        return _alert("success", text)
    messages.success(request, text)
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
        return _alert("warning", "Marked as no-show. Copy is now available for walk-in.")
    messages.warning(request, "Marked as no-show.")
    return redirect("core:member_desk")


# =====================================================================
# Pending special-borrower requests (clubs, RAs, TAs/TFs)
# =====================================================================

@member_required
@require_POST
def desk_request_decision(request, reservation_id, decision):
    reservation = get_object_or_404(Reservation, pk=reservation_id)
    if decision == "approve":
        ok, error = services.approve_reservation(reservation, request.user)
        done = "Request approved."
    else:
        ok, error = services.reject_reservation(
            reservation, request.user, request.POST.get("reason", "")
        )
        done = "Request rejected."

    if request.htmx:
        return _alert("success" if ok else "error", done if ok else error)
    (messages.success if ok else messages.error)(request, done if ok else error)
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
        override_reason = request.POST.get("override_reason", "")

        try:
            borrower = User.objects.get(email=borrower_email)
        except User.DoesNotExist:
            messages.error(request, f"No user found with email {borrower_email}.")
            return redirect("core:desk_walk_in")

        if phone_number and not borrower.phone:
            borrower.phone = phone_number
            borrower.save()

        if not borrower.phone:
            messages.error(request, "A contact number is mandatory to perform a walk-in checkout.")
            return redirect("core:desk_walk_in")

        copy = get_object_or_404(GameCopy, pk=copy_id)

        checkout, errors = services.perform_walk_in_checkout(
            borrower, copy, request.user, "", override_reason
        )
        if errors:
            for error in errors:
                messages.error(request, error)
            messages.info(request, "Enter an override reason to proceed anyway.")
            return redirect("core:desk_walk_in")

        messages.success(request, f"Walk-in checkout created for {checkout.borrower.email}.")
        return redirect("core:member_desk")

    available_copies = GameCopy.objects.filter(
        is_available=True, condition__in=["good", "fair"]
    ).select_related("game_title")
    all_emails = json.dumps(list(User.objects.values_list("email", flat=True)))
    return render(request, "core/desk/walk_in.html", {
        "available_copies": available_copies,
        "all_emails": all_emails,
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

    if action == "refund" and charge.checkout:
        services.refund_deposit(charge.checkout, request.user)
        charge.refresh_from_db()
    elif action == "collect":
        charge.status = Charge.ChargeStatus.COLLECTED
        charge.save()
        services._log("deposit_collected", request.user, target_user=charge.user)
    elif action == "waive":
        charge.status = Charge.ChargeStatus.WAIVED
        charge.save()
        services._log("charge_updated", request.user, target_user=charge.user, details={
            "charge_id": charge.pk, "new_status": "waived",
        })

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
    mrp = checkout.game_copy.game_title.mrp

    if request.method == "POST":
        description = request.POST.get("description", "")
        fee = _parse_decimal(request.POST.get("damage_fee", ""))
        category = request.POST.get("component_category", "")
        if category not in dict(Incident.ComponentCategory.choices):
            category = ""
        incident_type = request.POST.get("incident_type", Incident.IncidentType.DAMAGE)
        if incident_type not in (
            Incident.IncidentType.DAMAGE, Incident.IncidentType.MISSING_COMPONENTS
        ):
            incident_type = Incident.IncidentType.DAMAGE

        incident, warnings, outcome = services.log_damage_incident(
            checkout, request.user, description, fee,
            component_category=category,
            unplayable=request.POST.get("unplayable") == "on",
            incident_type=incident_type,
        )
        for warning in warnings:
            messages.warning(request, warning)
        if outcome:
            messages.warning(request, outcome)
        messages.success(request, "Incident logged.")
        return redirect("core:member_desk")

    guide = {}
    for key, _ in Incident.ComponentCategory.choices:
        rng = services.suggested_fee_range(key, mrp)
        if rng:
            guide[key] = [str(rng[0]), str(rng[1])]
    return render(request, "core/desk/log_incident.html", {
        "checkout": checkout,
        "component_choices": Incident.ComponentCategory.choices,
        "fee_guide": json.dumps(guide),
        "pre_existing": checkout.incidents.filter(
            incident_type=Incident.IncidentType.PRE_EXISTING_DAMAGE
        ),
    })


# =====================================================================
# F6-4  Finalize Lost Game
# =====================================================================

@member_required
@require_POST
def finalize_lost(request, checkout_id):
    checkout = get_object_or_404(Checkout, pk=checkout_id)
    ok, info = services.finalize_lost_game(
        checkout, request.user, _parse_decimal(request.POST.get("replacement_amount", ""))
    )
    if not ok:
        if request.htmx:
            return _alert("error", info)
        messages.error(request, info)
        return redirect("core:member_desk")

    text = "Game finalized as lost. Replacement charge created."
    if request.htmx:
        return _alert("error", text)
    messages.warning(request, text)
    if info:
        messages.warning(request, info)
    return redirect("core:member_desk")


@member_required
@require_POST
def resolve_lost(request, checkout_id):
    checkout = get_object_or_404(Checkout, pk=checkout_id)
    ok, error = services.resolve_lost(
        checkout, request.user, request.POST.get("method", "")
    )
    if request.htmx:
        return _alert("success" if ok else "error", "Lost game resolved." if ok else error)
    (messages.success if ok else messages.error)(
        request, "Lost game resolved." if ok else error
    )
    return redirect("core:member_desk")


# =====================================================================
# F6-6  Update Charge Status
# =====================================================================

@member_required
@require_POST
def update_charge_status(request, charge_id):
    charge = get_object_or_404(Charge, pk=charge_id)
    new_status = request.POST.get("status", "")

    if (
        charge.charge_type == Charge.ChargeType.LOST_REPLACEMENT
        and charge.status == Charge.ChargeStatus.UNPAID
        and new_status in ("paid", "waived")
        and charge.checkout
        and charge.checkout.return_time is None
    ):
        services.resolve_lost(charge.checkout, request.user, new_status)
        charge.refresh_from_db()
    elif new_status in dict(Charge.ChargeStatus.choices):
        charge.status = new_status
        charge.save()
        services._log("charge_updated", request.user, target_user=charge.user, details={
            "charge_id": charge.pk,
            "new_status": new_status,
        })

    if request.htmx:
        return HttpResponse(f'<span class="badge">{charge.get_status_display()}</span>')
    return redirect("core:member_desk")


# =====================================================================
# S7-2  Slot management (any Eeshto member)
# =====================================================================

@member_required
def slot_list(request):
    slots = WeeklySlot.objects.filter(
        date__gte=services.today_ist()
    ).order_by("date", "start_time")
    for slot in slots:
        slot.in_use = services.slot_in_use(slot)
    return render(request, "core/slots/slot_list.html", {"slots": slots})


@member_required
def slot_create(request):
    form = SlotForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        repeat = form.cleaned_data.get("repeat_weeks") or 0
        base = form.save(commit=False)
        created = 0
        skipped = 0
        for week in range(repeat + 1):
            day = base.date + timedelta(days=7 * week)
            if WeeklySlot.objects.filter(date=day, start_time=base.start_time).exists():
                skipped += 1
                continue
            slot = WeeklySlot.objects.create(
                slot_type=base.slot_type,
                date=day,
                start_time=base.start_time,
                end_time=base.end_time,
                notes=base.notes,
                created_by=request.user,
            )
            services._log("slot_created", request.user, details={
                "slot_id": slot.pk, "date": str(slot.date), "type": slot.slot_type,
            })
            created += 1
        messages.success(request, f"{created} slot(s) created." + (
            f" {skipped} skipped (a slot already exists at that date and time)." if skipped else ""
        ))
        return redirect("core:slot_list")
    return render(request, "core/slots/slot_form.html", {"form": form, "editing": False})


@member_required
def slot_edit(request, pk):
    slot = get_object_or_404(WeeklySlot, pk=pk)
    form = SlotForm(request.POST or None, instance=slot)
    del form.fields["repeat_weeks"]
    in_use = services.slot_in_use(slot)
    if in_use:
        form.fields["slot_type"].disabled = True

    if request.method == "POST" and form.is_valid():
        form.save()
        services._log("slot_updated", request.user, details={
            "slot_id": slot.pk, "date": str(slot.date), "type": slot.slot_type,
        })
        messages.success(request, "Slot updated.")
        return redirect("core:slot_list")
    return render(request, "core/slots/slot_form.html", {
        "form": form, "editing": True, "slot": slot, "in_use": in_use,
    })


@member_required
@require_POST
def cancel_slot(request, pk):
    slot = get_object_or_404(WeeklySlot, pk=pk)
    cancelled = services.cancel_slot(slot, request.user)
    text = f"Slot on {slot.date} cancelled."
    if cancelled:
        text += f" {cancelled} reservation(s) were cancelled."
    messages.success(request, text)
    return redirect("core:slot_list")


@member_required
@require_POST
def restore_slot(request, pk):
    slot = get_object_or_404(WeeklySlot, pk=pk)
    slot.is_cancelled = False
    slot.save()
    services._log("slot_updated", request.user, details={
        "slot_id": slot.pk, "date": str(slot.date), "restored": True,
    })
    messages.success(request, f"Slot on {slot.date} restored.")
    return redirect("core:slot_list")


@member_required
@require_POST
def delete_slot(request, pk):
    slot = get_object_or_404(WeeklySlot, pk=pk)
    ok, error = services.delete_slot(slot, request.user)
    if ok:
        messages.success(request, "Slot deleted.")
    else:
        messages.error(request, error)
    return redirect("core:slot_list")


# =====================================================================
# Borrower administration (members)
# =====================================================================

@member_required
def desk_borrower(request):
    """Look up a borrower, review their standing, and apply policy actions."""
    email = (request.GET.get("email") or request.POST.get("email") or "").strip()
    target = User.objects.filter(email__iexact=email).first() if email else None

    if request.method == "POST":
        if not target:
            messages.error(request, f"No user found with email {email}.")
            return redirect("core:desk_borrower")
        action = request.POST.get("action", "")
        actor = request.user
        reason = request.POST.get("reason", "").strip()

        if action == "flags":
            services.update_borrower_flags(
                target, actor,
                requires_voucher=request.POST.get("requires_voucher") == "on",
                tdi_exempt=request.POST.get("tdi_exempt") == "on",
                policy_type=request.POST.get("policy_type", ""),
            )
            messages.success(request, "Borrower details updated.")
        elif action == "add_vouch":
            voucher = User.objects.filter(
                email__iexact=request.POST.get("voucher_email", "").strip()
            ).first()
            if not voucher:
                messages.error(request, "No user found for the vouching student's email.")
            else:
                _, error = services.add_vouch(target, voucher, actor)
                (messages.error if error else messages.success)(
                    request, error or f"{voucher.email} now vouches for {target.email}."
                )
        elif action == "remove_vouch":
            vouch = Vouch.objects.filter(pk=request.POST.get("vouch_id"), borrower=target).first()
            if vouch:
                services.remove_vouch(vouch, actor)
                messages.success(request, "Vouch removed.")
        elif action == "add_restriction":
            rtype = request.POST.get("restriction_type", "")
            if rtype not in dict(Restriction.RestrictionType.choices) or not reason:
                messages.error(request, "Choose a type and give a reason.")
            elif rtype != Restriction.RestrictionType.PROBATION and not (
                actor.is_senior_admin or actor.is_superuser
            ):
                messages.error(request, "Only a senior admin can apply a ban or blacklist.")
            else:
                services.add_manual_restriction(target, actor, rtype, reason)
                messages.success(request, "Restriction applied.")
        elif action == "lift_restriction":
            restriction = Restriction.objects.filter(
                pk=request.POST.get("restriction_id"), user=target
            ).first()
            if restriction and restriction.restriction_type != Restriction.RestrictionType.PROBATION \
                    and not (actor.is_senior_admin or actor.is_superuser):
                messages.error(request, "Only a senior admin can lift a ban or blacklist.")
            elif restriction:
                services.lift_restriction(restriction, actor)
                messages.success(request, "Restriction lifted.")
        elif action == "cadi":
            if not reason:
                messages.error(request, "A reason is required to escalate to CADI.")
            else:
                services.record_cadi_escalation(target, actor, reason)
                messages.warning(request, "Escalation to CADI recorded.")
        elif action == "premises":
            if not reason:
                messages.error(request, "Describe what happened.")
            else:
                outcome = services.record_premises_violation(
                    target, actor, reason,
                    fine=_parse_decimal(request.POST.get("fine", "")),
                    probation=request.POST.get("probation") == "on",
                )
                messages.warning(request, "Premises violation recorded.")
                if outcome:
                    messages.warning(request, outcome)
        return redirect(f"{request.path}?email={target.email}")

    context = {"email": email, "target": target}
    if email and not target:
        context["not_found"] = True
    if target:
        profile = services.get_profile(target)
        context.update({
            "profile": profile,
            "borrower_type": services.get_borrower_type(target),
            "policy_choices": BorrowerProfile.PolicyType.choices,
            "restrictions": Restriction.objects.filter(user=target),
            "vouch": services.get_active_vouch(target),
            "dues": services.unpaid_dues(target),
            "active_checkouts": Checkout.objects.filter(
                borrower=target, return_time__isnull=True
            ).select_related("game_copy__game_title"),
            "incidents": Incident.objects.filter(user=target)[:15],
            "restriction_choices": Restriction.RestrictionType.choices,
            "max_games": services.get_max_active_limit(target),
        })
    return render(request, "core/desk/borrower.html", context)


# =====================================================================
# D5-9  Admin Logs Ledger
# =====================================================================

def _email_to_name(email):
    """Convert 'arjun.singh_ug2023@ashoka.edu.in' → 'Arjun Singh'."""
    if not email or email == '-':
        return email or '-'
    local = email.split('@')[0]          # arjun.singh_ug2023
    name_part = local.split('_')[0]      # arjun.singh
    return name_part.replace('.', ' ').title()  # Arjun Singh


@member_required
def admin_logs(request):
    checkouts = Checkout.objects.select_related(
        "borrower", "game_copy__game_title", "handoff_member", "return_member", "refund_member"
    ).prefetch_related("charges").order_by("-checkout_time")

    transactions = []
    balances = {}

    for co in checkouts:
        deposit_charge = next((c for c in co.charges.all() if c.charge_type == Charge.ChargeType.DEPOSIT), None)
        deposit_amount = deposit_charge.amount if deposit_charge else Decimal('0')
        
        # Senior admins don't pay security deposits
        if co.borrower.is_senior_admin:
            deposit_amount = Decimal('0')
        
        late_fee = next((c for c in co.charges.all() if c.charge_type == Charge.ChargeType.LATE_FEE), None)
        late_fee_amount = late_fee.amount if late_fee and late_fee.status == Charge.ChargeStatus.PAID else Decimal('0')
        
        damage_fee = next((c for c in co.charges.all() if c.charge_type == Charge.ChargeType.DAMAGE_FEE), None)
        damage_fee_amount = damage_fee.amount if damage_fee and damage_fee.status == Charge.ChargeStatus.PAID else Decimal('0')
        
        if co.handoff_member:
            uid = co.handoff_member.pk
            if uid not in balances:
                balances[uid] = {'user': co.handoff_member, 'collected': Decimal('0'), 'refunded': Decimal('0'), 'net': Decimal('0')}
            balances[uid]['collected'] += deposit_amount
            balances[uid]['net'] += deposit_amount
            
        is_refunded = deposit_charge and deposit_charge.status == Charge.ChargeStatus.REFUNDED
        # Policy: the full security deposit is refunded; fees are charged separately.
        amount_refunded = deposit_amount if is_refunded else Decimal('0')
        refunder = co.refund_member or co.return_member
        
        if is_refunded and refunder:
            rid = refunder.pk
            if rid not in balances:
                balances[rid] = {'user': refunder, 'collected': Decimal('0'), 'refunded': Decimal('0'), 'net': Decimal('0')}
            balances[rid]['refunded'] += amount_refunded
            balances[rid]['net'] -= amount_refunded
            
        transactions.append({
            'borrower': _email_to_name(co.borrower.email),
            'game': co.game_copy.game_title.name,
            'deposit_paid': deposit_amount,
            'checkout_admin': _email_to_name(co.handoff_member.email) if co.handoff_member else '-',
            'return_admin': _email_to_name(co.return_member.email) if co.return_member else '-',
            'amount_refunded': amount_refunded,
            'time': co.checkout_time,
            'status': deposit_charge.get_status_display() if deposit_charge else 'No Deposit'
        })

    debtors = []
    creditors = []
    
    for b in balances.values():
        if b['net'] > 0:
            debtors.append({'user': b['user'], 'amount': b['net']})
        elif b['net'] < 0:
            creditors.append({'user': b['user'], 'amount': -b['net']})
            
    debtors.sort(key=lambda x: x['amount'], reverse=True)
    creditors.sort(key=lambda x: x['amount'], reverse=True)
    
    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        debtor = debtors[i]
        creditor = creditors[j]
        amount = min(debtor['amount'], creditor['amount'])
        
        if amount > 0:
            settlements.append({
                'from': _email_to_name(debtor['user'].email),
                'to': _email_to_name(creditor['user'].email),
                'amount': amount
            })
            
        debtor['amount'] -= amount
        creditor['amount'] -= amount
        
        if debtor['amount'] == 0: i += 1
        if creditor['amount'] == 0: j += 1
            
    for k in range(i, len(debtors)):
        if debtors[k]['amount'] > 0:
            settlements.append({
                'from': _email_to_name(debtors[k]['user'].email),
                'to': 'Library Treasury',
                'amount': debtors[k]['amount']
            })

    for k in range(j, len(creditors)):
        if creditors[k]['amount'] > 0:
             settlements.append({
                 'from': 'Library Treasury',
                 'to': _email_to_name(creditors[k]['user'].email),
                 'amount': creditors[k]['amount']
             })
             
    # Format balance names for display
    balances_list = sorted(list(balances.values()), key=lambda x: x['net'], reverse=True)
    for b in balances_list:
        b['display_name'] = _email_to_name(b['user'].email)
            
    return render(request, "core/admin_logs.html", {
        "transactions": transactions,
        "balances": balances_list,
        "settlements": settlements
    })


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
