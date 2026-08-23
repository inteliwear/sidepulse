import Foundation
import Security

/// Snapshot handoff between the watch app and its widget extension.
///
/// Widgets cannot use WatchConnectivity and App Groups need portal
/// registration; a shared keychain access group needs neither — both
/// targets just declare the same keychain-access-groups entitlement.
enum SharedSnapshotStore {
    private static let service = "io.sidepulse.watch.snapshot"

    private static func query() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: "latest",
        ]
    }

    static func save(_ data: Data) {
        var attributes = query()
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        attributes[kSecValueData as String] = data
        let status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecDuplicateItem {
            SecItemUpdate(
                query() as CFDictionary,
                [kSecValueData as String: data] as CFDictionary
            )
        }
    }

    static func load() -> AgentSnapshot? {
        var request = query()
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else {
            return nil
        }
        return try? JSONDecoder().decode(AgentSnapshot.self, from: data)
    }
}
