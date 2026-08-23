import SwiftUI
import WidgetKit

@main
struct SidePulseWatchWidgetsBundle: WidgetBundle {
    var body: some Widget {
        AgentsLauncherWidget()
    }
}

/// Smart Stack shortcut into the watch app's session list. Live Activity
/// taps always route to the iPhone on watchOS; this widget is the one-tap
/// on-wrist path.
struct AgentsLauncherWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: "AgentsLauncher",
            provider: LauncherProvider()
        ) { _ in
            LauncherView()
                .containerBackground(.black.gradient, for: .widget)
        }
        .configurationDisplayName("Mac Agents")
        .description("Open the live agent session list.")
        .supportedFamilies([.accessoryRectangular, .accessoryCircular])
    }
}

struct LauncherEntry: TimelineEntry {
    let date: Date
}

struct LauncherProvider: TimelineProvider {
    func placeholder(in context: Context) -> LauncherEntry {
        LauncherEntry(date: .now)
    }

    func getSnapshot(in context: Context, completion: @escaping (LauncherEntry) -> Void) {
        completion(LauncherEntry(date: .now))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<LauncherEntry>) -> Void) {
        completion(Timeline(entries: [LauncherEntry(date: .now)], policy: .never))
    }
}

struct LauncherView: View {
    @Environment(\.widgetFamily) private var family

    var body: some View {
        Group {
            if family == .accessoryCircular {
                Image(systemName: "bolt.horizontal.circle.fill")
                    .font(.title2)
                    .foregroundStyle(Color(red: 0.25, green: 0.85, blue: 0.95))
            } else {
                HStack(spacing: 8) {
                    Image(systemName: "bolt.horizontal.circle.fill")
                        .font(.title3)
                        .foregroundStyle(Color(red: 0.25, green: 0.85, blue: 0.95))
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
        .widgetURL(URL(string: "sidepulse://agents"))
    }
}
