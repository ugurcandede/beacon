import Foundation
import Testing
@testable import Beacon

private let offline = URL(string: "http://127.0.0.1:9/")!

private func analytics(installID: String? = nil, enabled: Bool? = nil, in dir: URL? = nil) -> Analytics {
    let dir = dir ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    return Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, installID: installID, enabled: enabled, endpoint: offline)
}

private extension Analytics {
    func record(_ name: String, _ props: [String: Any] = [:]) { queue.sync { append(event(name, props)) } }
    func heartbeat() { record("heartbeat", ["duration_s": 300]) }
    var queued: [[String: Any]] { queue.sync { pending } }
    var names: [String] { queued.map { $0["name"] as! String } }
    func waitForSend() {
        for _ in 0..<500 where queue.sync(execute: { inFlight }) != 0 { usleep(10_000) }
    }
    func durations() -> [Int] { queued.filter { $0["name"] as? String == "heartbeat" }.map { ($0["props"] as! [String: Any])["duration_s"] as! Int } }
}

@Test func screenIsAPostHogScreenView() {
    let a = analytics()
    a.screen("Settings", ["tab": "theme"])
    let last = a.queued.last!
    #expect(last["name"] as? String == "$screen")
    #expect(last["props"] as? [String: String] == ["$screen_name": "Settings", "tab": "theme"])
}

@Test func heartbeatsFoldWhileUnsent() {
    let a = analytics()
    a.heartbeat(); a.heartbeat(); a.heartbeat()
    #expect(a.durations() == [900])
}

@Test func heartbeatAfterAnotherEventStartsANewStretch() {
    let a = analytics()
    a.heartbeat(); a.record("x"); a.heartbeat()
    #expect(a.names == ["heartbeat", "x", "heartbeat"])
}

@Test func heartbeatBeingSentIsNotChanged() {
    let a = analytics()
    a.heartbeat()
    a.queue.sync { a.inFlight = 1 }  // a send has it in its batch
    a.heartbeat()
    #expect(a.durations() == [300, 300])
}

@Test func disablingDropsEverythingAlsoTheBatchBeingSent() {
    let a = analytics()
    a.record("sending"); a.record("waiting")
    a.queue.sync { a.inFlight = 1 }
    a.enabled = false
    #expect(a.names.isEmpty)  // privacy.md: turning statistics off deletes what was waiting
}

@Test(arguments: [(60.0, false), (Analytics.sessionIdle + 1, true)])
func sessionEndsAfterIdleGap(gap: TimeInterval, newSession: Bool) {
    let a = analytics()
    let start = Date()
    a.now = { start }
    a.record("x")
    a.now = { start.addingTimeInterval(gap) }
    a.record("y")
    let ids = a.queued.map { $0["session_id"] as! String }
    #expect((ids[0] != ids[1]) == newSession)
}

@Test func heartbeatsOfAnotherSessionDoNotFold() {
    let a = analytics()
    let start = Date()
    a.now = { start }
    a.heartbeat()
    a.now = { start.addingTimeInterval(Analytics.sessionIdle + 1) }
    a.heartbeat()
    #expect(a.durations() == [300, 300])
}

@Test func sessionEndsAfterADayEvenWhenBusy() {
    let a = analytics()
    let start = Date()
    var sessions = Set<String>()
    for minute in stride(from: 0, to: 25 * 60, by: 10) {  // an event every 10 minutes for 25 hours
        a.now = { start.addingTimeInterval(TimeInterval(minute * 60)) }
        a.record("x")
        sessions.insert(a.queue.sync { a.sessionID })
    }
    #expect(sessions.count == 2)
}

@Test func firstRunTakesTheOldOptOut() {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let a = analytics(installID: "old", enabled: false, in: dir)
    a.record("x")
    #expect(!a.enabled && a.queued.isEmpty)
    a.enabled = true  // the user turns it back on: later runs keep that
    #expect(analytics(installID: "old", enabled: false, in: dir).enabled)
}

@Test func tickSendsAHeartbeatWhenDue() {
    let a = analytics()
    let start = Date()
    a.now = { start }
    a.start()
    a.queue.sync {}  // start's setup has run with the clock at `start`
    a.now = { start.addingTimeInterval(Analytics.heartbeatInterval + 1) }
    a.queue.sync { a.tick() }
    // Each tick also tries to send; offline, that fails. Five minutes apart in
    // real use, so the failed attempt is long over before the next heartbeat.
    a.waitForSend()
    a.now = { start.addingTimeInterval(2 * Analytics.heartbeatInterval + 1) }
    a.queue.sync { a.tick() }
    // Offline: both folded into one stretch.
    #expect(a.queued.filter { $0["name"] as? String == "heartbeat" }.count == 1)
    #expect(a.durations().last == 600)
}

// MARK: - Consent, what's new, launch time, memory

@Test func nothingIsSentBeforeConsentAndANoDropsIt() {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let a = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline, requireConsent: true)
    #expect(a.consentNeeded)
    a.record("first_thing")
    a.queue.sync { a.tick() }  // would send, but must not
    #expect(a.queue.sync { a.inFlight } == 0)
    #expect(a.names.contains("first_thing"))
    a.setConsent(false)
    #expect(!a.consentNeeded && !a.enabled && a.queued.isEmpty)
    #expect(!Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline,
                       requireConsent: true).consentNeeded)  // asked once
}

@Test func aCarriedOverChoiceCountsAsTheAnswer() {
    let a = analytics(installID: "old", enabled: true)
    #expect(!a.consentNeeded && a.enabled)
}

@Test func updatedFromSaysWhatTheLastVersionWas() {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let first = Analytics(app: "lumea", version: "1.0.0", dataDirectory: dir, endpoint: offline)
    first.start()
    first.queue.sync {}
    #expect(first.updatedFrom == nil)
    let second = Analytics(app: "lumea", version: "1.1.0", dataDirectory: dir, endpoint: offline)
    second.start()
    second.queue.sync {}
    #expect(second.updatedFrom == "1.0.0")
}

@Test func readyReportsTheLaunchTime() throws {
    let a = analytics()
    a.ready(["window": "main"])
    let event = try #require(a.queued.last)
    let props = event["props"] as! [String: Any]
    #expect(event["name"] as? String == "app_ready" && props["window"] as? String == "main")
    let launch = try #require(props["launch_ms"] as? Int)
    #expect(launch > 0 && launch < 3_600_000)
}

@Test func heartbeatsKeepTheHighestMemoryWhenFolded() {
    let a = analytics()
    a.record("heartbeat", ["duration_s": 300, "memory_peak_mb": 80])
    a.record("heartbeat", ["duration_s": 300, "memory_peak_mb": 120])
    #expect(a.queued.last?["props"] as? [String: Int] == ["duration_s": 600, "memory_peak_mb": 120])
    #expect(System.memoryPeakMB() > 0)
}
