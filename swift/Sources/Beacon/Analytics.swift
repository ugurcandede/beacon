import Foundation

/// Anonymous usage events, sent in batches to the beacon Worker, which adds the
/// location Cloudflare sees and forwards them to the analytics backend.
///
/// Every batch carries the machine id (the same in every beacon app on the
/// machine), a random install id and the machine context (OS, version, arch,
/// locale, model, screens, plus whatever the app adds); events carry what the
/// app passes to `track` -- never names, paths or content. Nothing is recorded
/// while disabled.
///
/// Recorded on its own: first_launch / app_updated, app_open, daily_active, a
/// heartbeat every 5 minutes while the app runs (what "open now" is counted
/// from), app_quit with the uptime, from `flushBeforeQuit`, and unclean_exit
/// when the last run ended without it (a crash, or the process was killed).
/// `ready()` adds app_ready with how long the launch took; heartbeats carry the
/// peak memory. With `requireConsent`, nothing is sent until the user answered
/// (`setConsent`); until then events wait on the machine, and a "no" drops them.
///
/// Events wait in `<dataDirectory>/beacon-analytics.json` until the Worker
/// accepts them, so time offline loses nothing: each keeps the time it
/// happened, and the batch says when it was sent, so the backend can correct a
/// wrong clock. While nothing gets through, heartbeats fold into one event per
/// stretch. All state lives on one serial queue; `track` returns at once.
public final class Analytics: @unchecked Sendable {
    public static let defaultEndpoint = URL(string: "https://beacon-api.ugurcan-dede.workers.dev/v1/events")!

    // Changed by tests only, before anything runs.
    nonisolated(unsafe) static var flushInterval: TimeInterval = 30
    nonisolated(unsafe) static var heartbeatInterval: TimeInterval = 300
    /// Answers that are about the batch itself: it is dropped. Any other answer
    /// (a proxy's 403, a 429, a 5xx) says nothing about it: it waits and goes again.
    static let rejected: Set<Int> = [400, 413, 422]
    static let sessionIdle: TimeInterval = 30 * 60  // a gap this long (the machine slept) starts a new session
    static let sessionMax: TimeInterval = 24 * 3600
    private static let batchSize = 100
    private static let maxQueue = 1000

    private let app: String
    private let endpoint: URL
    private let fileURL: URL
    private let context: [String: Any]
    private let machineID = System.machineID()
    let version: String
    let queue = DispatchQueue(label: "beacon.analytics")
    /// The clock, replaceable in tests.
    var now: () -> Date = Date.init
    private let started = ProcessInfo.processInfo.systemUptime
    private(set) var sessionID = uuid7()
    private var sessionStart = Date()
    private var lastEvent = Date()
    private(set) var state: [String: Any]
    private var timer: DispatchSourceTimer?
    private var nextHeartbeat = Date()
    private var sending = false
    /// Events at the head of the queue a send has in its batch: not to be touched.
    var inFlight = 0
    /// Goes up when the queue is dropped, so a send that was under way deletes nothing after.
    private var epoch = 0
    /// The state file is there but cannot be read: nothing is sent or saved this run.
    private var frozen = false
    /// flushBeforeQuit callers waiting for the send in flight.
    private var afterSend: [(Bool) -> Void] = []
    /// A new install, not an app that already counted this user before it moved to beacon.
    private var newInstall: Bool

