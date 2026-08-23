import Foundation

/// Snapshot of agents on the monitored Mac. Wire format shared with the
/// `sidepulse live-activity` daemon (same JSON the Live Activity receives).
struct AgentSnapshot: Codable, Equatable {
    struct Agent: Codable, Equatable, Identifiable {
        var id: String
        var name: String
        var mode: String
        var detail: String?
        var provider: String?
        var cwd: String?
        var finishedAt: Double?
    }

    var aggregateMode: String
    var activeCount: Int
    var agents: [Agent]
    var updatedAt: Double
}

/// Streams live agent snapshots from the daemon's `/stream` SSE endpoint.
/// Foreground-only by design; the Live Activity covers the locked phone.
@MainActor
final class AgentStreamClient: ObservableObject {
    enum ConnectionState: Equatable {
        case idle
        case connecting
        case live
        case failed(String)
    }

    @Published var state: ConnectionState = .idle
    @Published var snapshot: AgentSnapshot?

    private var task: Task<Void, Never>?

    func start(baseURL: String) {
        stop()
        task = Task { await run(baseURL: baseURL) }
    }

    func stop() {
        task?.cancel()
        task = nil
        state = .idle
    }

    private func run(baseURL: String) async {
        guard let url = URL(string: baseURL)?.appendingPathComponent("stream") else {
            state = .failed("Invalid server URL")
            return
        }
        var attempt = 0
        while !Task.isCancelled {
            state = attempt == 0 ? .connecting : .connecting
            do {
                var request = URLRequest(url: url)
                request.timeoutInterval = 15
                request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                let (bytes, response) = try await URLSession.shared.bytes(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                    throw URLError(.badServerResponse)
                }
                attempt = 0
                for try await line in bytes.lines {
                    if Task.isCancelled { return }
                    // AsyncLineSequence never yields empty lines, so the SSE
                    // blank-line delimiter is invisible here. Each event is a
                    // single-line JSON payload; decode it directly.
                    guard line.hasPrefix("data:") else { continue }
                    let body = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                    if let parsed = try? JSONDecoder().decode(
                        AgentSnapshot.self, from: Data(body.utf8)
                    ) {
                        snapshot = parsed
                        state = .live
                    }
                }
            } catch {
                if Task.isCancelled { return }
                state = .failed(error.localizedDescription)
            }
            attempt += 1
            let backoff = min(Double(attempt) * 2.0, 15.0)
            try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
        }
    }
}
