import ActivityKit
import SwiftUI
import WidgetKit

// MARK: - Status grouping

/// The four states that matter at a glance, in attention order.
private struct ModeGroups {
    var blocked = 0
    var waiting = 0
    var working = 0
    var done = 0

    init(agents: [AgentActivityAttributes.AgentRow]) {
        for agent in agents {
            switch agent.mode {
            case "blocked_error": blocked += 1
            case "waiting_for_input": waiting += 1
            case "completed": done += 1
            case "idle_ready": break
            default: working += 1
            }
        }
    }

    var headline: (count: Int, color: Color) {
        if blocked > 0 { return (blocked, .statusBlocked) }
        if waiting > 0 { return (waiting, .statusWaiting) }
        if working > 0 { return (working, .statusWorking) }
        return (done, .statusDone)
    }

    /// Icon for the most urgent state: warning when blocked, a question
    /// bubble when a session wants input, a bolt while working, a check
    /// when everything is done.
    var symbol: (name: String, color: Color) {
        if blocked > 0 { return ("exclamationmark.triangle.fill", .statusBlocked) }
        if waiting > 0 { return ("questionmark.bubble.fill", .statusWaiting) }
        if working > 0 { return ("bolt.fill", .statusWorking) }
        return ("checkmark.circle.fill", .statusDone)
    }
}

private extension Color {
    static let statusWorking = Color(red: 0.25, green: 0.85, blue: 0.95)
    static let statusWaiting = Color(red: 1.0, green: 0.62, blue: 0.11)
    static let statusBlocked = Color(red: 1.0, green: 0.28, blue: 0.29)
    static let statusDone = Color(red: 0.29, green: 0.87, blue: 0.42)

    static func forMode(_ mode: String) -> Color {
        let (r, g, b) = AgentModeStyle.rgb(mode)
        return Color(red: r, green: g, blue: b)
    }
}

// MARK: - Widget

@available(iOSApplicationExtension 16.2, *)
struct AgentLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AgentActivityAttributes.self) { context in
            LockScreenView(context: context)
                .activityBackgroundTint(Color(red: 0.07, green: 0.07, blue: 0.09))
                .activitySystemActionForegroundColor(.white)
                .widgetURL(URL(string: "sidepulse://agents"))
        } dynamicIsland: { context in
            let groups = ModeGroups(agents: context.state.agents)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 5) {
                        Image(systemName: groups.symbol.name)
                            .font(.caption2)
                            .foregroundStyle(groups.symbol.color)
                        Text(context.attributes.hostLabel)
                            .font(.caption.bold())
                            .foregroundStyle(.primary)
                    }
                    .padding(.leading, 2)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    StatusChips(groups: groups)
                        .padding(.trailing, 2)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(context.state.agents.prefix(4)) { agent in
                            AgentRowView(agent: agent)
                        }
                        if context.state.agents.count > 4 {
                            Text("+\(context.state.agents.count - 4) more")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.top, 2)
                    .widgetURL(URL(string: "sidepulse://agents"))
                }
            } compactLeading: {
                // Single-activity compact: state icon on the leading side.
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(groups.symbol.color)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } compactTrailing: {
                Text("\(context.state.activeCount)")
                    .font(.system(size: 14, weight: .bold, design: .rounded))
                    .foregroundStyle(groups.symbol.color)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } minimal: {
                // With a second Live Activity (e.g. Now Playing) present iOS
                // renders every activity as `minimal`, so the count lives
                // here — a colored number is more useful than an icon.
                ZStack {
                    Circle()
                        .fill(groups.symbol.color.opacity(0.22))
                    Text("\(context.state.activeCount)")
                        .font(.system(size: 12, weight: .bold, design: .rounded))
                        .foregroundStyle(groups.symbol.color)
                        .minimumScaleFactor(0.6)
                }
                .widgetURL(URL(string: "sidepulse://agents"))
            }
        }
        .supplementalActivityFamilies([.small])
    }
}

// MARK: - Pieces

/// Per-state counts as colored digits, most urgent first: a red digit
/// appearing means blocked, orange waiting, cyan working, green finished —
/// so transitions are visible right in the compact island.
/// Colored count chips, only for the most urgent groups that are present.
private struct StatusChips: View {
    let groups: ModeGroups

