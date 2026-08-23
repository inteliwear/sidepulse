import SwiftUI

@main
struct SidePulseApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .task {
                    LiveMonitorManager.shared.startIfEnabled(model: AppModel.shared)
                }
        }
    }
}
