import CryptoKit
import Foundation
import Testing
@testable import Beacon

// The signed manifest vector, served from a folder as releases/latest/download.
private let manifests = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().appendingPathComponent("../../../protocol/vectors/manifest").standardized
private let rfcKey = "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo="

private func updater(_ data: URL, serving: Bool = true) throws -> Updater {
    let served = data.appendingPathComponent("served")
    try FileManager.default.createDirectory(at: served, withIntermediateDirectories: true)
    if serving {
        for name in ["update.json", "update.json.sig"] where !FileManager.default.fileExists(atPath: served.appendingPathComponent(name).path) {
            try FileManager.default.copyItem(at: manifests.appendingPathComponent(name), to: served.appendingPathComponent(name))
        }
    }
    return try Updater(repo: "me/app", version: "1.1.9", publicKey: rfcKey, dataDirectory: data,
                       baseURL: served.appendingPathComponent("/"))
}

private func temporary() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }

@Test func aSkippedReleaseIsNotOfferedAgainAutomatically() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let u = try updater(dir)
    let release = try #require(try await u.check())
    #expect(release.version == "1.2.0")
    u.skip(release)
    #expect(try await u.check() == nil)
    #expect(try await u.check(manual: true)?.version == "1.2.0")  // the user asked
    #expect(try await updater(dir).check() == nil)  // the next run remembers
}

@MainActor @Test func updateModelGoesFromCheckToAvailableToSkipped() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    var allowed = false
    let model = UpdateModel(updater: try updater(dir), canInstall: { allowed })
    await model.check()
    guard case let .available(release) = model.state else { Issue.record("not available: \(model.state)"); return }
    #expect(release.version == "1.2.0")
    #expect(!model.installAllowed)
    await model.install()  // not allowed now: nothing happens
    #expect(model.state == .available(release))
    allowed = true
    #expect(model.installAllowed)
    model.skip()
    #expect(model.state == .idle)
    await model.check()
    #expect(model.state == .upToDate)
    await model.check(manual: true)
    #expect(model.state == .available(release))
}

@MainActor @Test func updateModelShowsAFailedCheck() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let model = UpdateModel(updater: try updater(dir, serving: false))
    await model.check()
    guard case .failed = model.state else { Issue.record("expected failed: \(model.state)"); return }
}

@Test func settingsLinksOpenThePrivacyPanes() {
    #expect(Permissions.settingsURL(.accessibility).absoluteString
            == "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
    #expect(Permissions.settingsURL(.inputMonitoring).absoluteString.hasSuffix("?Privacy_ListenEvent"))
    #expect(Permissions.settingsURL(.microphone).absoluteString.hasSuffix("?Privacy_Microphone"))
}

@Test func permissionStatusIsReadWithoutPrompting() {
    // Reading never shows a prompt; the request* functions do, so no test calls them.
    _ = Permissions.accessibility
    #expect([.granted, .denied, .notDetermined].contains(Permissions.inputMonitoring))
    #expect([.granted, .denied, .notDetermined].contains(Permissions.microphone))
}

@Test func waitForReturnsWhenGrantedOrTimesOut() async {
    var polls = 0
    #expect(await Permissions.waitFor(timeout: 5, every: 0.01) { polls += 1; return polls >= 3 })
    #expect(polls == 3)
    #expect(await Permissions.waitFor(timeout: 0.05, every: 0.01) { false } == false)
}

@Test func dataDirectoryIsMadeInApplicationSupport() throws {
    let name = "beacon-test-\(UUID().uuidString)"
    let url = dataDirectory(name)
    defer { try? FileManager.default.removeItem(at: url) }
    #expect(url.path.hasSuffix("Library/Application Support/\(name)"))
    var isDirectory: ObjCBool = false
    #expect(FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue)
}

@Test func launchAtLoginStatusCanBeRead() {
    // A test runner is not an app bundle: only reading is checked here.
    _ = LaunchAtLogin.isEnabled
}

@Test func theManifestIsNeverAnsweredFromACache() async throws {
    let request = Updater.manifestRequest(URL(string: "https://github.com/me/app/releases/latest/download/update.json")!)
    #expect(request.cachePolicy == .reloadIgnoringLocalAndRemoteCacheData)
    // And a changed manifest is seen at once (file:// stands in for the server here).
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let u = try updater(dir)
    #expect(try await u.check()?.version == "1.2.0")
    let served = dir.appendingPathComponent("served")
    try FileManager.default.removeItem(at: served.appendingPathComponent("update.json"))
    try FileManager.default.removeItem(at: served.appendingPathComponent("update.json.sig"))
    for name in ["paused.json", "paused.json.sig"] {
        try FileManager.default.copyItem(at: manifests.appendingPathComponent(name),
                                         to: served.appendingPathComponent(name.replacingOccurrences(of: "paused", with: "update")))
    }
    #expect(try await u.check() == nil)  // paused now
}

// MARK: - Channels and forced updates

