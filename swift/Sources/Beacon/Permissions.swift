import AVFoundation
import AppKit
import ApplicationServices
import IOKit.hid

/// The macOS privacy permissions the apps ask for, the same way in each:
/// read the status without prompting, ask (the system prompt, once), and send
/// the user to the right pane of System Settings when it was refused.
///
///     if !Permissions.accessibility {
///         Permissions.requestAccessibility()                 // the system prompt
///         await Permissions.waitFor { Permissions.accessibility }
///     }
///
/// macOS ties a grant to the app's code signature: builds signed by the same
/// certificate (beacon.build with BEACON_CODESIGN_IDENTITY) keep it across
/// updates; ad-hoc builds lose it every time.
public enum Permissions {
    public enum Status: String, Sendable { case granted, denied, notDetermined }

    // MARK: Accessibility (controlling the computer, e.g. blocking input)

    public static var accessibility: Bool { AXIsProcessTrusted() }

    /// Shows the system prompt that leads to System Settings, if not trusted yet.
    public static func requestAccessibility() {
        // The value of kAXTrustedCheckOptionPrompt; the global itself is not concurrency-safe in Swift 6.
        _ = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary)
    }

    // MARK: Input Monitoring (listening to the keyboard)

    public static var inputMonitoring: Status { status(IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)) }

    public static func requestInputMonitoring() -> Bool { IOHIDRequestAccess(kIOHIDRequestTypeListenEvent) }

    // MARK: Microphone

    public static var microphone: Status {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .notDetermined: return .notDetermined
        default: return .denied
        }
    }

    public static func requestMicrophone() async -> Bool { await AVCaptureDevice.requestAccess(for: .audio) }

    // MARK: System Settings

    public enum Pane: String, Sendable {
        case accessibility = "Privacy_Accessibility"
        case inputMonitoring = "Privacy_ListenEvent"
        case microphone = "Privacy_Microphone"
    }

    public static func settingsURL(_ pane: Pane) -> URL {
        URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane.rawValue)")!
    }

    public static func openSettings(_ pane: Pane) { NSWorkspace.shared.open(settingsURL(pane)) }

    /// Polls `granted` until it is true (the user flipped the switch in
    /// System Settings) or `timeout` passes; returns the last answer.
    @discardableResult
    public static func waitFor(timeout: TimeInterval = 120, every interval: TimeInterval = 1,
                               _ granted: @escaping () -> Bool) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !granted() {
            if Date() >= deadline { return false }
            try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
        }
        return true
    }

    static func status(_ access: IOHIDAccessType) -> Status {
        switch access {
        case kIOHIDAccessTypeGranted: return .granted
        case kIOHIDAccessTypeDenied: return .denied
        default: return .notDetermined
        }
    }
}
