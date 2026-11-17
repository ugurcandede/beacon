import Foundation
import ServiceManagement

/// Where an app keeps its files: ~/Library/Application Support/<name>,
/// created if missing -- the same folder Python's beacon.system.data_dir gives.
public func dataDirectory(_ name: String) -> URL {
    let url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent(name)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

/// Starting the app at login (SMAppService). Errors are logged, not thrown:
/// the switch in the app's settings shows what actually happened.
public enum LaunchAtLogin {
    public static var isEnabled: Bool { SMAppService.mainApp.status == .enabled }

    /// Registers or unregisters the app; returns whether it is now enabled.
    @discardableResult
    public static func set(_ enabled: Bool) -> Bool {
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            Log.warning("launch at login \(enabled ? "on" : "off") failed: \(error)")
        }
        return isEnabled
    }
}
