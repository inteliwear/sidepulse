import SwiftUI
import WidgetKit

@main
struct SidePulseWidgetsBundle: WidgetBundle {
    var body: some Widget {
        if #available(iOSApplicationExtension 16.2, *) {
            AgentLiveActivity()
        }
    }
}
