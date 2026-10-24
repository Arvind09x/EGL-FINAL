"""Tests for policy rules, services and the slot/desk views."""

import zoneinfo
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import (
    BorrowerProfile,
    Charge,
    Checkout,
    GameCopy,
    GameTitle,
    Incident,
    Reservation,
    Restriction,
    User,
    WeeklySlot,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
BORROW = WeeklySlot.SlotType.BORROW
RETURN = WeeklySlot.SlotType.RETURN


def make_user(name, **kwargs):
    return User.objects.create_user(
        username=name, email=f"{name}@ashoka.edu.in", password="x", phone="9999999999",
        **kwargs,
    )


def make_slot(kind, days, start=time(20, 30), end=time(21, 30)):
    """Create a slot `days` from today (negative = in the past)."""
    day = services.today_ist() + timedelta(days=days)
    return WeeklySlot.objects.create(slot_type=kind, date=day, start_time=start, end_time=end)


def make_game(name="Game", category="D", mrp=1000, copies=1):
    title = GameTitle.objects.create(name=name, deposit_category=category, mrp=mrp)
    return [
        GameCopy.objects.create(game_title=title, copy_label=f"{i + 1:03d}")
        for i in range(copies)
    ]


def set_type(user, policy_type):
    profile = services.get_profile(user)
    profile.policy_type = policy_type
    profile.save()


class DepositAndLimitTests(TestCase):
    def test_category_sets_deposit(self):
        for cat, amount in {"D": 50, "C": 150, "B": 300, "A": 600, "S": 650}.items():
            title = GameTitle.objects.create(name=f"G{cat}", deposit_category=cat)
            self.assertEqual(title.security_deposit, amount)

    def test_student_pays_category_deposit(self):
        copy = make_game(category="B")[0]
        self.assertEqual(services.required_deposit(make_user("stu"), copy), Decimal("300.00"))

    def test_ra_and_member_pay_nothing(self):
        copy = make_game(category="A")[0]
        ra = make_user("ra")
        set_type(ra, BorrowerProfile.PolicyType.RA)
        member = make_user("mem", is_member=True)
        self.assertEqual(services.required_deposit(ra, copy), 0)
        self.assertEqual(services.required_deposit(member, copy), 0)

    def test_limits(self):
        stu, ra, club, ta = (make_user(n) for n in ("stu", "ra", "club", "ta"))
        set_type(ra, BorrowerProfile.PolicyType.RA)
        set_type(club, BorrowerProfile.PolicyType.CLUB_SOCIETY)
        set_type(ta, BorrowerProfile.PolicyType.TA_TF)
        self.assertEqual(services.get_max_active_limit(stu), 2)
        self.assertEqual(services.get_max_active_limit(ra), 4)
        self.assertEqual(services.get_max_active_limit(club), 6)
        self.assertIsNone(services.get_max_active_limit(ta))

    def test_club_deposit_formula(self):
        club = make_user("club")
        set_type(club, BorrowerProfile.PolicyType.CLUB_SOCIETY)
        cheap = make_game("Cheap", "D", mrp=400)[0]       # 25% = 100 > 50
        pricey = make_game("Pricey", "D", mrp=10000)[0]   # 25% = 2500, capped 1000
        self.assertEqual(services.required_deposit(club, cheap), Decimal("100.00"))
        self.assertEqual(services.required_deposit(club, pricey), Decimal("1000.00"))
        # After one incident: 50% of MRP.
        Incident.objects.create(incident_type=Incident.IncidentType.DAMAGE, user=club)
        self.assertEqual(services.required_deposit(club, cheap), Decimal("200.00"))


class ReservationRuleTests(TestCase):
    def setUp(self):
        self.user = make_user("stu")
        self.pickup = make_slot(BORROW, 1)
        self.ret = make_slot(RETURN, 6)

    def reserve(self, copy, user=None, slot=None, **details):
        details.setdefault("premises_ack", True)
        return services.create_reservation(user or self.user, copy, slot or self.pickup, details)

    def test_basic_reservation_uses_next_return_slot(self):
        copy = make_game()[0]
        res, err = self.reserve(copy)
        self.assertIsNone(err)
        self.assertEqual(res.status, Reservation.Status.CONFIRMED)
        self.assertEqual(res.return_slot, self.ret)

    def test_premises_ack_required_unless_tdi(self):
        copy = make_game()[0]
        _, err = services.create_reservation(self.user, copy, self.pickup, {})
        self.assertIn("premises", err)
        profile = services.get_profile(self.user)
        profile.tdi_exempt = True
        profile.save()
        res, err = services.create_reservation(self.user, copy, self.pickup, {})
        self.assertIsNone(err)

    def test_max_two_games(self):
        copies = [make_game(f"G{i}")[0] for i in range(3)]
        self.assertIsNone(self.reserve(copies[0])[1])
        self.assertIsNone(self.reserve(copies[1])[1])
        self.assertIn("maximum of 2", self.reserve(copies[2])[1])

    def test_checked_out_reservation_not_double_counted(self):
        copies = [make_game(f"G{i}")[0] for i in range(2)]
        res, _ = self.reserve(copies[0])
        member = make_user("mem", is_member=True)
        services.perform_checkout(res, member)
        self.assertEqual(services.active_reservation_count(self.user), 1)
        self.assertIsNone(self.reserve(copies[1])[1])

    def test_only_one_s_category(self):
        s1 = make_game("S1", "S")[0]
        s2 = make_game("S2", "S")[0]
        self.assertIsNone(self.reserve(s1)[1])
        self.assertIn("S category", self.reserve(s2)[1])

    def test_no_consecutive_same_game(self):
        earlier = make_slot(BORROW, -6)
        copy = make_game()[0]
        Reservation.objects.create(
            user=self.user, game_copy=copy, pickup_slot=earlier,
            status=Reservation.Status.COMPLETED,
        )
        self.assertIn("consecutively", self.reserve(copy)[1])

    def test_same_game_ok_after_a_gap(self):
        old = make_slot(BORROW, -13)
        make_slot(BORROW, -6)  # a borrow slot the user skipped
        copy = make_game()[0]
        Reservation.objects.create(
            user=self.user, game_copy=copy, pickup_slot=old,
            status=Reservation.Status.COMPLETED,
        )
        self.assertIsNone(self.reserve(copy)[1])

    def test_unpaid_dues_block(self):
        Charge.objects.create(
            user=self.user, charge_type=Charge.ChargeType.LATE_FEE,
            amount=25, status=Charge.ChargeStatus.UNPAID,
        )
        self.assertIn("dues", self.reserve(make_game()[0])[1])

    def test_ban_blocks_but_probation_does_not(self):
        copy = make_game()[0]
        services.add_manual_restriction(
            self.user, None, Restriction.RestrictionType.PROBATION, "test"
        )
        self.assertIsNone(self.reserve(copy)[1])
        services.add_manual_restriction(
            self.user, None, Restriction.RestrictionType.TEMP_BAN, "test"
        )
        other = make_game("Other")[0]
        self.assertIn("barred", self.reserve(other)[1])

    def test_started_or_cancelled_slot_rejected(self):
        copy = make_game()[0]
        started = WeeklySlot.objects.create(
            slot_type=BORROW, date=services.today_ist() - timedelta(days=1),
            start_time=time(1, 0), end_time=time(2, 0),
        )
        self.assertIn("started", self.reserve(copy, slot=started)[1])
        self.pickup.is_cancelled = True
        self.pickup.save()
        self.assertIn("cancelled", self.reserve(copy)[1])

    def test_return_slot_cannot_be_used_for_pickup(self):
        self.assertIn("borrow slot", self.reserve(make_game()[0], slot=self.ret)[1])

    def test_copy_not_double_booked(self):
        copy = make_game()[0]
        self.reserve(copy)
        other = make_user("other")
        self.assertIn("already reserved", self.reserve(copy, user=other)[1])

    def test_graduating_needs_vouch(self):
        profile = services.get_profile(self.user)
        profile.requires_voucher = True
        profile.save()
        copy = make_game()[0]
        self.assertIn("vouch", self.reserve(copy)[1])
        voucher = make_user("voucher")
        member = make_user("mem", is_member=True)
        services.add_vouch(self.user, voucher, member)
        res, err = self.reserve(copy)
        self.assertIsNone(err)
        checkout, errors = services.perform_checkout(res, member)
        self.assertEqual(errors, [])
        self.assertEqual(checkout.borrower, voucher)

    def test_voucher_must_not_be_graduating(self):
        other = make_user("grad")
        profile = services.get_profile(other)
        profile.requires_voucher = True
        profile.save()
        vouch, err = services.add_vouch(self.user, other, None)
        self.assertIsNone(vouch)
        self.assertTrue(err)


class SpecialBorrowerTests(TestCase):
    def setUp(self):
        self.club = make_user("club")
        set_type(self.club, BorrowerProfile.PolicyType.CLUB_SOCIETY)
        self.far = make_slot(BORROW, 4)
        self.near = make_slot(BORROW, 1, start=time(23, 0), end=time(23, 30))
        make_slot(RETURN, 8)
        self.copy = make_game()[0]
        self.details = {
            "premises_ack": True,
            "purpose": "Event",
            "poc_name": "Pat",
            "poc_phone": "123",
            "return_by": services.today_ist() + timedelta(days=7),
        }

    def test_requires_two_days_notice(self):
        _, err = services.create_reservation(self.club, self.copy, self.near, self.details)
        self.assertIn("2 days", err)

    def test_request_is_pending_with_details(self):
        res, err = services.create_reservation(self.club, self.copy, self.far, self.details)
        self.assertIsNone(err)
        self.assertEqual(res.status, Reservation.Status.PENDING)
        self.assertEqual(res.purpose, "Event")

    def test_missing_details_rejected(self):
        self.details["purpose"] = ""
        _, err = services.create_reservation(self.club, self.copy, self.far, self.details)
        self.assertIn("purpose", err)

    def test_ra_needs_rh_floor_and_ta_needs_professor(self):
        ra = make_user("ra")
        set_type(ra, BorrowerProfile.PolicyType.RA)
        _, err = services.create_reservation(ra, self.copy, self.far, self.details)
        self.assertIn("RH", err)
        ta = make_user("ta")
        set_type(ta, BorrowerProfile.PolicyType.TA_TF)
        _, err = services.create_reservation(ta, self.copy, self.far, self.details)
        self.assertIn("Professor", err)
        self.details["authorizing_email"] = "prof@ashoka.edu.in"
        res, err = services.create_reservation(ta, self.copy, self.far, self.details)
        self.assertIsNone(err)

    def test_approval_confirms(self):
        res, _ = services.create_reservation(self.club, self.copy, self.far, self.details)
        member = make_user("mem", is_member=True)
        ok, _ = services.approve_reservation(res, member)
        self.assertTrue(ok)
        res.refresh_from_db()
        self.assertEqual(res.status, Reservation.Status.CONFIRMED)

    def test_return_slot_follows_return_by_date(self):
        make_slot(RETURN, 15)
        self.details["return_by"] = services.today_ist() + timedelta(days=14)
        res, _ = services.create_reservation(self.club, self.copy, self.far, self.details)
        self.assertEqual(res.return_slot.date, services.today_ist() + timedelta(days=15))


class FeeTests(TestCase):
    def setUp(self):
        self.member = make_user("mem", is_member=True)
        self.user = make_user("stu")
        self.returns = [make_slot(RETURN, d) for d in (-21, -14, -7, 7)]

    def checkout(self, due, copy=None):
        copy = copy or make_game(f"G{GameTitle.objects.count()}")[0]
        return Checkout.objects.create(
            borrower=self.user, game_copy=copy, handoff_member=self.member,
            due_date_slot=due, checkout_time=timezone.now() - timedelta(days=30),
        )

    def test_late_fee_is_25_per_missed_slot(self):
        co = self.checkout(self.returns[1])  # due -14: slots -14 and -7 missed
        self.assertEqual(services.calculate_late_fee(co), Decimal("50.00"))

    def test_policy_example_two_games_two_slots(self):
        total = sum(
            services.calculate_late_fee(self.checkout(self.returns[1])) for _ in range(2)
        )
        self.assertEqual(total, Decimal("100.00"))

    def test_late_fee_charge_is_unpaid_and_grows(self):
        co = self.checkout(self.returns[1])
        charge = services.update_late_fee_charge(co)
        self.assertEqual(charge.amount, Decimal("50.00"))
        self.assertEqual(charge.status, Charge.ChargeStatus.UNPAID)
        # Idempotent
        services.update_late_fee_charge(co)
        self.assertEqual(Charge.objects.filter(checkout=co).count(), 1)

    def test_paid_fee_then_more_slots_creates_new_charge(self):
        co = self.checkout(self.returns[1])
        charge = services.update_late_fee_charge(co)
        charge.status = Charge.ChargeStatus.PAID
        charge.save()
        # Another slot ends
        WeeklySlot.objects.filter(pk=self.returns[3].pk).update(
            date=services.today_ist() - timedelta(days=1)
        )
        services.update_late_fee_charge(co)
        amounts = sorted(c.amount for c in Charge.objects.filter(checkout=co))
        self.assertEqual(amounts, [Decimal("25.00"), Decimal("50.00")])

    def test_cancelled_slots_do_not_count(self):
        co = self.checkout(self.returns[1])
        self.returns[2].is_cancelled = True
        self.returns[2].save()
        # Due slot -14 plus the -7 slot is cancelled -> only 1 missed
        self.assertEqual(services.calculate_late_fee(co), Decimal("25.00"))

    def test_returning_during_due_slot_is_not_late(self):
        today_slot = WeeklySlot.objects.create(
            slot_type=RETURN, date=services.today_ist(),
            start_time=time(0, 0), end_time=time(23, 59),
        )
        co = self.checkout(today_slot)
        self.assertEqual(services.calculate_late_fee(co), 0)

    def test_lost_after_three_return_slots(self):
        co = self.checkout(self.returns[0])  # -21; slots -14, -7 after it
        self.assertFalse(services.check_lost_candidate(co))
        WeeklySlot.objects.filter(pk=self.returns[3].pk).update(
            date=services.today_ist() - timedelta(days=1)
        )
        self.assertTrue(services.check_lost_candidate(co))

    def test_finalize_lost_creates_replacement_and_probation(self):
        co = self.checkout(self.returns[0], make_game("Lost", "B", mrp=800)[0])
        ok, _ = services.finalize_lost_game(co, self.member)
        self.assertTrue(ok)
        charge = Charge.objects.get(checkout=co, charge_type=Charge.ChargeType.LOST_REPLACEMENT)
        self.assertEqual(charge.amount, Decimal("800.00"))
        self.assertEqual(charge.status, Charge.ChargeStatus.UNPAID)
        self.assertEqual(co.game_copy.condition, GameCopy.Condition.LOST)
        self.assertIsNotNone(services.get_active_probation(self.user))
        # Cannot finalize twice
        self.assertFalse(services.finalize_lost_game(co, self.member)[0])

    def test_finalize_lost_needs_amount_when_mrp_unset(self):
        co = self.checkout(self.returns[0], make_game("NoMrp", "B", mrp=0)[0])
        self.assertFalse(services.finalize_lost_game(co, self.member)[0])
        self.assertTrue(services.finalize_lost_game(co, self.member, Decimal("500"))[0])

    def test_return_after_lost_waives_replacement(self):
        co = self.checkout(self.returns[0], make_game("Lost", "B", mrp=800)[0])
        services.finalize_lost_game(co, self.member)
        services.perform_return(co, self.member)
        charge = Charge.objects.get(checkout=co, charge_type=Charge.ChargeType.LOST_REPLACEMENT)
        self.assertEqual(charge.status, Charge.ChargeStatus.WAIVED)
        co.game_copy.refresh_from_db()
        self.assertEqual(co.game_copy.condition, GameCopy.Condition.GOOD)
        self.assertTrue(co.game_copy.is_available)

    def test_resolve_lost_paid_closes_checkout(self):
        co = self.checkout(self.returns[0], make_game("Lost", "B", mrp=800)[0])
        services.finalize_lost_game(co, self.member)
        ok, _ = services.resolve_lost(co, self.member, "paid")
        self.assertTrue(ok)
        co.refresh_from_db()
        self.assertIsNotNone(co.return_time)


class InfractionLadderTests(TestCase):
    def setUp(self):
        self.member = make_user("mem", is_member=True)

    def damage(self, user, name):
        copy = make_game(name)[0]
        co = Checkout.objects.create(borrower=user, game_copy=copy, handoff_member=self.member)
        return services.log_damage_incident(co, self.member, "torn box", Decimal("100"))

    def test_probation_then_ban_then_probation_then_blacklist(self):
        user = make_user("stu")
        _, _, msg = self.damage(user, "A")
        self.assertIn("probation", msg)
        self.assertIsNotNone(services.get_active_probation(user))
        self.assertIsNone(services.get_blocking_restriction(user))

        _, _, msg = self.damage(user, "B")
        self.assertIn("banned", msg)
        self.assertIsNotNone(services.get_blocking_restriction(user))

        # Ban served; probation again.
        Restriction.objects.filter(user=user).update(
            start_date=services.today_ist() - timedelta(days=500),
            end_date=services.today_ist() - timedelta(days=200),
        )
        _, _, msg = self.damage(user, "C")
        self.assertIn("probation", msg)
        _, _, msg = self.damage(user, "D")
        self.assertIn("blacklisted", msg)
        self.assertIsNotNone(services.get_blocking_restriction(user))

    def test_one_checkout_escalates_once(self):
        user = make_user("stu")
        copy = make_game("A")[0]
        co = Checkout.objects.create(borrower=user, game_copy=copy, handoff_member=self.member)
        services.log_damage_incident(co, self.member, "first")
        services.log_damage_incident(co, self.member, "second")
        self.assertEqual(Restriction.objects.filter(user=user).count(), 1)

    def test_club_second_incident_bans_then_probation(self):
        club = make_user("club")
        set_type(club, BorrowerProfile.PolicyType.CLUB_SOCIETY)
        self.damage(club, "A")
        self.assertIsNotNone(services.get_active_probation(club))
        self.damage(club, "B")
        self.assertIsNotNone(services.get_blocking_restriction(club))
        later = Restriction.objects.filter(
            user=club, restriction_type=Restriction.RestrictionType.PROBATION
        ).order_by("-start_date").first()
        self.assertGreater(later.start_date, services.today_ist())

    def test_damage_fee_warnings(self):
        user = make_user("stu")
        copy = make_game("A", "B", mrp=500)[0]
        co = Checkout.objects.create(borrower=user, game_copy=copy, handoff_member=self.member)
        _, warnings, _ = services.log_damage_incident(
            co, self.member, "gone", Decimal("20"), unplayable=True
        )
        self.assertTrue(any("minimum" in w for w in warnings))
        self.assertTrue(any("unplayable" in w for w in warnings))
        copy.refresh_from_db()
        self.assertEqual(copy.condition, GameCopy.Condition.DAMAGED)
        self.assertFalse(copy.is_available)

    def test_fee_guide(self):
        low, high = services.suggested_fee_range("essential", Decimal("1000"))
        self.assertEqual((low, high), (Decimal("500.00"), Decimal("750.00")))
        self.assertEqual(
            services.suggested_fee_range("critical", Decimal("1000")),
            (Decimal("1000.00"), Decimal("1000.00")),
        )


class DeskFlowTests(TestCase):
    def setUp(self):
        self.member = make_user("mem", is_member=True)
        self.user = make_user("stu")
        self.pickup = make_slot(BORROW, 1)
        self.ret = make_slot(RETURN, 6)
        self.copy = make_game("G", "C")[0]

    def checked_out(self):
        res, err = services.create_reservation(
            self.user, self.copy, self.pickup, {"premises_ack": True}
        )
        self.assertIsNone(err)
        checkout, errors = services.perform_checkout(res, self.member)
        self.assertEqual(errors, [])
        return checkout

    def test_checkout_collects_deposit_and_sets_due_slot(self):
        co = self.checked_out()
        self.assertEqual(co.due_date_slot, self.ret)
        deposit = Charge.objects.get(checkout=co, charge_type=Charge.ChargeType.DEPOSIT)
        self.assertEqual(deposit.amount, Decimal("150.00"))
        self.assertFalse(self.copy.__class__.objects.get(pk=self.copy.pk).is_available)

    def test_checkout_blocked_by_dues_unless_overridden(self):
        res, _ = services.create_reservation(
            self.user, self.copy, self.pickup, {"premises_ack": True}
        )
        Charge.objects.create(
            user=self.user, charge_type=Charge.ChargeType.FINE, amount=100,
            status=Charge.ChargeStatus.UNPAID,
        )
        co, errors = services.perform_checkout(res, self.member)
        self.assertIsNone(co)
        self.assertTrue(errors)
        co, errors = services.perform_checkout(res, self.member, override_reason="cleared by HOD")
        self.assertIsNotNone(co)

    def test_return_sets_refund_due_in_a_week(self):
        co = self.checked_out()
        services.perform_return(co, self.member)
        co.refresh_from_db()
        self.assertEqual(co.refund_due_by, services.today_ist() + timedelta(days=7))
        deposit = Charge.objects.get(checkout=co, charge_type=Charge.ChargeType.DEPOSIT)
        self.assertEqual(deposit.status, Charge.ChargeStatus.COLLECTED)
        services.refund_deposit(co, self.member)
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Charge.ChargeStatus.REFUNDED)

    def test_return_with_refund_flag(self):
        co = self.checked_out()
        services.perform_return(co, self.member, refund=True)
        deposit = Charge.objects.get(checkout=co, charge_type=Charge.ChargeType.DEPOSIT)
        self.assertEqual(deposit.status, Charge.ChargeStatus.REFUNDED)

    def test_ra_checkout_has_no_deposit(self):
        ra = make_user("ra")
        set_type(ra, BorrowerProfile.PolicyType.RA)
        far = make_slot(BORROW, 4)
        res, err = services.create_reservation(ra, self.copy, far, {
            "premises_ack": True, "purpose": "floor event", "poc_name": "R",
            "poc_phone": "1", "rh_floor": "RH1 F2",
            "return_by": services.today_ist() + timedelta(days=9),
        })
        self.assertIsNone(err)
        services.approve_reservation(res, self.member)
        res.refresh_from_db()
        co, errors = services.perform_checkout(res, self.member)
        self.assertEqual(errors, [])
        self.assertFalse(Charge.objects.filter(checkout=co).exists())

    def test_walk_in_requires_borrow_slot_today_and_no_reservation(self):
        errors = services.walk_in_blockers(self.user, self.copy)
        self.assertTrue(any("no borrow slot today" in e for e in errors))

        today_slot = WeeklySlot.objects.create(
            slot_type=BORROW, date=services.today_ist(),
            start_time=time(0, 0), end_time=time(23, 59),
        )
        self.assertEqual(services.walk_in_blockers(self.user, self.copy), [])

        other = make_user("other")
        Reservation.objects.create(
            user=other, game_copy=self.copy, pickup_slot=today_slot,
            status=Reservation.Status.CONFIRMED,
        )
        errors = services.walk_in_blockers(self.user, self.copy)
        self.assertTrue(any("reserved for today" in e for e in errors))

    def test_walk_in_checkout_due_at_next_return_slot(self):
        WeeklySlot.objects.create(
            slot_type=BORROW, date=services.today_ist(),
            start_time=time(0, 0), end_time=time(23, 59),
        )
        co, errors = services.perform_walk_in_checkout(self.user, self.copy, self.member)
        self.assertEqual(errors, [])
        self.assertEqual(co.due_date_slot, self.ret)

    def test_report_existing_damage_window(self):
        co = self.checked_out()
        ok, _ = services.report_existing_damage(co, self.user, "scratched board")
        self.assertTrue(ok)
        self.assertTrue(Incident.objects.filter(
            checkout=co, incident_type=Incident.IncidentType.PRE_EXISTING_DAMAGE
        ).exists())
        # No charge or probation results.
        self.assertIsNone(services.get_active_probation(self.user))
        co.checkout_time = timezone.now() - timedelta(hours=4)
        co.save()
        ok, err = services.report_existing_damage(co, self.user, "too late")
        self.assertFalse(ok)
        self.assertIn("3 hour", err)

    def test_cancel_slot_cancels_reservations(self):
        res, _ = services.create_reservation(
            self.user, self.copy, self.pickup, {"premises_ack": True}
        )
        n = services.cancel_slot(self.pickup, self.member)
        res.refresh_from_db()
        self.assertEqual(n, 1)
        self.assertEqual(res.status, Reservation.Status.CANCELLED)

    def test_cancelled_return_slot_moves_reservations(self):
        later = make_slot(RETURN, 13)
        res, _ = services.create_reservation(
            self.user, self.copy, self.pickup, {"premises_ack": True}
        )
        services.cancel_slot(self.ret, self.member)
        res.refresh_from_db()
        self.assertEqual(res.return_slot, later)

    def test_due_slot_resolved_when_return_slot_added_later(self):
        WeeklySlot.objects.filter(pk=self.ret.pk).delete()
        res, _ = services.create_reservation(
            self.user, self.copy, self.pickup, {"premises_ack": True}
        )
        self.assertIsNone(res.return_slot)
        later = make_slot(RETURN, 6)
        co, errors = services.perform_checkout(res, self.member)
        self.assertEqual(co.due_date_slot, later)


