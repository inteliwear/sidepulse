import Foundation
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Shared model for the agent-monitor Live Activity. The mini's
/// `sidepulse live-activity` daemon builds the matching JSON, so field
/// names here are wire format — change them in both places or not at all.
#if canImport(ActivityKit)
struct AgentActivityAttributes: ActivityAttributes {
    struct AgentRow: Codable, Hashable, Identifiable {
        var id: String
        var name: String
        var mode: String
        var detail: String?
        var provider: String?
        var cwd: String?
    }

    struct ContentState: Codable, Hashable {
        var aggregateMode: String
        var activeCount: Int
        var agents: [AgentRow]
        /// Unix timestamp; a Double survives JSONDecoder's default date
        /// handling, which a Date in an APNs content-state does not.
        var updatedAt: Double

        var updatedDate: Date { Date(timeIntervalSince1970: updatedAt) }
    }

    var hostLabel: String
}
#endif

/// Mode presentation shared by the app and the Live Activity views.
enum AgentModeStyle {
    static func label(_ mode: String) -> String {
        switch mode {
        case "idle_ready": return "Idle"
        case "working": return "Working"
        case "tool_running": return "Tool Running"
        case "waiting_for_input": return "Needs Input"
        case "long_task_progress": return "Long Task"
        case "blocked_error": return "Blocked"
        case "completed": return "Done"
        default: return "Unknown"
        }
    }

    /// (red, green, blue) 0-1, matching the LED palette used on the Dot.
    static func rgb(_ mode: String) -> (Double, Double, Double) {
        switch mode {
        case "working": return (0.0, 0.9, 0.9)
        case "tool_running": return (0.2, 0.5, 1.0)
        case "waiting_for_input": return (1.0, 0.6, 0.0)
        case "long_task_progress": return (0.6, 0.3, 1.0)
        case "blocked_error": return (1.0, 0.2, 0.2)
        case "completed": return (0.2, 0.9, 0.3)
        case "idle_ready": return (0.5, 0.5, 0.5)
        default: return (0.4, 0.4, 0.4)
        }
    }
}
