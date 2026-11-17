import Foundation

/// The app's log file and its error reports -- the same as Python's beacon.log.
///
///     Log.setup(app: "taplock", dataDirectory: dir, analytics: analytics)  // at launch
///     Log.info("locked")
///     Log.report(error)                                                    // a caught error
///
/// - Lines go to `<dataDirectory>/<app>.log`, rotated at 1 MB, three files
///   kept: the file to ask a user for.
/// - An Objective-C exception nothing caught is logged and sent as a fatal
///   `$exception` before the app ends.
/// - A Swift crash (fatalError, a force unwrap of nil, a signal) runs no code;
///   on the next launch the crash report macOS wrote for this process is sent
///   as a fatal `$exception`, and Analytics reports `unclean_exit`.
///
/// Paths in reports have the home folder replaced by ~.
public enum Log {
    private static let queue = DispatchQueue(label: "beacon.log")
    nonisolated(unsafe) private static var fileURL: URL?
    nonisolated(unsafe) private static var analytics: Analytics?
    static let maxBytes = 1 << 20
    static let maxFrames = 30
    static let crashReports = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/DiagnosticReports")

    public static func setup(app: String, dataDirectory: URL, analytics: Analytics? = nil) {
        try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        queue.sync {
            fileURL = dataDirectory.appendingPathComponent("\(app).log")
            Log.analytics = analytics
        }
        // A line per launch: a log is still worth sending when the app itself logs nothing.
        let ctx = System.context()
        info("start \(app) \(analytics?.version ?? "?") on \(ctx["os"]!) \(ctx["os_version"]!) \(ctx["arch"]!), beacon \(ctx["lib_version"]!)")
        if let analytics {
            reportCrashes(process: ProcessInfo.processInfo.processName, dataDirectory: dataDirectory, analytics: analytics)
        }
        NSSetUncaughtExceptionHandler { exception in
            Log.error("uncaught \(exception.name.rawValue): \(exception.reason ?? "")\n"
                      + exception.callStackSymbols.joined(separator: "\n"))
            guard let analytics = Log.queue.sync(execute: { Log.analytics }) else { return }
            analytics.track("$exception", Log.exceptionProps(type: exception.name.rawValue, value: exception.reason ?? "",
                                                             frames: exception.callStackSymbols, handled: false))
            analytics.flushBeforeQuit()
        }
    }

    public static func info(_ message: String) { write("INFO", message) }
    public static func warning(_ message: String) { write("WARNING", message) }
    public static func error(_ message: String) { write("ERROR", message) }

    /// Sends `error` to Error tracking; for an error the app caught and carried on from.
    public static func report(_ error: Error, handled: Bool = true, _ props: [String: Any] = [:]) {
        let value = message(error)
        write(handled ? "ERROR" : "CRITICAL", "\(String(describing: type(of: error))): \(value)")
        guard let analytics = queue.sync(execute: { analytics }) else { return }
        analytics.track("$exception", exceptionProps(type: String(describing: type(of: error)), value: value,
                                                     frames: [], handled: handled).merging(props) { $1 })
    }

    /// What the error says: its LocalizedError description when it has one
    /// (a struct's "\(error)" is only its type name), else its description.
    static func message(_ error: Error) -> String {
        if let localized = error as? LocalizedError, let text = localized.errorDescription { return text }
        return "\(error)"
    }

    static func exceptionProps(type: String, value: String, frames: [String], handled: Bool) -> [String: Any] {
        var exception: [String: Any] = ["type": type, "value": scrub(value),
                                        "mechanism": ["handled": handled, "synthetic": false]]
        if !frames.isEmpty {
            exception["stacktrace"] = ["type": "raw", "frames": frames.prefix(maxFrames).map {
                ["function": scrub($0), "platform": "native"] as [String: Any]
            }]
        }
        return ["$exception_list": [exception], "$exception_level": handled ? "error" : "fatal"]
    }

