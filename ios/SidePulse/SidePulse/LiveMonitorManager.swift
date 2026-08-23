import Foundation
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
            let existing = Activity<AgentActivityAttributes>.activities
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
        Task {
            for await tokenData in activity.pushTokenUpdates {
                await self.register(kind: "update", token: tokenData, model: model, activityID: activity.id)
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