private let rfcSecret = Data(hex: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")

/// An Updater on 1.1.0 whose server folder holds `manifests` (name -> extra fields), signed here.
private func serving(_ manifests: [String: [String: Any]], channel: Updater.Channel = .stable,
                     in dir: URL, analytics: Analytics? = nil) throws -> Updater {
    let served = dir.appendingPathComponent("served")
    try FileManager.default.createDirectory(at: served, withIntermediateDirectories: true)
    let key = try Curve25519.Signing.PrivateKey(rawRepresentation: rfcSecret)
    for (name, fields) in manifests {
        var body: [String: Any] = ["notes_url": "https://example.com/notes",
                                   "assets": [System.platformKey: ["url": "https://example.com/App", "size": 1, "sha256": "0"]]]
        body.merge(fields) { $1 }
        let data = try JSONSerialization.data(withJSONObject: body)
        try data.write(to: served.appendingPathComponent(name))
        try key.signature(for: data).base64EncodedData().write(to: served.appendingPathComponent(name + ".sig"))
    }
    return try Updater(repo: "me/app", version: "1.1.0", publicKey: rfcKey, dataDirectory: dir,
                       baseURL: served.appendingPathComponent("/"), analytics: analytics, channel: channel)
}

private func recorder(in dir: URL) -> Analytics {
    Analytics(app: "lumea", version: "1.1.0", dataDirectory: dir.appendingPathComponent("analytics"),
              endpoint: URL(string: "http://127.0.0.1:9/")!)
}

private func tracked(_ a: Analytics) -> [(String, [String: Any])] {
    a.queue.sync { a.pending.map { ($0["name"] as! String, $0["props"] as! [String: Any]) } }
}

@Test func skippingIsReportedAndARequiredSkipIsNot() async throws {
    let dir = temporary(), other = temporary()
    defer { try? FileManager.default.removeItem(at: dir); try? FileManager.default.removeItem(at: other) }
    let a = recorder(in: dir)
    let u = try serving(["update.json": ["version": "1.2.0"]], in: dir, analytics: a)
    u.skip(try #require(try await u.check()))
    let events = tracked(a)
    #expect(events.map(\.0) == ["update_skipped"])
    #expect(events.first?.1["to"] as? String == "1.2.0")
    let b = recorder(in: other)
    let required = try serving(["update.json": ["version": "1.2.0", "min_version": "1.1.5"]], in: other, analytics: b)
    required.skip(try #require(try await required.check()))
    #expect(tracked(b).isEmpty)
}

@Test(arguments: [
    ["version": "1.2.0", "paused": true],  // paused after it was offered
    ["version": "1.2.0", "rollout": 0],    // rolled back to nobody
    ["version": "1.2.1"],                  // replaced by a fix
] as [[String: any Sendable]])
func installRefusesAReleaseNoLongerOffered(now: [String: any Sendable]) async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let a = recorder(in: dir)
    let u = try serving(["update.json": ["version": "1.2.0"]], in: dir, analytics: a)
    let offered = try #require(try await u.check())
    _ = try serving(["update.json": now], in: dir)  // the server changes
    do {
        try await u.install(offered)
        Issue.record("installed")
    } catch let error as UpdateError {
        #expect(error.step == .withdrawn)
    }
    let events = tracked(a)
    #expect(events.map(\.0) == ["update_started", "update_failed"])
    #expect(events.last?.1["step"] as? String == "withdrawn")
}

@Test func stableNeverReadsTheBetaManifest() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let u = try serving(["update.json": ["version": "1.2.0"], "update-beta.json": ["version": "1.3.0"]], in: dir)
    #expect(try await u.check()?.version == "1.2.0")
    u.channel = .beta  // the user turned on beta updates
    #expect(try await u.check()?.version == "1.3.0")
}

@Test func betaTakesStableWhenNewerOrNoBetaIsOut() async throws {
    let dir = temporary(), other = temporary()
    defer { try? FileManager.default.removeItem(at: dir); try? FileManager.default.removeItem(at: other) }
    let older = try serving(["update.json": ["version": "1.2.0"], "update-beta.json": ["version": "1.1.5"]],
                            channel: .beta, in: dir)
    #expect(try await older.check()?.version == "1.2.0")
    let none = try serving(["update.json": ["version": "1.2.0"]], channel: .beta, in: other)
    #expect(try await none.check()?.version == "1.2.0")
}

@Test func aRequiredReleaseCannotBeSkipped() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let u = try serving(["update.json": ["version": "1.2.0", "min_version": "1.1.5"]], in: dir)
    let release = try #require(try await u.check())
    #expect(release.required)
    u.skip(release)
    #expect(try await u.check()?.version == "1.2.0")
}

@MainActor @Test func theBannerModelWillNotSkipARequiredRelease() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let model = UpdateModel(updater: try serving(["update.json": ["version": "1.2.0", "min_version": "1.2.0"]], in: dir))
    await model.check()
    guard case let .available(release) = model.state else { Issue.record("\(model.state)"); return }
    model.skip()
    #expect(model.state == .available(release))
}

@MainActor @Test func theBannerModelShowsWhatIsOfferedNowWhenAReleaseIsWithdrawn() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    let model = UpdateModel(updater: try serving(["update.json": ["version": "1.2.0"]], in: dir), canInstall: { true })
    await model.check()
    _ = try serving(["update.json": ["version": "1.2.0", "paused": true]], in: dir)
    await model.install()
    #expect(model.state == .upToDate)
}

/// Answers api.github.com with a release text.
private final class GitHubStub: URLProtocol {
    nonisolated(unsafe) static var asked: [URL] = []
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "api.github.com" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.asked.append(request.url!)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!,
                            cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"body": "- Faster startup"}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@Test func releaseNotesComeFromTheGitHubRelease() async throws {
    let dir = temporary()
    defer { try? FileManager.default.removeItem(at: dir) }
    URLProtocol.registerClass(GitHubStub.self)
    defer { URLProtocol.unregisterClass(GitHubStub.self) }
    let u = try Updater(repo: "me/app", version: "1.1.0", publicKey: rfcKey, dataDirectory: dir)
    #expect(await u.releaseNotes(for: "1.1.0") == "- Faster startup")
    #expect(GitHubStub.asked.last?.absoluteString == "https://api.github.com/repos/me/app/releases/tags/v1.1.0")
}
