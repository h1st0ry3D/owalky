// The activity document: a closed schema, validated on the way in and rebuilt on
// the way out. A value that fails its check is dropped on its own, since the rest
// of the document is still this user's workout history.
//
// `rules` is the Progression module, which owns the timestamp shape: a QML .js
// library cannot import another one. owalky/activity.py enforces the same limits
// on the write side, so the two belong in sync.
.pragma library

var MAX_METERS = 10000000
var MAX_TRAINING_DAYS = 1000000
var HISTORY_MAX = 400

function emptyDocument() {
    return { lastTrainingDate: "", trainingDays: 0, history: [] }
}

// A finite integer in [min, max], or the fallback. The type is checked first, so
// a string, a boolean, null and NaN all take the same path. A range check alone
// would not catch 1e999, which parses to Infinity.
function boundedInt(value, min, max, fallback) {
    if (typeof value !== "number" || !isFinite(value)) return fallback
    var n = Math.floor(value)
    return n < min || n > max ? fallback : n
}

function parseTimestamp(value, rules) {
    return rules.isValidTimestamp(value) ? value : ""
}

// The stored history, oldest first. Entries that are not `{time, meters}`
// objects with a usable timestamp are dropped; the cap keeps the most recent
// HISTORY_MAX entries, as the writer does.
function parseHistory(value, rules) {
    var out = []
    if (!Array.isArray(value)) return out
    var start = Math.max(0, value.length - HISTORY_MAX)
    for (var i = start; i < value.length; i++) {
        var entry = value[i]
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue
        var time = parseTimestamp(entry.time, rules)
        if (!time) continue
        out.push({ time: time, meters: boundedInt(entry.meters, 0, MAX_METERS, 0) })
    }
    return out
}

// A missing, truncated, non-JSON or non-object document yields the empty
// document (no sessions yet), never a partly applied one.
function parse(raw, rules) {
    var doc = emptyDocument()
    var parsed = null
    try {
        parsed = JSON.parse(raw === undefined || raw === null ? "" : String(raw))
    } catch (e) {
        return doc
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return doc
    doc.trainingDays = boundedInt(parsed.trainingDays, 0, MAX_TRAINING_DAYS, 0)
    doc.lastTrainingDate = parseTimestamp(parsed.lastTrainingDate, rules)
    doc.history = parseHistory(parsed.history, rules)
    return doc
}

// One line: the helper reads stdin as a single bounded line and re-indents the
// file it writes.
function serialize(doc, rules) {
    var source = doc && typeof doc === "object" && !Array.isArray(doc) ? doc : emptyDocument()
    return JSON.stringify({
        lastTrainingDate: parseTimestamp(source.lastTrainingDate, rules),
        trainingDays: boundedInt(source.trainingDays, 0, MAX_TRAINING_DAYS, 0),
        history: Array.isArray(source.history) ? source.history.slice(-HISTORY_MAX) : []
    })
}
