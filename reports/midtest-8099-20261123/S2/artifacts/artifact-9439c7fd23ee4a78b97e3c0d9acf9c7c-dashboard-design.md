# Manager Dashboard — Requirements & Design Brief

Status: FINAL for development (task-63c069e09a53404c)
Date: 2026-11-22
Author: lead
Predecessor: design.md (artifact-c83f6b0269a34a19abf0ccf4b4383a56) — booking app v1, confirmed by human receipt human-reply-df8d0eda92ac4641 (approval-38b39cc6535f4e00, option-0).

## 1. Goal
Add an interactive analytics dashboard for the room manager. For a selected date range it must show, per room: booking frequency, total reserved time, busiest days and times, and cancellation frequency — with clear charts and summaries so the manager can spot heavily used vs. underused rooms and decide on room allocation.

## 2. Data source (how existing booking records are read)
- The dashboard reads the SAME records the booking app already uses: `localStorage` key `meetingRoomBookings.v1` (constant `STORAGE_KEY` in app.js), an array of booking objects `{ id, room, date (YYYY-MM-DD), start (HH:MM), end (HH:MM), booker, title }`.
- No new storage, no backend, no schema change. The dashboard is a read-only view over the existing array; it must not mutate bookings.
- Cancellations: the current app DELETES a booking on cancel (splice) and keeps no cancellation log. Therefore cancellation frequency cannot be derived from existing records alone. **Decision (see §6): add a lightweight append-only cancellation log** under a new key `meetingRoomBookings.cancellations.v1`, written by the existing cancel path, so the dashboard can count cancellations. This is the only change to the booking app's data model and is additive/backward-compatible (missing log => 0 cancellations).
- All date/time math reuses the existing pure helpers (`toMinutes`, `toHHMM`, `overlaps`) exposed on `window.BookingLogic`; no duplicated parsing logic.

## 3. Metrics (per selected date range, inclusive)
M1. Bookings per room — count of bookings whose `date` falls in the range, grouped by room (A–E).
M2. Total reserved time per room — sum of (end − start) in minutes, shown as hours (e.g. "12.5 h"), grouped by room.
M3. Busiest days — bookings per weekday (Mon–Sun) across the range; highlight the top day(s).
M4. Busiest times — bookings per 30-minute start slot (08:00–18:00); highlight peak slot(s).
M5. Cancellations per room — count from the cancellation log whose `date` falls in the range, grouped by room; plus a total cancellation count and cancellation rate (cancellations ÷ (bookings + cancellations)) for the range.
M6. Utilization summary — per room, reserved minutes ÷ available minutes in the range (available = 10 h/day × number of days in range), as a percentage, to distinguish heavily used vs. underused rooms.

## 4. Date-range filter
- Two date inputs: `From` and `To` (inclusive), defaulting to the current month-to-date (From = first day of current month, To = today).
- Validation: both required; From ≤ To; if From > To show an inline error and do not render charts.
- Changing either input re-renders immediately (no submit button needed); a `Refresh` button is also provided.
- Range is inclusive of both endpoints. Days with no bookings still count toward M6 available minutes.

## 5. Charts and summaries (no external libraries)
- Rendering: plain HTML/CSS + inline SVG built in JS (no CDN, no build step, consistent with the existing no-dependency app).
- C1. Bar chart — bookings per room (M1).
- C2. Bar chart — total reserved hours per room (M2).
- C3. Bar chart — bookings per weekday (M3), busiest day highlighted.
- C4. Bar chart — bookings per 30-min start slot (M4), peak slot highlighted.
- C5. Bar chart — cancellations per room (M5).
- S1. Summary table — one row per room with: bookings, reserved hours, utilization %, cancellations. Sorted by bookings descending so the busiest room is first.
- S2. Headline summary line — total bookings, total reserved hours, busiest day, busiest time, total cancellations, cancellation rate.
- S3. Allocation hint — a short text note naming the most-used and least-used room by utilization % (e.g. "Room A is most used (62%); Room E is least used (8%) — consider reallocating E.").
- Empty state: if the range has no bookings, show "No bookings in this range." and zeroed summaries; charts render empty axes rather than breaking.
- Accessibility: each chart has a text alternative (aria-label / visually-hidden table) summarizing its values.

## 6. Consequential decisions (resolved — human confirmed)
Human confirmation: receipt human-reply-4373babe15814748 (approval-53fbac01acc8486e), option-0 — all proposed defaults accepted (Q1 add cancellation log; Q2 10h/day×days; Q3 month-to-date).
- D1. Cancellation data: existing records do not retain cancellations (cancel deletes the row). Chosen approach: additive append-only cancellation log key `meetingRoomBookings.cancellations.v1`, entry `{ id, room, date, start, end, booker, title, cancelledAt }`, written by the existing cancel path. Backward-compatible: absent log => 0 cancellations. Alternative (rejected): infer cancellations from a "cancelled" flag on bookings — would require rewriting existing records and complicate the booking flow.
- D2. Utilization denominator: fixed 10 h/day (08:00–18:00) × days in range, since all rooms share the same hours. Simple and comparable across rooms.
- D3. No external chart library: keeps the app dependency-free and offline-capable, matching v1.
- D4. Dashboard is read-only over bookings; it never edits or deletes booking records.

## 7. Out of scope
- Authentication/roles, exporting reports (CSV/PDF), email alerts, per-room custom hours, time zones, backend/database, historical trend-over-time charts beyond the selected range.

## 8. Acceptance criteria for the dashboard
AC1. Selecting a valid From/To range renders all five charts and the summary table using only records in that range.
AC2. Bookings-per-room and reserved-hours-per-room match a hand-computed fixture for a known range.
AC3. Busiest day and busiest time are correctly identified for a fixture with a clear peak.
AC4. Cancellation counts match the cancellation log for the range; with no log present, cancellations show 0 without error.
AC5. From > To shows an inline error and renders no charts; empty range shows the empty state.
AC6. Dashboard does not modify `meetingRoomBookings.v1` (read-only verified).
AC7. Utilization % = reserved minutes ÷ (10 h × days) per room, correct for a fixture.

## 9. Handoff notes for dev
- Add a new dashboard section to the existing single-page app (index.html + app.js + styles.css); do not break existing booking/cancel behavior or the `window.BookingLogic` API.
- Keep all aggregation in pure, testable functions (e.g. `computeDashboard(bookings, cancellations, from, to)` returning a plain object) so QA can test them directly without the DOM.
- Add the cancellation log write to the existing cancel path only; keep it additive and guarded (missing key => empty array).
- Update README with dashboard usage steps.
- QA will independently verify AC1–AC7.

## 10. Open questions for the human (asked before implementation)
Q1. Cancellation tracking requires a new additive log (D1). Confirm this is acceptable, or should cancellations be omitted from the dashboard instead?
Q2. Utilization denominator fixed at 10 h/day × days in range (D2) — acceptable, or should it use only working days (Mon–Fri)?
Q3. Default range = current month-to-date — acceptable, or prefer last 30 days / current week?
