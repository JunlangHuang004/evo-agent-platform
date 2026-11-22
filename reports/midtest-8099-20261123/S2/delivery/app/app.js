/* Meeting Room Booking System
 * Single-page app, no backend. Data persisted in localStorage.
 * Rooms A-E, 08:00-18:00, 30-minute slots, hard-block conflicts, owner-only cancel.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "meetingRoomBookings.v1";
  var CANCEL_KEY = "meetingRoomBookings.cancellations.v1";
  var ROOMS = ["A", "B", "C", "D", "E"];
  var DAY_START_MIN = 8 * 60;   // 08:00
  var DAY_END_MIN = 18 * 60;    // 18:00
  var SLOT_MIN = 30;

  // ---------- pure helpers (testable) ----------

  function toMinutes(hhmm) {
    if (typeof hhmm !== "string" || !/^\d{2}:\d{2}$/.test(hhmm)) return NaN;
    var parts = hhmm.split(":");
    var h = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    if (h < 0 || h > 23 || m < 0 || m > 59) return NaN;
    return h * 60 + m;
  }

  function toHHMM(mins) {
    var h = Math.floor(mins / 60);
    var m = mins % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }

  // Two bookings for the same room+date conflict iff startA < endB AND startB < endA
  function overlaps(aStart, aEnd, bStart, bEnd) {
    return aStart < bEnd && bStart < aEnd;
  }

  function findConflict(bookings, candidate) {
    for (var i = 0; i < bookings.length; i++) {
      var b = bookings[i];
      if (b.room !== candidate.room || b.date !== candidate.date) continue;
      if (overlaps(toMinutes(candidate.start), toMinutes(candidate.end),
                   toMinutes(b.start), toMinutes(b.end))) {
        return b;
      }
    }
    return null;
  }

  // Returns {ok:true} or {ok:false, error:"..."}
  function validateBooking(bookings, candidate) {
    if (ROOMS.indexOf(candidate.room) === -1) {
      return { ok: false, error: "Unknown room: " + candidate.room };
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(candidate.date || "")) {
      return { ok: false, error: "Please choose a valid date." };
    }
    if (!candidate.booker || !candidate.booker.trim()) {
      return { ok: false, error: "Booker name is required." };
    }
    if (!candidate.title || !candidate.title.trim()) {
      return { ok: false, error: "Title is required." };
    }
    var s = toMinutes(candidate.start);
    var e = toMinutes(candidate.end);
    if (isNaN(s) || isNaN(e)) {
      return { ok: false, error: "Start and end times must be HH:MM." };
    }
    if (s >= e) {
      return { ok: false, error: "Start time must be before end time." };
    }
    if (s < DAY_START_MIN || e > DAY_END_MIN) {
      return { ok: false, error: "Bookings must be within 08:00-18:00." };
    }
    if (s % SLOT_MIN !== 0 || e % SLOT_MIN !== 0) {
      return { ok: false, error: "Times must align to 30-minute slots." };
    }
    var conflict = findConflict(bookings, candidate);
    if (conflict) {
      return {
        ok: false,
        error: "Room " + conflict.room + " is already booked " + conflict.start +
               "-" + conflict.end + " on " + conflict.date + " (" + conflict.title + ")."
      };
    }
    return { ok: true };
  }

  function cancelBooking(bookings, id, booker) {
    var idx = -1;
    for (var i = 0; i < bookings.length; i++) {
      if (bookings[i].id === id) { idx = i; break; }
    }
    if (idx === -1) return { ok: false, error: "Booking not found." };
    var b = bookings[idx];
    if ((b.booker || "").trim().toLowerCase() !== (booker || "").trim().toLowerCase()) {
      return { ok: false, error: "Only the booker (" + b.booker + ") can cancel this booking." };
    }
    bookings.splice(idx, 1);
    return { ok: true, removed: b };
  }

  // ---------- storage ----------

  function loadBookings() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }

  function saveBookings(bookings) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(bookings));
      return true;
    } catch (err) {
      return false;
    }
  }

  // Append-only cancellation log (additive, backward-compatible).
  function loadCancellations() {
    try {
      var raw = localStorage.getItem(CANCEL_KEY);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }

  function appendCancellation(removed) {
    try {
      var log = loadCancellations();
      log.push({
        id: removed.id,
        room: removed.room,
        date: removed.date,
        start: removed.start,
        end: removed.end,
        booker: removed.booker,
        title: removed.title,
        cancelledAt: new Date().toISOString()
      });
      localStorage.setItem(CANCEL_KEY, JSON.stringify(log));
      return true;
    } catch (err) {
      return false;
    }
  }

  // ---------- UI ----------

  var state = { bookings: [] };

  function $(id) { return document.getElementById(id); }

  function todayISO() {
    var d = new Date();
    var m = d.getMonth() + 1;
    var day = d.getDate();
    return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
  }

  function fillTimeOptions(select) {
    select.innerHTML = "";
    for (var t = DAY_START_MIN; t <= DAY_END_MIN; t += SLOT_MIN) {
      var opt = document.createElement("option");
      opt.value = toHHMM(t);
      opt.textContent = toHHMM(t);
      select.appendChild(opt);
    }
  }

  function fillRoomOptions(select) {
    select.innerHTML = "";
    ROOMS.forEach(function (r) {
      var opt = document.createElement("option");
      opt.value = r;
      opt.textContent = "Room " + r;
      select.appendChild(opt);
    });
  }

  function setMsg(el, text, kind) {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  function renderAvailability() {
    var date = $("avail-date").value;
    var container = $("availability");
    container.innerHTML = "";
    if (!date) {
      container.innerHTML = '<p class="empty">Choose a date to see availability.</p>';
      return;
    }
    ROOMS.forEach(function (room) {
      var block = document.createElement("div");
      block.className = "room-block";
      var h = document.createElement("h3");
      h.textContent = "Room " + room;
      block.appendChild(h);

      var list = document.createElement("div");
      list.className = "slot-list";
      for (var t = DAY_START_MIN; t < DAY_END_MIN; t += SLOT_MIN) {
        var slotStart = t;
        var slotEnd = t + SLOT_MIN;
        var booked = false;
        for (var i = 0; i < state.bookings.length; i++) {
          var b = state.bookings[i];
          if (b.room !== room || b.date !== date) continue;
          if (overlaps(slotStart, slotEnd, toMinutes(b.start), toMinutes(b.end))) {
            booked = true;
            break;
          }
        }
        var span = document.createElement("span");
        span.className = "slot" + (booked ? " booked" : "");
        span.textContent = toHHMM(slotStart) + "-" + toHHMM(slotEnd) + (booked ? " booked" : " free");
        list.appendChild(span);
      }
      block.appendChild(list);
      container.appendChild(block);
    });
  }

  function renderBookings() {
    var container = $("bookings");
    container.innerHTML = "";
    if (state.bookings.length === 0) {
      container.innerHTML = '<p class="empty">No bookings yet.</p>';
      return;
    }
    var sorted = state.bookings.slice().sort(function (a, b) {
      if (a.date !== b.date) return a.date < b.date ? -1 : 1;
      if (a.room !== b.room) return a.room < b.room ? -1 : 1;
      return toMinutes(a.start) - toMinutes(b.start);
    });
    sorted.forEach(function (b) {
      var item = document.createElement("div");
      item.className = "booking-item";
      item.setAttribute("data-booking-id", b.id);

      var meta = document.createElement("div");
      meta.className = "meta";
      var title = document.createElement("span");
      title.className = "title";
      title.textContent = b.title;
      meta.appendChild(title);
      meta.appendChild(document.createTextNode(
        " \u2014 Room " + b.room + ", " + b.date + " " + b.start + "-" + b.end +
        ", booker: " + b.booker
      ));
      item.appendChild(meta);

      var nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.placeholder = "Booker name";
      nameInput.className = "cancel-name";
      nameInput.setAttribute("aria-label", "Booker name to cancel " + b.title);
      item.appendChild(nameInput);

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary cancel-btn";
      btn.textContent = "Cancel";
      btn.addEventListener("click", function () {
        var entered = nameInput.value;
        var result = cancelBooking(state.bookings, b.id, entered);
        if (!result.ok) {
          setMsg($("cancel-msg"), result.error, "err");
          return;
        }
        if (!saveBookings(state.bookings)) {
          setMsg($("cancel-msg"), "Could not save changes (storage unavailable).", "err");
          return;
        }
        appendCancellation(result.removed);
        setMsg($("cancel-msg"), "Cancelled: " + result.removed.title + " (Room " + result.removed.room + ").", "ok");
        renderBookings();
        renderAvailability();
      });
      item.appendChild(btn);

      container.appendChild(item);
    });
  }

  function handleBookSubmit(ev) {
    ev.preventDefault();
    var candidate = {
      id: "bk-" + Date.now() + "-" + Math.floor(Math.random() * 100000),
      room: $("book-room").value,
      date: $("book-date").value,
      start: $("book-start").value,
      end: $("book-end").value,
      booker: $("book-booker").value.trim(),
      title: $("book-title").value.trim()
    };
    var check = validateBooking(state.bookings, candidate);
    if (!check.ok) {
      setMsg($("book-msg"), check.error, "err");
      return;
    }
    state.bookings.push(candidate);
    if (!saveBookings(state.bookings)) {
      state.bookings.pop();
      setMsg($("book-msg"), "Could not save booking (storage unavailable).", "err");
      return;
    }
    setMsg($("book-msg"),
      "Booked Room " + candidate.room + " on " + candidate.date + " " +
      candidate.start + "-" + candidate.end + " for " + candidate.booker + ".", "ok");
    $("book-form").reset();
    $("book-date").value = candidate.date;
    renderBookings();
    renderAvailability();
  }

  function init() {
    fillRoomOptions($("book-room"));
    fillTimeOptions($("book-start"));
    fillTimeOptions($("book-end"));
    $("book-start").value = "09:00";
    $("book-end").value = "10:00";

    var today = todayISO();
    $("avail-date").value = today;
    $("book-date").value = today;

    state.bookings = loadBookings();

    $("refresh-btn").addEventListener("click", renderAvailability);
    $("avail-date").addEventListener("change", renderAvailability);
    $("book-form").addEventListener("submit", handleBookSubmit);

    renderAvailability();
    renderBookings();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  // Expose pure logic for testing
  if (typeof window !== "undefined") {
    window.BookingLogic = {
      toMinutes: toMinutes,
      toHHMM: toHHMM,
      overlaps: overlaps,
      findConflict: findConflict,
      validateBooking: validateBooking,
      cancelBooking: cancelBooking,
      STORAGE_KEY: STORAGE_KEY,
      CANCEL_KEY: CANCEL_KEY,
      ROOMS: ROOMS
    };
  }
})();
