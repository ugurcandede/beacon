import CryptoKit
import Foundation
import Testing
@testable import Beacon

// The shared vectors in protocol/vectors: every client checks the same cases.
private let vectors = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().appendingPathComponent("../../../protocol/vectors").standardized

private func json(_ path: String) throws -> Any {
    try JSONSerialization.jsonObject(with: Data(contentsOf: vectors.appendingPathComponent(path)))
}

private struct VersionCase: Decodable, Sendable { let candidate, current: String; let newer: Bool }
private struct ManifestCases: Decodable { let cases: [ManifestCase] }
private struct ManifestCase: Decodable, Sendable, CustomTestStringConvertible {
    let file: String; let signature: String?; let keys: [String]; let current: String
    let machine_id: String?; let offered: String?; let required: Bool?; let error: String?; let why: String?
    var testDescription: String { why ?? "\(file) \(current)" }
}
private struct RolloutCase: Decodable, Sendable { let machine_id: String; let version: String; let bucket: Int }
private struct RolloutCases: Decodable { let cases: [RolloutCase] }

private func decode<T: Decodable>(_ type: T.Type, _ path: String) -> T {
    try! JSONDecoder().decode(T.self, from: Data(contentsOf: vectors.appendingPathComponent(path)))
}

private let manifestCases = decode(ManifestCases.self, "manifest/cases.json")

@Test(arguments: decode([VersionCase].self, "versions.json"))
private func versions(_ c: VersionCase) {
    #expect(Updater.isNewer(c.candidate, than: c.current) == c.newer)
}

@Test(arguments: manifestCases.cases)
private func manifest(_ c: ManifestCase) throws {
    let folder = vectors.appendingPathComponent("manifest")
    let data = try Data(contentsOf: folder.appendingPathComponent(c.file))
    let sigText = try String(contentsOf: folder.appendingPathComponent(c.signature ?? c.file + ".sig"), encoding: .utf8)
    let signature = Data(base64Encoded: sigText)!
    let keys = try c.keys.map { try Curve25519.Signing.PublicKey(rawRepresentation: Data(base64Encoded: $0)!) }
    if let step = c.error {
        do {
            _ = try Updater.parseManifest(data, signature: signature, keys: keys, current: c.current, machineID: c.machine_id ?? "")
            Issue.record("\(c.file): accepted, expected \(step)")
        } catch let error as UpdateError {
            #expect(error.step.rawValue == step)  // the step the code gives, against the vector's
        }
        return
    }
    let release = try Updater.parseManifest(data, signature: signature, keys: keys, current: c.current,
                                            machineID: c.machine_id ?? "")
    #expect(release?.version == c.offered)
    if c.offered != nil {
        #expect(release?.url.lastPathComponent == "App-\(System.platformKey)")
        #expect(release?.required == (c.required ?? false))
    }
}

@Test(arguments: decode(RolloutCases.self, "rollout.json").cases)
private func rolloutBucket(_ c: RolloutCase) {
    #expect(Updater.rolloutBucket(c.machine_id, c.version) == c.bucket)
}

@Test func batchHasTheProtocolFields() throws {
    let fields = try json("events.json") as! [String: Any]
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    URLProtocol.registerClass(Capture.self)
    defer { URLProtocol.unregisterClass(Capture.self) }

    let analytics = Analytics(app: "lumea", version: "1.4.0", dataDirectory: root,
                              endpoint: URL(string: "https://capture.invalid/v1/events")!)
    analytics.track("x")
    let before = Int(Date().timeIntervalSince1970 * 1000)
    analytics.flushBeforeQuit(timeout: 5)

    let batch = try #require(Capture.bodies.first)
    let events = try #require(batch["events"] as? [[String: Any]])
    #expect(events.last?["name"] as? String == "app_quit")
    #expect((events.last?["props"] as? [String: Int])?["uptime_s"] != nil)
    #expect((batch["sent_at"] as? Int ?? 0) >= before)
    #expect(batch["machine_id"] as? String == System.machineID())
    let event = try #require((batch["events"] as? [[String: Any]])?.first)
    let context = try #require(batch["context"] as? [String: Any])
    #expect(Set(batch.keys) == Set(fields["batch_fields"] as! [String]))
    #expect(Set(event.keys) == Set(fields["event_fields"] as! [String]))
    #expect(Set(context.keys) == Set((fields["context_fields"] as! [String]) + (fields["context_fields_macos"] as! [String])))
}

/// Answers 204 to the analytics endpoint and keeps what was posted.
private final class Capture: URLProtocol {
    nonisolated(unsafe) static var bodies: [[String: Any]] = []

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "capture.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buffer, maxLength: buffer.count)
                if n <= 0 { break }
                data.append(buffer, count: n)
            }
            stream.close()
        }
        if let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { Self.bodies.append(body) }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 204, httpVersion: nil, headerFields: nil)!,
                            cacheStoragePolicy: .notAllowed)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private struct MachineCase: Decodable, Sendable { let raw: String; let machine_id: String }
private struct MachineCases: Decodable { let cases: [MachineCase] }

@Test(arguments: decode(MachineCases.self, "machine_id.json").cases)
private func machineID(_ c: MachineCase) {
    #expect(System.hashedMachineID(c.raw) == c.machine_id)
}

@Test func thisMachineHasAnID() {
    #expect(System.machineID().count == 32)
}

@Test func libVersionMatchesPython() throws {
    let source = try String(contentsOf: vectors.appendingPathComponent("../../python/beacon/__init__.py").standardized, encoding: .utf8)
    #expect(source.contains("__version__ = \"\(System.libVersion)\""))
}
