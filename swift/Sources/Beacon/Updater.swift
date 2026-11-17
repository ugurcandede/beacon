import CryptoKit
import Foundation

/// Self-update from the signed manifest published with every release -- the
/// same update.json / update.json.sig the Python client reads (see
/// protocol/README.md and beacon.release).
///
/// `check()` fetches both through releases/latest/download, a plain download
/// rather than an API call, so GitHub's 60-an-hour API limit does not apply,
/// and believes the manifest only if its Ed25519 signature matches the public
/// key built into the app. `install(_:)` puts the new build in place and starts
/// it; the app then quits:
///
/// - `.brew`: the running bundle is the one a Homebrew cask installed, so
///   `brew upgrade --cask` does it and brew's own records stay right.
/// - `.bundle`: any other .app -- the zip is downloaded, checked against the
///   manifest's SHA-256, unpacked and swapped in for the running bundle.
///
/// Every step goes to `<dataDirectory>/update.log`, which is what to ask a
/// user for when an update fails on their machine.
public final class Updater: @unchecked Sendable {
    public enum InstallKind: String, Sendable { case bundle, brew }

    public let logURL: URL
    private let base: URL
    private let version: String
    private let repo: String
    private let keys: [Curve25519.Signing.PublicKey]
    private let dataDirectory: URL
    private let cask: String?
    private let bundleURL: URL
    private let analytics: Analytics?
    private static let workPrefix = "beacon-update-"

    /// `publicKeys` are the base64 keys `python -m beacon.release keygen` printed:
    /// the current and the next, so the signing key can change without a
    /// release no installed copy accepts.
    /// `baseURL` replaces GitHub's releases/latest/download/ -- for local tests.
    /// With `analytics`, installs are reported as update_started / update_failed;
    /// the new build reports app_updated itself.
    /// `.beta` also reads update-beta.json and takes whichever release is newer;
    /// it can change at run time (a "get beta updates" switch).
    public enum Channel: String, Sendable { case stable, beta }
    public var channel: Channel

    public init(repo: String, version: String, publicKeys: [String], dataDirectory: URL,
                cask: String? = nil, baseURL: URL? = nil, bundleURL: URL = Bundle.main.bundleURL,
                analytics: Analytics? = nil, channel: Channel = .stable) throws {
        base = baseURL ?? URL(string: "https://github.com/\(repo)/releases/latest/download/")!
        self.version = version
        self.repo = repo
        keys = try publicKeys.map { try Curve25519.Signing.PublicKey(rawRepresentation: Data(base64Encoded: $0) ?? Data()) }
        self.dataDirectory = dataDirectory
        self.cask = cask
        self.bundleURL = bundleURL
        self.analytics = analytics
        self.channel = channel
        logURL = dataDirectory.appendingPathComponent("update.log")
    }

    /// One key: the app has not rotated its signing key yet.
    public convenience init(repo: String, version: String, publicKey: String, dataDirectory: URL,
                            cask: String? = nil, baseURL: URL? = nil, bundleURL: URL = Bundle.main.bundleURL,
                            analytics: Analytics? = nil, channel: Channel = .stable) throws {
        try self.init(repo: repo, version: version, publicKeys: [publicKey], dataDirectory: dataDirectory,
                      cask: cask, baseURL: baseURL, bundleURL: bundleURL, analytics: analytics, channel: channel)
    }

    /// The newer Release for this platform, or nil when up to date -- or,
    /// unless `manual` (the user asked) or the release is required, when the
    /// user skipped that version.
    public func check(manual: Bool = false) async throws -> Release? {
        do {
            var release = try await offered("update.json")
            if channel == .beta, let beta = try await offered("update-beta.json", missingOK: true),
               release.map({ Self.isNewer(beta.version, than: $0.version) }) ?? true {
                release = beta
            }
            log("check: running \(version) (\(channel.rawValue)), offered "
                + (release.map { $0.version + ($0.required ? " (required)" : "") } ?? "nothing"))
            if let release, !manual, !release.required, release.version == state()["skipped"] as? String {
                log("check: \(release.version) was skipped")
                return nil
            }
            return release
        } catch let error as UpdateError {
            log("check failed: \(error)")
            throw error
        } catch {
            log("check failed: \(error)")
            throw UpdateError(.check, error.localizedDescription)
        }
    }

