import Foundation
import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        application.registerForRemoteNotifications()
        EventLog.append("App launched; registered for remote notifications")

        // Arm the Live Activity token observers here rather than in the UI:
        // a push-to-start launches the app in the background with no scene,
        // and the update token must still be captured and uploaded.
        Task { @MainActor in
            LiveMonitorManager.shared.startIfEnabled(model: AppModel.shared)
        }

        if let userInfo = launchOptions?[.remoteNotification] as? [AnyHashable: Any] {
            processNotification(userInfo, source: "Launch notification")
        }

        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in
            AppModel.shared.setPushToken(from: deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        EventLog.append("APNs registration failed: \(error.localizedDescription)")
        Task { @MainActor in
            AppModel.shared.recordError(error)
        }
    }

    func application(
        _ application: UIApplication,
        didReceiveRemoteNotification userInfo: [AnyHashable: Any],
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        let didHandle = processNotification(userInfo, source: "Background push")
        completionHandler(didHandle ? .newData : .failed)
    }

    func application(
        _ app: UIApplication,
        open url: URL,
        options: [UIApplication.OpenURLOptionsKey: Any] = [:]
    ) -> Bool {
        guard let resolution = PushPayloadResolver.resolve(url: url) else {
            return false
        }

        return processResolvedPayload(resolution, source: "Shortcut URL")
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        processNotification(
            response.notification.request.content.userInfo,
            source: "Opened notification"
        )
    }

    @discardableResult
    private func processNotification(_ userInfo: [AnyHashable: Any], source: String) -> Bool {
        let keys = userInfo.keys.map { String(describing: $0) }.sorted().joined(separator: ",")
        EventLog.append("\(source) received; keys=[\(keys)]")

        let resolution = PushPayloadResolver.resolve(userInfo: userInfo)
        return processResolvedPayload(resolution, source: source)
    }

    @discardableResult
    private func processResolvedPayload(_ resolution: PushPayloadResolution, source: String) -> Bool {
        var status: ReceivedPush.WriteStatus = .received
        var errorMessage: String?

        if resolution.isUnsupportedPattern {
            status = .unsupportedPattern
            EventLog.append("\(source) stored unsupported pattern \(resolution.patternName ?? "")")
        } else if let ledText = resolution.resolvedLEDText {
            if DriveWriter.shared.hasSavedFolder {
                do {
                    let targetURL = try DriveWriter.shared.write(ledText)
                    status = .wrote
                    EventLog.append("\(source) wrote \(targetURL.lastPathComponent)")
                } catch {
                    status = .failed
                    errorMessage = error.localizedDescription
                    EventLog.append("\(source) failed: \(error.localizedDescription)")
                }
            } else {
                status = .noFolder
                EventLog.append("\(source) stored LED payload; no SidePulse Dot folder selected")
            }
        } else {
            EventLog.append("\(source) stored general push")
        }

        let push = ReceivedPush(
            source: source,
            title: resolution.displayTitle,
            body: resolution.displayBody,
            patternName: resolution.patternName,
            ledText: resolution.resolvedLEDText,
            payloadSummary: resolution.payloadSummary,
            writeStatus: status,
            errorMessage: errorMessage
        )

        Task { @MainActor in
            AppModel.shared.recordReceivedPush(push)
        }

        return status != .failed
    }
}
