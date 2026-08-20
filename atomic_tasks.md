# ESHTO Games Library — Atomic Tasks

> Derived from [PLAN.md](file:///Users/arvindsrinivass/Desktop/EGL-FINAL/PLAN.md).
> Each task is a single, testable unit of work. Dependencies reference other task IDs.

---

## Phase 0 — Project Bootstrap

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **P0-1** | Create Django project skeleton (`eshto/`) with `manage.py`, settings module, and `wsgi.py`. | `python manage.py check` passes. | — |
| **P0-2** | Add PostgreSQL database configuration in `settings.py` (use `dj-database-url` or env vars). | `python manage.py migrate` connects to Postgres. | P0-1 |
| **P0-3** | Install and configure Tailwind CSS (e.g. `django-tailwind` or standalone CLI). | A test page renders with a Tailwind utility class. | P0-1 |
| **P0-4** | Install and configure HTMX (add JS to base template, set `django-htmx` middleware). | An `hx-get` request returns a partial and swaps into the page. | P0-1 |
| **P0-5** | Create base HTML template with shared layout, nav bar placeholder, and Tailwind/HTMX includes. | All pages extend `base.html` and render correctly. | P0-3, P0-4 |
| **P0-6** | Set up Docker Compose file with `web`, `db` (Postgres), and optional `tailwind` services. | `docker compose up` starts the app and connects to Postgres. | P0-2 |

---

## Phase 1 — Data Models

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **M1-1** | Create custom `User` model with fields: email, name, phone, `is_member`, `is_senior_admin`. | Migration runs; user can be created via shell. | P0-2 |
| **M1-2** | Create `BorrowerProfile` model with `policy_type` choices (`individual`, `club_society`, `ra`, `eeshto_member`) linked one-to-one to `User`. | Profile auto-created on first login (signal or middleware). | M1-1 |
| **M1-3** | Create `GameTitle` model with fields: name, description, image, player count, category, replacement cost. | Admin can CRUD a game title. | P0-2 |
| **M1-4** | Create `GameCopy` model with FK to `GameTitle`, fields: copy label/barcode, condition, `is_available` flag. | Multiple copies can exist per title. | M1-3 |
| **M1-5** | Create `WeeklySlot` model with date, start/end time, `is_cancelled` flag, and `created_by` FK. | Slots can be generated and queried. | P0-2 |
| **M1-6** | Create `Reservation` model with FKs to `User`, `GameCopy`, pickup `WeeklySlot`, return `WeeklySlot`, status choices (`confirmed`, `cancelled`, `no_show`, `completed`), and timestamps. | Reservation can be created in the shell. | M1-1, M1-4, M1-5 |
| **M1-7** | Create `Checkout` model with FK to `Reservation`, handoff member FK, pickup location text, condition notes, deposit status, actual checkout/return timestamps. | Checkout record can be created against a reservation. | M1-6 |
| **M1-8** | Create `Charge` model with FK to `User`, type choices (`deposit`, `late_fee`, `damage_fee`, `lost_replacement`), amount, status choices (`collected`, `refunded`, `paid`, `unpaid`, `waived`), linked `Checkout`/`Incident`. | Charges can be created and status-updated. | M1-7 |
| **M1-9** | Create `Incident` model with FK to `Checkout` or `GameCopy`, type choices (`damage`, `missing_components`, `lost_game`, `no_show`, `override`), description, created-by FK, and timestamp. | Incident record can be logged. | M1-7 |
| **M1-10** | Create `Restriction` model with FK to `User`, type (`temp_ban`, `blacklist`), reason, start/end dates, created-by FK. | Restriction can block a user. | M1-1 |

---

## Phase 2 — Auth & Roles

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **A2-1** | Install `django-allauth` and configure Google OAuth provider with client ID/secret from env vars. | Google login button redirects to Google and returns. | P0-1 |
| **A2-2** | Add post-login signal/adapter that rejects users whose email does not end with `@ashoka.edu.in` or whose `email_verified` is false. | Non-Ashoka email sees an error page and is logged out. | A2-1, M1-1 |
| **A2-3** | Auto-create `BorrowerProfile` on first successful login; detect RA/club emails from preloaded allowlists and set `policy_type` accordingly. | RA email → `ra`; club email → `club_society`; others → `individual`. | A2-2, M1-2 |
| **A2-4** | Build login page with Google sign-in button styled with Tailwind. | Page renders; clicking the button starts OAuth flow. | A2-1, P0-5 |
| **A2-5** | Implement role-based access middleware/decorators: `@borrower_required`, `@member_required`, `@senior_admin_required`. | Unauthenticated/unauthorized user is redirected or gets 403. | M1-1 |
| **A2-6** | Build "Manage Roles" page (senior_admin only) to grant/revoke `is_member` and `is_senior_admin` flags on other users. | Senior admin can promote a user to member; non-admins cannot access the page. | A2-5 |

---

## Phase 3 — Borrower Portal

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **B3-1** | Build "All Games" page listing every `GameTitle` with image, name, player count, and category. | Page renders the full catalog. | M1-3, P0-5 |
| **B3-2** | Build "Available Games" page showing only titles that have at least one available copy for the upcoming slot. | Only available games are shown. | M1-4, M1-5 |
| **B3-3** | Build "Game Detail" page showing title info, list of copies with availability status, and a "Reserve" button per available copy. | Detail page displays correct availability. | M1-3, M1-4 |
| **B3-4** | Build "My Reservations" page listing the logged-in user's active and past reservations with status, pickup/return slots, and game info. | Page shows correct data; HTMX cancellation works. | M1-6, P0-4 |
| **B3-5** | Build "My Checkouts & Dues" page showing active checkouts, due dates, deposit status, late fees, and any charges. | All financial info and due dates are accurate. | M1-7, M1-8 |
| **B3-6** | Display restriction status banner on every borrower page when the user has an active `Restriction`. | Banned user sees a prominent warning. | M1-10 |
| **B3-7** | Show fixed pickup/return location text ("Student Commons, 2nd floor of the mess, near the couches between the meeting rooms") on every reservation and checkout screen. | Location text visible on relevant pages. | B3-4, B3-5 |

---

## Phase 4 — Reservation & Checkout Logic (Service Layer)

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **R4-1** | Implement booking window check: reservations can only be created between Monday 08:00 IST and the target pickup slot start time. | Attempts outside the window raise a validation error. | M1-5, M1-6 |
| **R4-2** | Implement active-limit validation: normal borrowers and eeshto members ≤ 2 active reserved+borrowed games. | Third reservation is rejected. | M1-6, M1-2 |
| **R4-3** | Implement RA limit override: RA borrowers ≤ 4 active games, zero deposit. | RA can reserve up to 4; deposit charge is skipped. | R4-2 |
| **R4-4** | Implement club/society deposit escalation: if the club has prior damage/loss incidents, set deposit = MRP/replacement cost. | Club with incidents sees higher deposit. | R4-2, M1-9 |
| **R4-5** | Implement club 2-incident ban: after 2 incidents, auto-create a 2-semester `Restriction`. | Third incident triggers restriction. | R4-4, M1-10 |
| **R4-6** | Implement default return slot calculation: return slot = next `WeeklySlot` after pickup. | Field auto-populates on reservation create. | M1-5, M1-6 |
| **R4-7** | Implement two-slot extension: when a borrower books both the Tuesday and Friday slots in the same week, return slot = same weekday next week. | Extended due date is computed correctly. | R4-6 |
| **R4-8** | Implement first-come-first-served copy locking: prevent double-booking the same `GameCopy` for the same pickup slot (DB-level unique constraint or `select_for_update`). | Concurrent reservation attempts do not double-book. | M1-6 |
| **R4-9** | Implement cancellation logic: allowed up to 1 hour before pickup slot start; rejected otherwise. | Cancel within cutoff succeeds; cancel after cutoff fails. | M1-6 |
| **R4-10** | Implement restriction check: users/clubs with active `Restriction` are blocked from creating a reservation. | Restricted user sees an error when trying to reserve. | M1-10, M1-6 |
| **R4-11** | Wire reservation create view (HTMX): borrower clicks "Reserve" → service validates → reservation created or error shown. | End-to-end reservation flow works via HTMX partial swap. | R4-1 through R4-10, B3-3 |
| **R4-12** | Wire cancellation view (HTMX): borrower clicks "Cancel" on My Reservations → cancellation processed. | Reservation status updates in-page. | R4-9, B3-4 |

---

## Phase 5 — Member Console / Desk UI

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **D5-1** | Build member desk layout: list of today's slot reservations with borrower name, game, copy label, and action buttons. | Members see all upcoming pickups for the current slot. | M1-6, A2-5 |
| **D5-2** | Implement "Mark Deposit Collected" action (HTMX): updates `Charge` status to `collected` for the reservation's deposit. | Deposit status flips and UI reflects it. | M1-8, D5-1 |
| **D5-3** | Implement "Check Out" action (HTMX): creates `Checkout` record with handoff member, condition notes, and actual checkout time. | Checkout record is created; copy marked as unavailable. | M1-7, D5-2 |
| **D5-4** | Implement "Check In / Return" action (HTMX): records return time, member marks condition, and copy becomes available again. | Copy is released back to available pool. | D5-3 |
| **D5-5** | Implement "Mark Deposit Refunded" action on return: updates deposit `Charge` status to `refunded`. | Deposit charge shows as refunded after return. | D5-4, M1-8 |
| **D5-6** | Implement "Mark No-Show" action (HTMX): sets reservation status to `no_show`, creates a `no_show` incident, and releases the copy. | Copy becomes available; incident is logged. | M1-6, M1-9, D5-1 |
| **D5-7** | Implement "Walk-in Checkout" flow: after no-show or unreserved copy, member searches for a borrower and creates an ad-hoc checkout. | Walk-in checkout is created with full audit fields. | D5-6, D5-3 |
| **D5-8** | Implement "Member Override" form: member can override deposit, fees, deadlines, availability, and borrower limits; **reason field is mandatory**. | Override is applied and reason is saved to `Incident` with type `override`. | D5-1, M1-9 |

---

## Phase 6 — Fees, Incidents & Charges

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **F6-1** | Implement late fee calculation: `INR 50 × number_of_slots_missed` since due date. | Correct fee is computed for any overdue checkout. | M1-5, M1-8 |
| **F6-2** | Implement periodic/on-access late fee auto-generation: create or update a `late_fee` `Charge` each time a checkout is viewed or a cron runs. | Fee amount stays in sync with missed slots. | F6-1 |
| **F6-3** | Implement 30-day lost-game candidate flag: if a checkout is overdue by ≥ 30 days past due date, mark it as `lost_candidate`. | Flag appears on the desk UI for old overdues. | M1-7 |
| **F6-4** | Implement "Finalize Lost Game" action (member/admin): creates `lost_replacement` charge = replacement cost + accumulated late fees; creates `lost_game` incident. | Charge and incident created; copy marked as lost. | F6-3, M1-8, M1-9 |
| **F6-5** | Implement "Log Damage / Missing Components" action: member creates `Incident` and optional `damage_fee` `Charge`; warn if charge > replacement cost. | Incident saved; warning displayed when amount is high. | M1-9, M1-8 |
| **F6-6** | Implement charge status management UI (HTMX): member can update any `Charge` status (`collected`, `refunded`, `paid`, `unpaid`, `waived`). | Status updates reflect immediately. | M1-8, D5-1 |
| **F6-7** | Implement audit trail: every policy-relevant action (checkout, return, fee, override, restriction) logs actor, timestamp, and details to an `AuditLog` model or Django's built-in logging. | All critical actions are traceable. | All Phase 5 & 6 tasks |

---

## Phase 7 — Slot Management

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **S7-1** | Build management command `generate_slots` that creates recurring Tuesday/Friday `18:30–19:30 Asia/Kolkata` `WeeklySlot` records for a given date range. | Slots for the next semester are generated. | M1-5 |
| **S7-2** | Build member UI to cancel/edit a specific slot (e.g., holiday exception). | Cancelled slot hides from borrower-facing views but stays in DB. | S7-1, A2-5 |
| **S7-3** | Implement booking window logic tied to slot schedule: window opens Monday 08:00 IST for the week's Tuesday and Friday slots. | Window opens and closes at the correct times. | S7-1, R4-1 |

---

## Phase 8 — Admin & Data Import

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **I8-1** | Register `GameTitle`, `GameCopy`, `User`, `BorrowerProfile`, `Charge`, `Incident`, `Restriction` in Django admin with search/filter. | Admin can manage all models via `/admin/`. | All Phase 1 models |
| **I8-2** | Build CSV import management command for initial game catalog (title, copy count, replacement cost, category). | Running the command populates `GameTitle` and `GameCopy` from a CSV. | M1-3, M1-4 |
| **I8-3** | Build CSV import for RA and club/society email allowlists used by `A2-3`. | Allowlist tables are populated; login correctly detects RA/club emails. | A2-3 |

---

## Phase 9 — Testing

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **T9-1** | Write tests for auth: Ashoka email login succeeds; non-Ashoka rejected; `email_verified=false` rejected. | All auth tests pass. | A2-1, A2-2 |
| **T9-2** | Write tests for reservation rules: booking window, active limits, cancellation cutoff, double-booking prevention. | All reservation tests pass. | R4-1 through R4-10 |
| **T9-3** | Write tests for special borrower rules: RA 4-game limit + zero deposit; club escalated deposit; club 2-incident ban; restriction blocking. | All borrower-rule tests pass. | R4-3, R4-4, R4-5, R4-10 |
| **T9-4** | Write tests for desk operations: deposit collected/refunded, checkout, return, no-show → walk-in, override requires reason. | All desk tests pass. | Phase 5 |
| **T9-5** | Write tests for fees & incidents: late fee slot math, damage charge warning, 30-day lost flow, audit trail entries. | All fee/incident tests pass. | Phase 6 |
| **T9-6** | Write tests for slot generation and booking window timing. | Slot and window tests pass. | Phase 7 |

---

## Phase 10 — Deployment

| ID | Task | Done when… | Depends on |
|----|------|-----------|------------|
| **X10-1** | Write production `Dockerfile` (multi-stage: build Tailwind → collect static → run Gunicorn). | `docker build` succeeds and image runs. | P0-6 |
| **X10-2** | Write production `docker-compose.prod.yml` with `web`, `db`, and `nginx` (or Caddy) services. | `docker compose -f docker-compose.prod.yml up` serves the app over HTTP. | X10-1 |
| **X10-3** | Configure TLS via Let's Encrypt (Certbot sidecar or Caddy auto-TLS). | App is accessible over HTTPS with a valid cert. | X10-2 |
| **X10-4** | Set up DuckDNS (or club domain) DNS pointing to the OCI VM's public IP. | Domain resolves to the VM. | X10-3 |
| **X10-5** | Add nightly DB backup cron (pg_dump to object storage or local rotation). | Backups are created and verified restorable. | X10-2 |
| **X10-6** | Write deployment runbook documenting SSH access, env vars, secrets rotation, and rollback steps. | Runbook reviewed and tested by at least one team member. | X10-5 |

---

> **Total: 60 tasks across 11 phases.**
> Recommended execution order: P0 → M1 → A2 → S7 → R4 → B3 → D5 → F6 → I8 → T9 → X10.
