import AppKit
import ApplicationServices
import Foundation

// Claude Desktop Chat -> SidePulse optional Accessibility monitor.
// No chat contents, window titles, or text fields are read.
// Reads only control roles and accessibility labels required for state detection.

let scope = "claude-desktop-chat"
let pollSeconds = 0.5
let confirmationSamples = 2
let doneHoldSeconds = 5.0
let approvalDebug = CommandLine.arguments.contains("--approval-debug")
let enableAmber = !CommandLine.arguments.contains("--disable-amber")

func log(_ message: String) {
    let formatter = DateFormatter()
    formatter.dateFormat = "HH:mm:ss.SSS"
    print("[\(formatter.string(from: Date()))] \(message)")
    fflush(stdout)
}

guard AXIsProcessTrusted() else {
    fputs("Grant Accessibility access to Terminal in System Settings > Privacy & Security > Accessibility.\n", stderr)
    exit(1)
}

func attr(_ el: AXUIElement, _ key: String) -> (AXError, AnyObject?) {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(el, key as CFString, &value)
    return (error, value)
}

func str(_ el: AXUIElement, _ key: String) -> String {
    (attr(el, key).1 as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
}

func claudeProcess() -> NSRunningApplication? {
    NSWorkspace.shared.runningApplications.first {
        $0.localizedName == "Claude" && $0.activationPolicy == .regular
    }
}

enum Observation: Equatable { case approval, working, idle, unavailable }

// Claude Desktop labels the active session "… awaiting answer" while it is
// waiting for a user answer. An actionable AXCheckBox also appears in the
// chat pane in the observed approval/question-card flow. Require BOTH
// signals to avoid interpreting stale sessions as current approvals.
// This detects actionable answers, not necessarily OS tool permissions.

func observe(_ pid: pid_t) -> Observation {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.8)
    let (error, rawWindows) = attr(app, kAXWindowsAttribute)
    guard error == .success, let windows = rawWindows as? [AXUIElement], !windows.isEmpty else {
        return .unavailable
    }
    var hadScanFailure = false
    var sawStop = false
    var sawAwaitingAnswer = false
    var sawAnswerCard = false
    var debugCandidates = Set<String>()
    for window in windows {
        var stack: [(AXUIElement, Int)] = [(window, 0)]
        var visited = Set<UInt>()
        while let (node, depth) = stack.popLast() {
            if depth > 45 || visited.count >= 3000 { hadScanFailure = true; break }
            guard visited.insert(CFHash(node)).inserted else { continue }
            let role = str(node, kAXRoleAttribute)
            if role == "AXButton" || role == "AXCheckBox" {
                // Avoid printing full session names or checkbox descriptions.
                let labels = [str(node, kAXDescriptionAttribute), str(node, kAXTitleAttribute)]
                    .map { $0.lowercased() }
                if labels.contains("stop response") { sawStop = true }
                if role == "AXButton" && labels.contains(where: { $0.hasSuffix(" awaiting answer") }) {
                    sawAwaitingAnswer = true
                    debugCandidates.insert("session awaiting answer")
                }
                // The observed answer card uses a large checkbox in the
                // main chat pane. Geometry is a supporting signal, not an ID.
                if role == "AXCheckBox" {
                    let (posErr, posRaw) = attr(node, kAXPositionAttribute)
                    let (sizeErr, sizeRaw) = attr(node, kAXSizeAttribute)
                    var point = CGPoint.zero
                    var size = CGSize.zero
                    if posErr == .success, sizeErr == .success,
                       let positionObject = posRaw, let sizeObject = sizeRaw,
                       CFGetTypeID(positionObject) == AXValueGetTypeID(),
                       CFGetTypeID(sizeObject) == AXValueGetTypeID() {
                        let position = positionObject as! AXValue
                        let dimensions = sizeObject as! AXValue
                        if AXValueGetType(position) == .cgPoint,
                           AXValueGetType(dimensions) == .cgSize,
                           AXValueGetValue(position, .cgPoint, &point),
                           AXValueGetValue(dimensions, .cgSize, &size),
                           point.x >= 300, point.y >= 100,
                           size.width >= 250, size.height >= 30 {
                            sawAnswerCard = true
                            debugCandidates.insert("answer card checkbox")
                        }
                    }
                }
            }
            let (childError, raw) = attr(node, kAXChildrenAttribute)
            if childError == .success, let children = raw as? [AXUIElement] {
                for child in children.reversed() { stack.append((child, depth + 1)) }
            }
        }
    }
    if approvalDebug && !debugCandidates.isEmpty {
        log("Approval signals: \(debugCandidates.sorted().joined(separator: ", "))")
    }
    if enableAmber && sawAwaitingAnswer && sawAnswerCard { return .approval }
    if sawStop { return .working }
    return hadScanFailure ? .unavailable : .idle
}

@discardableResult
func report(_ state: String, kind: String? = nil) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    var args = ["sidepulse", "report", "--scope", scope, "--state", state,
                "--app", "Claude Desktop Chat"]
    if let kind = kind { args += ["--kind", kind] }
    process.arguments = args
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = NSHomeDirectory() + "/.local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
    process.environment = env
    process.standardOutput = FileHandle.nullDevice
    do {
        try process.run()
        process.waitUntilExit()
        if process.terminationStatus == 0 {
            log("SidePulse: \(state)\(kind.map { " (\($0))" } ?? "")")
            return true
        }
        log("SidePulse report failed: \(process.terminationStatus)")
    } catch {
        log("SidePulse report launch error: \(error)")
    }
    return false
}

log("Monitoring Claude Desktop Chat. Ctrl-C to stop. No chat text is read.")
log("Awaiting-answer detection is enabled; use --disable-amber to turn it off. --approval-debug logs redacted state signals.")
var lastPID: pid_t? = nil
var state: Observation = .idle
var previous: Observation? = nil
var consecutive = 0
var doneAt: Date? = nil

while true {
    guard let app = claudeProcess() else {
        if lastPID != nil { log("Claude closed or unavailable") }
        if state != .idle || doneAt != nil { _ = report("idle") }
        lastPID = nil
        state = .idle
        previous = nil
        consecutive = 0
        doneAt = nil
        Thread.sleep(forTimeInterval: pollSeconds)
        continue
    }
    if lastPID != app.processIdentifier {
        lastPID = app.processIdentifier
        state = .idle
        previous = nil
        consecutive = 0
        doneAt = nil
        log("Connected to Claude (PID \(app.processIdentifier))")
    }
    let observation = observe(app.processIdentifier)
    if observation != previous {
        if observation != .unavailable { log("Detected \(observation)") }
        previous = observation
        consecutive = 1
    } else {
        consecutive += 1
    }
    if observation != .unavailable && consecutive >= confirmationSamples && observation != state {
        let sent: Bool
        switch observation {
        case .approval:
            // SidePulse maps blocked + permission to the Waiting for Input/amber state.
            sent = report("blocked", kind: "permission")
        case .working:
            sent = report("working")
        case .idle:
            if state == .working || state == .approval {
                // The absence of Stop response alone can't tell a successful
                // completion from user cancellation; this is a best-effort Done.
                sent = report("done")
                if sent { doneAt = Date() }
            } else { sent = true }
        case .unavailable:
            sent = false
        }
        if sent {
            state = observation
            if observation != .idle { doneAt = nil }
        }
    }
    if let since = doneAt, state == .idle,
       Date().timeIntervalSince(since) >= doneHoldSeconds {
        if report("idle") { doneAt = nil }
    }
    Thread.sleep(forTimeInterval: pollSeconds)
}
