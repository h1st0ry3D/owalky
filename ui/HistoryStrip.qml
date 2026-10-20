import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../core/Progression.js" as Progression
import "Status.js" as Status

// The recorded workouts, newest last, seven to a page.
//
// Each disc is one completed session, coloured by the gap to the session before
// it (core/Progression.js historyValue) and labelled with its kilometres. A
// disabled arrow keeps its slot so the strip does not jump sideways, and the
// page counter only appears when there is more than one page.
Column {
    id: strip

    required property var history        // [{time, meters}], oldest first
    required property int page           // already clamped by the panel
    required property int pageCount

    signal previousRequested()
    signal nextRequested()

    spacing: Style.space(6)

    readonly property bool hasPrevious: strip.page > 0
    readonly property bool hasNext: strip.page < strip.pageCount - 1

    Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(8)

        Button {
            text: "‹"
            width: 32
            enabled: strip.hasPrevious
            opacity: enabled ? 1 : 0
            onClicked: strip.previousRequested()
        }

        Row {
            spacing: 6

            Repeater {
                model: Progression.PAGE_SIZE

                Rectangle {
                    id: slot
                    required property int index
                    readonly property int globalIndex: strip.page * Progression.PAGE_SIZE + slot.index
                    readonly property var entry: strip.history[slot.globalIndex]
                    readonly property bool filled: slot.entry !== undefined
                    readonly property int statusValue: Progression.historyValue(strip.history, slot.globalIndex)
                    readonly property color fillColor: Status.colorForValue(slot.statusValue)

                    width: 32
                    height: 32
                    radius: 16
                    color: slot.filled ? slot.fillColor : Status.EMPTY
                    border.width: 1
                    border.color: slot.filled ? slot.fillColor : Util.alpha(Color.foreground, 0.3)

                    Text {
                        anchors.centerIn: parent
                        visible: slot.filled
                        text: Progression.formatMeters(Progression.entryMeters(slot.entry))
                        textFormat: Text.PlainText
                        color: "white"
                        font.family: Style.font.family
                        font.pixelSize: 11
                    }
                }
            }
        }

        Button {
            text: "›"
            width: 32
            enabled: strip.hasNext
            opacity: enabled ? 1 : 0
            onClicked: strip.nextRequested()
        }
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        visible: strip.pageCount > 1
        text: (strip.page + 1) + " / " + strip.pageCount
        textFormat: Text.PlainText
        color: Util.alpha(Color.foreground, 0.5)
        font.family: Style.font.family
        font.pixelSize: 9
    }
}
