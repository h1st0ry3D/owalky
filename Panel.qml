import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons
import qs.Ui
import "ui"
import "core/Progression.js" as Progression
import "core/Document.js" as Document

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
    // The activity document can hold 400 sessions, so its read gets its own,
    // larger ceiling. It is only fetched on open and after a session ends, never
    // on the 1.5s status poll.
    readonly property int activityLimit: 65536
    readonly property int macLength: 17
    readonly property int scanLimit: 8

    property string mac: ""
    property string macInput: ""
    property double currentSpeed: 1.0
    property double liveSpeed: 0.0
    // The stored speed is applied once, on the first idle status after load.
    // Later status polls must not overwrite the 1.0 a Start just selected.
    property bool speedLoaded: false
    // The value the slider is being dragged to. Zero means "not dragging", and
    // the pad's minimum is 1.0, so it doubles as the flag.
    property double sliderPreview: 0.0
    // Walk caps the belt at 6.0 km/h because the handle bar is down; run allows
    // the full range. The pad does not report which mode it is in, so this is
    // the user's choice and it is stored with the rest of the configuration.
    property string mode: "walk"
    readonly property double maxSpeed: mode === "run" ? 12.0 : 6.0
    property bool connected: false
    property bool running: false
    property bool paused: false
    // The workout the daemon is measuring, and its distance so far. Both come
    // from the daemon, so a shell reload does not restart the count.
    property bool sessionActive: false
    property int sessionDistance: 0
    property int sessionElapsed: 0
    property int sessionCalories: 0
    property bool sessionCaloriesEstimated: false

    // -- activity tracker
    property string lastTrainingDate: ""
    property int trainingDays: 0
    property var history: []
    property string activityError: ""
    // A double, not an int: Date.now() is epoch milliseconds and a QML int is
    // 32-bit, so an int property would wrap it to a date in 1970.
    property double nowMs: Date.now()
    readonly property int daysSinceLast: Progression.daysSince(root.lastTrainingDate, root.nowMs)
    readonly property bool everTrained: root.lastTrainingDate !== ""
    readonly property int statusValue: root.everTrained
        ? Progression.statusForDays(root.daysSinceLast) : Progression.ON_TRACK
    readonly property int pageCount: Math.max(1, Math.ceil(root.history.length / Progression.PAGE_SIZE))
    property int historyPage: 0
    // Clamped where it is read, so a shrinking history cannot point the strip
    // past the end.
    readonly property int page: Math.max(0, Math.min(root.historyPage, root.pageCount - 1))
    readonly property var lastSessionEntry: root.history.length > 0
        ? root.history[root.history.length - 1] : null
    readonly property int lastMeters: root.lastSessionEntry
        ? Progression.entryMeters(root.lastSessionEntry) : 0
    readonly property int lastSeconds: root.lastSessionEntry
        ? Progression.entrySeconds(root.lastSessionEntry) : 0
    readonly property int lastCalories: root.lastSessionEntry
        ? Progression.entryCalories(root.lastSessionEntry) : 0
    readonly property bool lastCaloriesEstimated: root.lastSessionEntry
        ? Progression.entryCaloriesEstimated(root.lastSessionEntry) : false
    property bool daemon: false
    property string errorText: ""
    // The last failed command, shown in the hero line until something works.
    property string commandError: ""
    property string pendingConfig: ""
    // "connect" or "disconnect" while a link change is in flight, else empty.
    // The panel does not assume the outcome: it waits for the daemon to report.
    property string pending: ""
    property bool daemonSeen: false
    // The pads a scan found, each {mac, name, rssi}. Empty until the user scans.
    property var foundPads: []
    property bool scanning: false
    property string scanMessage: ""
    // Where in the dot count's cycle the hero line is, indexing ``dotCounts``.
    property int dotsPhase: 0
    readonly property bool waiting: root.scanning || root.pending !== ""

    // A 1.5s poll, and the config write that rides along with an action, must not
    // disable the controls. Only a belt command does.
    property bool busy: commandProcess.running

    // The label in the bar while the belt is in use: the workout's kilometres.
    // Idle, the bar shows the day bubble instead.
    readonly property string barLabel: root.running || root.paused
        ? Progression.formatMeters(root.sessionDistance) : ""
    readonly property bool barShowsDistance: root.barLabel !== ""
    // The MAC and link controls are setup, not workout UI. Hide them while a
    // session is running, including the separator above them.
    readonly property bool connectionAreaVisible: !root.running && !root.sessionActive

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
        return Math.min(root.maxSpeed, Math.max(1.0, speed))
    }

    function setMode(mode) {
        if (mode !== "walk" && mode !== "run") return
        root.mode = mode
        root.saveConfig()
        if (root.currentSpeed > root.maxSpeed) {
            root.setSpeed(root.maxSpeed)
            root.commandError = "walk mode tops out at 6.0 km/h, the speed was lowered"
        } else if (!root.running) {
            root.commandError = ""
        }
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

    // The activity document is read on its own schedule: when the panel opens,
    // when it starts, and after a workout ends. The status poll stays small.
    function refreshActivity() {
        if (activityPoll.running) {
            activityPoll.queued = true
            return
        }
        activityPoll.buffer = ""
        activityPoll.command = root.localCommand("activity")
        activityPoll.running = true
    }

    function applyActivity(document) {
        root.lastTrainingDate = document.lastTrainingDate
        root.trainingDays = document.trainingDays
        root.history = document.history
        root.historyPage = root.pageCount - 1     // land on the newest sessions
        root.nowMs = Date.now()
        root.activityError = ""
    }

    function pagePrevious() { root.historyPage = root.page - 1 }
    function pageNext() { root.historyPage = root.page + 1 }

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

    // A scan is its own long-lived call, so it must not share the belt command's
    // process: an eleven-second Bluetooth scan would disable the controls and
    // delay a Stop for as long as it ran.
    function scanForPads() {
        // A live daemon owns the adapter for as long as it is alive, even while it
        // has not connected yet, so a scan would fight it rather than help.
        if (scanProcess.running || root.daemon) return
        root.scanning = true
        root.scanMessage = "Listening for pads …"
        run(scanProcess, root.localCommand("scan"))
        scanWatchdog.restart()
    }

    // Only the three fields the panel drew from the document are taken, and each
    // is validated here, so a padded document cannot push a row the user cannot
    // read or an address that is not one.
    function adoptScan(document) {
        if (!document || typeof document !== "object") return []
        var devices = document.devices
        if (!(devices instanceof Array)) return []
        // A pad advertises repeatedly, so one address can arrive more than once. The
        // rows are keyed by address rather than appended, so a pad is listed once
        // even if the helper's own merge is bypassed.
        var rows = {}
        var listed = 0
        for (var index = 0; index < devices.length && listed < root.scanLimit; index++) {
            var device = devices[index]
            if (!device || typeof device !== "object") continue
            var address = String(device.mac === undefined ? "" : device.mac).trim().toUpperCase()
            if (!root.validMac(address)) continue
            var sighting = {
                mac: address,
                name: root.plain(device.name, 32),
                // A pad without a reported strength sorts last, same as in the
                // helper, so the nearest one stays on top.
                rssi: isFinite(Number(device.rssi)) ? Number(device.rssi) : -999
            }
            var known = rows[address]
            if (known === undefined) {
                rows[address] = sighting
                listed++
                continue
            }
            if (sighting.rssi > known.rssi) known.rssi = sighting.rssi
            if (sighting.name !== "") known.name = sighting.name
        }
        var pads = Object.keys(rows).map(function(address) { return rows[address] })
        pads.sort(function(a, b) { return b.rssi - a.rssi || (a.mac < b.mac ? -1 : 1) })
        return pads
    }

    function padLabel(pad) {
        var strength = pad.rssi > -999 ? "  " + pad.rssi.toFixed(0) + " dBm" : ""
        return pad.name + "  " + pad.mac + strength
    }

    // Selecting a pad is the same as typing its address: it is validated, stored,
    // and the Connect button then acts on it. Nothing connects behind the user's
    // back, so a mis-tap cannot start a belt.
    function selectPad(pad) {
        if (!root.validMac(pad.mac)) {
            root.commandError = "That is not a BLE MAC address"
            return
        }
        root.mac = pad.mac
        root.macInput = pad.mac
        root.foundPads = []
        root.scanMessage = "Selected " + pad.name + " - press Connect"
        root.saveConfig()
    }

    function startLinkChange(name) {
        root.pending = name
        root.daemonSeen = false
        root.commandError = ""
        // A scan result is only a way to choose an address, so it goes away once
        // the link starts changing.
        root.foundPads = []
        root.scanMessage = ""
        send([name])
        stateTimer.restart()
    }

    function setSpeed(value) {
        var speed = root.clampSpeed(value)
        root.speedLoaded = true
        root.currentSpeed = speed
        root.liveSpeed = speed
        root.saveConfig()
        if (root.running && !root.paused) send(["speed", speed.toFixed(1)])
    }

    function saveConfig() {
        root.pendingConfig = JSON.stringify({
            mac: root.mac,
            lastSpeed: root.currentSpeed,
            mode: root.mode
        }) + "\n"
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
        if (document.mode !== undefined && document.mode !== root.mode) root.mode = document.mode
        if (document.mac !== undefined && String(document.mac) !== root.mac) {
            root.mac = String(document.mac)
            root.macInput = root.mac
        }
        if (!root.speedLoaded && document.running !== true && document.lastSpeed !== undefined) {
            root.currentSpeed = root.clampSpeed(document.lastSpeed)
            root.speedLoaded = true
        }
        if (document.speed !== undefined && isFinite(Number(document.speed)))
            root.liveSpeed = Number(document.speed)
        root.sessionActive = document.sessionActive === true
        if (root.sessionActive) {
            if (document.sessionDistance !== undefined && isFinite(Number(document.sessionDistance)))
                root.sessionDistance = Math.max(0, Math.floor(Number(document.sessionDistance)))
            if (document.sessionElapsed !== undefined && isFinite(Number(document.sessionElapsed)))
                root.sessionElapsed = Math.max(0, Math.floor(Number(document.sessionElapsed)))
            if (document.sessionCalories !== undefined && isFinite(Number(document.sessionCalories)))
                root.sessionCalories = Math.max(0, Math.floor(Number(document.sessionCalories)))
            root.sessionCaloriesEstimated = document.sessionCaloriesEstimated === true
        } else {
            root.sessionDistance = 0
            root.sessionElapsed = 0
            root.sessionCalories = 0
            root.sessionCaloriesEstimated = false
        }
        var wasRunning = root.running
        root.daemon = document.daemon === true
        root.connected = document.connected === true
        root.running = document.running === true
        root.paused = document.paused === true
        root.errorText = root.plain(document.error, 120)
        // The daemon records the workout as it stops, including when the link
        // dropped or another client sent the Stop. A running -> idle edge is the
        // signal that the history may have grown.
        if (wasRunning && !root.running) root.refreshActivity()
        settlePending(document)
    }

    /*
     * A growing ellipsis, so a wait that lasts several seconds reads as progress
     * rather than as a panel that has stopped.
     *
     * One, two, three, then back to one. The lap wraps on a two-dot step, which
     * is the visible jump; an earlier version eased back down through two dots
     * to avoid it, but the plain cycle is what was asked for and it reads as a
     * steady beat rather than a wobble.
     *
     * The label does get wider as it grows, and nothing else moves with it: the
     * hero line is left-aligned inside a column of fixed width and the panel's
     * own width is a constant, so the word stays where it is and only the dots
     * extend to its right. That was measured, not assumed.
     */
    readonly property var dotCounts: [1, 2, 3]

    function dots() {
        return ".".repeat(root.dotCounts[root.dotsPhase])
    }

    function waitingFor(label) {
        return label + " " + root.dots()
    }

    function statusLabel() {
        if (root.errorText) return root.errorText
        if (root.commandError) return root.plain(root.commandError, 80)
        if (root.scanning) return root.waitingFor("Scanning")
        if (root.pending === "connect") return root.waitingFor("Connecting")
        if (root.pending === "disconnect") return root.waitingFor("Disconnecting")
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

    // The activity document is larger than a status line and is read only when
    // it can have changed. A read asked for while one is in flight is queued so
    // the history is re-read after the write it was waiting for.
    Process {
        id: activityPoll
        running: false
        property string buffer: ""
        property string failure: ""
        property bool queued: false
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { activityPoll.collect(chunk) }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (activityPoll.failure.length <= 2048) activityPoll.failure += chunk
            }
        }
        environment: root.helperEnvironment
        clearEnvironment: true
        function collect(chunk) {
            buffer += chunk
            if (buffer.length > root.activityLimit) {
                buffer = ""
                signal(15)
                activityKillTimer.start()
            }
        }
        onExited: function(code) {
            if (code === 0 && buffer !== "") {
                root.applyActivity(Document.parse(buffer, Progression))
            } else if (code !== 0) {
                // The helper's own wording is the only thing the user can act
                // on; it is flattened here because the panel renders it as text.
                root.activityError = root.plain(failure.trim(), 120)
                    || "Could not read the activity file."
            }
            buffer = ""
            failure = ""
            if (activityPoll.queued) {
                activityPoll.queued = false
                activityPoll.running = true
            }
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
            // Start, Stop and Disconnect all change what the daemon is
            // measuring; a failed command still costs one small read and
            // cannot leave a stale history on screen.
            root.refreshActivity()
        }
    }

    // A successful scan prints one JSON line and, when it found nothing, a line
    // saying so for the command line. Only the document is shown here: the line
    // beside it already says to power-cycle the pad, and saying it twice reads
    // as two problems. A failed scan is the other way round, since there the
    // helper's own wording is the only thing the user can act on.
    function readScan(output, code) {
        // Split before stripping: plain() drops control characters, a newline
        // among them, so stripping first would weld the lines together.
        var lines = String(output === undefined || output === null ? "" : output).split("\n")
        for (var index = 0; index < lines.length; index++)
            lines[index] = root.plain(lines[index].trim(), 400)
        var document = null
        try { document = JSON.parse(lines[0]) } catch (error) { document = null }
        var parsed = document && typeof document === "object"
        var pads = code === 0 && parsed ? root.adoptScan(document) : []
        if (pads.length > 0)
            return { pads: pads, message: pads.length + (pads.length === 1 ? " pad found" : " pads found") }
        if (code !== 0) return { pads: [], message: lines.join(" ").trim() || "the scan failed" }
        // A success that printed no readable document is not an empty scan, it is
        // output this cannot use.
        if (!parsed) return { pads: [], message: "the scan printed nothing usable" }
        return { pads: [], message: "no pad is advertising" }
    }

    // One scan, one JSON document, then the process is gone. It is killed with
    // the others when the panel closes, so a scan cannot outlive it and leave the
    // Bluetooth adapter busy.
    Process {
        id: scanProcess
        running: false
        property string buffer: ""
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { scanProcess.collect(chunk) }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) { scanProcess.collect(chunk) }
        }
        environment: root.helperEnvironment
        clearEnvironment: true
        function collect(chunk) {
            buffer += chunk
            if (buffer.length > root.outputLimit) {
                buffer = ""
                signal(15)
                scanKillTimer.start()
            }
        }
        onExited: function(code) {
            root.scanning = false
            var result = readScan(buffer, code)
            buffer = ""
            root.foundPads = result.pads
            root.scanMessage = result.message
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
    Timer { id: scanKillTimer; interval: 2000; onTriggered: scanProcess.signal(9) }
    Timer { id: activityKillTimer; interval: 4000; onTriggered: activityPoll.signal(9) }

    // A scan is capped at 30 seconds by the helper. This is the backstop for the
    // case where the call does not come back at all, which a Bluetooth stack can
    // do when the adapter is being switched underneath it.
    Timer {
        id: scanWatchdog
        interval: 45000
        onTriggered: if (scanProcess.running) scanProcess.signal(15)
    }

    // Drives the moving ellipsis in the hero line. It only runs while something
    // is actually waiting, so an idle panel is not repainting four times a
    // second for nothing, and it resets the phase so each wait starts at one dot.
    Timer {
        id: dotsTimer
        interval: 380
        repeat: true
        running: root.waiting
        triggeredOnStart: false
        onTriggered: root.dotsPhase = (root.dotsPhase + 1) % root.dotCounts.length
    }

    onWaitingChanged: if (!root.waiting) root.dotsPhase = 0

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

    // The day bubble counts days, so it has to re-evaluate when the count
    // changes rather than at the shell's start. This re-arms from the clock each
    // tick, so a suspend or a clock change lands on the next boundary. The
    // hourly tick keeps nowMs close to the wall clock for the bar readout.
    Timer {
        interval: 3600000
        running: true
        repeat: true
        onTriggered: root.nowMs = Date.now()
    }

    Timer {
        id: dayRollover
        interval: Progression.msUntilNextDay(root.nowMs)
        running: true
        repeat: true
        onTriggered: {
            root.nowMs = Date.now()
            dayRollover.interval = Progression.msUntilNextDay(root.nowMs)
        }
    }

    onOpenedChanged: {
        if (!opened) return
        root.refresh()
        root.refreshActivity()
    }

    Component.onCompleted: {
        root.refresh()
        root.refreshActivity()
    }

    // Nothing started here may outlive the panel. The daemon is exempt: it holds
    // the BLE link across a shell reload.
    Component.onDestruction: {
        if (commandProcess.running) commandProcess.signal(15)
        if (statusPoll.running) statusPoll.signal(15)
        if (configProcess.running) configProcess.signal(15)
        if (scanProcess.running) scanProcess.signal(15)
        if (activityPoll.running) activityPoll.signal(15)
    }

    // -- bar button
    // The day bubble is the idle icon, as in the activity panel; while the belt
    // is in use the slot carries the workout's kilometres instead. The status
    // dot stays on top either way.
    Component {
        id: dayBubbleMark
        DayBubble {
            statusValue: root.statusValue
            daysSinceLast: root.daysSinceLast
            everTrained: root.everTrained
            // A 20px disc derives a label at the 8px floor, which is too small
            // to read at a glance in the bar, so the size is asked for here.
            labelSize: 9
            quietOnTrack: true
        }
    }

    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        // Four characters ("12.4") would not fit at the icon size, so the
        // longer label steps down inside the same 27px slot. While idle the
        // component above supplies the icon instead of this text.
        text: root.barLabel
        iconComponent: root.barShowsDistance ? null : dayBubbleMark
        // The icon slot is a fixed box and its 16px default is too small for
        // "12d" inside a disc.
        opticalSize: 20
        fontSize: root.barLabel.length > 3
            ? Math.round(Style.bar.iconFont * 0.95)
            : Math.round(Style.bar.iconFont * 1.15)
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
        contentHeight: dropdown.fittedContentHeight(col.implicitHeight, Style.space(680))

        PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent
            onCloseRequested: root.close()
            onTabRequested: function(direction) { root.switchPanel(direction) }
            // Left and right page the activity strip; up and down are the shell's.
            onMoveRequested: function(dx) {
                if (dx < 0) root.pagePrevious()
                else if (dx > 0) root.pageNext()
            }

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
                        id: hero
                        width: parent.width
                        title: "Owalky"
                        // The shell renders this itself, so strip and cap first.
                        meta: root.plain(root.statusLabel(), 80)
                        foreground: Color.foreground
                        fontFamily: Style.font.family
                        // The same day bubble as the bar's idle icon, so the
                        // panel and the bar count the same days.
                        iconComponent: Component {
                            DayBubble {
                                statusValue: root.statusValue
                                daysSinceLast: root.daysSinceLast
                                everTrained: root.everTrained
                                // The disc already carries the on-track/keep/drop
                                // colour; the tooltip spells out which is which.
                                tooltip: true
                                // The hero's icon slot sizes the item from its
                                // implicit size, so the size is asked for here.
                                implicitWidth: Math.round(Style.font.display * 1.25)
                                implicitHeight: Math.round(Style.font.display * 1.25)
                            }
                        }
                    }

                    PanelSeparator {
                        visible: root.connectionAreaVisible
                        foreground: Color.foreground
                    }

                    Column {
                        width: parent.width
                        visible: root.connectionAreaVisible
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
                                width: parent.width - connectButton.width - scanButton.width
                                    - Style.space(16)
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

                            // Disabled whenever a daemon is alive: it holds the adapter even while it
                            // is still looking for the pad.
                            Button {
                                id: scanButton
                                text: "Scan"
                                enabled: !root.busy && root.pending === "" && !root.daemon
                                onClicked: root.scanForPads()
                            }
                        }

                        // One row carries the scan's progress and its outcome, so the panel neither
                        // grows nor jumps when the scan finishes. The spinner takes
                        // its space only while it is showing.
                        Row {
                            width: parent.width
                            visible: root.scanning || root.scanMessage !== ""
                            spacing: Style.space(8)

                            BusyIndicator {
                                running: root.scanning
                                implicitWidth: 11
                                implicitHeight: 11
                            }

                            Text {
                                width: parent.width - (root.scanning ? 19 : 0)
                                text: root.plain(root.scanMessage, 80)
                                textFormat: Text.PlainText
                                color: Util.alpha(Color.foreground, 0.7)
                                font.family: Style.font.family
                                font.pixelSize: 9
                                wrapMode: Text.WordWrap
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

                        // The pads the scan found, nearest first. Each row is the whole selection:
                        // pressing one fills the address field and stores it, and
                        // the Connect button beside it acts on it from there.
                        Column {
                            width: parent.width
                            spacing: Style.space(4)
                            visible: root.foundPads.length > 0

                            Repeater {
                                model: root.foundPads

                                Button {
                                    id: padRow
                                    required property var modelData
                                    width: parent.width
                                    text: root.padLabel(padRow.modelData)
                                    enabled: !root.busy && root.pending === "" && !root.daemon
                                    onClicked: root.selectPad(padRow.modelData)
                                }
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

                        Row {
                            width: parent.width
                            spacing: Style.space(10)

                            Column {
                                width: parent.width - modeButton.width - Style.space(10)
                                spacing: Style.space(6)

                                // The shell's slider only snaps whole numbers, so
                                // the handlers round to half a kilometre. Dragging
                                // previews locally; the belt is written on release.
                                PanelSlider {
                                    id: speedSlider
                                    width: parent.width
                                    bar: root.bar
                                    minimum: 1.0
                                    maximum: root.maxSpeed
                                    step: 0.5
                                    // one notch per half-kilometre, to aim at
                                    tickCount: Math.round((root.maxSpeed - 1.0) / 0.5) + 1
                                    value: root.currentSpeed
                                    onMoved: function(v) { root.sliderPreview = Math.round(v * 2) / 2 }
                                    onReleased: function(v) {
                                        root.sliderPreview = 0
                                        root.setSpeed(Math.round(v * 2) / 2)
                                    }
                                }

                                // A whole kilometre under every other notch. As wide
                                // as the track above it, not the whole row.
                                Item {
                                    id: speedScale
                                    readonly property int stops: Math.round(root.maxSpeed)
                                    width: parent.width
                                    height: 11

                                    Repeater {
                                        model: speedScale.stops
                                        Text {
                                            required property int index
                                            x: (parent.width - width) * (index / (parent.stops - 1))
                                            text: index + 1
                                            textFormat: Text.PlainText
                                            color: Util.alpha(Color.foreground, 0.45)
                                            font.family: Style.font.family
                                            font.pixelSize: 8
                                        }
                                    }
                                }
                            }

                            // The pad cannot say which mode it is in, so this is
                            // the only place to change it. Walk caps the belt at 6.0.
                            Button {
                                id: modeButton
                                // A constant width, so the track beside it does not
                                // jump when the label changes.
                                width: 68
                                anchors.verticalCenter: parent.verticalCenter
                                text: root.mode === "run" ? "Run" : "Walk"
                                enabled: !root.busy
                                onClicked: root.setMode(root.mode === "run" ? "walk" : "run")
                            }
                        }

                        Text {
                            width: parent.width
                            text: "Half-kilometre steps, 1.0 to " + root.maxSpeed.toFixed(1)
                                + " km/h in " + (root.mode === "run" ? "run" : "walk")
                                + " mode. The step buttons move by 0.1, and the mouse wheel works here too."
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
                            text: "Start Treadmill"
                            enabled: !root.busy
                            onClicked: {
                                // This pad starts at 1.0 km/h and ignores a speed
                                // sent with Start. Show that value instead of the
                                // remembered target; the slider is a target again
                                // once the user changes it.
                                root.saveConfig()
                                root.speedLoaded = true
                                root.currentSpeed = 1.0
                                root.liveSpeed = 1.0
                                root.sendBelt("start")
                            }
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

                    // Live session numbers, only while a workout is active.
                    // There is no block of zeroes before the first session.
                    PanelSeparator {
                        visible: root.sessionActive
                        foreground: Color.foreground
                    }

                    SessionMetrics {
                        width: parent.width
                        visible: root.sessionActive
                        label: "This session"
                        meters: root.sessionDistance
                        seconds: root.sessionElapsed
                        calories: root.sessionCalories
                        estimated: root.sessionCaloriesEstimated
                    }

                    // The activity chain is for the stopped state: during a
                    // workout the panel stays on the live controls and metrics,
                    // and the history comes back with Start Treadmill.
                    PanelSeparator {
                        visible: !root.running
                        foreground: Color.foreground
                    }

                    // One disc per workout, in the same shape as the pushup
                    // tracker: the gap to the session before it sets the colour,
                    // and the day bubble in the hero counts forward from the last
                    // one. The treadmill controls above start and end a session.
                    ProgressSection {
                        width: parent.width
                        visible: !root.running
                        history: root.history
                        page: root.page
                        pageCount: root.pageCount
                        trainingDays: root.trainingDays
                        activityError: root.activityError
                        onPreviousPageRequested: root.pagePrevious()
                        onNextPageRequested: root.pageNext()
                    }

                    // The finished session's numbers live below the chain; the
                    // live block above only exists while the workout is active.
                    PanelSeparator {
                        visible: !root.running && !root.sessionActive && root.history.length > 0
                        foreground: Color.foreground
                    }

                    SessionMetrics {
                        width: parent.width
                        visible: !root.running && !root.sessionActive && root.history.length > 0
                        label: "Last session"
                        meters: root.lastMeters
                        seconds: root.lastSeconds
                        calories: root.lastCalories
                        estimated: root.lastCaloriesEstimated
                    }
                }
            }
        }
    }
}
