# Meeting Room Booking System

A single-page web app to check room availability, book meeting rooms, and cancel
bookings. It replaces the shared spreadsheet and prevents double bookings.

## How to use

1. Open `app/index.html` in a desktop browser (no build step, no server needed).
2. **Check availability** — pick a date in the *Availability* panel. Each room
   (A–E) shows its booked slots for that date, so you can see what is free.
3. **Book a room** — fill in room, date, start time, end time, your name, and a
   title, then click *Book*. Times are 30-minute slots between 08:00 and 18:00.
   If the slot overlaps an existing booking for that room, the booking is
   rejected with a clear message (hard block — no double bookings).
4. **Cancel a booking** — find the booking in the list, type the same booker
   name used to create it, and click *Cancel*. Only the original booker can
   cancel; a wrong name is rejected.
5. Bookings are saved in your browser (localStorage) and survive reloads.

## Rules

- Rooms: A, B, C, D, E.
- Hours: 08:00–18:00, 30-minute slots.
- Overlapping bookings for the same room and date are blocked.
- Adjacent slots (e.g. 10:00–10:30 and 10:30–11:00) do not conflict.
- Cancellation is owner-only (matching booker name).

## Files

- `app/index.html` — page structure and entrypoint.
- `app/app.js` — booking logic (validation, conflict check, persistence).
- `app/styles.css` — styling.

## Data

Bookings are stored under the localStorage key `meetingRoomBookings.v1` as a
JSON array of `{ id, room, date, start, end, booker, title }`.
