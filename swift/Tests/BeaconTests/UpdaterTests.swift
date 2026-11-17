import CryptoKit
import Foundation
import Testing
@testable import Beacon

// RFC 8032 section 7.1, test 1 -- the same vector the Python tests use.
private let secret = Data(hex: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
private let rfcSignature = Data(hex: "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
private let privateKey = try! Curve25519.Signing.PrivateKey(rawRepresentation: secret)

@Test func verifiesRFCVector() {
    #expect(privateKey.publicKey.isValidSignature(rfcSignature, for: Data()))
}

private func manifest(_ version: String) -> Data {
    Data("""
    {"version": "\(version)", "notes_url": "https://example.com/notes",
     "assets": {"\(System.platformKey)": {"url": "https://example.com/App.zip", "size": 3, "sha256": "abc"}}}
    """.utf8)
}

@Test func offersNewerSignedRelease() throws {
    let data = manifest("1.2.0")
    let release = try Updater.parseManifest(data, signature: try privateKey.signature(for: data),
                                            keys: [privateKey.publicKey], current: "1.1.9")
    #expect(release?.version == "1.2.0")
    #expect(release?.url == URL(string: "https://example.com/App.zip"))
}

@Test func ignoresSameVersion() throws {
    let data = manifest("1.2.0")
    #expect(try Updater.parseManifest(data, signature: try privateKey.signature(for: data),
                                      keys: [privateKey.publicKey], current: "1.2.0") == nil)
}

@Test func rejectsTamperedManifest() throws {
    let signature = try privateKey.signature(for: manifest("1.2.0"))
    #expect(throws: UpdateError.self) {
        try Updater.parseManifest(manifest("9.9.9"), signature: signature, keys: [privateKey.publicKey], current: "1.0.0")
    }
}

@Test func swapsRunningBundle() async throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    func makeApp(in dir: String, version: String) throws -> URL {
        let exe = root.appendingPathComponent("\(dir)/Demo.app/Contents/MacOS/Demo")
        try FileManager.default.createDirectory(at: exe.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(version.utf8).write(to: exe)
        return root.appendingPathComponent("\(dir)/Demo.app")
    }
    let installed = try makeApp(in: "Applications", version: "1.0.0")
    let built = try makeApp(in: "build", version: "2.0.0")
    let archive = root.appendingPathComponent("Demo.zip")
    let ditto = try Process.run(URL(fileURLWithPath: "/usr/bin/ditto"), arguments: ["-c", "-k", "--keepParent", built.path, archive.path])
    ditto.waitUntilExit()
    let data = try Data(contentsOf: archive)
    let release = Release(version: "2.0.0", url: archive, size: data.count,
                          sha256: SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined(),
                          notesURL: URL(string: "https://example.com")!)

    // The server still offers it when the install asks again.
    let served = root.appendingPathComponent("served")
    try FileManager.default.createDirectory(at: served, withIntermediateDirectories: true)
    // It installs what the server offers at that moment, so the manifest names the real archive.
    let offered = Data("""
    {"version": "2.0.0", "notes_url": "https://example.com/notes",
     "assets": {"\(System.platformKey)": {"url": "\(archive.absoluteString)", "size": \(release.size), "sha256": "\(release.sha256)"}}}
    """.utf8)
    try offered.write(to: served.appendingPathComponent("update.json"))
    try privateKey.signature(for: offered).base64EncodedData().write(to: served.appendingPathComponent("update.json.sig"))
    let updater = try Updater(repo: "me/app", version: "1.0.0",
                              publicKey: privateKey.publicKey.rawRepresentation.base64EncodedString(),
                              dataDirectory: root.appendingPathComponent("data"),
                              baseURL: served.appendingPathComponent("/"), bundleURL: installed)
    #expect(updater.installKind == .bundle)
    // The helper that reopens the app is not started for real: its arguments are checked instead.
    nonisolated(unsafe) var launched: [[String]] = []
    updater.launch = { executable, arguments in launched.append([executable] + arguments) }
    // What the caller holds is stale (a wrong hash): the install must not use it.
    let held = Release(version: "2.0.0", url: archive, size: 1, sha256: "stale", notesURL: URL(string: "https://example.com")!)
    try await updater.install(held)
    #expect(launched.count == 1 && launched[0].first == "/bin/sh" && launched[0].last == installed.path)
    #expect(try String(contentsOf: installed.appendingPathComponent("Contents/MacOS/Demo"), encoding: .utf8) == "2.0.0")
    updater.cleanup()
    let left = try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("data").path)
    #expect(left == ["update.log"])
}

@Test func sessionIDIsUUID7() {
    let id = uuid7()
    #expect(id.count == 36)
    #expect(Array(id)[14] == "7")
}

extension Data {
    init(hex: String) {
        var bytes = [UInt8]()
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            bytes.append(UInt8(hex[index..<next], radix: 16)!)
            index = next
        }
        self.init(bytes)
    }
}
