import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Back up, restore and show the folder, from the bar icon's right-click menu.
//
// The file chooser is a separate process, /usr/bin/zenity, and deliberately not
// Qt's own FileDialog. QtQuick.Dialogs pulls the GTK file chooser and GIO into
// this process, and a crash inside the picker would take the whole shell down
// with it. As a separate process the chooser is a window that can fail on its
// own.
//
// The chooser prints the path the user picked on stdout. That is data, never a
// command: it is length-checked here, and the helper then walks it from the
// root and validates every component before anything is read or written. No
// shell is involved, and every argument is its own element of the argv array.
Item {
    id: menu

    required property Item anchorItem
    required property QtObject bar
    required property bool canUseFiles
    // The folder the activity lives in, handed in rather than rebuilt here so
    // the menu and the panel cannot disagree about where that is.
    required property string stateDir

    // What the panel last reported, passed in as a binding. Never assigned here:
    // writing to a bound property drops the binding, and the menu would then
    // never show a second result.
    required property string resultNotice

    // Only this file writes this one, for the failures that belong to the menu
    // itself rather than to a backup or a restore.
    property string localNotice: ""

    readonly property string shownNotice: localNotice !== "" ? localNotice : resultNotice

    // A result from the panel supersedes whatever the menu was saying about its
    // own failure, so a stale "could not be opened" cannot sit on top of the
    // success that followed it.
    onResultNoticeChanged: {
        if (resultNotice !== "") localNotice = ""
    }

    // The bar keeps a single popout and closes the previous owner when a new
    // one opens. This owner is not the panel: the panel's own close() would
    // hide the panel, and the two have to be different owners so opening the
    // menu dismisses the panel and opening the panel dismisses the menu.
    property bool opened: false

    // Which row asked for a file: "export" or "restore". Set here from a fixed
    // string in this file, never from anything a caller passes in.
    property string choosing: ""

    // What the chooser printed, and whether it printed too much to be a path.
    property string chosenBuffer: ""
    property bool chosenOverflow: false

    // A ceiling on one line of chooser output. The helper applies its own
    // limits; this stops a runaway process filling memory before it does.
    readonly property int maxPathChars: 4096

    readonly property string homeDir: Quickshell.env("HOME")

    // The fallback is the documented default, used only until the first status
    // has told the panel where the helper actually keeps its files.
    readonly property string folder: stateDir !== "" ? stateDir
        : Quickshell.env("HOME") + "/.local/state/owalky"

    readonly property bool chooserBusy: choosing !== ""

    // ---- is there a chooser at all ----
    //
    // Measured, not assumed: a Process whose binary is missing fires neither
    // onStarted nor onExited, it only logs a warning. Without this probe a
    // missing zenity would leave the rows dead until the deadline below, ten
    // minutes later. FileView is no help either: with preload off and nothing
    // watched it reports nothing at all for either a present or absent path.
    property bool chooserAvailable: false
    // Set once the probe has answered either way, so the rows and the message
    // below them do not flicker through "disabled" on the first frame.
    property bool chooserChecked: false

    Process {
        id: chooserProbe
        // --version starts and exits immediately, so this is the cheapest
        // honest way to ask whether the binary is there.
        command: ["/usr/bin/zenity", "--version"]
        onStarted: menu.markChecked(true)
        onExited: menu.markChecked(true)
    }

    // If neither signal arrived, the process never started. Two seconds is
    // generous for a --version.
    Timer {
        id: chooserProbeDeadline
        interval: 2000
        onTriggered: menu.markChecked(false)
    }

    // Set once the probe has answered, either way.
    function markChecked(available) {
        chooserProbeDeadline.stop()
        chooserAvailable = available
        chooserChecked = true
    }

    // owalky_activity_<today>.json, the same shape the helper writes. The date
    // is in the name so a backup taken on another day cannot land on today's.
    function suggestedName() {
        var now = new Date()
        function pad(n) { return (n < 10 ? "0" : "") + n }
        return "owalky_activity_" + now.getFullYear() + "-" + pad(now.getMonth() + 1)
                + "-" + pad(now.getDate()) + ".json"
    }

    function openExport() {
        // `!menu.opened` and not `menu.opened`: a row is only clickable while
        // the card is open, so testing it the other way round made this return
        // every time and left both rows dead.
        if (!menu.opened || !menu.rowsLive) return
        menu.opened = false
        menu.startChooser("export")
    }

    function openRestore() {
        if (!menu.opened || !menu.rowsLive) return
        menu.opened = false
        menu.startChooser("restore")
    }

    // ---- showing the folder ----
    //
    // Needs no chooser and no idle workout, so it is live whenever the menu is.
    // `opening` is only there to grey the row out while the handler runs, and to
    // give a check something to see.
    property bool opening: false

    function openLocation() {
        if (!menu.opened || menu.opening) return
        menu.opened = false
        menu.opening = true
        openProc.running = true
        openDeadline.restart()
    }

    Process {
        id: openProc
        // A fixed absolute executable in an argv array, and the plugin's own
        // folder, which begins with "/" and so cannot be read as an option. No
        // shell anywhere near it.
        command: ["/usr/bin/xdg-open", menu.folder]
        onStarted: openDeadline.stop()
        onExited: function (code) {
            openDeadline.stop()
            menu.opening = false
            // The handler hands the folder to the desktop and exits straight
            // away, so a non-zero code means it did not take it. The path goes in
            // the notice either way, since a menu that cannot open a file
            // manager is no use without it.
            if (code !== 0) menu.localNotice = "Nothing opened it. The folder is " + menu.folder + "."
        }
    }

    // Same lesson as the chooser probe: a Process whose binary is missing fires
    // neither onStarted nor onExited. Without this the row would go dead with no
    // word about why.
    Timer {
        id: openDeadline
        interval: 3000
        onTriggered: {
            menu.opening = false
            menu.localNotice = "xdg-open could not be run. The folder is " + menu.folder + "."
        }
    }

    // What the rows offer right now: only when nothing is running, nothing is
    // already being chosen, and there is a chooser to choose with.
    readonly property bool rowsLive: canUseFiles && !chooserBusy && chooserChecked
                                       && chooserAvailable

    function startChooser(verb) {
        menu.localNotice = ""
        menu.chosenBuffer = ""
        menu.chosenOverflow = false
        menu.choosing = verb
        chooserProc.running = true
    }

    // The one path the chooser printed, or "" when there is nothing usable.
    function chosenPath() {
        if (menu.chosenOverflow) return ""
        // Trimmed here so the helper is handed one bare path. It is checked
        // properly there; this only keeps the obvious non-answers out.
        var path = menu.chosenBuffer.replace(/[\r\n]/g, "")
        if (path === "" || path.charAt(0) !== "/") return ""
        return path
    }

    Process {
        id: chooserProc

        // No shell, so each element is literal. A zenity that is not installed
        // fails to start, which onExited below turns into a message rather than
        // a row that does nothing. No --confirm-overwrite: zenity deprecated it
        // and asks about an existing file on its own.
        command: menu.choosing === "export"
            ? ["/usr/bin/zenity", "--file-selection", "--save",
               "--title", "Save your activity", "--file-filter", "JSON files | *.json",
               "--filename", menu.homeDir + "/" + menu.suggestedName()]
            : ["/usr/bin/zenity", "--file-selection",
               "--title", "Restore your activity", "--file-filter", "JSON files | *.json"]

        stdout: SplitParser {
            splitMarker: ""
            onRead: function (chunk) {
                if (menu.chosenBuffer.length + chunk.length <= menu.maxPathChars) {
                    menu.chosenBuffer += chunk
                    return
                }
                // More than a path can be. Keep what fits and let the chooser be
                // reported as unhelpful, rather than growing without bound.
                menu.chosenBuffer += chunk.substring(
                    0, Math.max(0, menu.maxPathChars - menu.chosenBuffer.length))
                menu.chosenOverflow = true
            }
        }

        onStarted: chooserDeadline.restart()

        onExited: function (code) {
            chooserDeadline.stop()
            var verb = menu.choosing
            // Read both before clearing them. chosenPath() refuses an
            // overflowing buffer by consulting chosenOverflow, so clearing that
            // first would quietly turn "too much output" into "no path".
            var overflow = menu.chosenOverflow
            var path = overflow ? "" : menu.chosenPath()
            menu.choosing = ""
            menu.chosenBuffer = ""
            menu.chosenOverflow = false

            if (overflow) {
                menu.localNotice = "The file chooser said more than a file path."
                return
            }
            // 0 picked, 1 cancelled, 5 timed out. Both of those are the user's
            // own doing and are not worth a message.
            if (code === 1 || code === 5) return
            if (code !== 0 || path === "") {
                menu.localNotice = "The file chooser could not be opened."
                return
            }
            if (verb === "export") menu.exportRequested(path)
            else menu.restoreRequested(path)
        }
    }

    // A backstop, not a timeout the user feels. Choosing a file is interactive
    // and takes as long as it takes; this only stops a chooser that never exits
    // from leaving the rows dead for the rest of the session.
    Timer {
        id: chooserDeadline
        interval: 600000
        onTriggered: {
            chooserProc.signal(15)          // to the chooser, not to the shell
            menu.choosing = ""
            menu.localNotice = "The file chooser did not answer."
        }
    }

    // One probe at load, then the rows are live or they explain themselves.
    Component.onCompleted: {
        chooserProbe.running = true
        chooserProbeDeadline.restart()
    }

    Component.onDestruction: {
        chooserProbeDeadline.stop()
        chooserDeadline.stop()
        // A chooser still open outlives the menu otherwise, holding its window
        // on screen after the plugin is gone.
        if (chooserProc.running) chooserProc.signal(15)
    }

    QtObject {
        id: menuOwner
        function close() { menu.opened = false }
    }

    signal exportRequested(string path)
    signal restoreRequested(string path)

    PopupCard {
        id: card
        anchorItem: menu.anchorItem
        owner: menuOwner
        bar: menu.bar
        open: menu.opened
        padding: Style.space(8)
        contentWidth: card.fittedContentWidth(Style.space(260))
        contentHeight: card.fittedContentHeight(rows.implicitHeight)

        Column {
            id: rows
            width: parent.width
            spacing: 0

            MenuRow {
                label: "Backup"
                onTriggered: menu.openExport()
            }

            MenuRow {
                label: "Restore"
                onTriggered: menu.openRestore()
            }

            // Below Restore, and independent of the chooser: this one only needs
            // a file manager, so it stays live even where the two above cannot
            // run.
            MenuRow {
                label: "Location"
                needsChooser: false
                onTriggered: menu.openLocation()
            }

            // Why the two rows above are dead, when they are. Only ever one of
            // these is true at a time, and each says the thing to do about it.
            Text {
                width: parent.width
                visible: menu.chooserChecked && !menu.chooserAvailable
                leftPadding: Style.space(12)
                rightPadding: Style.space(12)
                bottomPadding: Style.space(6)
                text: "Back up and restore need zenity (package: zenity)."
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Util.alpha(Color.foreground, 0.7)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
            }

            Text {
                width: parent.width
                visible: menu.chooserBusy
                leftPadding: Style.space(12)
                rightPadding: Style.space(12)
                bottomPadding: Style.space(6)
                text: "Waiting for the file chooser."
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Util.alpha(Color.foreground, 0.7)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
            }

            Item {
                width: parent.width
                visible: menu.shownNotice !== ""
                implicitHeight: visible ? noticeBlock.implicitHeight : 0

                Column {
                    id: noticeBlock
                    width: parent.width
                    spacing: 0

                    Item {
                        width: parent.width
                        implicitHeight: Style.space(11)

                        Rectangle {
                            anchors.left: parent.left
                            anchors.leftMargin: Style.space(10)
                            anchors.right: parent.right
                            anchors.rightMargin: Style.space(10)
                            anchors.verticalCenter: parent.verticalCenter
                            height: 1
                            color: Color.popups.border
                            opacity: 0.45
                        }
                    }

                    Text {
                        width: parent.width
                        leftPadding: Style.space(12)
                        rightPadding: Style.space(12)
                        topPadding: Style.space(4)
                        bottomPadding: Style.space(6)
                        text: menu.shownNotice
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                        color: Util.alpha(Color.foreground, 0.7)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                    }
                }
            }
        }
    }

    component MenuRow: Item {
        id: row

        required property string label
        signal triggered()

        // A row that needs the file chooser follows its availability. One that
        // does not, like Location, is live whenever the menu is.
        property bool needsChooser: true
        readonly property bool live: menu.opened
                                       && !menu.opening
                                       && (needsChooser ? menu.rowsLive : true)

        width: rows.width
        implicitHeight: Style.space(30)
        opacity: live ? 1 : 0.45

        Rectangle {
            anchors.fill: parent
            radius: Math.max(2, Style.cornerRadius)
            color: rowMouse.containsMouse && row.live
                   ? Style.hoverFillFor(Color.foreground, Color.foreground)
                   : "transparent"
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: Style.space(12)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(12)
            text: row.label
            textFormat: Text.PlainText
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
        }

        MouseArea {
            id: rowMouse
            anchors.fill: parent
            enabled: row.live
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: row.triggered()
        }
    }
}
