import ActivityKit
import SwiftUI
import WidgetKit

@available(iOSApplicationExtension 16.2, *)
struct AgentLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AgentActivityAttributes.self) { context in
            LockScreenView(context: context)
                .activityBackgroundTint(Color.black.opacity(0.8))
                .activitySystemActionForegroundColor(.white)
                .widgetURL(URL(string: "sidepulse://agents"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 6) {
                        modeDot(context.state.aggregateMode, size: 10)
                        Text(context.attributes.hostLabel)
                            .font(.caption.bold())
                            .foregroundStyle(.secondary)
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text("\(context.state.activeCount) active")
                        .font(.caption.bold())
                        .foregroundStyle(modeColor(context.state.aggregateMode))
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(context.state.agents.prefix(4)) { agent in
                            AgentRowView(agent: agent)
                        }
                    }
                    .widgetURL(URL(string: "sidepulse://agents"))
                }
            } compactLeading: {
                modeDot(context.state.aggregateMode, size: 10)
            } compactTrailing: {
                Text("\(context.state.activeCount)")
                    .font(.caption2.bold())
                    .foregroundStyle(modeColor(context.state.aggregateMode))
            } minimal: {
                modeDot(context.state.aggregateMode, size: 10)
            }
        }
    }
}

@available(iOSApplicationExtension 16.2, *)
private struct LockScreenView: View {
    let context: ActivityViewContext<AgentActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                modeDot(context.state.aggregateMode, size: 12)
                Text("Agents on \(context.attributes.hostLabel)")
                    .font(.subheadline.bold())
                Spacer()
                Text("\(context.state.activeCount) active")
                    .font(.caption.bold())
                    .foregroundStyle(modeColor(context.state.aggregateMode))
            }

            if context.state.agents.isEmpty {
                Text("All quiet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(context.state.agents.prefix(5)) { agent in
                    AgentRowView(agent: agent)
                }
            }

            Text(context.state.updatedDate, style: .relative)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(12)
    }
}

private struct AgentRowView: View {
    let agent: AgentActivityAttributes.AgentRow

    var body: some View {
        HStack(spacing: 6) {
            modeDot(agent.mode, size: 8)
            Text(agent.name)
                .font(.caption)
                .lineLimit(1)
            Spacer(minLength: 4)
            Text(agent.detail ?? AgentModeStyle.label(agent.mode))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }
}

private func modeColor(_ mode: String) -> Color {
    let (r, g, b) = AgentModeStyle.rgb(mode)
    return Color(red: r, green: g, blue: b)
}

private func modeDot(_ mode: String, size: CGFloat) -> some View {
    Circle()
        .fill(modeColor(mode))
        .frame(width: size, height: size)
}
