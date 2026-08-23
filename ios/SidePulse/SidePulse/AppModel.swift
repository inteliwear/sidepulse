import Foundation
import UIKit

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var pushToken: String {
        didSet { UserDefaults.standard.set(pushToken, forKey: Defaults.pushToken) }
    }

    @Published var selectedFolderPath: String = "No USB folder selected"
    @Published var hasFolderAccess: Bool = false

    @Published var ledText: String {
        didSet { UserDefaults.standard.set(ledText, forKey: Defaults.ledText) }
    }

    @Published var serverBaseURL: String {
        didSet { UserDefaults.standard.set(serverBaseURL, forKey: Defaults.serverBaseURL) }
    }

    @Published var sharedSecret: String {
        didSet { UserDefaults.standard.set(sharedSecret, forKey: Defaults.sharedSecret) }
    }

    @Published var liveMonitorEnabled: Bool {
        didSet { UserDefaults.standard.set(liveMonitorEnabled, forKey: Defaults.liveMonitorEnabled) }
    }

    @Published var liveMonitorServerURL: String {
        didSet { UserDefaults.standard.set(liveMonitorServerURL, forKey: Defaults.liveMonitorServerURL) }
    }

    @Published var lastMessage: String = "Ready"
    @Published var eventLog: [String] = []
    @Published var receivedPushes: [ReceivedPush] {
        didSet { persistReceivedPushes() }
    }

    private enum Defaults {
        static let pushToken = "pushToken"
        static let ledText = "ledText"
        static let serverBaseURL = "serverBaseURL"
        static let sharedSecret = "sharedSecret"
        static let receivedPushes = "receivedPushes"
        static let liveMonitorEnabled = "liveMonitorEnabled"
        static let liveMonitorServerURL = "liveMonitorServerURL"
    }

    private init() {
        self.pushToken = UserDefaults.standard.string(forKey: Defaults.pushToken) ?? ""
        self.ledText = UserDefaults.standard.string(forKey: Defaults.ledText) ?? """
        #404040 1.4s pulse
        off 400ms none
        repeat
        """
        self.serverBaseURL = UserDefaults.standard.string(forKey: Defaults.serverBaseURL) ?? "http://127.0.0.1:8787"
        self.sharedSecret = UserDefaults.standard.string(forKey: Defaults.sharedSecret) ?? ""
        self.liveMonitorEnabled = UserDefaults.standard.bool(forKey: Defaults.liveMonitorEnabled)
        self.liveMonitorServerURL = UserDefaults.standard.string(forKey: Defaults.liveMonitorServerURL)
            ?? "http://macmini8005:8787"
        self.receivedPushes = Self.loadReceivedPushes()
        self.eventLog = EventLog.entries()
        refreshFolderStatus()
    }

    func setPushToken(from deviceToken: Data) {
        pushToken = deviceToken.map { String(format: "%02x", $0) }.joined()
        EventLog.append("APNs token updated")
        lastMessage = "Push token updated"
        refreshEventLog()
    }

    func refreshFolderStatus() {
        hasFolderAccess = DriveWriter.shared.hasSavedFolder
        selectedFolderPath = DriveWriter.shared.savedFolderDisplayName
    }

    func recordWriteSuccess(_ message: String) {
        EventLog.append(message)
        lastMessage = message
        refreshFolderStatus()
        refreshEventLog()
    }

    func recordError(_ error: Error) {
        let message = error.localizedDescription
        EventLog.append("Error: \(message)")
        lastMessage = message
        refreshFolderStatus()
        refreshEventLog()
    }

    func recordReceivedPush(_ push: ReceivedPush) {
        var next = [push]
        next.append(contentsOf: receivedPushes)
        if next.count > 50 {
            next.removeLast(next.count - 50)
        }
        receivedPushes = next

        let status = push.writeStatus.displayName
        EventLog.append("\(push.source): \(push.title) (\(status))")
        lastMessage = "\(push.title) - \(status)"
        refreshFolderStatus()
        refreshEventLog()
    }

    func clearReceivedPushes() {
        receivedPushes = []
        lastMessage = "Cleared received pushes"
    }

    func refreshEventLog() {
        eventLog = EventLog.entries()
    }

    func clearEventLog() {
        EventLog.clear()
        refreshEventLog()
    }

    var preauthenticatedPostURL: String? {
        let trimmedBase = serverBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !trimmedBase.isEmpty, !pushToken.isEmpty else {
            return nil
        }

        var components = URLComponents(string: trimmedBase + "/v1/push")
        var items = [
            URLQueryItem(name: "device_token", value: pushToken)
        ]

        if !sharedSecret.isEmpty {
            items.append(URLQueryItem(name: "key", value: sharedSecret))
        }

        components?.queryItems = items
        return components?.url?.absoluteString
    }

    var pushEndpointURL: String? {
        let trimmedBase = serverBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !trimmedBase.isEmpty else {
            return nil
        }
        return trimmedBase + "/v1/push"
    }

    var curlExample: String? {
        guard let pushEndpointURL else {
            return nil
        }

        let tokenLine = pushToken.isEmpty ? "" : "\n  -d '{\"device_token\":\"\(pushToken)\",\"pattern\":\"green_pulse_2\"}'"
        let authHeader = sharedSecret.isEmpty ? "" : " \\\n  -H \"Authorization: Bearer \(sharedSecret)\""
        if tokenLine.isEmpty {
            return """
            curl -X POST \(pushEndpointURL)\(authHeader) \\
              -H "content-type: application/json" \\
              -d '{"pattern":"green_pulse_2"}'
            """
        }

        return """
        curl -X POST \(pushEndpointURL)\(authHeader) \\
          -H "content-type: application/json" \(tokenLine)
        """
    }

    var shortcutWriteURL: String? {
        guard !ledText.isEmpty else {
            return nil
        }

        var components = URLComponents()
        components.scheme = "sidepulse"
        components.host = "write"
        components.queryItems = [
            URLQueryItem(name: "text", value: ledText)
        ]
        return components.url?.absoluteString
    }

    func shortcutPatternURL(for pattern: LEDPattern) -> String? {
        var components = URLComponents()
        components.scheme = "sidepulse"
        components.host = "write"
        components.queryItems = [
            URLQueryItem(name: "pattern", value: pattern.name)
        ]
        return components.url?.absoluteString
    }

    private func persistReceivedPushes() {
        if let data = try? JSONEncoder().encode(receivedPushes) {
            UserDefaults.standard.set(data, forKey: Defaults.receivedPushes)
        }
    }

    private static func loadReceivedPushes() -> [ReceivedPush] {
        guard let data = UserDefaults.standard.data(forKey: Defaults.receivedPushes),
              let pushes = try? JSONDecoder().decode([ReceivedPush].self, from: data) else {
            return []
        }
        return Array(pushes.prefix(50))
    }
}

extension ReceivedPush.WriteStatus {
    var displayName: String {
        switch self {
        case .received:
            return "Received"
        case .wrote:
            return "Wrote LEDS.LED"
        case .noFolder:
            return "Folder needed"
        case .failed:
            return "Failed"
        case .unsupportedPattern:
            return "Unknown pattern"
        }
    }
}
