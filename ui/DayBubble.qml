import QtQuick
import qs.Commons
import qs.Ui
import "../core/Progression.js" as Progression
import "Status.js" as Status
import "Plain.js" as Plain

// The days-since-last-workout bubble: the plugin's icon in the bar, and the
// panel's day readout in the hero. One component for both, so they cannot drift.
//
// Painted like the workout bubbles in HistoryStrip: a disc filled with the
// status colour, no ring, number on top. The bar's copy drops the colour while
// the chain is on track (see `quietOnTrack`), because a green disc for every
// ordinary day is noise in a 27px slot, while amber and red still say something.
//
// Sized from the item rather than a `size` property, because the bar's
// `iconComponent` slot hands out a fixed box while the hero's slot sizes the
// item from its implicit size. The label scales with the disc.
Item {
    id: bubble

    required property int daysSinceLast
    required property bool everTrained
    required property int statusValue

    property bool tooltip: false
    // The bar's icon, not the panel's: one colour while the chain is on track,
    // the status colour once it slips. The label flips with the disc, since a
    // foreground disc needs the bar's background on it to stay readable.
    property bool quietOnTrack: false
    // 0 sizes the label off the disc, which is what the panel wants. The bar
    // passes a size of its own: its disc is small enough that the derived size
    // lands on the floor and the number stops reading.
    property int labelSize: 0

    readonly property real disc: Math.min(width, height)
    readonly property bool singleColour: bubble.quietOnTrack
        && bubble.statusValue === Progression.ON_TRACK
    readonly property real labelPixelSize: bubble.labelSize > 0
        ? bubble.labelSize : Math.max(8, Math.round(bubble.disc * 0.38))
    // Progression.daysSince returns 999 for a timestamp it cannot read, and
    // three characters ("12d") is already the widest a 22px disc takes. The
    // wording is Status.js's; only the overflow is capped here.
    readonly property string dayLabel: {
        if (!bubble.everTrained) return Status.dayLabel(false, 0)
        return bubble.daysSinceLast > 99 ? "99" : Status.dayLabel(true, bubble.daysSinceLast)
    }

    implicitWidth: Style.font.display
    implicitHeight: Style.font.display

    Rectangle {
        anchors.fill: parent
        // A rectangle with radius half its width is the circle, and a plain
        // Rectangle is cheaper than a Shape for one solid disc.
        radius: width / 2
        color: bubble.singleColour ? Color.foreground : Status.colorForValue(bubble.statusValue)
    }

    Text {
        anchors.centerIn: parent
        text: bubble.dayLabel
        textFormat: Text.PlainText
        // "12d" has to sit inside the circle, so the label takes a smaller share
        // of the disc than a plain caption would.
        font.pixelSize: bubble.labelPixelSize
        font.family: Style.font.family
        color: bubble.singleColour ? Color.bar.background : "white"
    }

    MouseArea {
        id: hover
        anchors.fill: parent
        hoverEnabled: bubble.tooltip
    }

    PanelToolTip {
        visible: hover.containsMouse
        text: Plain.plain(Status.labelForValue(bubble.statusValue))
        fontFamily: Style.font.family
    }
}
