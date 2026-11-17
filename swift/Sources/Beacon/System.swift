import CoreGraphics
import CryptoKit
import Foundation
import IOKit

/// What the apps share about the machine they run on -- the same keys the
/// Python client sends, so reports split the same way across apps.
enum System {
    /// beacon's own version, sent as lib_version; the Python client's
    /// __version__ is kept the same (ProtocolTests checks).
    static let libVersion = "0.2.0"

    static var arch: String {
        #if arch(arm64)
        return "arm64"
        #else
        return "x64"
        #endif
    }

    /// The manifest key for this build: macos-arm64 or macos-x64.
    static var platformKey: String { "macos-\(arch)" }

    static func context() -> [String: Any] {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        return [
            "os": "macOS",
            "os_version": "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)",
            "arch": arch,
            "locale": Locale.current.identifier,
            "cpu_count": ProcessInfo.processInfo.activeProcessorCount,
            "memory_gb": Int(ProcessInfo.processInfo.physicalMemory >> 30),
            "model": sysctl("hw.model"),
            "lib": "beacon-swift",
            "lib_version": libVersion,
        ].merging(screens()) { $1 }
    }

    /// The same id in every beacon app on this machine, so one person running
    /// three apps counts once. A salted hash: the hardware UUID never leaves
    /// the machine. Empty when the OS does not say.
    static func machineID() -> String {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        defer { IOObjectRelease(service) }
        guard service != 0,
              let raw = IORegistryEntryCreateCFProperty(service, "IOPlatformUUID" as CFString, kCFAllocatorDefault, 0)?
                .takeRetainedValue() as? String else { return "" }
        return hashedMachineID(raw)
    }

    static func hashedMachineID(_ raw: String) -> String {
        let input = "beacon-machine:" + raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        return SHA256.hash(data: Data(input.utf8)).map { String(format: "%02x", $0) }.joined().prefix(32).description
    }

    /// When this process started, from the kernel (kinfo_proc), or nil.
    static func processStart() -> Date? {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()]
        guard Darwin.sysctl(&mib, 4, &info, &size, nil, 0) == 0 else { return nil }
        let start = info.kp_proc.p_un.__p_starttime
        return Date(timeIntervalSince1970: TimeInterval(start.tv_sec) + TimeInterval(start.tv_usec) / 1e6)
    }

    /// The most memory this process has held so far, in MB.
    static func memoryPeakMB() -> Int {
        var usage = rusage()
        getrusage(RUSAGE_SELF, &usage)
        return Int(usage.ru_maxrss >> 20)  // bytes on macOS
    }

    /// The main display's size in points and how many there are -- the same
    /// CoreGraphics calls the Python client makes.
    private static func screens() -> [String: Any] {
        var count: UInt32 = 0
        CGGetActiveDisplayList(0, nil, &count)
        let main = CGMainDisplayID()
        return ["screen_width": CGDisplayPixelsWide(main), "screen_height": CGDisplayPixelsHigh(main), "screen_count": Int(count)]
    }

    private static func sysctl(_ name: String) -> String {
        var size = 0
        sysctlbyname(name, nil, &size, nil, 0)
        var value = [CChar](repeating: 0, count: size)
        sysctlbyname(name, &value, &size, nil, 0)
        return String(cString: value)
    }
}
