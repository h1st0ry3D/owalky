import QtQuick
import QtQuick.Controls
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
    readonly property int macLength: 17

    property string mac: ""
    property string macInput: ""
    property double currentSpeed: 1.0
    property double liveSpeed: 0.0
    // The value the slider is being dragged to. Zero means "not dragging", and
    // the pad's minimum is 1.0, so it doubles as the flag.
    property double sliderPreview: 0.0
    property double distance: 0
    property bool connected: false
    property bool running: false
    property bool paused: false
    property bool daemon: false
    property string errorText: ""
    // The last failed command, shown in the hero line until something works.
    property string commandError: ""
    property string pendingConfig: ""
    // "connect" or "disconnect" while a link change is in flight, else empty.
    // The panel does not assume the outcome: it waits for the daemon to report.
    property string pending: ""
    property bool daemonSeen: false

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
        return Math.min(12.0, Math.max(1.0, speed))
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
            root.commandError = "Set the pad's BLE MAC address first"
            return
        }
        root.commandError = ""
        run(commandProcess, root.beltCommand(command[0], command.slice(1)))
        stateTimer.restart()
    }

    function sendBelt(name) {
        send([name])
    }

    function startLinkChange(name) {
        root.pending = name
        root.daemonSeen = false
        root.commandError = ""
        send([name])
        stateTimer.restart()
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


    // These work before a MAC is configured, so they skip the belt path.

    // -- state
    // A link change is finished when the daemon reports the state that was asked
    // for. Before the daemon appears, the first polls still report no daemon, so
    // a connect waits until one has been seen before it accepts that as a
    // failure.
    function settlePending(document) {
        if (root.pending === "") return
        var done = false
        if (root.pending === "connect") {
            if (document.daemon === true) root.daemonSeen = true
            done = document.connected === true
                || document.error
                || (root.daemonSeen && document.daemon === false)
        } else {
            // While a daemon winds down it reports connected=false, so wait for
            // the process to be gone as well before calling the disconnect over.
            done = document.daemon === false && document.connected === false
        }
        if (done) {
            root.pending = ""
            root.daemonSeen = false
        }
    }

    function applyState(document) {
        if (document.mac !== undefined && String(document.mac) !== root.mac) {
            root.mac = String(document.mac)
            root.macInput = root.mac
        }
        if (document.lastSpeed !== undefined && root.sliderPreview === 0) {
            var stored = root.clampSpeed(document.lastSpeed)
            if (stored !== root.currentSpeed) root.currentSpeed = stored
        }
        if (document.speed !== undefined && isFinite(Number(document.speed)))
            root.liveSpeed = Number(document.speed)
        if (document.distance !== undefined && isFinite(Number(document.distance)))
            root.distance = Number(document.distance)
        root.daemon = document.daemon === true
        root.connected = document.connected === true
        root.running = document.running === true
        root.paused = document.paused === true
        root.errorText = root.plain(document.error, 120)
        settlePending(document)
    }

    function statusLabel() {
        if (root.errorText) return root.errorText
        if (root.commandError) return root.plain(root.commandError, 80)
        if (root.pending === "connect") return "Connecting …"
        if (root.pending === "disconnect") return "Disconnecting …"
        if (!root.mac) return "Set MAC to connect"
        if (root.paused) return "Paused"
        if (root.running) return "Running " + root.liveSpeed.toFixed(1) + " km/h"
        if (root.connected) return "Connected"
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
            root.commandError = code === 0 ? "" : (text || "the helper exited with " + code)
            buffer = ""
            if (code !== 0 && root.pending !== "") {
                root.pending = ""
                root.daemonSeen = false
            }
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
            if (code !== 0) root.commandError = "could not save the configuration: "
            buffer = ""
            flushConfig()
        }
    }


    // The helper answers config-set in well under a second. This is the backstop
    // that keeps a wedged write from holding a process handle open forever.
    Timer {
        id: configWatchdog
        interval: 5000
        onTriggered: if (configProcess.running) configProcess.signal(15)
    }


    Timer { id: statusKillTimer; interval: 2000; onTriggered: statusPoll.signal(9) }
    Timer { id: commandKillTimer; interval: 2000; onTriggered: commandProcess.signal(9) }

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

    onOpenedChanged: if (opened) root.refresh()

    Component.onCompleted: root.refresh()

    // Nothing started here may outlive the panel. The daemon is exempt: it holds
    // the BLE link across a shell reload.
    Component.onDestruction: {
        if (commandProcess.running) commandProcess.signal(15)
        if (statusPoll.running) statusPoll.signal(15)
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
                                enabled: !root.busy && root.pending === "" && root.validMac(root.macInput)
                                onClicked: {
                                    if (!root.validMac(root.macInput)) {
                                        root.commandError = "That is not a BLE MAC address"
                                        return
                                    }
                                    root.mac = root.macInput.trim().toUpperCase()
                                    root.saveConfig()
                                    root.startLinkChange(root.connected ? "disconnect" : "connect")
                                }
                            }
                        }

                        Row {
                            visible: root.pending !== ""
                            spacing: Style.space(8)

                            BusyIndicator {
                                running: root.pending !== ""
                                implicitWidth: 11
                                implicitHeight: 11
                            }

                            Text {
                                width: parent.width - 19
                                text: root.pending === "connect"
                                    ? "Connecting to " + root.mac + " - the pad advertises for about 30 seconds after power-on"
                                    : "Disconnecting …"
                                textFormat: Text.PlainText
                                color: Util.alpha(Color.foreground, 0.7)
                                font.family: Style.font.family
                                font.pixelSize: 9
                                wrapMode: Text.WordWrap
                            }
                        }

                        Text {
                            visible: root.pending === ""
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
                        visible: root.pending === "" && root.connected && root.running
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

                        Row {
                            width: parent.width

                            Text {
                                width: parent.width / 2
                                text: "Target speed"
                                textFormat: Text.PlainText
                                color: Util.alpha(Color.foreground, 0.6)
                                font.family: Style.font.family
                                font.pixelSize: 9
                            }

                            Text {
                                width: parent.width / 2
                                horizontalAlignment: Text.AlignRight
                                text: (root.sliderPreview > 0 ? root.sliderPreview : root.currentSpeed).toFixed(1) + " km/h"
                                textFormat: Text.PlainText
                                color: Color.foreground
                                font.family: Style.font.family
                                font.pixelSize: 9
                            }
                        }

                        /*
                         * The shell's slider only snaps whole numbers, so the
                         * handlers round to half a kilometre. Dragging previews
                         * locally; the belt is written once, on release.
                         */
                        PanelSlider {
                            id: speedSlider
                            width: parent.width
                            bar: root.bar
                            minimum: 1.0
                            maximum: 12.0
                            step: 0.5
                            // one notch per half-kilometre, so a value can be aimed at
                            tickCount: 23
                            value: root.currentSpeed
                            onMoved: function(v) { root.sliderPreview = Math.round(v * 2) / 2 }
                            onReleased: function(v) {
                                root.sliderPreview = 0
                                root.setSpeed(Math.round(v * 2) / 2)
                            }
                        }

                        // The whole-kilometre labels, one under every other notch.
                        Item {
                            id: speedScale
                            width: parent.width
                            height: 11

                            Repeater {
                                model: 12
                                Text {
                                    required property int index
                                    // the scale is as wide as the track above it
                                    x: (parent.width - width) * (index / 11)
                                    text: index + 1
                                    textFormat: Text.PlainText
                                    color: Util.alpha(Color.foreground, 0.45)
                                    font.family: Style.font.family
                                    font.pixelSize: 8
                                }
                            }
                        }

                        Text {
                            width: parent.width
                            text: "Drag or click the scale for half-kilometre steps, 1.0 to 12.0 km/h. The step buttons move by 0.1, and the mouse wheel works here too."
                            textFormat: Text.PlainText
                            color: Util.alpha(Color.foreground, 0.5)
                            font.family: Style.font.family
                            font.pixelSize: 8
                            wrapMode: Text.WordWrap
                        }
                    }

                    Row {
                        width: parent.width
                        spacing: Style.space(8)

                        Button {
                            visible: root.pending === "" && root.connected && !root.running
                            width: parent.width
                            text: "Start"
                            enabled: !root.busy
                            onClicked: { root.saveConfig(); root.sendBelt("start") }
                        }

                        Button {
                            visible: root.pending === "" && root.connected && root.running && !root.paused
                            width: (parent.width - Style.space(8)) / 2
                            text: "Pause"
                            enabled: !root.busy
                            onClicked: root.sendBelt("pause")
                        }

                        Button {
                            visible: root.pending === "" && root.connected && root.running && root.paused
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
                }
            }
        }
    }
}
