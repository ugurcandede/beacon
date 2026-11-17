import CryptoKit
import Foundation
import Testing
@testable import Beacon

// Behavior found missing by the 1.0.0 review: bad input and failing files must
// not take the app down.

private func temporary() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }
private enum Mode { case hold }

@Test func aPropThatIsNotJSONGoesAsText() throws {
    let date = Date(timeIntervalSince1970: 0)
    let clean = Analytics.jsonable(["when": date, "link": URL(string: "https://example.com/a")!, "ratio": Double.nan,
                                    "mode": Mode.hold, "n": 3, "ok": true, "nested": ["x": [date, 1.5]]]) as! [String: Any]
    #expect(JSONSerialization.isValidJSONObject(clean))
    #expect(clean["when"] as? String == "1970-01-01T00:00:00Z")
    #expect(clean["link"] as? String == "https://example.com/a")
    #expect(clean["ratio"] as? String == "nan")
    #expect(clean["mode"] as? String == "hold")
    #expect(clean["n"] as? Int == 3 && clean["ok"] as? Bool == true)
    #expect(((clean["nested"] as? [String: Any])?["x"] as? [Any])?.last as? Double == 1.5)
}

@Test func trackingAPropThatIsNotJSONDoesNotCrashAndIsSaved() throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let a = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, context: ["started": Date()],
                      endpoint: URL(string: "http://127.0.0.1:9/")!)
    a.track("opened", ["when": Date(), "ratio": Double.infinity])
    a.track("next")
    let names = a.queue.sync { a.pending.map { $0["name"] as! String } }
    #expect(names == ["opened", "next"])
    let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("beacon-analytics.json"))) as! [String: Any]
    #expect((saved["queue"] as! [[String: Any]]).count == 2)
}

@Test func appendingToALogThatCannotBeWrittenFailsQuietly() throws {
    let dir = temporary()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let file = dir.appendingPathComponent("app.log")
    #expect(Log.append(Data("one\n".utf8), to: file))
    #expect(Log.append(Data("two\n".utf8), to: file))
    #expect(try String(contentsOf: file, encoding: .utf8) == "one\ntwo\n")
    #expect(!Log.append(Data("x".utf8), to: dir))  // a folder: cannot be written, must not raise
    #expect(!Log.append(Data("x".utf8), to: dir.appendingPathComponent("missing/app.log")))
}

// MARK: - Analytics

private let offline = URL(string: "http://127.0.0.1:9/")!

/// Answers requests to beacon-hardening.test with the status set for their path.
private final class Answers: URLProtocol {
    nonisolated(unsafe) static var status: [String: Int] = [:]
    nonisolated(unsafe) static var seen: [String] = []
    static let lock = NSLock()
    /// Once for the process and never taken out: tests run in parallel, and one
    /// test unregistering it would send another's request to the real network.
    static let installed: Void = { URLProtocol.registerClass(Answers.self) }()
    static func install() { _ = installed }
    static func set(_ path: String, _ code: Int) { lock.lock(); status[path] = code; lock.unlock() }
    static func count(_ path: String) -> Int { lock.lock(); defer { lock.unlock() }; return seen.filter { $0 == path }.count }

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "beacon-hardening.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        Self.lock.lock(); Self.seen.append(path); let code = Self.status[path] ?? 500; Self.lock.unlock()
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!,
                            cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("<html>error page</html>".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private func savedState(_ dir: URL, _ json: String) throws {
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try Data(json.utf8).write(to: dir.appendingPathComponent("beacon-analytics.json"))
}

@Test func updatedFromIsThereAsSoonAsStartReturns() throws {
    for _ in 0..<20 {  // it was a race: nil most times, set a moment later
        let dir = temporary()
        defer { try? FileManager.default.removeItem(at: dir) }
        try savedState(dir, #"{"install_id":"i","enabled":true,"queue":[],"last_version":"1.0.0"}"#)
        let a = Analytics(app: "lumea", version: "1.1.0", dataDirectory: dir, endpoint: offline)
        a.start()
        #expect(a.updatedFrom == "1.0.0")
    }
}

@Test(arguments: ["null", "123", "[]", #"{"enab"#, #"{"queue": null, "enabled": "yes"}"#])
func aBrokenStateFileNeverTurnsStatisticsOn(content: String) throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    try savedState(dir, content)
    let a = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline)
    #expect(a.enabled == false)
    a.track("x")  // and nothing breaks
    let asking = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline, requireConsent: true)
    #expect(asking.consentNeeded)  // an app that asks, asks again
}

@Test func turningStatisticsOffDropsEvenWhatIsBeingSent() throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let a = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline)
    a.track("sending"); a.track("waiting")
    a.queue.sync { a.inFlight = 1 }  // a send has the first one in its batch
    a.enabled = false
    #expect(a.queue.sync { a.pending.isEmpty })
}