    public var installKind: InstallKind? {
        guard bundleURL.pathExtension == "app" else { return nil }
        return brewPrefix() != nil ? .brew : .bundle
    }

    /// Downloads, verifies and installs `release`, then starts it once this
    /// process has exited. Returns when the caller should quit. Asks the server
    /// first: a release paused or replaced since it was offered is not installed
    /// (UpdateError `.withdrawn`).
    public func install(_ release: Release) async throws {
        let kind = installKind?.rawValue ?? "none"
        log("install \(release.version) as \(kind) over \(bundleURL.path)")
        analytics?.track("update_started", ["to": release.version, "kind": kind])
        do {
            let offered = try await check(manual: true)
            guard offered?.version == release.version else {
                throw UpdateError(.withdrawn, "\(release.version) is no longer offered")
            }
            switch installKind {
            case .bundle: try await installBundle(release)
            case .brew: try installBrew(release)
            case nil: throw UpdateError(.install, "this build cannot update itself")
            }
        } catch {
            let failure = error as? UpdateError ?? UpdateError(.install, error.localizedDescription)
            log("install failed: \(error)")
            analytics?.track("update_failed", ["to": release.version, "kind": kind, "step": failure.step.rawValue,
                                               "error": String(failure.description.prefix(200))])
            throw failure
        }
        log("install done, relaunching")
        relaunch()
    }

    /// The release a manifest offers this build. With `missingOK`, a manifest
    /// that is not there (no beta out) offers nothing.
    private func offered(_ name: String, missingOK: Bool = false) async throws -> Release? {
        let data: Data
        do {
            data = try await get(base.appendingPathComponent(name))
        } catch is NotFound where missingOK {
            return nil
        }
        let sigText = String(decoding: try await get(base.appendingPathComponent(name + ".sig")), as: UTF8.self)
        return try Self.parseManifest(data, signature: Data(base64Encoded: sigText.trimmingCharacters(in: .whitespacesAndNewlines)) ?? Data(),
                                      keys: keys, current: version, machineID: System.machineID())
    }

    /// The GitHub release text of `version` (Markdown), for a "what's new" after
    /// an update (`Analytics.updatedFrom`), or nil if it cannot be had.
    public func releaseNotes(for version: String) async -> String? {
        let url = URL(string: "https://api.github.com/repos/\(repo)/releases/tags/v\(version)")!
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode ?? 200 == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let body = json["body"] as? String, !body.isEmpty else {
            log("release notes for \(version): none")
            return nil
        }
        return body
    }

    /// Automatic checks stop offering `release`; a newer one is offered again.
    /// A required release cannot be skipped: nothing changes.
    public func skip(_ release: Release) {
        guard !release.required else {
            log("skip \(release.version): required, ignored")
            return
        }
        log("skip \(release.version)")
        analytics?.track("update_skipped", ["to": release.version, "kind": installKind?.rawValue ?? "none"])
        var current = state()
        current["skipped"] = release.version
        try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        try? JSONSerialization.data(withJSONObject: current).write(to: stateURL, options: .atomic)
    }

    private var stateURL: URL { dataDirectory.appendingPathComponent("beacon-update.json") }

