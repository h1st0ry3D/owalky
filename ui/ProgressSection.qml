import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../core/Progression.js" as Progression
import "Status.js" as Status

// The activity screen: the recorded workouts as discs, and what they add up to.
// The treadmill controls above the section are what starts and ends a session;
// this section only reports what has already been logged.
Column {
    id: progress

    required property var history
    required property int page
    required property int pageCount
    required property int trainingDays
    property string activityError: ""

    signal previousPageRequested()
    signal nextPageRequested()

    width: parent.width
    spacing: Style.space(10)

    readonly property int record: Progression.recordMeters(progress.history)
    readonly property int lastMeters: progress.history.length > 0
        ? Progression.entryMeters(progress.history[progress.history.length - 1]) : 0

    Text {
        width: parent.width
        horizontalAlignment: Text.AlignHCenter
        text: "My Activity"
        textFormat: Text.PlainText
        color: Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.title
        font.bold: true
    }

    Text {
        width: parent.width
        horizontalAlignment: Text.AlignHCenter
        text: progress.history.length > 0
            ? "Last " + Progression.metersLabel(progress.lastMeters)
              + "  •  Best " + Progression.metersLabel(progress.record)
              + "  •  " + progress.trainingDays
              + (progress.trainingDays === 1 ? " session" : " sessions")
            : "No sessions yet — start the treadmill; the distance is logged when you stop"
        textFormat: Text.PlainText
        color: Util.alpha(Color.foreground, 0.7)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
    }

    HistoryStrip {
        width: parent.width
        history: progress.history
        page: progress.page
        pageCount: progress.pageCount
        onPreviousRequested: progress.previousPageRequested()
        onNextRequested: progress.nextPageRequested()
    }

    // The unit of the numbers in the discs. Kept out of each disc so four digits
    // still fit inside a 32px circle.
    Text {
        width: parent.width
        horizontalAlignment: Text.AlignHCenter
        visible: progress.history.length > 0
        text: "Kilometres per session"
        textFormat: Text.PlainText
        color: Util.alpha(Color.foreground, 0.45)
        font.family: Style.font.family
        font.pixelSize: 8
    }

    // The same three statuses as the discs above, in words.
    Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(12)

        Repeater {
            model: [
                { value: 1, text: "1-2 days" },
                { value: 0, text: "3 days" },
                { value: -1, text: "4 or more days" }
            ]

            Row {
                id: legendItem
                required property var modelData
                spacing: 4

                Rectangle {
                    width: 10
                    height: 10
                    radius: 5
                    color: Status.colorForValue(legendItem.modelData.value)
                    anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                    text: legendItem.modelData.text
                    textFormat: Text.PlainText
                    color: Util.alpha(Color.foreground, 0.6)
                    font.family: Style.font.family
                    font.pixelSize: 9
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }
    }

    Text {
        width: parent.width
        visible: progress.activityError !== ""
        text: progress.activityError
        textFormat: Text.PlainText
        color: Util.alpha(Color.foreground, 0.7)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
    }
}
