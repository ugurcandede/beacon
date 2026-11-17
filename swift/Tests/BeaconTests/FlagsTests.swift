import Foundation
import Testing
@testable import Beacon

private let flagVectors = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().appendingPathComponent("../../../protocol/vectors/flags.json").standardized

/// Answers flags.invalid with the vector's Worker answer, or a status set by the test.
private final class FlagsStub: URLProtocol {
    nonisolated(unsafe) static var status = 200
    nonisolated(unsafe) static var asked: [URL] = []
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "flags.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.asked.append(request.url!)
        let vector = try! JSONSerialization.jsonObject(with: Data(contentsOf: flagVectors)) as! [String: Any]
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: Self.status, httpVersion: nil, headerFields: nil)!,
                            cacheStoragePolicy: .notAllowed)
        if Self.status == 200 { client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: vector["worker"]!)) }
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private let stubbed = URL(string: "https://flags.invalid/v1/flags")!

@Suite(.serialized) struct FlagsTests {
    init() { URLProtocol.registerClass(FlagsStub.self); FlagsStub.status = 200 }

    @Test func flagsReadAsTheVectorSays() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let f = Flags(app: "lumea", dataDirectory: dir, endpoint: stubbed)
        #expect(await f.refresh())
        let vector = try JSONSerialization.jsonObject(with: Data(contentsOf: flagVectors)) as! [String: Any]
        for c in vector["client"] as! [[String: Any]] {
            let key = c["key"] as! String
            #expect(f.enabled(key) == c["enabled"] as! Bool, "\(key)")
            let expected = c["value"] is NSNull ? nil : c["value"] as? NSObject  // JSON null: no such flag
            #expect((f.value(key) as? NSObject) == expected, "\(key)")
            #expect((f.payload(key) as? NSDictionary) == (c["payload"] as? NSDictionary), "\(key)")
        }
        #expect(FlagsStub.asked.last?.query == "app=lumea&id=\(System.machineID())")
    }

    @Test func theLastAnswerIsKeptOnErrors() async {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        #expect(await Flags(app: "lumea", dataDirectory: dir, endpoint: stubbed).refresh())
        FlagsStub.status = 502
        let later = Flags(app: "lumea", dataDirectory: dir, endpoint: stubbed)  // a later launch
        #expect(!(await later.refresh()))
        #expect(later.value("theme") as? String == "dark")
    }

    @Test func nothingKnownYetMeansOff() {
        let f = Flags(app: "lumea", dataDirectory: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString),
                      endpoint: stubbed)
        #expect(!f.enabled("anything") && f.value("theme") == nil && f.payload("x") == nil)
    }
}
