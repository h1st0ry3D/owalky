// Flattening for text the shell renders (PanelHero, PanelToolTip, the bar
// button). Those widgets own their Text item, so the format cannot be pinned
// from here, and rich text loads <img src="..."> as a request from the shell.
.pragma library

var MAX_LEN = 96

function plain(value) {
    var s = String(value === undefined || value === null ? "" : value);
    return s.replace(/[<>&]/g, "")                                  // markup
            .replace(/[\u0000-\u001f\u007f-\u009f]/g, "")           // C0/C1 controls
            .replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "") // bidi controls
            .slice(0, MAX_LEN);
}
