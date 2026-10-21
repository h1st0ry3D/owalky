import QtQuick
import qs.Commons
import qs.Ui
import "../core/Progression.js" as Progression

// One session's three live numbers: distance, active time and calories.
//
// The panel uses it twice: above the activity chain for the workout in progress,
// and below the chain for the last recorded one. The caller owns visibility, so
// there is no block of zeroes before the first session.
Column {
    id: metrics

    required property string label
    required property int meters
    required property int seconds
    required property int calories
    property bool estimated: false

    width: parent.width
    spacing: Style.space(6)

    Text {
        width: parent.width
        horizontalAlignment: Text.AlignHCenter
        text: metrics.label
        textFormat: Text.PlainText
        color: Util.alpha(Color.foreground, 0.55)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
    }

    Row {
        width: parent.width
        spacing: Style.space(8)

        Column {
            width: (parent.width - Style.space(16)) / 3
            spacing: Style.space(2)

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "DISTANCE"
                textFormat: Text.PlainText
                color: Util.alpha(Color.foreground, 0.45)
                font.family: Style.font.family
                font.pixelSize: 8
            }

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: Progression.metersLabel(metrics.meters)
                textFormat: Text.PlainText
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: 15
                font.bold: true
            }
        }

        Column {
            width: (parent.width - Style.space(16)) / 3
            spacing: Style.space(2)

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "TIME"
                textFormat: Text.PlainText
                color: Util.alpha(Color.foreground, 0.45)
                font.family: Style.font.family
                font.pixelSize: 8
            }

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: Progression.formatDuration(metrics.seconds)
                textFormat: Text.PlainText
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: 15
                font.bold: true
            }
        }

        Column {
            width: (parent.width - Style.space(16)) / 3
            spacing: Style.space(2)

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "CALORIES"
                textFormat: Text.PlainText
                color: Util.alpha(Color.foreground, 0.45)
                font.family: Style.font.family
                font.pixelSize: 8
            }

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: metrics.calories + " kcal"
                textFormat: Text.PlainText
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: 15
                font.bold: true
            }

            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                visible: metrics.estimated
                text: "estimated"
                textFormat: Text.PlainText
                color: Util.alpha(Color.foreground, 0.45)
                font.family: Style.font.family
                font.pixelSize: 7
            }
        }
    }
}
