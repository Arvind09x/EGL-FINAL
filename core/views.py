"""
Views for the ESHTO Games Library.

Covers: Borrower portal (B3-*), Reservation actions (R4-11, R4-12),
Member desk (D5-*), Fees & incidents (F6-*), Slot management (S7-*),
and Role management (A2-6).
"""

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q, Exists, OuterRef
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
    today = timezone.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    upcoming_slots = WeeklySlot.objects.filter(
        date__gte=today, date__lte=sunday, is_cancelled=False
    ).order_by("date", "start_time")

    games = GameTitle.objects.prefetch_related("copies").filter(
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
    
    today = timezone.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    upcoming_slots = WeeklySlot.objects.filter(
        date__gte=today, date__lte=sunday, is_cancelled=False
    ).order_by("date", "start_time")

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
    raw_reservations = Reservation.objects.filter(user=request.user).select_related(
        "game_copy__game_title", "pickup_slot", "return_slot"
    ).annotate(
        has_checkout=Exists(Checkout.objects.filter(reservation=OuterRef('pk')))
    ).order_by("-created_at")

    reservations = list(raw_reservations)

    # Group reservations by (game_copy_id, iso_year, iso_week)
    from collections import defaultdict
    groups = defaultdict(list)
    for r in reservations:
        y, w, d = r.pickup_slot.date.isocalendar()
        groups[(r.game_copy_id, y, w)].append(r)
        
    unique_reservations = []
    seen_groups = set()
    for r in reservations:
        y, w, d = r.pickup_slot.date.isocalendar()
        key = (r.game_copy_id, y, w)
        group_res = groups[key]

        # Two-slot extension: ONLY when same copy booked for both Tue and Fri
        if len(group_res) > 1 and {1, 4}.issubset({res.pickup_slot.date.weekday() for res in group_res}):
            r.is_extended_group = True
            r.is_fully_checked_out = any(g.has_checkout for g in group_res)
            # Find the Tue reservation (earliest pickup)
            tue_res = min(group_res, key=lambda x: x.pickup_slot.date)
            # Find the return slot from the Tue reservation (should be next Tue)
            r.display_pickup = tue_res.pickup_slot
            r.display_return = tue_res.return_slot
            if key not in seen_groups:
                seen_groups.add(key)
                unique_reservations.append(r)
        else:
            r.is_extended_group = False
            r.is_fully_checked_out = r.has_checkout
            r.display_pickup = r.pickup_slot
            r.display_return = r.return_slot
            unique_reservations.append(r)
            
    restriction = services.get_active_restriction(request.user)
    return render(request, "core/my_reservations.html", {
        "reservations": unique_reservations,
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
    ).select_related(
        "user", "game_copy__game_title", "pickup_slot", "return_slot"
    ).annotate(
        has_checkout=Exists(Checkout.objects.filter(reservation=OuterRef('pk')))
    ).filter(has_checkout=False)

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

    # Calculate final late fee before setting return_time
    late_fee_charge = services.update_late_fee_charge(checkout)

    services.perform_return(checkout, request.user, condition_notes)

    # Refund deposit & Settle Late Fees
    deposit_charges = Charge.objects.filter(
        checkout=checkout,
        charge_type=Charge.ChargeType.DEPOSIT,
        status=Charge.ChargeStatus.COLLECTED,
    )
    for deposit in deposit_charges:
        # Mark deposit as refunded
        deposit.status = Charge.ChargeStatus.REFUNDED
        deposit.save()

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
            borrower, copy, request.user, "", due_slot
        )
        messages.success(request, f"Walk-in checkout created for {borrower.email}.")
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
        "borrower", "game_copy__game_title", "handoff_member", "return_member"
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
        amount_refunded = max(Decimal('0'), deposit_amount - late_fee_amount - damage_fee_amount) if is_refunded else Decimal('0')
        
        if is_refunded and co.return_member:
            rid = co.return_member.pk
            if rid not in balances:
                balances[rid] = {'user': co.return_member, 'collected': Decimal('0'), 'refunded': Decimal('0'), 'net': Decimal('0')}
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
