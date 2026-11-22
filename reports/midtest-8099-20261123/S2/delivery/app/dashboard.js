/* Manager Dashboard for the Meeting Room Booking System.
 * Read-only analytics over existing booking records + additive cancellation log.
 * Pure aggregation functions are exposed on window.DashboardLogic for testing.
 */
(function () {
  "use strict";

  var ROOMS = ["A", "B", "C", "D", "E"];
  var DAY_START_MIN = 8 * 60;   // 08:00
  var DAY_END_MIN = 18 * 60;    // 18:00
  var SLOT_MIN = 30;
  var AVAIL_MIN_PER_DAY = DAY_END_MIN - DAY_START_MIN; // 600 = 10h
  var WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function toMinutes(hhmm) {
    if (typeof hhmm !== "string" || !/^\d{2}:\d{2}$/.test(hhmm)) return NaN;
    var p = hhmm.split(":");
    var h = parseInt(p[0], 10), m = parseInt(p[1], 10);
    if (h < 0 || h > 23 || m < 0 || m > 59) return NaN;
    return h * 60 + m;
  }

  function toHHMM(mins) {
    var h = Math.floor(mins / 60), m = mins % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }

  // Inclusive day count between two YYYY-MM-DD strings. Returns 0 if invalid/from>to.
  function daysInRange(from, to) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from || "") || !/^\d{4}-\d{2}-\d{2}$/.test(to || "")) return 0;
    var a = Date.UTC(+from.slice(0, 4), +from.slice(5, 7) - 1, +from.slice(8, 10));
    var b = Date.UTC(+to.slice(0, 4), +to.slice(5, 7) - 1, +to.slice(8, 10));
    if (b < a) return 0;
    return Math.round((b - a) / 86400000) + 1;
  }

  function weekdayIndex(dateStr) {
    var d = new Date(Date.UTC(+dateStr.slice(0, 4), +dateStr.slice(5, 7) - 1, +dateStr.slice(8, 10)));
    return (d.getUTCDay() + 6) % 7; // 0=Mon .. 6=Sun
  }

  function inRange(dateStr, from, to) {
    return dateStr >= from && dateStr <= to;
  }

  // Pure aggregation. bookings/cancellations are arrays; from/to inclusive YYYY-MM-DD.
  function computeDashboard(bookings, cancellations, from, to) {
    bookings = Array.isArray(bookings) ? bookings : [];
    cancellations = Array.isArray(cancellations) ? cancellations : [];

    var perRoom = {};
    ROOMS.forEach(function (r) { perRoom[r] = { room: r, count: 0, minutes: 0, cancellations: 0 }; });
    var weekdayCounts = [0, 0, 0, 0, 0, 0, 0];
    var slotCounts = {};
    for (var t = DAY_START_MIN; t < DAY_END_MIN; t += SLOT_MIN) slotCounts[toHHMM(t)] = 0;

    var totalBookings = 0, totalMinutes = 0;

    bookings.forEach(function (b) {
      if (!b || !inRange(b.date, from, to)) return;
      var s = toMinutes(b.start), e = toMinutes(b.end);
      if (isNaN(s) || isNaN(e) || e <= s) return;
      var dur = e - s;
      totalBookings++;
      totalMinutes += dur;
      if (perRoom[b.room]) {
        perRoom[b.room].count++;
        perRoom[b.room].minutes += dur;
      }
      weekdayCounts[weekdayIndex(b.date)]++;
      // bucket by 30-min start slot (floor to slot)
      var slotStart = Math.floor(s / SLOT_MIN) * SLOT_MIN;
      var key = toHHMM(slotStart);
      if (slotCounts[key] === undefined) slotCounts[key] = 0;
      slotCounts[key]++;
    });

    var totalCancellations = 0;
    cancellations.forEach(function (c) {
      if (!c || !inRange(c.date, from, to)) return;
      totalCancellations++;
      if (perRoom[c.room]) perRoom[c.room].cancellations++;
    });

    var days = daysInRange(from, to);
    var availMinutes = AVAIL_MIN_PER_DAY * days;

    var rooms = ROOMS.map(function (r) {
      var pr = perRoom[r];
      var util = availMinutes > 0 ? (pr.minutes / availMinutes) * 100 : 0;
      return {
        room: r,
        count: pr.count,
        minutes: pr.minutes,
        hours: pr.minutes / 60,
        cancellations: pr.cancellations,
        utilization: util
      };
    });

    // busiest day
    var busiestDayIdx = -1, busiestDayMax = 0;
    weekdayCounts.forEach(function (c, i) {
      if (c > busiestDayMax) { busiestDayMax = c; busiestDayIdx = i; }
    });

    // busiest time slot
    var busiestSlot = null, busiestSlotMax = 0;
    Object.keys(slotCounts).sort().forEach(function (k) {
      if (slotCounts[k] > busiestSlotMax) { busiestSlotMax = slotCounts[k]; busiestSlot = k; }
    });

    var rate = (totalBookings + totalCancellations) > 0
      ? (totalCancellations / (totalBookings + totalCancellations)) * 100 : 0;

    // allocation hint: most/least used by utilization (only rooms with any data or all)
    var sortedByUtil = rooms.slice().sort(function (a, b) { return b.utilization - a.utilization; });
    var mostUsed = sortedByUtil[0];
    var leastUsed = sortedByUtil[sortedByUtil.length - 1];

    return {
      from: from,
      to: to,
      days: days,
      availMinutes: availMinutes,
      totalBookings: totalBookings,
      totalMinutes: totalMinutes,
      totalHours: totalMinutes / 60,
      totalCancellations: totalCancellations,
      cancellationRate: rate,
      rooms: rooms,
      weekdayCounts: weekdayCounts,
      weekdayLabels: WEEKDAYS.slice(),
      busiestDayIdx: busiestDayIdx,
      busiestDayMax: busiestDayMax,
      slotCounts: slotCounts,
      busiestSlot: busiestSlot,
      busiestSlotMax: busiestSlotMax,
      mostUsedRoom: mostUsed ? mostUsed.room : null,
      mostUsedUtil: mostUsed ? mostUsed.utilization : 0,
      leastUsedRoom: leastUsed ? leastUsed.room : null,
      leastUsedUtil: leastUsed ? leastUsed.utilization : 0
    };
  }

  // ---------- rendering ----------

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Build a simple horizontal bar chart as HTML rows (accessible, no libs).
  function barChart(title, items, unit) {
    var max = 0;
    items.forEach(function (it) { if (it.value > max) max = it.value; });
    var rows = items.map(function (it) {
      var pct = max > 0 ? (it.value / max) * 100 : 0;
      var cls = it.highlight ? "bar-fill highlight" : "bar-fill";
      return '<div class="bar-row">' +
        '<span class="bar-label">' + esc(it.label) + '</span>' +
        '<span class="bar-track"><span class="' + cls + '" style="width:' + pct.toFixed(1) + '%"></span></span>' +
        '<span class="bar-value">' + esc(it.display != null ? it.display : it.value) + (unit ? " " + unit : "") + '</span>' +
        '</div>';
    }).join("");
    return '<div class="chart" role="img" aria-label="' + esc(title) + '">' +
      '<h3>' + esc(title) + '</h3>' + rows + '</div>';
  }

  function renderDashboard() {
    var from = $("dash-from").value;
    var to = $("dash-to").value;
    var errEl = $("dash-error");
    var out = $("dashboard-output");

    if (!from || !to) {
      errEl.textContent = "Please choose both From and To dates.";
      errEl.className = "msg err";
      out.innerHTML = "";
      return;
    }
    if (from > to) {
      errEl.textContent = "From date must be on or before To date.";
      errEl.className = "msg err";
      out.innerHTML = "";
      return;
    }
    errEl.textContent = "";
    errEl.className = "msg";

    var bookings = window.BookingLogic ? loadBookings() : [];
    var cancellations = loadCancellations();
    var d = computeDashboard(bookings, cancellations, from, to);

    if (d.totalBookings === 0 && d.totalCancellations === 0) {
      out.innerHTML = '<p class="empty">No bookings in this range.</p>' + summaryTable(d);
      return;
    }

    var html = "";
    html += '<p class="headline">' + d.totalBookings + ' bookings, ' + d.totalHours.toFixed(1) +
      ' h reserved, ' + d.totalCancellations + ' cancellations (' + d.cancellationRate.toFixed(1) + '%). ' +
      'Busiest day: ' + (d.busiestDayIdx >= 0 ? d.weekdayLabels[d.busiestDayIdx] : "\u2014") +
      '. Busiest time: ' + (d.busiestSlot || "\u2014") + '.</p>';

    html += '<p class="hint">' + allocationHint(d) + '</p>';

    html += barChart("Bookings per room", d.rooms.map(function (r) {
      return { label: "Room " + r.room, value: r.count };
    }));

    html += barChart("Reserved hours per room", d.rooms.map(function (r) {
      return { label: "Room " + r.room, value: r.hours, display: r.hours.toFixed(1) };
    }), "h");

    html += barChart("Bookings per weekday", d.weekdayLabels.map(function (w, i) {
      return { label: w, value: d.weekdayCounts[i], highlight: i === d.busiestDayIdx && d.busiestDayMax > 0 };
    }));

    var slotItems = Object.keys(d.slotCounts).sort().map(function (k) {
      return { label: k, value: d.slotCounts[k], highlight: k === d.busiestSlot && d.busiestSlotMax > 0 };
    });
    html += barChart("Bookings per start time (30-min slots)", slotItems);

    html += barChart("Cancellations per room", d.rooms.map(function (r) {
      return { label: "Room " + r.room, value: r.cancellations };
    }));

    html += summaryTable(d);
    out.innerHTML = html;
  }

  function allocationHint(d) {
    if (d.totalBookings === 0) return "No bookings in this range to compare.";
    return "Room " + d.mostUsedRoom + " is most used (" + d.mostUsedUtil.toFixed(0) +
      "%); Room " + d.leastUsedRoom + " is least used (" + d.leastUsedUtil.toFixed(0) +
      "%) \u2014 consider reallocating " + d.leastUsedRoom + ".";
  }

  function summaryTable(d) {
    var sorted = d.rooms.slice().sort(function (a, b) { return b.count - a.count; });
    var rows = sorted.map(function (r) {
      return '<tr><td>Room ' + esc(r.room) + '</td><td>' + r.count + '</td><td>' +
        r.hours.toFixed(1) + '</td><td>' + r.utilization.toFixed(1) + '%</td><td>' +
        r.cancellations + '</td></tr>';
    }).join("");
    return '<table class="summary"><caption>Per-room summary (sorted by bookings)</caption>' +
      '<thead><tr><th>Room</th><th>Bookings</th><th>Reserved (h)</th><th>Utilization</th><th>Cancellations</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  // ---------- storage ----------

  function loadBookings() {
    try {
      var raw = localStorage.getItem("meetingRoomBookings.v1");
      if (!raw) return [];
      var p = JSON.parse(raw);
      return Array.isArray(p) ? p : [];
    } catch (e) { return []; }
  }

  function loadCancellations() {
    try {
      var raw = localStorage.getItem("meetingRoomBookings.cancellations.v1");
      if (!raw) return [];
      var p = JSON.parse(raw);
      return Array.isArray(p) ? p : [];
    } catch (e) { return []; }
  }

  function todayISO() {
    var d = new Date();
    var m = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day;
  }

  function firstOfMonthISO() {
    var d = new Date();
    var m = d.getMonth() + 1;
    return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-01";
  }

  function init() {
    if (!$("dash-from")) return;
    $("dash-from").value = firstOfMonthISO();
    $("dash-to").value = todayISO();
    $("dash-from").addEventListener("change", renderDashboard);
    $("dash-to").addEventListener("change", renderDashboard);
    $("dash-refresh").addEventListener("click", renderDashboard);
    renderDashboard();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  if (typeof window !== "undefined") {
    window.DashboardLogic = {
      toMinutes: toMinutes,
      toHHMM: toHHMM,
      daysInRange: daysInRange,
      weekdayIndex: weekdayIndex,
      computeDashboard: computeDashboard,
      AVAIL_MIN_PER_DAY: AVAIL_MIN_PER_DAY,
      ROOMS: ROOMS
    };
  }
})();
