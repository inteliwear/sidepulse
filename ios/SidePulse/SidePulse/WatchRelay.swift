import Combine
import Foundation
#if canImport(WatchConnectivity)
import WatchConnectivity
#endif

/// Streams agent snapshots on the watch's behalf. The watch has no
/// Tailscale and awkward local networking, so it asks the phone, which
/// relays the daemon's SSE snapshots over WatchConnectivity.
@MainActor
final class WatchRelay: NSObject, ObservableObject {
    static let shared = WatchRelay()

    private let stream = AgentStreamClient()
    private var cancellable: AnyCancellable?
    private var streaming = false

    func activate() {
        #if canImport(WatchConnectivity)
        guard WCSession.isSupported() else { return }
        let session = WCSession.default
        session.delegate = self
        session.activate()

        cancellable = stream.$snapshot
            .compactMap { $0 }
            .sink { [weak self] snapshot in
                self?.forward(snapshot)
            }
        #endif
    }

    private func startStreamingIfNeeded() {
        guard !streaming else { return }
        streaming = true
        stream.start(baseURL: AppModel.shared.liveMonitorServerURL)
    }

    private func stopStreaming() {
        streaming = false
        stream.stop()
    }

    private func forward(_ snapshot: AgentSnapshot) {
        #if canImport(WatchConnectivity)
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        let session = WCSession.default
        if session.isReachable {
            session.sendMessage(["snapshot": data], replyHandler: nil, errorHandler: nil)
        }
        try? session.updateApplicationContext(["snapshot": data])
        #endif
    }
}

#if canImport(WatchConnectivity)
extension WatchRelay: WCSessionDelegate {
    nonisolated func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {}

    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}

    nonisolated func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let reachable = session.isReachable
        Task { @MainActor in
            if reachable {
                self.startStreamingIfNeeded()
            } else {
                self.stopStreaming()
            }
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        if message["cmd"] as? String == "start" {
            Task { @MainActor in
                self.startStreamingIfNeeded()
            }
        }
    }
}
#endif
