import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

/*
 * Owalky: a bar widget and panel for an FTMS walking pad.
 *
 * The panel never speaks Bluetooth and never reads a file. It runs the helper next
 * to this file as an argv array and reads one bounded line of output per call.
 *
 * Helper output is untrusted input. A Text without textFormat sits on
 * Text.AutoText, which renders a string that looks like markup as rich text, and
 * rich text can load an image from a URL the string chooses.
 */
Panel {
    id: root

    moduleName: "h1st0ry3d.owalky"
    ipcTarget: "h1st0ry3d.owalky"
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    readonly property string interpreter: "/usr/bin/python3"
    readonly property string helperPath: decodeURIComponent(
        Qt.resolvedUrl("owalky_helper.py").toString().replace("file://", ""))
    readonly property var helperEnvironment: ({ "PATH": "/usr/bin:/bin", "LANG": "C" })

    readonly property int pollInterval: 1500
    readonly property int outputLimit: 4096
    readonly property int logLimit: 8192
    readonly property int macLength: 17

    property string mac: ""
    property string macInput: ""
    property double currentSpeed: 1.0
    property double liveSpeed: 0.0
    property double distance: 0
    property bool connected: false
    property bool running: false
    property bool paused: false
    property bool daemon: false
    property string errorText: ""
    property string logPath: ""
    property string message: ""
    property string logTail: ""
    property string pendingConfig: ""

    // A 1.5s poll, and the config write that rides along with an action, must not
    // disable the controls. Only a belt command does.
    property bool busy: commandProcess.running

    // -- untrusted input
    /*
     * Strip what a rich-text renderer could act on, then cap the length, for the
     * components the shell renders itself and where textFormat cannot be set.
     */
    function plain(value, limit) {
        var source = String(value === undefined || value === null ? "" : value)
        var cleaned = ""
        for (var index = 0; index < source.length && cleaned.length < limit; index++) {
            var character = source[index]
            var code = source.charCodeAt(index)
            var printable = code >= 0x20 && code !== 0x7f && code < 0xa0
            var notSurrogate = !(code >= 0xd800 && code <= 0xdfff)
            if (printable && notSurrogate && character !== "<" && character !== ">" && character !== "&")
                cleaned += character
        }
        return cleaned
    }

    function validMac(value) {
        return /^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/.test(String(value).trim())
    }

    function clampSpeed(value) {
        var speed = Number(value)
        if (!isFinite(speed)) return root.currentSpeed
        speed = Math.round(speed * 10) / 10
        return Math.min(12.0, Math.max(0.5, speed))
    }

    // -- helper invocations
    // Belt commands pass the MAC, which the helper validates again.
    function beltCommand(name, trailing) {
        return [root.interpreter, "-I", root.helperPath, "--mac", root.mac, name]
            .concat(trailing || [])
    }

    // Local commands need no address.
    function localCommand(name, trailing) {
        return [root.interpreter, "-I", root.helperPath, name].concat(trailing || [])
    }

    // One bounded call returns the whole state as one JSON document. Anything
    // that does not parse as one is discarded.
    function refresh() {
        if (statusPoll.running) return
        statusPoll.buffer = ""
        statusPoll.command = root.localCommand("status")
        statusPoll.running = true
    }

    function run(process, command) {
        if (process.running) return
        process.buffer = ""
        process.command = command
        process.running = true
    }

    function send(command) {
        if (commandProcess.running) return
        if (!root.mac) {
            root.message = "Set the pad's BLE MAC address first"
            return
        }
        root.message = command.join(" ") + " …"
        run(commandProcess, root.beltCommand(command[0], command.slice(1)))
        stateTimer.restart()
    }

    function sendBelt(name) {
        send([name])
    }

    function setSpeed(value) {
        var speed = root.clampSpeed(value)
        root.currentSpeed = speed
        root.liveSpeed = speed
        root.saveConfig()
        if (root.running && !root.paused) send(["speed", speed.toFixed(1)])
    }

    function saveConfig() {
        root.pendingConfig = JSON.stringify({ mac: root.mac, lastSpeed: root.currentSpeed }) + "\n"
        flushConfig()
    }

    // The helper reads the payload and exits, but a write already in flight would
    // drop the next one, so the newest payload waits for the current write to end.
    function flushConfig() {
        if (configProcess.running || root.pendingConfig === "") return
        configProcess.payload = root.pendingConfig
        root.pendingConfig = ""
        run(configProcess, root.localCommand("config-set"))
    }

    function readLog() {
        run(logProcess, root.localCommand("log", ["60"]))
    }

    // These work before a MAC is configured, so they skip the belt path.
    function sendLocal(command) {
        if (commandProcess.running) return
        root.message = command.join(" ") + " …"
        run(commandProcess, root.localCommand(command[0], command.slice(1)))
        logTimer.restart()
    }

    // -- state
    function applyState(document) {
        if (document.mac !== undefined && String(document.mac) !== root.mac) {
            root.mac = String(document.mac)
            root.macInput = root.mac
        }
        if (document.lastSpeed !== undefined) root.currentSpeed = root.clampSpeed(document.lastSpeed)
        if (document.speed !== undefined && isFinite(Number(document.speed)))
            root.liveSpeed = Number(document.speed)
        if (document.distance !== undefined && isFinite(Number(document.distance)))
            root.distance = Number(document.distance)
        root.daemon = document.daemon === true
        root.connected = document.connected === true
        root.running = document.running === true
        root.paused = document.paused === true
        root.errorText = root.plain(document.error, 120)
        root.logPath = root.plain(document.log, 200)
    }

    function statusLabel() {
        if (root.errorText) return root.errorText
        if (!root.mac) return "Set MAC to connect"
        if (root.paused) return "Paused"
        if (root.running) return "Running " + root.liveSpeed.toFixed(1) + " km/h"
        if (root.connected) return "Connected"
        if (root.daemon) return "Connecting …"
        return "Idle"
    }

    // -- processes
    /*
     * An empty split marker hands over raw chunks, so an oversized reply can be
     * cut off. A line-buffered parser has to hold the whole line first.
     */
    Process {
        id: statusPoll
        running: false
        property string buffer: ""
        property string failure: ""
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { statusPoll.collect(chunk) }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (statusPoll.failure.length <= root.outputLimit) statusPoll.failure += chunk
            }
        }
        environment: root.helperEnvironment
        clearEnvironment: true
        function collect(chunk) {
            buffer += chunk
            if (buffer.length > root.outputLimit) {
                buffer = ""
                signal(15)
                statusKillTimer.start()
            }
        }
        onExited: function(code) {
            if (code === 0 && buffer !== "") {
                var document = null
                try { document = JSON.parse(buffer.trim()) } catch (error) { document = null }
                if (document && typeof document === "object") root.applyState(document)
            } else if (code !== 0 && failure !== "") {
                root.errorText = root.plain(failure.trim(), 120)
            }
            buffer = ""
            failure = ""
        }
    }

    Process {
        id: commandProcess
        running: false
        property string buffer: ""
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { commandProcess.collect(chunk) }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { commandProcess.collect(chunk) }
        }
        environment: root.helperEnvironment
        clearEnvironment: true
        function collect(chunk) {
            buffer += chunk
            if (buffer.length > root.outputLimit) {
                buffer = ""
                signal(15)
                commandKillTimer.start()
            }
        }
        onExited: function(code) {
            var text = root.plain(buffer.trim(), 400)
            if (text) root.message = text
            else if (code !== 0) root.message = "the helper exited with " + code
            buffer = ""
            root.refresh()
        }
    }

    Process {
        id: configProcess
        running: false
        stdinEnabled: true
        property string payload: ""
        property string buffer: ""
        // Collected and dropped. An unset stream inherits the shell's stdout.
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { configProcess.collect(chunk) }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { configProcess.collect(chunk) }
        }
        environment: root.helperEnvironment
        clearEnvironment: true
        function collect(chunk) {
            buffer = (buffer + chunk).slice(0, 200)
        }
        onStarted: {
            configProcess.write(configProcess.payload)
            configWatchdog.restart()
        }
        onExited: function(code) {
            payload = ""
            if (code !== 0) root.message = "could not save the configuration: " + root.plain(buffer.trim(), 200)
            buffer = ""
            flushConfig()
        }
    }

    Process {
        id: logProcess
        running: false
        property string buffer: ""
        property string failure: ""
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                logProcess.buffer += chunk
                if (logProcess.buffer.length > root.logLimit) {
                    logProcess.buffer = ""
                    logProcess.signal(15)
                    logKillTimer.start()
                }
            }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (logProcess.failure.length <= 200) logProcess.failure += chunk
            }
        }
        environment: root.helperEnvironment
        clearEnvironment: true
        onExited: function(code) {
            root.logTail = code === 0 ? root.plain(buffer.trim(), root.logLimit) : ""
            buffer = ""
            failure = ""
        }
    }

    // The helper answers config-set in well under a second. This is the backstop
    // that keeps a wedged write from holding a process handle open forever.
    Timer {
        id: configWatchdog
        interval: 5000
        onTriggered: if (configProcess.running) configProcess.signal(15)
    }

    Timer {
        id: logTimer
        interval: 600
        repeat: false
        onTriggered: root.readLog()
    }

    Timer { id: statusKillTimer; interval: 2000; onTriggered: statusPoll.signal(9) }
    Timer { id: commandKillTimer; interval: 2000; onTriggered: commandProcess.signal(9) }
    Timer { id: logKillTimer; interval: 2000; onTriggered: logProcess.signal(9) }

    Timer {
        id: stateTimer
        interval: root.pollInterval
        repeat: false
        onTriggered: root.refresh()
    }

    Timer {
        id: pollTimer
        interval: root.pollInterval
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    onOpenedChanged: if (opened) { root.refresh(); root.readLog() }

    Component.onCompleted: root.refresh()

    // Nothing started here may outlive the panel. The daemon is exempt: it holds
    // the BLE link across a shell reload.
    Component.onDestruction: {
        if (commandProcess.running) commandProcess.signal(15)
        if (statusPoll.running) statusPoll.signal(15)
        if (logProcess.running) logProcess.signal(15)
        if (configProcess.running) configProcess.signal(15)
    }

    // -- bar button
    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "󰑮"
        onPressed: function(pressed) {
            if (pressed === Qt.RightButton) root.sendBelt("stop")
            else root.toggle()
        }

        Rectangle {
            visible: root.running
            width: 6
            height: 6
            radius: 3
            color: root.paused ? "#eab308" : "#22c55e"
            anchors { right: parent.right; top: parent.top; margins: 2 }
            border.color: Util.alpha(Color.foreground, 0.2)
        }

        Rectangle {
            visible: !root.running && root.connected
            width: 6
            height: 6
            radius: 3
            color: "#3b82f6"
            anchors { right: parent.right; top: parent.top; margins: 2 }
            border.color: Util.alpha(Color.foreground, 0.2)
        }
    }

    // -- panel
    KeyboardPanel {
        id: dropdown
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: keyCatcher
        contentWidth: dropdown.fittedContentWidth(Style.space(380))
        contentHeight: dropdown.fittedContentHeight(col.implicitHeight, Style.space(580))

        PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent
            onCloseRequested: root.close()
            onTabRequested: function(direction) { root.switchPanel(direction) }

            Flickable {
                anchors.fill: parent
                contentWidth: width
                contentHeight: col.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: col
                    width: parent.width
                    spacing: Style.space(12)
                    topPadding: Style.space(12)
                    bottomPadding: Style.space(12)

                    PanelHero {
                        width: parent.width
                        title: "Owalky"
                        // The shell renders this itself, so strip and cap first.
                        meta: root.plain(root.statusLabel(), 80)
                        foreground: Color.foreground
                        fontFamily: Style.font.family
                        iconComponent: Component {
                            Text {
                                text: "󰑮"
                                textFormat: Text.PlainText
                                color: Color.foreground
                                font.family: Style.font.family
                                font.pixelSize: Style.font.display
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }

                    PanelSeparator { foreground: Color.foreground }

                    Column {
                        width: parent.width
                        spacing: Style.space(8)

                        Text {
                            text: "BLE MAC address"
                            textFormat: Text.PlainText
                            color: Util.alpha(Color.foreground, 0.7)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                        }

                        Row {
                            width: parent.width
                            spacing: Style.space(8)

                            TextField {
                                width: parent.width - connectButton.width - Style.space(8)
                                placeholderText: "AA:BB:CC:DD:EE:FF"
                                // the address grammar is 17 characters
                                maximumLength: root.macLength
                                text: root.macInput
                                onTextChanged: root.macInput = text
                                font.family: Style.font.family
                                onAccepted: connectButton.clicked()
                            }

                            Button {
                                id: connectButton
                                text: root.connected ? "Disconnect" : "Connect"
                                enabled: !root.busy && root.validMac(root.macInput)
                                onClicked: {
                                    if (!root.validMac(root.macInput)) {
                                        root.message = "That is not a BLE MAC address"
                                        return
                                    }
                                    root.mac = root.macInput.trim().toUpperCase()
                                    root.saveConfig()
                                    if (root.connected) {
                                        root.sendBelt("disconnect")
                                    } else {
                                        root.connected = true
                                        root.message = "connecting …"
                                        root.sendBelt("connect")
                                        stateTimer.restart()
                                    }
                                }
                            }
                        }

                        Text {
                            width: parent.width
                            text: root.connected
                                ? "● Connected to " + root.mac
                                : "○ Not connected - power-cycle the pad, it advertises for about 30 seconds"
                            textFormat: Text.PlainText
                            color: root.connected ? "#22c55e" : Util.alpha(Color.foreground, 0.6)
                            font.family: Style.font.family
                            font.pixelSize: 9
                            wrapMode: Text.WordWrap
                        }
                    }

                    PanelSeparator { foreground: Color.foreground }

                    Column {
                        visible: root.connected && root.running
                        width: parent.width
                        spacing: Style.space(8)

                        Text {
                            width: parent.width
                            horizontalAlignment: Text.AlignHCenter
                            text: (root.liveSpeed > 0 ? root.liveSpeed : root.currentSpeed).toFixed(1) + " km/h"
                            textFormat: Text.PlainText
                            color: root.running ? Color.accent : Color.foreground
                            font.family: Style.font.family
                            font.pixelSize: 28
                            font.bold: true
        }

                        Row {
                            anchors.horizontalCenter: parent.horizontalCenter
                            spacing: Style.space(8)
                            Button { text: "-0.5"; enabled: !root.busy; onClicked: root.setSpeed(root.currentSpeed - 0.5) }
                            Button { text: "-0.1"; enabled: !root.busy; onClicked: root.setSpeed(root.currentSpeed - 0.1) }
                            Button { text: "+0.1"; enabled: !root.busy; onClicked: root.setSpeed(root.currentSpeed + 0.1) }
                            Button { text: "+0.5"; enabled: !root.busy; onClicked: root.setSpeed(root.currentSpeed + 0.5) }
                        }

                        Flow {
                            width: parent.width
                            spacing: Style.space(6)
                            Repeater {
                                model: ["1.0", "2.0", "3.0", "4.0", "5.0", "6.0", "8.0", "10.0"]
                                Button {
                                    required property string modelData
                                    text: modelData
                                    enabled: !root.busy
                                    onClicked: root.setSpeed(Number(modelData))
                                }
                            }
                        }
                    }

                    Row {
                        width: parent.width
                        spacing: Style.space(8)

                        Button {
                            visible: root.connected && !root.running
                            width: parent.width
                            text: "Start"
                            enabled: !root.busy
                            onClicked: { root.saveConfig(); root.sendBelt("start") }
                        }

                        Button {
                            visible: root.connected && root.running && !root.paused
                            width: (parent.width - Style.space(8)) / 2
                            text: "Pause"
                            enabled: !root.busy
                            onClicked: root.sendBelt("pause")
                        }

                        Button {
                            visible: root.connected && root.running && root.paused
                            width: (parent.width - Style.space(8)) / 2
                            text: "Resume"
                            enabled: !root.busy
                            onClicked: root.sendBelt("resume")
                        }

                        Button {
                            visible: root.connected && root.running
                            width: (parent.width - Style.space(8)) / 2
                            text: "Stop"
                            enabled: !root.busy
                            onClicked: root.sendBelt("stop")
                        }

                        Text {
                            visible: !root.connected
                            width: parent.width
                            horizontalAlignment: Text.AlignHCenter
                            text: "Power-cycle the pad, then press Connect"
                            textFormat: Text.PlainText
                            color: Util.alpha(Color.foreground, 0.6)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.bodySmall
                            wrapMode: Text.WordWrap
                        }
                    }

                    Text {
                        visible: root.message !== ""
                        width: parent.width
                        text: root.message
                        textFormat: Text.PlainText
                        color: Util.alpha(Color.foreground, 0.7)
                        font.family: Style.font.family
                        font.pixelSize: 9
                        wrapMode: Text.WordWrap
                    }

                    PanelSeparator { foreground: Color.foreground }

                    Column {
                        width: parent.width
                        spacing: Style.space(4)

                        Text {
                            width: parent.width
                            text: "Debug log - " + root.logPath
                            textFormat: Text.PlainText
                            color: Util.alpha(Color.foreground, 0.6)
                            font.family: Style.font.family
                            font.pixelSize: 9
                            elide: Text.ElideMiddle
                        }

                        Rectangle {
                            width: parent.width
                            height: 90
                            radius: Style.cornerRadius
                            color: Util.alpha(Color.foreground, 0.06)
                            border.color: Util.alpha(Color.foreground, 0.12)
                            clip: true

                            Flickable {
                                anchors.fill: parent
                                anchors.margins: 6
                                contentWidth: logText.width
                                contentHeight: logText.height
                                clip: true
                                boundsBehavior: Flickable.StopAtBounds

                                Text {
                                    id: logText
                                    width: 360
                                    text: root.logTail || "no log entries yet"
                                    textFormat: Text.PlainText
                                    color: Util.alpha(Color.foreground, 0.75)
                                    font.family: Style.font.family
                                    font.pixelSize: 8
                                    wrapMode: Text.Wrap
                                }
                            }
                        }

                        Row {
                            spacing: Style.space(6)
                            Button {
                                text: "Refresh"
                                onClicked: root.readLog()
                            }
                            Button {
                                text: "Clear log"
                                onClicked: root.sendLocal(["log-clear"])
                            }
                            Button {
                                text: "Copy path"
                                enabled: root.logPath !== ""
                                onClicked: {
                                    // fixed argv: the path cannot become a shell word
                                    Quickshell.execDetached(["/usr/bin/wl-copy", "--", root.logPath])
                                    root.message = "log path copied"
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