    /// A zip for support, made when the user asks: the update log, the app's logs
    /// and the machine context, with the home folder replaced by ~. Returns its URL.
    public static func diagnostics(app: String, dataDirectory: URL, to folder: URL) throws -> URL {
        flush()
        let fm = FileManager.default
        let stamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current,
                                                formatOptions: [.withFullDate, .withTime]).replacingOccurrences(of: ":", with: "")
        let work = fm.temporaryDirectory.appendingPathComponent("\(app)-diagnostics-\(stamp)")
        try? fm.removeItem(at: work)
        try fm.createDirectory(at: work, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: work) }
        for name in ["update.log"] + ["", ".1", ".2", ".3"].map({ "\(app).log\($0)" }) {
            let file = dataDirectory.appendingPathComponent(name)
            if let text = try? String(contentsOf: file, encoding: .utf8) {
                try scrub(text).write(to: work.appendingPathComponent(name), atomically: true, encoding: .utf8)
            }
        }
        var system = System.context()
        system["platform"] = System.platformKey
        try JSONSerialization.data(withJSONObject: system, options: [.prettyPrinted, .sortedKeys])
            .write(to: work.appendingPathComponent("system.json"))
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        let zip = folder.appendingPathComponent("\(app)-diagnostics-\(stamp).zip")
        let ditto = try Process.run(URL(fileURLWithPath: "/usr/bin/ditto"), arguments: ["-c", "-k", work.path, zip.path])
        ditto.waitUntilExit()
        return zip
    }

    // MARK: - Crash reports

    /// Sends the crash reports macOS wrote for `process` since the last look.
    /// The first time, only marks where to start: older crashes are not news.
    static func reportCrashes(process: String, dataDirectory: URL, analytics: Analytics, reports: URL = crashReports) {
        try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        let mark = dataDirectory.appendingPathComponent("beacon-crash-check")
        let since = (try? String(contentsOf: mark, encoding: .utf8)).flatMap(TimeInterval.init)
        try? String(Date().timeIntervalSince1970).write(to: mark, atomically: true, encoding: .utf8)
        guard let since else { return }
        let files = (try? FileManager.default.contentsOfDirectory(at: reports, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        for file in files.sorted(by: { $0.path < $1.path })
        where file.lastPathComponent.hasPrefix("\(process)-") && file.pathExtension == "ips" {
            let modified = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            guard let modified, modified.timeIntervalSince1970 > since else { continue }
            if let props = crashReportProps(file) {
                analytics.track("$exception", props)
            } else {
                warning("crash report \(file.lastPathComponent) unreadable")
            }
        }
    }

    /// PostHog's $exception properties for a macOS .ips crash report.
    static func crashReportProps(_ url: URL) -> [String: Any]? {
        guard let text = try? String(contentsOf: url, encoding: .utf8), let newline = text.firstIndex(of: "\n"),
              let report = try? JSONSerialization.jsonObject(with: Data(text[text.index(after: newline)...].utf8)) as? [String: Any],
              let threads = report["threads"] as? [[String: Any]], let faulting = report["faultingThread"] as? Int,
              faulting < threads.count, let rawFrames = threads[faulting]["frames"] as? [[String: Any]] else { return nil }
        let images = (report["usedImages"] as? [[String: Any]] ?? []).map { $0["name"] as? String ?? "" }
        let process = report["procName"] as? String
        let frames: [[String: Any]] = rawFrames.prefix(maxFrames).map { frame in
            let index = frame["imageIndex"] as? Int ?? -1
            let module = images.indices.contains(index) ? images[index] : ""
            var out: [String: Any] = ["function": frame["symbol"] as? String ?? "?", "module": module,
                                      "in_app": module == process, "platform": "native"]
            if let file = frame["sourceFile"] as? String {
                out["filename"] = file
                out["lineno"] = frame["sourceLine"]
            }
            return out
        }
        let exception = report["exception"] as? [String: Any] ?? [:]
        let termination = report["termination"] as? [String: Any] ?? [:]
        var kind = exception["type"] as? String ?? "crash"
        if let signal = exception["signal"] as? String { kind += " (\(signal))" }
        return [
            "$exception_list": [["type": kind, "value": termination["indicator"] as? String ?? exception["subtype"] as? String
                                     ?? exception["signal"] as? String ?? "",
                                 "mechanism": ["handled": false, "synthetic": false],
                                 "stacktrace": ["type": "raw", "frames": frames]]],
            "$exception_level": "fatal",
            "crash_report": true,
            "crashed_version": (report["bundleInfo"] as? [String: Any])?["CFBundleShortVersionString"] as? String ?? "",
            "crashed_at": report["captureTime"] as? String ?? "",
        ]
    }

    // MARK: - File

    private static func write(_ level: String, _ message: String) {
        let line = "\(timestamp()) \(level) \(message)\n"
        queue.async {
            guard let url = fileURL else { return }
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
            if size > maxBytes { rotate(url) }
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(Data(line.utf8))
                try? handle.close()
            } else {
                try? Data(line.utf8).write(to: url)
            }
        }
    }

    /// <app>.log -> .log.1 -> .log.2 -> .log.3, the oldest dropped.
    private static func rotate(_ url: URL) {
        let fm = FileManager.default
        try? fm.removeItem(at: URL(fileURLWithPath: url.path + ".3"))
        for i in stride(from: 2, through: 1, by: -1) {
            try? fm.moveItem(at: URL(fileURLWithPath: url.path + ".\(i)"), to: URL(fileURLWithPath: url.path + ".\(i + 1)"))
        }
        try? fm.moveItem(at: url, to: URL(fileURLWithPath: url.path + ".1"))
    }

    /// Waits until every line written so far is on disk: before quitting, or
    /// before doing something the process may not survive.
    public static func flush() { queue.sync {} }

    /// Local time with its offset, as every beacon log writes it: 2026-11-17 19:58:15+03:00.
    static func timestamp(_ date: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")  // else the user's 12-hour setting wins
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ssxxx"
        return formatter.string(from: date)
    }

    static func scrub(_ text: String) -> String {
        text.replacingOccurrences(of: FileManager.default.homeDirectoryForCurrentUser.path, with: "~")
    }
}
