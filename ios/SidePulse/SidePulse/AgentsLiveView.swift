import SwiftUI
import UIKit

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
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)) { _ in
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
        Button {
            openProviderApp()
        } label: {
            rowContent
        }
        .buttonStyle(.plain)
    }

    /// A Remote-Control session deep-links to the exact conversation;
    /// otherwise fall back to opening the provider app.
    private func openProviderApp() {
        if let link = agent.deepLink, let url = URL(string: link) {
            open(candidates: [url])
            return
        }
        let provider = agent.provider ?? String(agent.id.split(separator: ":").first ?? "")
        let candidates: [URL]
        switch provider {
        case "claude":
            candidates = [URL(string: "claude://")!, URL(string: "https://claude.ai")!]
        case "codex":
            candidates = [URL(string: "chatgpt://")!, URL(string: "https://chatgpt.com")!]
        default:
            return
        }
        open(candidates: candidates)
    }

    private func open(candidates: [URL]) {
        guard let first = candidates.first else { return }
        UIApplication.shared.open(first) { success in
            if !success, candidates.count > 1 {
                open(candidates: Array(candidates.dropFirst()))
            }
        }
    }

    private var rowContent: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 10) {
                Circle()
                    .fill(color(agent.mode))
                    .frame(width: 10, height: 10)
                    .padding(.top, 5)
                Text(agent.name)
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 6) {
                if let provider = agent.provider {
                    Text(provider.capitalized)
                        .font(.caption2.bold())
                        .lineLimit(1)
                        .fixedSize()
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color(.tertiarySystemFill))
                        .clipShape(Capsule())
                }
                Text(secondaryLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text(AgentModeStyle.label(agent.mode))
                    .font(.caption.bold())
                    .foregroundStyle(color(agent.mode))
                    .fixedSize()
                if let finishedAt = agent.finishedAt {
                    Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                } else {
                    Image(systemName: "arrow.up.forward.app")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.leading, 20)
        }
        .padding(.vertical, 2)
    }

    private var secondaryLine: String {
        let parts = [agent.cwd, agent.detail].compactMap { $0 }
        return parts.isEmpty ? AgentModeStyle.label(agent.mode) : parts.joined(separator: " · ")
    }

    private func color(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}