    private func state() -> [String: Any] {
        (try? Data(contentsOf: stateURL)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
    }

    /// Call at launch: removes what a previous update left behind.
    public func cleanup() {
        let items = (try? FileManager.default.contentsOfDirectory(at: dataDirectory, includingPropertiesForKeys: nil)) ?? []
        for item in items where item.lastPathComponent.hasPrefix(Self.workPrefix) {
            try? FileManager.default.removeItem(at: item)
        }
    }

    // MARK: - Steps

    private func installBundle(_ release: Release) async throws {
        let fm = FileManager.default
        let work = dataDirectory.appendingPathComponent(Self.workPrefix + UUID().uuidString)
        try fm.createDirectory(at: work, withIntermediateDirectories: true)
        let archive = work.appendingPathComponent("update.zip")
        try await download(release, to: archive)
        try run("/usr/bin/ditto", ["-x", "-k", archive.path, work.appendingPathComponent("new").path])
        let unpacked = try fm.contentsOfDirectory(at: work.appendingPathComponent("new"), includingPropertiesForKeys: nil)
        guard let new = unpacked.first(where: { $0.pathExtension == "app" }) else {
            throw UpdateError(.install, "no .app in the archive")
        }
        let old = work.appendingPathComponent("old.app")
        try fm.moveItem(at: bundleURL, to: old)
        log("moved running bundle to \(old.path)")
        do {
            try fm.moveItem(at: new, to: bundleURL)
        } catch {
            try? fm.moveItem(at: old, to: bundleURL)
            throw error
        }
        log("new bundle in place at \(bundleURL.path)")
    }

    private func installBrew(_ release: Release) throws {
        guard let prefix = brewPrefix(), let cask else { throw UpdateError(.install, "not a Homebrew install") }
        // brew refreshes its taps at most once a day on its own, so the cask may
        // still name the previous version.
        _ = try? run("\(prefix)/bin/brew", ["update", "--quiet"], logOutput: true)
        try run("\(prefix)/bin/brew", ["upgrade", "--cask", cask], logOutput: true)
        // The cask is bumped a few minutes after the GitHub release; until then
        // brew succeeds without changing anything.
        let installed = URL(fileURLWithPath: "\(prefix)/Caskroom/\(cask)/\(release.version)")
        guard FileManager.default.fileExists(atPath: installed.path) else {
            throw UpdateError(.brewBehind, "the \(cask) cask is not at \(release.version) yet")
        }
    }

    private func download(_ release: Release, to destination: URL) async throws {
        log("downloading \(release.url) (\(release.size) bytes)")
        let downloaded: URL
        do {
            (downloaded, _) = try await URLSession.shared.download(from: release.url)
        } catch {
            log("download failed: \(error)")
            throw UpdateError(.download, error.localizedDescription)
        }
        try FileManager.default.moveItem(at: downloaded, to: destination)
        guard try sha256(of: destination) == release.sha256 else {
            try? FileManager.default.removeItem(at: destination)
            throw UpdateError(.verify, "sha256 mismatch")
        }
        log("download verified")
    }

    // MARK: - Helpers

    /// The Release for this platform, if the signature matches one of `keys`,
    /// it is newer than `current`, not paused, and this machine is within its rollout.
    static func parseManifest(_ data: Data, signature: Data, keys: [Curve25519.Signing.PublicKey],
                              current: String, machineID: String = "") throws -> Release? {
        guard keys.contains(where: { $0.isValidSignature(signature, for: data) }) else {
            throw UpdateError(.verify, "manifest signature does not match")
        }
        let manifest = try JSONDecoder().decode(Manifest.self, from: data)
        guard let asset = manifest.assets[System.platformKey], isNewer(manifest.version, than: current),
              manifest.paused != true, rolloutBucket(machineID, manifest.version) < (manifest.rollout ?? 100) else {
            return nil
        }
        return Release(version: manifest.version, url: asset.url, size: asset.size, sha256: asset.sha256,
                       notesURL: manifest.notesURL,
                       required: manifest.minVersion.map { isNewer($0, than: current) } ?? false)
    }

    /// The manifest and its signature, always from the server: URLSession would
    /// otherwise answer from its cache by a guess of its own, so a paused or
    /// changed release would show late -- and a cached update.json next to a
    /// fresh .sig (or the other way round) would fail to verify.
    static func manifestRequest(_ url: URL) -> URLRequest {
        URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 30)
    }

    /// 0-99, fixed per machine and release: a release with rollout N reaches the
    /// machines below N, and a different set each release.
    static func rolloutBucket(_ machineID: String, _ version: String) -> Int {
        let digest = SHA256.hash(data: Data("\(machineID):\(version)".utf8))
        let first4 = digest.prefix(4).reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
        return Int(first4 % 100)
    }

