import Foundation

/// Feature flags and remote config from PostHog, through the beacon Worker --
/// the same as Python's beacon.flags.
///
///     let flags = Flags(app: "taplock", dataDirectory: dir)
///     await flags.refresh()                          // at launch; keeps the last answer offline
///     if flags.enabled("new-editor") { ... }
///     let theme = flags.value("theme") as? String ?? "light"
///
/// A flag can be turned off in PostHog without a release, for every app or by
/// product, or for a percentage of machines. The answer is kept in
/// `<dataDirectory>/beacon-flags.json` and used until the next refresh.
public final class Flags: @unchecked Sendable {
    public static let defaultEndpoint = URL(string: "https://beacon-api.ugurcan-dede.workers.dev/v1/flags")!

    private let app: String
    private let endpoint: URL
    private let fileURL: URL
    private let lock = NSLock()
    private var answer: [String: Any] = ["flags": [String: Any](), "payloads": [String: Any]()]

    public init(app: String, dataDirectory: URL, endpoint: URL = defaultEndpoint) {
        self.app = app
        self.endpoint = endpoint
        fileURL = dataDirectory.appendingPathComponent("beacon-flags.json")
        if let data = try? Data(contentsOf: fileURL),
           let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            answer = saved
        }
    }

    /// Asks again; false (and the last answer kept) when it cannot.
    @discardableResult
    public func refresh(timeout: TimeInterval = 5) async -> Bool {
        var parts = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "app", value: app), URLQueryItem(name: "id", value: System.machineID())]
        let request = URLRequest(url: parts.url!, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: timeout)
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        let fresh: [String: Any] = ["flags": json["flags"] ?? [String: Any](), "payloads": json["payloads"] ?? [String: Any](),
                                    "fetched_at": Int(Date().timeIntervalSince1970)]
        lock.withLock { answer = fresh }
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? JSONSerialization.data(withJSONObject: fresh).write(to: fileURL, options: .atomic)
        return true
    }

    /// On: true, or any variant.
    public func enabled(_ key: String) -> Bool {
        switch value(key) {
        case let on as Bool: return on
        case .some: return true
        case nil: return false
        }
    }

    /// The variant (a String), true or false; nil for a flag PostHog does not know.
    public func value(_ key: String) -> Any? {
        lock.withLock { (answer["flags"] as? [String: Any])?[key] }
    }

    public func payload(_ key: String) -> Any? {
        lock.withLock { (answer["payloads"] as? [String: Any])?[key] }
    }
}