    /// `installID` and `enabled` are used only on the first run, so an app
    /// moving to beacon keeps the id its users were counted under and the
    /// choice of those who had turned analytics off (which also counts as their
    /// answer when `requireConsent` is set).
    public init(app: String, version: String, dataDirectory: URL, context: [String: Any] = [:],
                installID: String? = nil, enabled: Bool? = nil, endpoint: URL = defaultEndpoint,
                requireConsent: Bool = false) {
        self.requireConsent = requireConsent
        self.app = app
        self.endpoint = endpoint
        self.version = version
        fileURL = dataDirectory.appendingPathComponent("beacon-analytics.json")
        self.context = System.context().merging(["app_version": version]) { $1 }
            .merging(Self.jsonable(context) as? [String: Any] ?? [:]) { $1 }
        state = ["install_id": installID ?? UUID().uuidString.lowercased(), "enabled": enabled ?? true,
                 "queue": [[String: Any]]()]
        if let enabled { state["consent"] = enabled }
        newInstall = installID == nil
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return }  // the first run
        newInstall = false
        guard let data = try? Data(contentsOf: fileURL) else {
            frozen = true  // locked, no permission: this run does not know the user's choice
            return
        }
        if let saved = Self.validState(data) {
            state.merge(saved) { $1 }
        } else {
            // Broken: the user's choice was in it. Off until they choose again
            // (an app that asks for consent asks again).
            state["enabled"] = false
            state["consent"] = nil
        }
    }

    /// The state file's content with fields of the wrong type left out, or nil
    /// when it is not a state at all (cut short, or not an object).
    static func validState(_ data: Data) -> [String: Any]? {
        guard let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        var good = saved
        func keep<T>(_ key: String, _: T.Type) { if good[key] != nil, !(good[key] is T), !(good[key] is NSNull) { good[key] = nil } }
        keep("install_id", String.self); keep("active_day", String.self); keep("last_version", String.self)
        keep("running", Bool.self); keep("consent", Bool.self)
        if saved["enabled"] != nil, !(saved["enabled"] is Bool) { good["enabled"] = false }  // unreadable is not a yes
        if saved["queue"] != nil {
            good["queue"] = (saved["queue"] as? [Any] ?? []).compactMap { $0 as? [String: Any] }
                .filter { $0["name"] is String && $0["props"] is [String: Any] }
        }
        for key in ["install_id", "active_day", "last_version", "consent"] where good[key] is NSNull { good[key] = nil }
        return good
    }

    public var installID: String { queue.sync { state["install_id"] as? String ?? "" } }

    private let requireConsent: Bool
    /// The previous version, on the first run after an update ("what's new"); set by `start()`.
    public private(set) var updatedFrom: String?

    /// True until the user answered, when the app asks first (`requireConsent`).
    public var consentNeeded: Bool { queue.sync { requireConsent && state["consent"] == nil } }

    /// The user's answer: yes sends what waited, no drops it and turns analytics off.
    public func setConsent(_ granted: Bool) {
        queue.sync { state["consent"] = granted }
        enabled = granted
        if granted { queue.async { self.sendNext(timeout: 10) } }
    }

    /// Call when the app is ready for the user: app_ready with how long the launch took.
    public func ready(_ props: [String: Any] = [:]) {
        var all = props
        if let started = System.processStart() { all["launch_ms"] = Int(now().timeIntervalSince(started) * 1000) }
        track("app_ready", all)
    }

    public var enabled: Bool {
        get { queue.sync { !frozen && state["enabled"] as? Bool ?? true } }
        set {
            queue.sync {
                guard !frozen else { return }
                state["enabled"] = newValue
                if !newValue {
                    // Everything, also what a send has on its way: if that send
                    // fails, nothing is left to go out later.
                    state["queue"] = [[String: Any]]()
                    inFlight = 0
                    epoch += 1
                }
                save()
            }
        }
    }

    /// Records app_open (and first_launch or app_updated) and starts sending.
    public func start() {
        // Not async: `updatedFrom` is read right after start() returns.
        queue.sync {
            if self.state["running"] as? Bool == true {
                self.append(self.event("unclean_exit", ["version": self.state["last_version"] as? String ?? self.version]))
            }
            if self.newInstall {
                self.append(self.event("first_launch", [:]))
            } else if let previous = self.state["last_version"] as? String, previous != self.version {
                self.updatedFrom = previous
                self.append(self.event("app_updated", ["from": previous]))
            }
            self.state["last_version"] = self.version
            self.state["running"] = true
            self.save()
            self.nextHeartbeat = self.now().addingTimeInterval(Self.heartbeatInterval)
        }
        track("app_open")
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: Self.flushInterval)
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    public func track(_ name: String, _ props: [String: Any] = [:]) {
        // Props are the app's: what is not JSON would raise an Objective-C
        // exception in JSONSerialization, which `try?` does not catch.
        let props = Self.jsonable(props) as? [String: Any] ?? [:]
        queue.async { self.append(self.event(name, props)) }
    }

    /// `value` as JSON can carry it: what is not JSON (a Date, a URL, an enum,
    /// NaN) goes as text, as the Python client does.
    static func jsonable(_ value: Any) -> Any {
        switch value {
        case let dict as [String: Any]: return dict.mapValues(jsonable)
        case let list as [Any]: return list.map(jsonable)
        case is String, is NSNull: return value
        case let number as NSNumber: return number.doubleValue.isFinite ? number : "\(number.doubleValue)"
        case let date as Date: return ISO8601DateFormatter().string(from: date)
        case let url as URL: return url.absoluteString
        default: return "\(value)"
        }
    }

    /// A window, page or panel the user is looking at: PostHog's $screen.
    public func screen(_ name: String, _ props: [String: Any] = [:]) {
        track("$screen", props.merging(["$screen_name": name]) { $1 })
    }

    /// Sends what is queued and waits up to `timeout`; the run is not marked as
    /// ended (for an uncaught exception: the next launch still sees it as unclean).
    func flush(timeout: TimeInterval = 2) {
        let done = DispatchSemaphore(value: 0)
        queue.async { self.sendNext(timeout: timeout) { _ in done.signal() } }
        _ = done.wait(timeout: .now() + timeout)
    }

    /// For applicationWillTerminate: records app_quit with the uptime and
    /// blocks up to `timeout` sending what is queued.
    public func flushBeforeQuit(timeout: TimeInterval = 2) {
        let uptime = Int(ProcessInfo.processInfo.systemUptime - started)
        let done = DispatchSemaphore(value: 0)
        queue.async {
            self.append(self.event("app_quit", ["uptime_s": uptime]))
            self.state["running"] = false
            self.save()
            self.sendNext(timeout: timeout) { _ in done.signal() }
        }
        _ = done.wait(timeout: .now() + timeout)
    }

    // MARK: - On `queue`

    /// The version goes with each event: what is queued before an update is
    /// sent by the new version. uuid lets the backend drop an event sent twice
    /// -- when the Worker took it but the answer never came back.
    func event(_ name: String, _ props: [String: Any]) -> [String: Any] {
        let now = now()
        if now.timeIntervalSince(lastEvent) > Self.sessionIdle || now.timeIntervalSince(sessionStart) > Self.sessionMax {
            sessionID = uuid7()
            sessionStart = now
        }
        lastEvent = now
        return ["name": name, "props": props, "ts": Int(now.timeIntervalSince1970 * 1000),
                "session_id": sessionID, "app_version": version, "uuid": uuid7()]
    }

    var pending: [[String: Any]] { state["queue"] as? [[String: Any]] ?? [] }

    func append(_ event: [String: Any]) {
        guard state["enabled"] as? Bool ?? true else { return }
        var queued = pending
        if event["name"] as? String == "heartbeat", queued.count > inFlight, let last = queued.last,
           last["name"] as? String == "heartbeat", last["session_id"] as? String == event["session_id"] as? String,
           var props = last["props"] as? [String: Any] {
            // Not sent yet: the one waiting stretches to cover this one too.
            let new = event["props"] as? [String: Any] ?? [:]
            props["duration_s"] = (props["duration_s"] as? Int ?? 0) + (new["duration_s"] as? Int ?? 0)
            if let memory = new["memory_peak_mb"] as? Int {
                props["memory_peak_mb"] = max(props["memory_peak_mb"] as? Int ?? 0, memory)
            }
            queued[queued.count - 1]["props"] = props
        } else if queued.count < Self.maxQueue {
            // Full means the Worker has been unreachable for a long time; the
            // oldest events go first, so drop the new one.
            queued.append(event)
        } else {
            return
        }
        state["queue"] = queued
        save()
    }

    func tick() {
        // A menu bar app can run for weeks untouched; this keeps it counted
        // as a daily active user.
        let today = ISO8601DateFormatter.string(from: now(), timeZone: .current, formatOptions: [.withFullDate])
        if state["active_day"] as? String != today {
            append(event("daily_active", [:]))
            state["active_day"] = today
            save()
        }
        if now() >= nextHeartbeat {
            append(event("heartbeat", ["duration_s": Int(Self.heartbeatInterval), "memory_peak_mb": System.memoryPeakMB()]))
            nextHeartbeat = nextHeartbeat.addingTimeInterval(Self.heartbeatInterval)
            if nextHeartbeat < now() {  // slept through some: no burst after waking
                nextHeartbeat = now().addingTimeInterval(Self.heartbeatInterval)
            }
        }
        sendNext(timeout: 10)
    }

    private func sendNext(timeout: TimeInterval, completion: ((Bool) -> Void)? = nil) {
        // Not before the user answered, and not while turned off.
        if (requireConsent && state["consent"] == nil) || frozen || state["enabled"] as? Bool == false {
            completion?(false)
            return
        }
        if sending {
            // Sending the same batch again would duplicate it; wait for this one.
            if let completion { afterSend.append(completion) }
            return
        }
        let batch = Array(pending.prefix(Self.batchSize))
        guard !batch.isEmpty else {
            completion?(true)
            return
        }
        sending = true
        inFlight = batch.count
        let epoch = self.epoch
        post(batch, timeout: timeout) { sent in
            self.queue.async {
                let ok = sent && epoch == self.epoch  // turned off meanwhile: the queue was dropped already
                self.sending = false
                self.inFlight = 0
                let waiting = self.afterSend
                self.afterSend = []
                let done = { (ok: Bool) in
                    completion?(ok)
                    waiting.forEach { $0(ok) }
                }
                guard ok else {
                    done(false)
                    return
                }
                self.state["queue"] = Array(self.pending.dropFirst(batch.count))
                self.save()
                self.sendNext(timeout: timeout, completion: done)
            }
        }
    }

    private func post(_ events: [[String: Any]], timeout: TimeInterval, completion: @escaping (Bool) -> Void) {
        let body: [String: Any] = ["app": app, "install_id": state["install_id"] ?? "", "machine_id": machineID,
                                   "sent_at": Int(now().timeIntervalSince1970 * 1000), "context": context, "events": events]
        var request = URLRequest(url: endpoint, timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        guard JSONSerialization.isValidJSONObject(body) else { return completion(true) }  // unsendable: dropped
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            // 2xx accepted; rejected for good -- dropped, or it would block
            // everything queued behind it; anything else retried later.
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            completion((200..<300).contains(status) || Self.rejected.contains(status))
        }.resume()
    }

    private func save() {
        guard !frozen, JSONSerialization.isValidJSONObject(state),
              let data = try? JSONSerialization.data(withJSONObject: state) else { return }
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: fileURL, options: .atomic)
    }
}

/// A UUIDv7 (time-ordered), the form PostHog expects for $session_id.
func uuid7() -> String {
    var bytes = [UInt8](repeating: 0, count: 16)
    _ = SecRandomCopyBytes(kSecRandomDefault, 16, &bytes)
    let ms = UInt64(Date().timeIntervalSince1970 * 1000)
    for i in 0..<6 { bytes[i] = UInt8((ms >> (8 * (5 - i))) & 0xFF) }
    bytes[6] = (bytes[6] & 0x0F) | 0x70  // version
    bytes[8] = (bytes[8] & 0x3F) | 0x80  // variant
    let u = bytes
    return UUID(uuid: (u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7],
                       u[8], u[9], u[10], u[11], u[12], u[13], u[14], u[15])).uuidString.lowercased()
}
