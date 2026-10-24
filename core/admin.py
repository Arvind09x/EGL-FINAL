"""
Django admin configuration for the ESHTO Games Library.
Covers task I8-1: register all models with search/filter.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    AuditLog,
    BorrowerProfile,
    Charge,
    Checkout,
    ClubSocietyEmailAllowlist,
    GameCopy,
    GameTitle,
    Incident,
    RAEmailAllowlist,
    Reservation,
    Restriction,
    TAEmailAllowlist,
    User,
    Vouch,
    WeeklySlot,
)


# ── Inlines ──────────────────────────────────────────────────────────


class BorrowerProfileInline(admin.StackedInline):
    model = BorrowerProfile
    can_delete = False
    verbose_name_plural = "Borrower Profile"


class GameCopyInline(admin.TabularInline):
    model = GameCopy
    extra = 1


# ── User ─────────────────────────────────────────────────────────────


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [BorrowerProfileInline]
    list_display = [
        "email",
        "first_name",
        "last_name",
        "is_member",
        "is_senior_admin",
        "is_active",
    ]
    list_filter = ["is_member", "is_senior_admin", "is_active", "is_staff"]
    search_fields = ["email", "first_name", "last_name", "username"]

    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "ESHTO Roles",
            {"fields": ("phone", "is_member", "is_senior_admin")},
        ),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        (
            "ESHTO Roles",
            {"fields": ("phone", "is_member", "is_senior_admin")},
        ),
    )


# ── Game ─────────────────────────────────────────────────────────────


@admin.register(GameTitle)
class GameTitleAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "deposit_category", "security_deposit", "mrp", "min_players", "max_players"]
    list_filter = ["category", "deposit_category"]
    search_fields = ["name", "category"]
    inlines = [GameCopyInline]


@admin.register(GameCopy)
class GameCopyAdmin(admin.ModelAdmin):
    list_display = ["game_title", "copy_label", "condition", "is_available"]
    list_filter = ["condition", "is_available"]
    search_fields = ["copy_label", "game_title__name"]


# ── Slots ────────────────────────────────────────────────────────────


@admin.register(WeeklySlot)
class WeeklySlotAdmin(admin.ModelAdmin):
    list_display = ["date", "slot_type", "start_time", "end_time", "is_cancelled"]
    list_filter = ["slot_type", "is_cancelled", "date"]
    date_hierarchy = "date"


# ── Reservations & Checkouts ────────────────────────────────────────


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "game_copy", "pickup_slot", "return_slot", "status", "purpose"]
    list_filter = ["status"]
    search_fields = ["user__email", "game_copy__game_title__name"]
    raw_id_fields = ["user", "game_copy", "pickup_slot", "return_slot"]


@admin.register(Checkout)
class CheckoutAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "borrower",
        "game_copy",
        "checkout_time",
        "return_time",
        "deposit_status",
    ]
    list_filter = ["deposit_status", "is_lost_candidate"]
    search_fields = ["borrower__email", "game_copy__game_title__name"]
    raw_id_fields = ["reservation", "borrower", "game_copy", "handoff_member", "return_member", "refund_member", "due_date_slot"]


# ── Financial ────────────────────────────────────────────────────────


@admin.register(Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "charge_type", "amount", "status", "created_at"]
    list_filter = ["charge_type", "status"]
    search_fields = ["user__email"]
    raw_id_fields = ["user", "checkout", "incident"]


# ── Incidents & Restrictions ────────────────────────────────────────


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ["id", "incident_type", "user", "game_copy", "created_by", "created_at"]
    list_filter = ["incident_type"]
    search_fields = ["user__email", "description"]
    raw_id_fields = ["checkout", "game_copy", "user", "created_by"]


@admin.register(Restriction)
class RestrictionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "restriction_type",
        "start_date",
        "end_date",
        "is_active",
    ]
    list_filter = ["restriction_type"]
    search_fields = ["user__email", "reason"]
    raw_id_fields = ["user", "created_by"]


# ── Audit ────────────────────────────────────────────────────────────


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["timestamp", "action", "actor", "target_user"]
    list_filter = ["action"]
    search_fields = ["actor__email", "target_user__email"]
    readonly_fields = ["action", "actor", "target_user", "details", "timestamp"]
    raw_id_fields = ["actor", "target_user"]


# ── Allowlists ───────────────────────────────────────────────────────


@admin.register(RAEmailAllowlist)
class RAEmailAllowlistAdmin(admin.ModelAdmin):
    list_display = ["email", "name"]
    search_fields = ["email", "name"]


@admin.register(ClubSocietyEmailAllowlist)
class ClubSocietyEmailAllowlistAdmin(admin.ModelAdmin):
    list_display = ["email", "name"]
    search_fields = ["email", "name"]


@admin.register(TAEmailAllowlist)
class TAEmailAllowlistAdmin(admin.ModelAdmin):
    list_display = ["email", "name"]
    search_fields = ["email", "name"]


@admin.register(Vouch)
class VouchAdmin(admin.ModelAdmin):
    list_display = ["borrower", "voucher", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["borrower__email", "voucher__email"]
    raw_id_fields = ["borrower", "voucher", "created_by"]