@Test(arguments: [(400, 0), (413, 0), (422, 0), (403, 2), (429, 2), (500, 2)])
func onlyAnAnswerAboutTheBatchItselfDropsIt(status: Int, kept: Int) throws {
    Answers.install()
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let path = "/events-\(status)"
    Answers.set(path, status)
    let a = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: URL(string: "https://beacon-hardening.test\(path)")!)
    a.track("a"); a.track("b")
    a.queue.sync { a.tick() }
    for _ in 0..<500 where Answers.count(path) == 0 || a.queue.sync(execute: { a.inFlight }) != 0 { usleep(10_000) }
    a.queue.sync {}
    let left = a.queue.sync { a.pending.filter { ["a", "b"].contains($0["name"] as? String ?? "") }.count }
    #expect(left == kept)
}

// MARK: - Updates

@MainActor @Test func aFailedAutomaticCheckSaysNothingAndKeepsWhatWasKnown() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let served = dir.appendingPathComponent("served")
    let updater = try Updater(repo: "me/app", version: "1.0.0", publicKey: "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=",
                              dataDirectory: dir, baseURL: served.appendingPathComponent("/"))
    let model = UpdateModel(updater: updater)
    await model.check()  // nothing is served: the check fails, as at an offline launch
    #expect(model.state == .idle)
    await model.check(manual: true)  // the user asked: now it is said
    guard case .failed = model.state else { Issue.record("\(model.state)"); return }
}

@MainActor @Test func aCheckDoesNotResetAnInstallUnderWay() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let updater = try Updater(repo: "me/app", version: "1.0.0", publicKey: "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=",
                              dataDirectory: dir, baseURL: dir.appendingPathComponent("served/"))
    let model = UpdateModel(updater: updater)
    let release = Release(version: "2.0.0", url: URL(string: "https://example.com/a.zip")!, size: 1, sha256: "x",
                          notesURL: URL(string: "https://example.com")!)
    model.state = .installing(release)
    await model.check()  // the daily automatic check, while brew is still upgrading
    #expect(model.state == .installing(release))
    await model.check(manual: true)
    #expect(model.state == .installing(release))
}

@Test func theRelaunchPassesThePathAsAnArgumentNotAsScriptText() throws {
    let bundle = URL(fileURLWithPath: "/Users/x/Tom's Apps/Demo.app")
    let args = Updater.relaunchArguments(pid: 42, bundle: bundle)
    #expect(args[0] == "-c" && !args[1].contains("Tom") && args[2] == "42" && args[3] == bundle.path)
    // The script itself parses, and with a stand-in `open` the path arrives whole.
    let dir = temporary()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let fake = dir.appendingPathComponent("open")
    try Data("#!/bin/sh\nprintf '%s' \"$1\" > \"$OUT\"\n".utf8).write(to: fake)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: fake.path)
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/sh")
    process.arguments = Updater.relaunchArguments(pid: 999_999, bundle: bundle)  // no such process: opens at once
    process.environment = ["PATH": "\(dir.path):/usr/bin:/bin", "OUT": dir.appendingPathComponent("out").path]
    try process.run()
    process.waitUntilExit()
    #expect(process.terminationStatus == 0)
    #expect(try String(contentsOf: dir.appendingPathComponent("out"), encoding: .utf8) == bundle.path)
}

