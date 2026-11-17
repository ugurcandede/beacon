import Foundation
import Testing
@testable import Beacon

private let crashVectors = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().appendingPathComponent("../../../protocol/vectors/crash").standardized

private func temporary() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
}

private func offlineAnalytics(_ dir: URL) -> Analytics {
    Analytics(app: "taplock", version: "1.0.0", dataDirectory: dir, endpoint: URL(string: "http://127.0.0.1:9/")!)
}

private extension Analytics {
    var names: [String] { queue.sync { pending.map { $0["name"] as! String } } }
}

@Test func crashReportPropsMatchTheVector() throws {
    let vector = try JSONSerialization.jsonObject(with: Data(contentsOf: crashVectors.appendingPathComponent("cases.json"))) as! [String: Any]
    for case let c as [String: Any] in vector["cases"] as! [Any] {
        let props = try #require(Log.crashReportProps(crashVectors.appendingPathComponent(c["file"] as! String)))
        // Through JSON, as it is sent, so number and bool types compare as the vector's.
        let sent = try JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject: props)) as! NSDictionary
        #expect(sent == c["expected"] as! NSDictionary, "\(c["file"]!)")
    }
}

@Test func crashReportsAreSentOnceAndOnlyNewOnes() throws {
    let root = temporary(), reports = root.appendingPathComponent("reports"), data = root.appendingPathComponent("data")
    defer { try? FileManager.default.removeItem(at: root) }
    try FileManager.default.createDirectory(at: reports, withIntermediateDirectories: true)
    let source = crashVectors.appendingPathComponent("TapLockApp-2026-11-16-154836.ips")
    let a = offlineAnalytics(data)
    try FileManager.default.copyItem(at: source, to: reports.appendingPathComponent("TapLockApp-old.ips"))
    Log.reportCrashes(process: "TapLockApp", dataDirectory: data, analytics: a, reports: reports)  // first look
    #expect(a.names.isEmpty)
    Thread.sleep(forTimeInterval: 0.05)
    // copyItem keeps the source's date; a real report is written when the app crashes.
    for name in ["TapLockApp-new.ips", "OtherApp-new.ips"] {
        let copy = reports.appendingPathComponent(name)
        try FileManager.default.copyItem(at: source, to: copy)
        try FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: copy.path)
    }
    Log.reportCrashes(process: "TapLockApp", dataDirectory: data, analytics: a, reports: reports)
    #expect(a.names == ["$exception"])
    Log.reportCrashes(process: "TapLockApp", dataDirectory: data, analytics: a, reports: reports)
    #expect(a.names == ["$exception"])
}

@Test func reportedErrorsHideTheHomeFolder() {
    let props = Log.exceptionProps(type: "CocoaError", value: "no file at \(NSHomeDirectory())/x.txt",
                                   frames: ["0 TapLock \(NSHomeDirectory())/a"], handled: true)
    let exception = (props["$exception_list"] as! [[String: Any]])[0]
    #expect(exception["value"] as? String == "no file at ~/x.txt")
    #expect(props["$exception_level"] as? String == "error")
    let frames = (exception["stacktrace"] as! [String: Any])["frames"] as! [[String: Any]]
    #expect(frames[0]["function"] as? String == "0 TapLock ~/a")
}

@Test func uncleanExitIsReportedOnTheNextStartOnly() {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let first = offlineAnalytics(dir)
    first.start()
    first.queue.sync {}  // its state is saved; the process "dies" here
    let second = offlineAnalytics(dir)
    second.start()
    second.queue.sync {}
    #expect(second.names.filter { $0 == "unclean_exit" }.count == 1)
    second.flushBeforeQuit(timeout: 0.1)
    let third = offlineAnalytics(dir)
    third.start()
    third.queue.sync {}
    #expect(third.names.filter { $0 == "unclean_exit" }.count == 1)  // only second's, still queued
}

/// The time format of every beacon log (and the Python client's): 2026-11-17 19:58:15+03:00.
private func stamped(_ text: String) -> Bool {
    text.range(of: #"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d "#, options: .regularExpression) != nil
}

@Test func updateLogLinesHaveTheSameTimeFormat() throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let u = try Updater(repo: "me/app", version: "1.0.0", publicKey: "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=", dataDirectory: dir)
    u.skip(Release(version: "1.1.0", url: URL(string: "https://example.com")!, size: 0, sha256: "", notesURL: URL(string: "https://example.com")!))
    let text = try String(contentsOf: u.logURL, encoding: .utf8)
    #expect(stamped(text) && text.hasSuffix("skip 1.1.0\n"))
}

@Test func logFileRotates() throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    Log.setup(app: "rotate-test", dataDirectory: dir)
    Log.flush()
    let first = try String(contentsOf: dir.appendingPathComponent("rotate-test.log"), encoding: .utf8)
    #expect(first.contains("INFO start rotate-test ? on macOS"))  // every launch, even when the app logs nothing
    #expect(stamped(first))  // local time with its offset
    let line = String(repeating: "x", count: 1000)
    for _ in 0..<1200 { Log.info(line) }  // ~1.2 MB
    Log.flush()
    let log = dir.appendingPathComponent("rotate-test.log")
    #expect(FileManager.default.fileExists(atPath: log.path + ".1"))
    let size = try FileManager.default.attributesOfItem(atPath: log.path)[.size] as! Int
    #expect(size < Log.maxBytes)
}

@Test func reportedErrorsSayWhatTheErrorSays() {
    struct Described: LocalizedError { var errorDescription: String? { "disk full at \(NSHomeDirectory())/x" } }
    struct Plain: Error {}
    #expect(Log.message(Described()) == "disk full at \(NSHomeDirectory())/x")
    #expect(Log.message(Plain()) == "Plain()")
    let props = Log.exceptionProps(type: "Described", value: Log.message(Described()), frames: [], handled: true)
    #expect((props["$exception_list"] as! [[String: Any]])[0]["value"] as? String == "disk full at ~/x")
}

@Test func diagnosticsZipHasTheLogsAndTheMachineWithoutTheHomeFolder() throws {
    let root = temporary(), data = root.appendingPathComponent("data")
    defer { try? FileManager.default.removeItem(at: root) }
    try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
    try "check: from \(NSHomeDirectory())/x\n".write(to: data.appendingPathComponent("update.log"), atomically: true, encoding: .utf8)
    try "hello\n".write(to: data.appendingPathComponent("taplock.log"), atomically: true, encoding: .utf8)
    let zip = try Log.diagnostics(app: "taplock", dataDirectory: data, to: root.appendingPathComponent("out"))
    let unpacked = root.appendingPathComponent("unpacked")
    let ditto = try Process.run(URL(fileURLWithPath: "/usr/bin/ditto"), arguments: ["-x", "-k", zip.path, unpacked.path])
    ditto.waitUntilExit()
    let names = try FileManager.default.contentsOfDirectory(atPath: unpacked.path).sorted()
    #expect(names == ["system.json", "taplock.log", "update.log"])
    #expect(try String(contentsOf: unpacked.appendingPathComponent("update.log"), encoding: .utf8) == "check: from ~/x\n")
}
