// beacon demo app (SwiftUI): shows its version, checks for an update and
// installs it, and sends analytics events. examples/run-local.sh builds it
// twice (1.0.0 and 1.1.0) to watch a self-update end to end.
//
// Settings come from demo_config.json in the bundle's Resources, written by
// the build script: version, public_key, base_url (where update.json is
// served), endpoint (the Worker), auto_update (install without a click), cask
// (the Homebrew cask it was installed with, if any).

import AppKit
import Beacon
import SwiftUI

struct Config: Decodable {
    let version: String
    let public_key: String
    let base_url: URL
    let endpoint: URL
    let auto_update: Bool
    let cask: String?
    let data_dir: String?  // the run-local script's work folder, so a test run leaves nothing behind
}

@main
struct DemoApp: App {
    @StateObject private var model = Model()

    var body: some Scene {
        WindowGroup("beacon demo (Swift) \(model.config.version)") {
            VStack(alignment: .leading, spacing: 10) {
                Text(model.status).textSelection(.enabled)
                Button("Check for update") { Task { await model.check() } }
                Button("Install") { Task { await model.install() } }
                    .disabled(model.release == nil || model.updater.installKind == nil)
                Button("Send 'button_clicked' event") { model.analytics.track("button_clicked", ["button": "demo"]) }
                Toggle("Send anonymous usage stats", isOn: $model.statsOn)
            }
            .padding()
            .frame(minWidth: 360)
            .onAppear { model.analytics.screen("Main") }
        }
    }
}

@MainActor
final class Model: ObservableObject {
    let config: Config
    let updater: Updater
    let analytics: Analytics
    @Published var status = ""
    @Published var release: Release?
    @Published var statsOn: Bool { didSet { analytics.enabled = statsOn } }

    init() {
        let url = Bundle.main.url(forResource: "demo_config", withExtension: "json")!
        config = try! JSONDecoder().decode(Config.self, from: Data(contentsOf: url))
        let dataDirectory = config.data_dir.map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("beacon-demo-swift")
        analytics = Analytics(app: "beacon-demo-swift", version: config.version, dataDirectory: dataDirectory,
                              endpoint: config.endpoint)
        Log.setup(app: "beacon-demo-swift", dataDirectory: dataDirectory, analytics: analytics)
        analytics.start()
        updater = try! Updater(repo: "ugurcandede/beacon-demo", version: config.version, publicKey: config.public_key,
                               dataDirectory: dataDirectory, cask: config.cask, baseURL: config.base_url,
                               analytics: analytics)
        updater.cleanup()
        statsOn = analytics.enabled
        status = "Version \(config.version) (\(updater.installKind?.rawValue ?? "not a bundle"))"
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: nil) {
            [analytics] _ in analytics.flushBeforeQuit()
        }
        Task { await check() }
    }

    func check() async {
        status = "Checking…"
        do {
            release = try await updater.check()
            status = release.map { "\($0.version) is available" } ?? "Up to date (\(config.version))"
            if config.auto_update, release != nil, updater.installKind != nil {
                await install()
            }
        } catch {
            status = "Update failed (\(error)).\nDetails: update.log"
        }
    }

    func install() async {
        guard let release else { return }
        status = "Installing \(release.version)…"
        do {
            try await updater.install(release)
            NSApp.terminate(nil)
        } catch {
            status = "Update failed (\(error)).\nDetails: update.log"
        }
    }
}
