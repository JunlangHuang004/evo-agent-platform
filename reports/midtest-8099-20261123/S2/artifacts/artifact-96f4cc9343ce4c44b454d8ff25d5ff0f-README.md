# Meeting Room Booking — Manager Dashboard

A dependency-free single-page web app for booking meeting rooms (A–E, 08:00–18:00,
30-minute slots, no double bookings) with an added **Manager Dashboard** that shows
room usage analytics for a selected date range.

## Files
- `index.html` — entrypoint (open this file in a browser).
- `app.js` — booking logic, availability, cancel, and the additive cancellation log.
- `dashboard.js` — read-only analytics dashboard (pure `computeDashboard` + rendering).
- `styles.css` — styles for the app and dashboard.

## How to run
1. Open `index.html` in any modern browser (no server, no build step, no network needed).
2. Data is stored in this browser's `localStorage` and survives reloads.

## Using the dashboard
1. Scroll to section **4. Manager dashboard**.
2. Pick a **From** and **To** date (inclusive). Defaults to the current month-to-date.
3. The dashboard updates immediately; a **Refresh** button is also provided.
4. It shows:
   - Headline summary: total bookings, total reserved hours, total cancellations and
     cancellation rate, busiest day and busiest time.
   - Bar charts: bookings per room, reserved hours per room, bookings per weekday,
     bookings per 30-minute start slot, cancellations per room.
   - A per-room summary table (bookings, reserved hours, utilization %, cancellations),
     sorted by bookings descending.
   - An allocation hint naming the most-used and least-used room by utilization %.
5. If **From > To**, an inline error is shown and no charts render. If the range has no
   bookings, an empty-state message is shown with zeroed summaries.

## Metrics and definitions
- **Bookings per room** — count of bookings in the range, grouped by room.
- **Reserved time per room** — sum of (end − start) minutes, shown in hours.
- **Busiest day** — weekday with the most bookings in the range.
- **Busiest time** — 30-minute start slot with the most bookings.
- **Cancellations** — counted from the additive cancellation log (see below).
- **Utilization %** — reserved minutes ÷ (10 h/day × number of days in range) per room.
- **Cancellation rate** — cancellations ÷ (bookings + cancellations) for the range.

## Data model
- Bookings: `localStorage` key `meetingRoomBookings.v1` — array of
  `{ id, room, date (YYYY-MM-DD), start (HH:MM), end (HH:MM), booker, title }`.
- Cancellations: `localStorage` key `meetingRoomBookings.cancellations.v1` — append-only
  log written by the cancel path, entry
  `{ id, room, date, start, end, booker, title, cancelledAt }`.
  This is additive and backward-compatible: if the key is absent, cancellations show 0.
- The dashboard is **read-only** over bookings; it never edits or deletes records.

## Notes
- No external libraries or CDNs; charts are plain HTML/CSS bars with text alternatives.
- Pure aggregation is exposed as `window.DashboardLogic.computeDashboard(bookings, cancellations, from, to)`
  for direct testing.
