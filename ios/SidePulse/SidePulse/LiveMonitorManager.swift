import Foundation
import UserNotifications
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Bridges ActivityKit tokens to the `sidepulse live-activity` daemon.
///
/// The daemon owns the activity lifecycle: it starts the Live Activity with a
/// push-to-start token whenever agents wake up, streams content-state updates,
/// and ends it when the host goes idle. All this app does is hand the daemon
/// its tokens.
@MainActor
final class LiveMonitorManager: ObservableObject {
    static let shared = LiveMonitorManager()

    @Published var statusMessage: String = "Off"

    private var observersStarted = false

    var isSupported: Bool {
        if #available(iOS 17.2, *) { return true }
        return false
    }

    func startIfEnabled(model: AppModel) {
        guard model.liveMonitorEnabled else { return }
        start(model: model)
    }

    func start(model: AppModel) {
        guard #available(iOS 17.2, *) else {
            statusMessage = "Requires iOS 17.2 or later"
            return
        }
        guard !observersStarted else { return }
        observersStarted = true
        statusMessage = "Registering with \(model.liveMonitorServerURL)…"

        // Alert pushes (finished / needs input / blocked) are silent unless
        // the user granted notification permission, which otherwise only the
        // Get Push Token button requests.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }

        // The daemon sends alert pushes (finished / needs input / blocked)
        // to the app's normal APNs device token.
        if !model.pushToken.isEmpty, let tokenData = Data(hexString: model.pushToken) {
            Task { await self.register(kind: "device", token: tokenData, model: model) }
        }

        Task {
            for await tokenData in Activity<AgentActivityAttributes>.pushToStartTokenUpdates {
                await self.register(kind: "push_to_start", token: tokenData, model: model)
            }
        }

        Task {
            for await activity in Activity<AgentActivityAttributes>.activityUpdates {
                self.observe(activity: activity, model: model)
            }
        }
        // Activities that already exist when the app launches — and if there
        // are none, tell the daemon so it can restart one immediately instead
        // of updating an activity the last app update destroyed.
        if #available(iOS 17.2, *) {
            var existing = Activity<AgentActivityAttributes>.activities
            if existing.count > 1 {
                existing.sort { $0.content.state.updatedAt > $1.content.state.updatedAt }
                for stale in existing.dropFirst() {
                    let activity = stale
                    Task { await activity.end(nil, dismissalPolicy: .immediate) }
                }
                existing = [existing[0]]
            }
            for activity in existing {
                observe(activity: activity, model: model)
            }
            if existing.isEmpty {
                Task { await self.sendReset(model: model) }
            }
        }
    }

    private func sendReset(model: AppModel) async {
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("register") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["kind": "reset"])
        request.timeoutInterval = 10
        _ = try? await URLSession.shared.data(for: request)
    }

    @available(iOS 17.2, *)
    private func observe(activity: Activity<AgentActivityAttributes>, model: AppModel) {
        // iOS happily stacks a second Live Activity when the daemon restarts
        // one — the newest activity wins, everything else ends immediately.
        Task {
            for other in Activity<AgentActivityAttributes>.activities where other.id != activity.id {
                await other.end(nil, dismissalPolicy: .immediate)
            }
        }
        Task {
            for await tokenData in activity.pushTokenUpdates {
                await self.register(kind: "update", token: tokenData, model: model, activityID: activity.id)
            }
        }
        // A swiped-away (or 8-hour-expired) activity reports .dismissed;
        // tell the daemon so it can start a fresh one while agents are
        // active. Programmatic ends (.ended) stay silent — those are the
        // daemon's own idle-end and the dedup cleanup.
        Task {
            for await state in activity.activityStateUpdates where state == .dismissed {
                await self.sendReset(model: model)
                break
            }
        }
    }

    private func register(kind: String, token: Data, model: AppModel, activityID: String? = nil) async {
        let tokenHex = token.map { String(format: "%02x", $0) }.joined()
        guard let url = URL(string: model.liveMonitorServerURL)?.appendingPathComponent("register") else {
            statusMessage = "Invalid server URL"
            return
        }
        var payload: [String: Any] = [
            "kind": kind,
            "token": tokenHex,
            "device": await deviceName(),
        ]
        if let activityID { payload["activity_id"] = activityID }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 10

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200..<300).contains(code) {
                statusMessage = kind == "push_to_start"
                    ? "Registered — the Mac can now start the Live Activity"
                    : "Live Activity connected"
            } else {
                statusMessage = "Server error \(code) registering \(kind) token"
            }
        } catch {
            statusMessage = "Cannot reach server: \(error.localizedDescription)"
        }
    }

    private func deviceName() async -> String {
        #if canImport(UIKit)
        return await UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }
}

#if canImport(UIKit)
import UIKit
#endif

private extension Data {
    init?(hexString: String) {
        let cleaned = hexString.filter(\.isHexDigit)
        guard cleaned.count % 2 == 0 else { return nil }
        var bytes: [UInt8] = []
        var index = cleaned.startIndex
        while index < cleaned.endIndex {
            let next = cleaned.index(index, offsetBy: 2)
            guard let byte = UInt8(cleaned[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }
}
