// The activity rules, as pure functions. Time is passed in as `nowMs`; nothing
// here calls Date.now().
.pragma library

// ---- day thresholds ----
// <=2 days since the last session is on track, day 3 keeps the chain, day 4 and
// later is a drop. The session's distance does not change this rule.
var ON_TRACK_MAX_DAYS = 2
var KEEP_DAYS = 3
var MS_PER_DAY = 86400000
// What an unusable date reads as: far past the decay threshold.
var UNKNOWN_DAYS = 999

// The writer owns the document limits; this module only needs the timestamp
// length for validation.
var MAX_TS_LEN = 32

// ---- history strip ----
var PAGE_SIZE = 7

// ---- status values (the colours are in ui/Status.js) ----
var ON_TRACK = 1
var KEEP = 0
var DROP = -1

// An ISO-8601 UTC instant, as produced by Date.toISOString(). Fractional
// seconds are 3 digits from toISOString() and 6 from Python, so 1 to 6 are
// accepted. A local time or a date without a zone is not a timestamp.
var TS_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/

function isValidTimestamp(value) {
    return typeof value === "string" && value.length <= MAX_TS_LEN && TS_PATTERN.test(value)
}

// Milliseconds since the epoch, or NaN when the value is not a timestamp.
function parseTimestamp(value) {
    if (!isValidTimestamp(value)) return NaN
    var ms = Date.parse(value)
    return isFinite(ms) ? ms : NaN
}

// Whole days between a stored timestamp and `nowMs`. A future timestamp reads
// as today, not as -1d. A panel left open while a session is recorded has an
// older nowMs, so the clamp also covers the moment the session is written.
function daysSince(value, nowMs) {
    var ms = parseTimestamp(value)
    if (!isFinite(ms)) return UNKNOWN_DAYS
    return Math.max(0, Math.floor((nowMs - ms) / MS_PER_DAY))
}

// Ms from `nowMs` to the next day boundary, so a day count can be re-evaluated
// when it actually changes. daysSince counts whole 24h periods from a UTC
// instant, so the boundary is UTC midnight rather than local midnight, and the
// caller has to follow it or the count flips at the wrong hour. The few seconds
// of slack keep the tick off the boundary itself, where a session saved a
// moment earlier would already read as the day before.
function msUntilNextDay(nowMs) {
    var d = new Date(nowMs)
    var next = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1, 0, 0, 3)
    return Math.max(1000, next - nowMs)
}

function statusForDays(days) {
    if (days <= ON_TRACK_MAX_DAYS) return ON_TRACK
    if (days === KEEP_DAYS) return KEEP
    return DROP
}

// The timestamp of one stored history entry.
function entryTime(entry) {
    if (!entry || typeof entry !== "object") return ""
    return typeof entry.time === "string" ? entry.time : ""
}

function entryMeters(entry) {
    if (!entry || typeof entry !== "object") return 0
    var meters = Number(entry.meters)
    return isFinite(meters) ? Math.max(0, Math.floor(meters)) : 0
}

function entrySeconds(entry) {
    if (!entry || typeof entry !== "object") return 0
    var seconds = Number(entry.seconds)
    return isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0
}

function entryCalories(entry) {
    if (!entry || typeof entry !== "object") return 0
    var calories = Number(entry.calories)
    return isFinite(calories) ? Math.max(0, Math.floor(calories)) : 0
}

function entryCaloriesEstimated(entry) {
    return !!(entry && typeof entry === "object" && entry.estimated === true)
}

// A clock label for the workout time: 4:07, or 1:04:07 once it passes an hour.
function formatDuration(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0))
    var hours = Math.floor(total / 3600)
    var minutes = Math.floor((total % 3600) / 60)
    var rest = total % 60
    var tail = (rest < 10 ? "0" : "") + rest
    if (hours > 0) return hours + ":" + (minutes < 10 ? "0" : "") + minutes + ":" + tail
    return minutes + ":" + tail
}

// The status of a recorded session, derived from the gap to the one before it.
// Never stored: a later session can make an earlier one's gap longer.
function historyValue(history, index) {
    if (!Array.isArray(history) || index < 0 || index >= history.length) return ON_TRACK
    if (index === 0) return ON_TRACK
    var prev = parseTimestamp(entryTime(history[index - 1]))
    var curr = parseTimestamp(entryTime(history[index]))
    if (!isFinite(prev) || !isFinite(curr)) return ON_TRACK
    return statusForDays(Math.floor((curr - prev) / MS_PER_DAY))
}

// A compact label for a 32px disc: 0.45, 2.4, 12.4, 123. It never grows past
// four characters, so the text stays inside the circle.
function formatMeters(meters) {
    var value = Number(meters)
    if (!isFinite(value) || value <= 0) return "0"
    var km = value / 1000
    if (km < 1) return km.toFixed(2)
    if (km < 100) return km.toFixed(1)
    return String(Math.round(km))
}

// The same number with its unit, for the summary line rather than a disc.
function metersLabel(meters) {
    return formatMeters(meters) + " km"
}

function recordMeters(history) {
    if (!Array.isArray(history)) return 0
    var best = 0
    for (var index = 0; index < history.length; index++)
        best = Math.max(best, entryMeters(history[index]))
    return best
}

function totalMeters(history) {
    if (!Array.isArray(history)) return 0
    var total = 0
    for (var index = 0; index < history.length; index++)
        total += entryMeters(history[index])
    return total
}
