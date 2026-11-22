# Meeting Room Booking System — Requirements & Design Brief

Status: FINAL for development (task-4b7e14c9c7874a00)
Date: 2026-11-22
Author: lead
Human confirmation: receipt human-reply-df8d0eda92ac4641 (approval-38b39cc6535f4e00), option-0 — proceed with sensible defaults.

## 1. Problem
Rooms are managed in a shared spreadsheet, causing double bookings and stale records after cancellations. We need a simple, usable web app to check availability, book rooms, and cancel bookings reliably.

## 2. Scope (confirmed)
- Single-page web app (HTML/CSS/JS), runs in a browser, no backend required.
- Data persisted in browser localStorage so records survive page reloads/restarts.
- Rooms: 5 fixed rooms, labeled A, B, C, D, E.
- Operating hours: 08:00–18:00 (same for all rooms, all days).
- Slot granularity: 30 minutes. Bookings are aligned to 30-min slots.
- Conflict handling: HARD BLOCK. A booking that overlaps an existing booking for the same room and date is rejected with a clear error. No double bookings possible.
- Cancellation: owner-only. A booking can be cancelled only by entering the same booker name used to create it.
- Required fields: room, date, start time, end time, booker name, title.

## 3. Functional requirements
FR1. Availability view: for a chosen date, show each room's booked slots (and free slots) so users can see what is open.
FR2. Book: create a booking with room, date, start, end, booker, title. Validate: within 08:00–18:00, start < end, 30-min alignment, no overlap with existing bookings for that room/date. On success, persist and refresh the view.
FR3. Cancel: cancel an existing booking by selecting it and confirming the booker name. Only the matching booker may cancel. On success, persist and refresh the view.
FR4. Persistence: all bookings stored in localStorage under a single key; reload restores state.
FR5. Feedback: clear success and error messages for every action (e.g., "Room A is already booked 10:00–11:00").

## 4. Non-functional requirements
- Usable on a desktop browser; no build step, no external services.
- Deterministic conflict logic (half-open intervals [start, end)) so adjacent bookings (e.g., 10:00–10:30 and 10:30–11:00) do NOT conflict.
- Data model: booking = { id, room, date (YYYY-MM-DD), start (HH:MM), end (HH:MM), booker, title }.
- Time comparison done in minutes since midnight to avoid string pitfalls.

## 5. Conflict rule (precise)
Two bookings for the same room and date conflict iff startA < endB AND startB < endA. Overlap => reject (hard block).

## 6. Out of scope (this iteration)
- Authentication/accounts, recurring bookings, email notifications, multi-day views, admin overrides, backend/database, time zones (assume single local time).

## 7. Acceptance criteria for the app
AC1. Booking a free slot succeeds and appears in the availability view.
AC2. Booking an overlapping slot is rejected with a clear message and does not alter stored data.
AC3. Cancelling with the correct booker name removes the booking; wrong name is rejected.
AC4. After reload, bookings persist (localStorage).
AC5. Bookings outside 08:00–18:00 or misaligned to 30 min are rejected.

## 8. Handoff notes for dev
- Deliver a single-page app (index.html + app.js + styles) with the above behavior.
- Keep conflict logic in one pure function so QA can test it directly.
- Provide a short README with usage steps.
- QA will independently verify AC1–AC5.
