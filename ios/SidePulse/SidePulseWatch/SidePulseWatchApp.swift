import SwiftUI

@main
struct SidePulseWatchApp: App {
    var body: some Scene {
        WindowGroup {
            WatchAgentsView()
        }
    }
}

/// Companion view: agent snapshots arrive relayed from the iPhone app,
/// which streams them from the Mac over its own network (incl. Tailscale).
struct WatchAgentsView: View {
    @StateObject private var store = WatchSessionStore.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack {
            List {
                header

                if let snapshot = store.snapshot, !snapshot.agents.isEmpty {
                    ForEach(snapshot.agents) { agent in
                        WatchAgentRow(agent: agent)
                    }
                } else if store.snapshot != nil {
                    Text("All quiet")
                        .foregroundStyle(.secondary)
                } else {
                    Text("Open SidePulse on the iPhone once if this persists.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Agents")
        }
        .task {
            store.activate()
        }
        .onOpenURL { _ in
            // Live Activity taps land here; the agents list is the whole app.
            store.requestStart()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                store.requestStart()
            }
        }
    }

    @ViewBuilder
    private var header: some View {
        HStack {
            if store.snapshot != nil {
                Image(systemName: "iphone.radiowaves.left.and.right")
                    .foregroundStyle(store.phoneReachable ? .green : .orange)
            } else {
                Image(systemName: "iphone.slash")
                    .foregroundStyle(.orange)
            }
            Spacer()
            if let snapshot = store.snapshot {
                Text("\(snapshot.activeCount) active")
                    .font(.footnote.bold())
                Text(Date(timeIntervalSince1970: snapshot.updatedAt), style: .relative)
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
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
