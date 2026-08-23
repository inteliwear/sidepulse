import SwiftUI
import WidgetKit

@main
struct SidePulseWatchWidgetsBundle: WidgetBundle {
    var body: some Widget {
        AgentsLauncherWidget()
    }
}

/// Smart Stack tile: last relayed agent snapshot, and always a one-tap way
/// into the watch app (Live Activity taps route to the iPhone by policy).
struct AgentsLauncherWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: "AgentsLauncher",
            provider: LauncherProvider()
        ) { entry in
            LauncherView(snapshot: entry.snapshot)
                .containerBackground(.black.gradient, for: .widget)
        }
        .configurationDisplayName("Mac Agents")
        .description("Agent sessions on the Mac.")
        .supportedFamilies([.accessoryRectangular])
    }
}

struct LauncherEntry: TimelineEntry {
    let date: Date
    let snapshot: AgentSnapshot?
}

struct LauncherProvider: TimelineProvider {
    private static let endpoints = [
        "http://macmini8005:8787/snapshot",
        "http://192.168.1.168:8787/snapshot",
    ]

    func placeholder(in context: Context) -> LauncherEntry {
        LauncherEntry(date: .now, snapshot: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (LauncherEntry) -> Void) {
        completion(LauncherEntry(date: .now, snapshot: SharedSnapshotStore.load()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<LauncherEntry>) -> Void) {
        // Try a live fetch each refresh; fall back to whatever the watch app
        // last relayed. Refresh again after ten minutes, budget permitting.
        fetchLive { fetched in
            let snapshot = fetched ?? SharedSnapshotStore.load()
            if let fetched, let data = try? JSONEncoder().encode(fetched) {
                SharedSnapshotStore.save(data)
            }
            let entry = LauncherEntry(date: .now, snapshot: snapshot)
            completion(Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(10 * 60))))
        }
    }

    private func fetchLive(
        _ completion: @escaping (AgentSnapshot?) -> Void,
        index: Int = 0
    ) {
        guard index < Self.endpoints.count, let url = URL(string: Self.endpoints[index]) else {
            completion(nil)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 4
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data,
               let parsed = try? JSONDecoder().decode(AgentSnapshot.self, from: data) {
                completion(parsed)
            } else {
                self.fetchLive(completion, index: index + 1)
            }
        }.resume()
    }
}

struct LauncherView: View {
    let snapshot: AgentSnapshot?

    private var accent: Color {
        guard let snapshot, snapshot.activeCount > 0 else {
            return Color(red: 0.29, green: 0.87, blue: 0.42)
        }
        let modes = snapshot.agents.map(\.mode)
        if modes.contains("blocked_error") { return Color(red: 1.0, green: 0.28, blue: 0.29) }
        if modes.contains("waiting_for_input") { return Color(red: 1.0, green: 0.62, blue: 0.11) }
        return Color(red: 0.25, green: 0.85, blue: 0.95)
    }

    var body: some View {
        rectangular
            .widgetURL(URL(string: "sidepulse://agents"))
    }

    @ViewBuilder
    private var rectangular: some View {
        if let snapshot {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Circle().fill(accent).frame(width: 7, height: 7)
                    Text("\(snapshot.activeCount) active")
                        .font(.headline)
                    Spacer()
                    Text(Date(timeIntervalSince1970: snapshot.updatedAt), style: .relative)
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                if let top = snapshot.agents.first {
                    Text(top.name)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Text(top.detail ?? AgentModeStyle.label(top.mode))
                        .font(.system(size: 10))
                        .foregroundStyle(accent)
                        .lineLimit(1)
                } else {
                    Text("All quiet")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            HStack(spacing: 8) {
                Image(systemName: "bolt.horizontal.circle.fill")
                    .font(.title3)
                    .foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Mac Agents")
                        .font(.headline)
                    Text("Live session list")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
        }
    }
}
