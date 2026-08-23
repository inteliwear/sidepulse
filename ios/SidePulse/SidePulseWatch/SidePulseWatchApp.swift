import SwiftUI

@main
struct SidePulseWatchApp: App {
    var body: some Scene {
        WindowGroup {
            WatchAgentsView()
        }
    }
}

/// Live session list on the wrist. Streams from the daemon over the local
/// network; away from home the mirrored Live Activity in the Smart Stack
/// covers the glanceable case.
struct WatchAgentsView: View {
    @StateObject private var stream = AgentStreamClient()
    @AppStorage("watchServerURL") private var serverURL = "http://macmini8005.local:8787"

    var body: some View {
        NavigationStack {
            List {
                header

                if let snapshot = stream.snapshot, !snapshot.agents.isEmpty {
                    ForEach(snapshot.agents) { agent in
                        WatchAgentRow(agent: agent)
                    }
                } else if stream.snapshot != nil {
                    Text("All quiet")
                        .foregroundStyle(.secondary)
                } else {
                    Text("Waiting for data…")
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Agents")
        }
        .task {
            stream.start(baseURL: serverURL)
        }
    }

    @ViewBuilder
    private var header: some View {
        HStack {
            switch stream.state {
            case .live:
                Image(systemName: "dot.radiowaves.left.and.right")
                    .foregroundStyle(.green)
            case .connecting:
                Image(systemName: "arrow.triangle.2.circlepath")
                    .foregroundStyle(.orange)
            case .failed:
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            case .idle:
                Image(systemName: "pause.circle")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let snapshot = stream.snapshot {
                Text("\(snapshot.activeCount) active")
                    .font(.footnote.bold())
            }
        }
    }
}

private struct WatchAgentRow: View {
    let agent: AgentSnapshot.Agent

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color(agent.mode))
                .frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 1) {
                Text(agent.name)
                    .font(.footnote)
                    .lineLimit(2)
                HStack(spacing: 4) {
                    if let provider = agent.provider {
                        Text(provider.capitalized)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    if let finishedAt = agent.finishedAt {
                        Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    } else {
                        Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                            .font(.system(size: 10))
                            .foregroundStyle(color(agent.mode))
                    }
                }
            }
        }
        .padding(.vertical, 1)
    }

    private func color(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}
