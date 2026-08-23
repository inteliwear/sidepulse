import Foundation
import WatchConnectivity

/// Receives agent snapshots relayed by the iPhone app.
@MainActor
final class WatchSessionStore: NSObject, ObservableObject {
    static let shared = WatchSessionStore()

    @Published var snapshot: AgentSnapshot?
    @Published var phoneReachable = false

    func activate() {
        guard WCSession.isSupported() else { return }
        let session = WCSession.default
        session.delegate = self
        session.activate()
    }

    func requestStart() {
        let session = WCSession.default
        guard session.activationState == .activated else { return }
        phoneReachable = session.isReachable
        session.sendMessage(["cmd": "start"], replyHandler: nil, errorHandler: nil)
        // Whatever the phone last relayed while we were away.
        decode(from: session.receivedApplicationContext)
    }

    private func decode(from payload: [String: Any]) {
        guard let data = payload["snapshot"] as? Data,
              let parsed = try? JSONDecoder().decode(AgentSnapshot.self, from: data) else {
            return
        }
        snapshot = parsed
    }
}

extension WatchSessionStore: WCSessionDelegate {
    nonisolated func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        Task { @MainActor in
            self.requestStart()
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let reachable = session.isReachable
        Task { @MainActor in
            self.phoneReachable = reachable
            if reachable {
                self.requestStart()
            }
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        let payload = message
        Task { @MainActor in
            self.decode(from: payload)
        }
    }

    nonisolated func session(
        _ session: WCSession,
        didReceiveApplicationContext applicationContext: [String: Any]
    ) {
        let payload = applicationContext
        Task { @MainActor in
            self.decode(from: payload)
        }
    }
}
