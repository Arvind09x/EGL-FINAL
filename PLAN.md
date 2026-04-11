# ESHTO Games Library System Plan

## Summary
- Build a Python-first web app as a Django monolith with server-rendered pages, `HTMX` for interactive member workflows, `Tailwind CSS` for styling, and PostgreSQL for storage.
- Support Google OAuth login only for verified `@ashoka.edu.in` emails. A single account can be both a borrower and an Eeshto member/admin.
- Ship two surfaces in v1: a borrower-facing portal for browsing/reserving/seeing dues, and a member console for slots, handoff, returns, overrides, incidents, and borrower status.
- Default hosting: one Oracle Cloud Always Free VM ([Oracle docs](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)) running Docker Compose, with free TLS from [Let’s Encrypt](https://letsencrypt.org/) and either a club-owned domain or a free subdomain such as [DuckDNS](https://www.duckdns.org/why.jsp).

## Core Product and Interfaces
- `User`: authenticated Ashoka user with verified email, name, phone, and role flags.
- `BorrowerProfile`: policy type for `individual`, `club_society`, `ra`, `eeshto_member`; clubs/socs and RAs are identified from preloaded email tables.
- `GameTitle` and `GameCopy`: model copies separately so each physical box can be reserved, checked out, damaged, or lost independently.
- `WeeklySlot`: recurring Tuesday and Friday `18:30-19:30 Asia/Kolkata`, with reservations opening every Monday `08:00`; members can edit/cancel slots for exceptions.
- `Reservation`: auto-confirmed if rules pass; ties a borrower, a copy, a pickup slot, and either:
  - default return slot = next slot
  - extended return slot = same weekday next week when the user books both weekly slots
- `Checkout`: created by a member at pickup after marking deposit status; stores handoff member, pickup location text, condition notes, and actual checkout time.
- `Charge`: manual-status financial records for deposit, late fee, damage fee, and lost-game replacement charge; system calculates defaults, members mark `collected/refunded/paid/unpaid/waived`.
- `Incident`: damage, missing components, lost-game, no-show, override reason, and audit trail.
- `Restriction`: temporary ban or blacklist on users/clubs; blocks new reservations until lifted.

## Implementation Changes
- Auth and access:
  - Use Google OAuth in Django (`django-allauth` or equivalent).
  - Enforce `email_verified = true` and `email.endswith("@ashoka.edu.in")` server-side after OAuth.
  - Roles: `borrower`, `member`, `senior_admin`; `senior_admin` is the only role that can manage role grants and high-risk overrides.
- Borrower experience:
  - After login, show `All Games` and `Available Games` views, game detail pages, active reservations/checkouts, due dates, deposit due, late fees, and restriction status.
  - Allow reservation only during the open booking window.
  - Allow cancellation until 1 hour before pickup slot start.
  - Show fixed pickup/return location on every reservation and checkout screen: Student Commons, 2nd floor of the mess, near the couches between the meeting rooms.
- Reservation and checkout rules:
  - Normal borrowers and Eeshto members: max 2 active borrowed/reserved games at once.
  - Clubs/societies: same base limits as individuals; if they have prior damage/loss history, require deposit = MRP/replacement cost; after 2 incidents, apply 2-semester ban.
  - RAs: up to 4 games, no deposit, still subject to late/loss/damage tracking.
  - First-come, first-served walk-ins are supported only through the member console after a copy is unreserved or a reservation is marked `no_show`.
  - Members can mark a reservation `no_show` during the slot and immediately convert the copy into a manual checkout for another borrower.
- Returns, fees, and policy enforcement:
  - Default due date is the next slot; if both slots are booked together, due date becomes the same weekday next week.
  - Late fee = `INR 50 x number_of_slots_missed`.
  - If overdue by 30 days past due date, mark as `lost_candidate` and allow member/admin to finalize lost-game charge = replacement cost + accumulated late fees.
  - Damage/missing-piece charges are entered manually, with a warning if the amount exceeds the game’s stored replacement cost.
  - Members can override deposits, fees, deadlines, availability, and borrower limits, but must record a reason.
- Staff tooling:
  - Build a custom member desk UI for fast pickup/return/no-show/manual-walk-in operations.
  - Keep Django admin for lower-frequency data management: game catalog, replacement cost, deposit amount, role grants, email allowlists, borrower restrictions.
  - Import the initial catalog from your current sheet/CSV once, then manage data in-app.

## Test Plan
- Auth:
  - Ashoka Google account can log in.
  - Non-`@ashoka.edu.in` account is rejected even if Google login succeeds.
  - Member can also borrow using the same account.
- Reservations:
  - Booking opens Monday 08:00 IST and blocks before then.
  - User cannot exceed active-limit rules.
  - Next-slot reservation and two-slot extension both compute correct return slot.
  - Cancellation fails inside the 1-hour cutoff.
  - Reserved copy cannot be double-booked.
- Special borrower rules:
  - RA email gets 4-game limit and zero deposit.
  - Club/society email gets club rules, including elevated deposit after prior incidents.
  - Banned or blacklisted borrower cannot create a reservation.
- Desk operations:
  - Member can mark deposit collected/refunded and check copy out/in.
  - Member can mark `no_show` and convert copy to walk-in checkout.
  - Member override always requires a reason.
- Fees and incidents:
  - Late fee increments by missed slot count.
  - Damage charge warns when above replacement cost.
  - Lost-game flow becomes available at 30 overdue days and adds replacement charge plus late fees.
  - Audit trail records who performed every policy-relevant action.

## Assumptions and Defaults
- `@ashoka.edu.in` identities are backed by Google accounts, so Google OAuth is viable.
- v1 is web-only, mobile-responsive, and does not send automated emails or integrate payment rails.
- v1 does not include a public JSON API; keep logic inside a Django service layer so an API can be added later if needed.
- Slot generation default: recurring Tuesday/Friday schedule with member-editable exceptions, since policy says timing may vary.
- Hosting default is zero-cost but ops-managed: OCI Always Free VM, single app node, single PostgreSQL instance, nightly DB backups, and HTTPS via Let’s Encrypt. If ESHTO later gets a proper domain, swap it in without changing the app design.
