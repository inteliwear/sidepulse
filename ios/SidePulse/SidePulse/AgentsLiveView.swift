import SwiftUI

/// Realtime agent monitor: streams snapshots from the Mac over the local
/// network / Tailscale while the app is in the foreground.
struct AgentsLiveView: View {
    @ObservedObject var model: AppModel
    @StateObject private var stream = AgentStreamClient()

    var body: some View {
        List {
            Section {
                header
            }

            Section("Agents") {
                if let snapshot = stream.snapshot, !snapshot.agents.isEmpty {
                    ForEach(snapshot.agents) { agent in
                        AgentLiveRow(agent: agent)
                    }
                } else if stream.snapshot != nil {
                    Text("All quiet — no active agents.")
                        .foregroundStyle(.secondary)
                } else {
                    Text("Waiting for data…")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Mac Agents")
        .task {
            stream.start(baseURL: model.liveMonitorServerURL)
        }
        .onDisappear {
            stream.stop()
        }
    }

    @ViewBuilder
    private var header: some View {
        HStack {
            switch stream.state {
            case .live:
                Label("Live", systemImage: "dot.radiowaves.left.and.right")
                    .foregroundStyle(.green)
            case .connecting:
                Label("Connecting…", systemImage: "arrow.triangle.2.circlepath")
                    .foregroundStyle(.orange)
            case .failed(let message):
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
                    .lineLimit(2)
            case .idle:
                Label("Idle", systemImage: "pause.circle")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let snapshot = stream.snapshot {
                Text("\(snapshot.activeCount) active")
                    .font(.subheadline.bold())
            }
        }
        .font(.subheadline)
    }
}

private struct AgentLiveRow: View {
    let agent: AgentSnapshot.Agent

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(color(agent.mode))
                .frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 2) {
                Text(agent.name)
                    .font(.body)
                    .lineLimit(1)
                Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            Text(AgentModeStyle.label(agent.mode))
                .font(.caption.bold())
                .foregroundStyle(color(agent.mode))
        }
        .padding(.vertical, 2)
    }

    private func color(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}