    /// Numeric dot-separated comparison: 1.10.0 is newer than 1.9.3.
    /// Non-numeric parts ("dev") count as 0.
    static func isNewer(_ candidate: String, than current: String) -> Bool {
        func parts(_ s: String) -> [Int] { s.split(separator: ".").map { Int($0) ?? 0 } }
        var a = parts(candidate), b = parts(current)
        let width = max(a.count, b.count)
        a += Array(repeating: 0, count: width - a.count)
        b += Array(repeating: 0, count: width - b.count)
        return b.lexicographicallyPrecedes(a)
    }

    /// The Homebrew prefix whose cask installed the bundle this process runs from.
    private func brewPrefix() -> String? {
        guard let cask else { return nil }
        let running = bundleURL.resolvingSymlinksInPath()
        for prefix in ["/opt/homebrew", "/usr/local"] {
            let caskroom = URL(fileURLWithPath: "\(prefix)/Caskroom/\(cask)")
            guard FileManager.default.isExecutableFile(atPath: "\(prefix)/bin/brew"),
                  let versions = try? FileManager.default.contentsOfDirectory(at: caskroom, includingPropertiesForKeys: nil)
            else { continue }
            for version in versions {
                let apps = (try? FileManager.default.contentsOfDirectory(at: version, includingPropertiesForKeys: nil)) ?? []
                if apps.contains(where: { $0.pathExtension == "app" && $0.resolvingSymlinksInPath() == running }) {
                    return prefix
                }
            }
        }
        return nil
    }

    private func relaunch() {
        // Reopen by path once this process is gone: another copy with the same
        // bundle id may be registered with LaunchServices.
        let pid = ProcessInfo.processInfo.processIdentifier
        let script = "while kill -0 \(pid) 2>/dev/null; do sleep 0.2; done; open '\(bundleURL.path)'"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", script]
        // Inheriting our stdout/stderr would tie the helper to whoever reads them.
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
    }

    private struct NotFound: Error {}

    private func get(_ url: URL) async throws -> Data {
        if url.isFileURL, !FileManager.default.fileExists(atPath: url.path) { throw NotFound() }  // tests
        let (data, response) = try await URLSession.shared.data(for: Self.manifestRequest(url))
        let status = (response as? HTTPURLResponse)?.statusCode ?? 200  // file:// in tests
        if status == 404 { throw NotFound() }
        guard (200..<300).contains(status) else { throw UpdateError(.check, "HTTP \(status) for \(url)") }
        return data
    }

    @discardableResult
    private func run(_ executable: String, _ arguments: [String], logOutput: Bool = false) throws -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        if logOutput, let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            process.standardOutput = handle
            process.standardError = handle
        }
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw UpdateError(.install, "\(executable) \(arguments.joined(separator: " ")) exited with \(process.terminationStatus)")
        }
        return process.terminationStatus
    }

    private func sha256(of file: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 16), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private func log(_ message: String) {
        let line = Data("\(Log.timestamp()) \(message)\n".utf8)
        try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            handle.write(line)
            try? handle.close()
        } else {
            try? line.write(to: logURL)
        }
    }
}

public struct Release: Equatable, Sendable {
    public let version: String
    public let url: URL
    public let size: Int
    public let sha256: String
    public let notesURL: URL
    /// The running version is below the manifest's min_version: not to be skipped.
    public var required = false
}

public struct UpdateError: Error, CustomStringConvertible {
    public enum Step: String, Sendable { case check, withdrawn, download, verify, install, brewBehind = "brew_behind" }
    public let step: Step
    public let detail: String
    init(_ step: Step, _ detail: String) { self.step = step; self.detail = detail }
    public var description: String { "\(step.rawValue): \(detail)" }
}

private struct Manifest: Decodable {
    struct Asset: Decodable {
        let url: URL
        let size: Int
        let sha256: String
    }

    let version: String
    let notesURL: URL
    let assets: [String: Asset]
    let paused: Bool?
    let rollout: Int?
    let minVersion: String?

    enum CodingKeys: String, CodingKey {
        case version, assets, paused, rollout
        case minVersion = "min_version"
        case notesURL = "notes_url"
    }
}
