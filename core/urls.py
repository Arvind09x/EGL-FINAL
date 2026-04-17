"""Core app URL configuration."""

from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    # ── Public / Borrower pages ─────────────────────────────
    path("", views.home, name="home"),
    path("policy/", views.policy, name="policy"),
    path("games/", views.all_games, name="all_games"),
    path("games/available/", views.available_games, name="available_games"),
    path("games/<int:pk>/", views.game_detail, name="game_detail"),

    # ── Borrower authenticated pages ────────────────────────
    path("my/reservations/", views.my_reservations, name="my_reservations"),
    path("my/checkouts/", views.my_checkouts, name="my_checkouts"),

    # ── Reservation actions (HTMX) ──────────────────────────
    path(
        "reserve/<int:copy_id>/<int:slot_id>/",
        views.create_reservation,
        name="create_reservation",
    ),
    path(
        "reservation/<int:pk>/cancel/",
        views.cancel_reservation,
        name="cancel_reservation",
    ),

    # ── Member desk ─────────────────────────────────────────
    path("desk/", views.member_desk, name="member_desk"),
    path("desk/checkout/<int:reservation_id>/", views.desk_checkout, name="desk_checkout"),
    path("desk/return/<int:checkout_id>/", views.desk_return, name="desk_return"),
    path("desk/no-show/<int:reservation_id>/", views.desk_no_show, name="desk_no_show"),
    path("desk/walk-in/", views.desk_walk_in, name="desk_walk_in"),
    path("desk/deposit/<int:charge_id>/<str:action>/", views.desk_deposit, name="desk_deposit"),
    path("desk/override/", views.desk_override, name="desk_override"),
    path("desk/logs/", views.admin_logs, name="admin_logs"),

    # ── Fees & incidents ────────────────────────────────────
    path("desk/incident/<int:checkout_id>/", views.log_incident, name="log_incident"),
    path("desk/lost/<int:checkout_id>/", views.finalize_lost, name="finalize_lost"),
    path("desk/charge/<int:charge_id>/status/", views.update_charge_status, name="update_charge_status"),

    # ── Slot management (members) ───────────────────────────
    path("slots/", views.slot_list, name="slot_list"),
    path("slots/<int:pk>/cancel/", views.cancel_slot, name="cancel_slot"),

    # ── Admin: role management (senior_admin only) ──────────
    path("manage/roles/", views.manage_roles, name="manage_roles"),
]
