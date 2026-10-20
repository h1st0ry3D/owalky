// The three status colours and labels, so the day bubble, the strip and the
// legend agree. The values are core/Progression.js (ON_TRACK / KEEP / DROP); the
// hexes are Material 700 green/amber/red, which stays distinguishable for the
// common forms of colour blindness.
.pragma library

var ON_TRACK = "#2E7D32"
var KEEP = "#F57F17"
var DROP = "#C62828"
var EMPTY = "transparent"

function colorForValue(value) {
    if (value === 1) return ON_TRACK
    if (value === 0) return KEEP
    return DROP
}

function labelForValue(value) {
    if (value === 1) return "On track (2 days or less)"
    if (value === 0) return "Keep (3 days)"
    return "Drop (4 days or more)"
}

// What the day bubble reads. No session to count from yet, so an infinity
// rather than a day count that would say the chain is already broken.
function dayLabel(everTrained, daysSinceLast) {
    return everTrained ? (daysSinceLast + "d") : "∞"
}