@Test func aDownloadThatAnswersWithAnErrorPageIsAFailedDownload() async throws {
    Answers.install()
    Answers.set("/App.zip", 404)
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let app = dir.appendingPathComponent("Applications/Demo.app")
    try FileManager.default.createDirectory(at: app.appendingPathComponent("Contents/MacOS"), withIntermediateDirectories: true)
    let served = dir.appendingPathComponent("served")
    try FileManager.default.createDirectory(at: served, withIntermediateDirectories: true)
    let key = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(hex: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"))
    let manifest = Data("""
    {"version": "2.0.0", "notes_url": "https://example.com/notes",
     "assets": {"\(System.platformKey)": {"url": "https://beacon-hardening.test/App.zip", "size": 3, "sha256": "abc"}}}
    """.utf8)
    try manifest.write(to: served.appendingPathComponent("update.json"))
    try key.signature(for: manifest).base64EncodedData().write(to: served.appendingPathComponent("update.json.sig"))
    let updater = try Updater(repo: "me/app", version: "1.0.0", publicKey: key.publicKey.rawRepresentation.base64EncodedString(),
                              dataDirectory: dir.appendingPathComponent("data"), baseURL: served.appendingPathComponent("/"), bundleURL: app)
    updater.launch = { _, _ in Issue.record("relaunched after a failed download") }
    let release = try #require(try await updater.check())
    do {
        try await updater.install(release)
        Issue.record("installed an error page")
    } catch let error as UpdateError {
        #expect(error.step == .download)  // as the Python client says; it was "verify"
    }
    #expect(FileManager.default.fileExists(atPath: app.path))  // the installed app is untouched
}

@Test func anUncaughtExceptionIsSentWithoutMarkingTheRunAsEnded() throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let a = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline)
    a.start()
    a.flush(timeout: 0.2)  // what the uncaught-exception handler calls
    a.queue.sync {}
    let names = a.queue.sync { a.pending.map { $0["name"] as! String } }
    #expect(!names.contains("app_quit"))
    let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("beacon-analytics.json"))) as! [String: Any]
    #expect(saved["running"] as? Bool == true)  // so the next launch reports unclean_exit
}

@Test func aCrashReportWithANegativeThreadIndexIsSkipped() throws {
    let dir = temporary()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let file = dir.appendingPathComponent("App-1.ips")
    try Data("{\"app_name\": \"App\"}\n{\"faultingThread\": -1, \"threads\": [{\"frames\": []}]}".utf8).write(to: file)
    #expect(Log.crashReportProps(file) == nil)
}

@Test func theMachineIsDescribedAsThePythonClientDoes() {
    #expect(System.osVersion(15, 1, 0) == "15.1")
    #expect(System.osVersion(15, 1, 1) == "15.1.1")
    #expect(System.locale("en_TR@rg=trzzzz") == "en_TR")
    #expect(System.locale("tr_TR") == "tr_TR")
}

// MARK: - Flags and consent

@Test func flagsAreNotAskedForBeforeConsentOrWhenStatisticsAreOff() async throws {
    Answers.install()
    Answers.set("/flags-consent", 200)
    let endpoint = URL(string: "https://beacon-hardening.test/flags-consent")!
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let waiting = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir.appendingPathComponent("a"), endpoint: offline, requireConsent: true)
    #expect(await Flags(app: "lumea", dataDirectory: dir, endpoint: endpoint, analytics: waiting).refresh() == false)
    let off = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir.appendingPathComponent("b"), enabled: false, endpoint: offline)
    #expect(await Flags(app: "lumea", dataDirectory: dir, endpoint: endpoint, analytics: off).refresh() == false)
    #expect(Answers.count("/flags-consent") == 0)
    let on = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir.appendingPathComponent("c"), endpoint: offline)
    _ = await Flags(app: "lumea", dataDirectory: dir, endpoint: endpoint, analytics: on).refresh()
    _ = await Flags(app: "lumea", dataDirectory: dir, endpoint: endpoint).refresh()  // an app without analytics still gets flags
    #expect(Answers.count("/flags-consent") == 2)
}

@Test(arguments: ["[]", "null", #"{"flags": null, "payloads": 3}"#])
func aBrokenFlagsCacheReadsAsNoFlags(content: String) throws {
    let dir = temporary()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    try Data(content.utf8).write(to: dir.appendingPathComponent("beacon-flags.json"))
    let flags = Flags(app: "lumea", dataDirectory: dir, endpoint: offline)
    #expect(flags.enabled("x") == false && flags.value("x") == nil && flags.payload("x") == nil)
}

@Test func theConsentPromptSaysWhatIsSent() {
    let text = ConsentView.text.lowercased()
    for word in ["machine", "city", "error", "never"] { #expect(text.contains(word)) }
}

@Test func flagsAreAskedForUnderTheIdEventsAreCountedUnder() {
    #expect(Flags.id(machineID: "m1", installID: "i1") == "m1")
    #expect(Flags.id(machineID: "", installID: "i1") == "i1")  // a machine whose OS gives no id
    #expect(Flags.id(machineID: "", installID: nil) == nil)    // nobody to ask for: no request
}
