// services/dashboard/app/static/time-et.js
// Every time on the dashboard is shown in the site's own time zone -- the one
// the server puts on <html data-tz> from TZ -- with daylight saving applied
// from the browser's time-zone data and the zone's abbreviation shown,
// whatever zone the viewer's computer is set to (follow-up spec F; the review
// page's RV.fmtTime follows the same rule). Loaded before app.js.
"use strict";

const WB_TIME_ZONE =
  (typeof document !== "undefined" && document.documentElement && document.documentElement.dataset.tz) ||
  "UTC";
const ET_STYLES = {
  full: { timeZoneName: "short" },                                             // 7/1/2026, 10:40:56 AM EDT
  time: { hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short" },  // 10:40:56 AM EDT
  short: { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    timeZoneName: "short" },                                                   // Jul 1, 10:40 AM EDT
  day: { month: "short", day: "numeric" },                                     // Jul 1
  date: { year: "numeric", month: "numeric", day: "numeric" },                 // 7/1/2026
};
const ET_PARTS = new Intl.DateTimeFormat("en-US", { timeZone: WB_TIME_ZONE, hourCycle: "h23",
  month: "numeric", day: "numeric", hour: "numeric", minute: "numeric" });

function fmtET(value, style) {
  const d = value instanceof Date ? value : new Date(value);
  return d.toLocaleString("en-US", { timeZone: WB_TIME_ZONE, ...ET_STYLES[style || "full"] });
}

// The Eastern calendar and clock fields of a moment, for charts that bucket
// or label by hour.
function etParts(value) {
  const d = value instanceof Date ? value : new Date(value);
  const parts = {};
  for (const p of ET_PARTS.formatToParts(d)) parts[p.type] = p.value;
  return { month: Number(parts.month), day: Number(parts.day),
    hour: Number(parts.hour) % 24, minute: Number(parts.minute) };
}
