import SwiftUI

/// Asks once whether usage statistics may be sent, for an Analytics made with
/// `requireConsent: true`. Show it while `analytics.consentNeeded`:
///
///     if analytics.consentNeeded { ConsentView(analytics: analytics, appName: "TapLock", privacyURL: url) }
public struct ConsentView: View {
    let analytics: Analytics
    let appName: String
    let privacyURL: URL
    var onAnswer: (Bool) -> Void

    public init(analytics: Analytics, appName: String, privacyURL: URL, onAnswer: @escaping (Bool) -> Void = { _ in }) {
        self.analytics = analytics
        self.appName = appName
        self.privacyURL = privacyURL
        self.onAnswer = onAnswer
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Help improve \(appName)?").font(.headline)
            Text("Send anonymous usage statistics and error reports: how the app is used, which version, "
                 + "the OS and the country -- never your files, typed text or names. "
                 + "You can change this in the settings at any time.")
                .font(.caption).fixedSize(horizontal: false, vertical: true)
            Link("What is collected", destination: privacyURL).font(.caption)
            HStack {
                Button("Don't send") { answer(false) }
                Button("Send") { answer(true) }.keyboardShortcut(.defaultAction)
            }
        }
        .padding()
    }

    private func answer(_ granted: Bool) {
        analytics.setConsent(granted)
        onAnswer(granted)
    }
}
