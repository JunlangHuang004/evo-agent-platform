# Meeting Room Booking System — Release Notes

**Version:** 1.0.0
**Release date:** 2026-11-22
**Status:** Released (independently QA-verified)

## Overview

Version 1.0.0 is the first release of the Meeting Room Booking System. It
replaces the shared spreadsheet used to manage meeting rooms and eliminates the
two recurring problems reported by the team: double bookings and records that
were not updated after cancellations.

This is a single-page web app with no build step and no server. It runs entirely
in the browser and stores data in browser localStorage.

## What's included

- **Availability view** — pick a date and see each room's booked slots.
- **Booking** — book a room for a date and a 30-minute time slot; overlapping
  bookings for the same room and date are hard-blocked with a clear message.
- **Cancellation** — cancel a booking by entering the same booker name used to
  create it; only the original booker can cancel.
- **Persistence** — bookings are saved in localStorage and survive page reloads.

## Fixed in this release

- Double bookings: overlapping bookings for the same room and date are rejected
  (half-open interval rule; adjacent slots such as 10:00–10:30 and 10:30–11:00
  do not conflict).
- Stale records after cancellation: cancelling a booking removes it from the
  list and from persisted storage, so availability is always current.

## Rules and scope

- Rooms: A, B, C, D, E.
- Hours: 08:00–18:00, 30-minute slots.
- Conflict handling: hard block (no double bookings).
- Cancellation: owner-only (matching booker name).
- Fields: room, date, start, end, booker, title.
- Storage: browser localStorage under key `meetingRoomBookings.v1`.

## Verification

Independently verified by QA via real Chromium browser tests against the
released file hashes (review of dev task-abf42320a6aa4a11, verdict PASS):

- Availability rendering (5 rooms; booked slots marked).
- Booking creation.
- Double-booking prevention (overlap rejected; adjacent slot accepted).
- Owner-only cancellation (wrong booker rejected; correct booker removes).
- Persistence across reload.
- Storage-write failure rollback (no phantom booking, nothing persisted).
- Storage-read failure (no crash; prior record not overwritten).

No console or page errors were observed.

## Known limitations

- No automated narrow-screen (mobile) overflow check was run.
- No explicit two-page stale-tab conflict test was run.
- Data is per-browser (localStorage); it is not shared across devices or users.
  Concurrent edits from two browsers are not synchronized.

## Files in this release

- `app/index.html` — entrypoint (page structure).
- `app/app.js` — booking logic (validation, conflict check, persistence).
- `app/styles.css` — styling.
- `README.md` — usage instructions.
- `RELEASE_NOTES.md` — this document.