    var body: some View {
        let parts: [(Int, Color)] = [
            (groups.blocked, .statusBlocked),
            (groups.waiting, .statusWaiting),
            (groups.working, .statusWorking),
            (groups.done, .statusDone),
        ]

        HStack(spacing: 5) {
            ForEach(Array(parts.prefix(3).enumerated()), id: \.offset) { _, part in
                chip(part.0, part.1)
            }
        }
    }

    @ViewBuilder
    private func chip(_ count: Int, _ color: Color) -> some View {
        if count > 0 {
            HStack(spacing: 3) {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
                Text("\(count)")
                    .font(.caption2.bold())
                    .foregroundStyle(color)
                    .contentTransition(.numericText())
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
        }
    }
}

private struct WatchAgentRowView: View {
    let agent: AgentActivityAttributes.AgentRow

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(Color.forMode(agent.mode))
                .frame(width: 6, height: 6)
            Text(agent.name)
                .font(.system(size: 11))
                .foregroundStyle(agent.mode == "completed" ? .secondary : .primary)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            Group {
                if let finishedAt = agent.finishedAt {
                    Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                        .foregroundStyle(.tertiary)
                } else {
                    Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                        .fontWeight(.medium)
                        .foregroundStyle(Color.forMode(agent.mode))
                }
            }
            .font(.system(size: 9))
            .lineLimit(1)
            .layoutPriority(1)
        }
    }
}

private struct AgentRowView: View {
    let agent: AgentActivityAttributes.AgentRow

    private var isDone: Bool { agent.mode == "completed" }

    var body: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(Color.forMode(agent.mode))
                .frame(width: 7, height: 7)
                .shadow(color: Color.forMode(agent.mode).opacity(isDone ? 0 : 0.8), radius: 3)
            if let provider = agent.provider {
                Text(provider.capitalized)
                    .font(.system(size: 9, weight: .semibold))
                    .lineLimit(1)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.white.opacity(0.12), in: Capsule())
            }
            Text(agent.name)
                .font(.caption)
                .foregroundStyle(isDone ? .secondary : .primary)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            // layoutPriority (not fixedSize) gives the trailing text its
            // natural width without the measurement that blanked the card.
            Group {
                if let finishedAt = agent.finishedAt {
                    Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                        .foregroundStyle(.tertiary)
                } else {
                    Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                        .fontWeight(.medium)
                        .foregroundStyle(Color.forMode(agent.mode))
                }
            }
            .font(.caption2)
            .lineLimit(1)
            .layoutPriority(1)
        }
    }
}

@available(iOSApplicationExtension 16.2, *)
private struct LockScreenView: View {
    let context: ActivityViewContext<AgentActivityAttributes>
    @Environment(\.activityFamily) private var activityFamily

    var body: some View {
        let groups = ModeGroups(agents: context.state.agents)
        if activityFamily == .small {
            watchBody(groups: groups)
        } else {
            phoneBody(groups: groups)
        }
    }

    /// Smart Stack on the watch: the card's height is fixed and small, so
    /// two compact rows at most — the header chips carry the full counts.
    private func watchBody(groups: ModeGroups) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 10))
                    .foregroundStyle(groups.symbol.color)
                Text(context.attributes.hostLabel)
                    .font(.system(size: 11, weight: .bold))
                    .lineLimit(1)
                Spacer(minLength: 3)
                StatusChips(groups: groups)
                    .fixedSize()
            }
            ForEach(context.state.agents.prefix(3)) { agent in
                WatchAgentRowView(agent: agent)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
    }

    private func phoneBody(groups: ModeGroups) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                HStack(spacing: 5) {
                    Image(systemName: groups.symbol.name)
                        .font(.caption)
                        .foregroundStyle(groups.symbol.color)
                    Text(context.attributes.hostLabel)
                        .font(.subheadline.bold())
                        .foregroundStyle(.white)
                }
                Spacer()
                StatusChips(groups: groups)
            }

            if context.state.agents.isEmpty {
                Text("All quiet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(context.state.agents.prefix(5)) { agent in
                        AgentRowView(agent: agent)
                    }
                    if context.state.agents.count > 5 {
                        Text("+\(context.state.agents.count - 5) more")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}