class ViewTests(TestCase):
    def setUp(self):
        # Django 5.1's test client cannot copy template contexts on Python 3.14.
        patcher = mock.patch("django.test.client.store_rendered_templates")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.member = make_user("mem", is_member=True)
        self.user = make_user("stu")
        self.client.force_login(self.member)

    def test_non_member_cannot_manage_slots(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("core:slot_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("core:slot_create")).status_code, 403)

    def test_member_creates_slot_with_repeat(self):
        day = services.today_ist() + timedelta(days=3)
        resp = self.client.post(reverse("core:slot_create"), {
            "slot_type": "borrow", "date": day.isoformat(),
            "start_time": "20:30", "end_time": "21:30", "notes": "", "repeat_weeks": 2,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(WeeklySlot.objects.filter(slot_type=BORROW).count(), 3)
        self.assertEqual(
            WeeklySlot.objects.get(date=day).created_by, self.member
        )

    def test_slot_end_must_follow_start(self):
        day = services.today_ist() + timedelta(days=3)
        resp = self.client.post(reverse("core:slot_create"), {
            "slot_type": "return", "date": day.isoformat(),
            "start_time": "21:30", "end_time": "20:30",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(WeeklySlot.objects.count(), 0)

    def test_member_edits_date_and_time(self):
        slot = make_slot(BORROW, 3)
        new_day = services.today_ist() + timedelta(days=5)
        resp = self.client.post(reverse("core:slot_edit", args=[slot.pk]), {
            "slot_type": "borrow", "date": new_day.isoformat(),
            "start_time": "19:00", "end_time": "20:00", "notes": "moved",
        })
        self.assertEqual(resp.status_code, 302)
        slot.refresh_from_db()
        self.assertEqual((slot.date, slot.start_time), (new_day, time(19, 0)))

    def test_delete_blocked_when_in_use(self):
        slot = make_slot(BORROW, 3)
        copy = make_game()[0]
        Reservation.objects.create(
            user=self.user, game_copy=copy, pickup_slot=slot,
            status=Reservation.Status.CONFIRMED,
        )
        self.client.post(reverse("core:delete_slot", args=[slot.pk]))
        self.assertTrue(WeeklySlot.objects.filter(pk=slot.pk).exists())
        free = make_slot(RETURN, 9)
        self.client.post(reverse("core:delete_slot", args=[free.pk]))
        self.assertFalse(WeeklySlot.objects.filter(pk=free.pk).exists())

    def test_pages_render(self):
        make_slot(BORROW, 2)
        make_slot(RETURN, 8)
        copy = make_game()[0]
        title_pk = copy.game_title.pk
        urls = [
            reverse("core:home"), reverse("core:policy"), reverse("core:all_games"),
            reverse("core:available_games"), reverse("core:game_detail", args=[title_pk]),
            reverse("core:my_reservations"), reverse("core:my_checkouts"),
            reverse("core:member_desk"), reverse("core:slot_list"),
            reverse("core:slot_create"), reverse("core:desk_walk_in"),
            reverse("core:admin_logs"), reverse("core:desk_borrower"),
            reverse("core:desk_borrower") + f"?email={self.user.email}",
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_desk_page_with_activity_renders(self):
        pickup = make_slot(BORROW, 1)
        make_slot(RETURN, -3)
        copy = make_game("G", "B", mrp=900)[0]
        res, _ = services.create_reservation(self.user, copy, pickup, {"premises_ack": True})
        co, _ = services.perform_checkout(res, self.member)
        co.due_date_slot = WeeklySlot.objects.filter(slot_type=RETURN).first()
        co.save()
        services.finalize_lost_game(co, self.member)
        self.assertEqual(self.client.get(reverse("core:member_desk")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("core:log_incident", args=[co.pk])).status_code, 200
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("core:my_checkouts")).status_code, 200)

    def test_reserve_view_end_to_end(self):
        self.client.force_login(self.user)
        slot = make_slot(BORROW, 2)
        make_slot(RETURN, 8)
        copy = make_game()[0]
        resp = self.client.post(
            reverse("core:create_reservation", args=[copy.pk, slot.pk]),
            {"premises_ack": "on"},
        )
        self.assertRedirects(resp, reverse("core:my_reservations"))
        self.assertEqual(Reservation.objects.filter(user=self.user).count(), 1)

    def test_reserve_view_without_ack_fails(self):
        self.client.force_login(self.user)
        slot = make_slot(BORROW, 2)
        copy = make_game()[0]
        self.client.post(reverse("core:create_reservation", args=[copy.pk, slot.pk]), {})
        self.assertEqual(Reservation.objects.count(), 0)

    def test_report_damage_view(self):
        copy = make_game()[0]
        co = Checkout.objects.create(borrower=self.user, game_copy=copy, handoff_member=self.member)
        self.client.force_login(self.user)
        self.client.post(reverse("core:report_damage", args=[co.pk]), {"description": "torn"})
        self.assertEqual(co.incidents.count(), 1)

    def test_borrower_page_actions(self):
        url = reverse("core:desk_borrower")
        self.client.post(url, {
            "email": self.user.email, "action": "flags",
            "policy_type": "individual", "tdi_exempt": "on",
        })
        self.assertTrue(services.get_profile(self.user).tdi_exempt)

        self.client.post(url, {
            "email": self.user.email, "action": "add_restriction",
            "restriction_type": "temp_ban", "reason": "x",
        })
        # members may not apply bans
        self.assertFalse(Restriction.objects.filter(user=self.user).exists())
        self.client.post(url, {
            "email": self.user.email, "action": "add_restriction",
            "restriction_type": "probation", "reason": "x",
        })
        self.assertTrue(services.get_active_probation(self.user))

        self.client.post(url, {"email": self.user.email, "action": "cadi", "reason": "no reply"})
        self.assertTrue(Incident.objects.filter(
            user=self.user, incident_type=Incident.IncidentType.CADI_ESCALATION
        ).exists())
