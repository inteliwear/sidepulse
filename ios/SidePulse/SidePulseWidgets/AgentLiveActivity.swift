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

    var presentColors: [Color] {
        var colors: [Color] = []
        if blocked > 0 { colors.append(.statusBlocked) }
        if waiting > 0 { colors.append(.statusWaiting) }
        if working > 0 { colors.append(.statusWorking) }
        if done > 0 { colors.append(.statusDone) }
        return colors
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
                        Image(systemName: "desktopcomputer")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
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
                DotCluster(colors: groups.presentColors)
                    .widgetURL(URL(string: "sidepulse://agents"))
            } compactTrailing: {
                let headline = groups.headline
                Text("\(headline.count)")
                    .font(.caption.bold())
                    .foregroundStyle(headline.color)
                    .contentTransition(.numericText())
                    .widgetURL(URL(string: "sidepulse://agents"))
            } minimal: {
                let headline = groups.headline
                ZStack {
                    Circle()
                        .fill(headline.color.opacity(0.25))
                    Text("\(headline.count)")
                        .font(.system(size: 11, weight: .bold, design: .rounded))
                        .foregroundStyle(headline.color)
                }
                .widgetURL(URL(string: "sidepulse://agents"))
            }
        }
    }
}

// MARK: - Pieces

/// Up to three overlapping colored dots — one per status group present.
private struct DotCluster: View {
    let colors: [Color]

    var body: some View {
        HStack(spacing: -4) {
            ForEach(Array(colors.prefix(3).enumerated()), id: \.offset) { _, color in
                Circle()
                    .fill(color)
                    .frame(width: 9, height: 9)
                    .overlay(Circle().stroke(.black, lineWidth: 1.5))
                    .shadow(color: color.opacity(0.8), radius: 3)
            }
        }
        .padding(.leading, 2)
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

private struct AgentRowView: View {
    let agent: AgentActivityAttributes.AgentRow

    private var isDone: Bool { agent.mode == "completed" }

    var body: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(Color.forMode(agent.mode))
                .frame(width: 7, height: 7)
                .shadow(color: Color.forMode(agent.mode).opacity(isDone ? 0 : 0.8), radius: 3)
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

    var body: some View {
        let groups = ModeGroups(agents: context.state.agents)
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                HStack(spacing: 5) {
                    Image(systemName: "desktopcomputer")
                        .font(.caption)
                        .foregroundStyle(.secondary)
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
