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
                let symbol = groups.symbol
                Image(systemName: symbol.name)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(symbol.color)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } compactTrailing: {
                CompactCounts(groups: groups)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } minimal: {
                let symbol = groups.symbol
                ZStack {
                    Circle()
                        .fill(symbol.color.opacity(0.22))
                    Image(systemName: symbol.name)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(symbol.color)
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
private struct CompactCounts: View {
    let groups: ModeGroups

    var body: some View {
        let parts: [(Int, Color)] = [
            (groups.blocked, .statusBlocked),
            (groups.waiting, .statusWaiting),
            (groups.working, .statusWorking),
            (groups.done, .statusDone),
        ].filter { $0.0 > 0 }

        HStack(spacing: 3) {
            ForEach(Array(parts.prefix(3).enumerated()), id: \.offset) { _, part in
                Text("\(part.0)")
                    .font(.system(size: 13, weight: .bold, design: .rounded))
                    .foregroundStyle(part.1)
                    .contentTransition(.numericText())
            }
        }
    }
}

/// Colored count chips, only for the groups that are present.
private struct StatusChips: View {
    let groups: ModeGroups

    var body: some View {
        HStack(spacing: 5) {
            chip(groups.blocked, .statusBlocked)
            chip(groups.waiting, .statusWaiting)
            chip(groups.working, .statusWorking)
            chip(groups.done, .statusDone)
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
            Spacer(minLength: 4)
            if let finishedAt = agent.finishedAt {
                Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            } else {
                Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(Color.forMode(agent.mode))
                    .lineLimit(1)
            }
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
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.white.opacity(0.12), in: Capsule())
            }
            Text(agent.name)
                .font(.caption)
                .foregroundStyle(isDone ? .secondary : .primary)
                .lineLimit(1)
            Spacer(minLength: 6)
            if let finishedAt = agent.finishedAt {
                Text(Date(timeIntervalSince1970: finishedAt), style: .relative)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            } else {
                Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(Color.forMode(agent.mode))
                    .lineLimit(1)
            }
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
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Image(systemName: groups.symbol.name)
                    .font(.system(size: 10))
                    .foregroundStyle(groups.symbol.color)
                Text(context.attributes.hostLabel)
                    .font(.system(size: 11, weight: .bold))
                    .lineLimit(1)
                Spacer(minLength: 4)
                StatusChips(groups: groups)
            }
            ForEach(context.state.agents.prefix(2)) { agent in
                WatchAgentRowView(agent: agent)
            }
            if context.state.agents.count > 2 {
                Text("+\(context.state.agents.count - 2) more")
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
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

            HStack(spacing: 4) {
                Image(systemName: "clock")
                    .font(.system(size: 8))
                Text(context.state.updatedDate, style: .relative)
                    .font(.caption2)
            }
            .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}
