import SwiftUI

/// What an app shows about updates, driven by an Updater:
///
///     @StateObject var updates = UpdateModel(updater: updater, canInstall: { !session.isLocked })
///     UpdateBanner(model: updates)                  // in the menu or window
///     .task { await updates.check() }               // at launch; updates.check(manual: true) from a menu item
///
/// `canInstall` is the app's say in when an install may happen (TapLock: not
/// while the screen is locked); while it is false the banner shows why it waits.
@MainActor
public final class UpdateModel: ObservableObject {
    public enum State: Equatable {
        case idle, checking, upToDate
        case available(Release)
        case installing(Release)
        case failed(String)
    }

    @Published public internal(set) var state: State = .idle
    public let updater: Updater
    private let canInstall: () -> Bool

    public init(updater: Updater, canInstall: @escaping () -> Bool = { true }) {
        self.updater = updater
        self.canInstall = canInstall
    }

    public var installAllowed: Bool { canInstall() }

    public func check(manual: Bool = false) async {
        if case .installing = state { return }  // a check must not reset an install under way
        let before = state
        state = .checking
        do {
            state = try await updater.check(manual: manual).map(State.available) ?? .upToDate
        } catch {
            // Only when the user asked: a failed automatic check (offline at
            // launch) says nothing, and keeps a release already known.
            state = manual ? .failed("\(error)") : (before == .checking ? .idle : before)
        }
    }

    /// Installs and, once the new build is starting, quits the app.
    public func install() async {
        guard case let .available(release) = state, canInstall() else { return }
        state = .installing(release)
        do {
            try await updater.install(release)
            NSApplication.shared.terminate(nil)
        } catch let error as UpdateError where error.step == .withdrawn {
            state = .idle  // no longer installing
            await check(manual: true)  // paused or replaced since: show what the server offers now
        } catch {
            state = .failed("\(error)")
        }
    }

    /// Does nothing for a required release.
    public func skip() {
        guard case let .available(release) = state, !release.required else { return }
        updater.skip(release)
        state = .idle
    }
}

/// A compact banner for a menu bar popover or a settings window; it shows
/// nothing unless there is something to say.
public struct UpdateBanner: View {
    @ObservedObject var model: UpdateModel

    public init(model: UpdateModel) { self.model = model }

    public var body: some View {
        switch model.state {
        case .idle, .checking, .upToDate:
            EmptyView()
        case let .available(release):
            VStack(alignment: .leading, spacing: 6) {
                Text("Version \(release.version) is available").font(.headline)
                HStack {
                    Button("Update and restart") { Task { await model.install() } }
                        .disabled(!model.installAllowed)
                    Button("Release notes") { NSWorkspace.shared.open(release.notesURL) }
                    if !release.required { Button("Skip this version") { model.skip() } }
                }
                if !model.installAllowed {
                    Text("The update will be possible once the app is idle.").font(.caption).foregroundStyle(.secondary)
                }
            }
            .focusable(false)
        case let .installing(release):
            HStack {
                ProgressView().controlSize(.small)
                Text("Installing \(release.version)…")
            }
        case let .failed(reason):
            Text("Update failed: \(reason)").font(.caption).foregroundStyle(.secondary)
        }
    }
}
