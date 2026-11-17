// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "BeaconDemo",
    platforms: [.macOS(.v13)],
    dependencies: [.package(path: "../../swift")],
    targets: [
        .executableTarget(name: "BeaconDemo", dependencies: [.product(name: "Beacon", package: "swift")]),
    ],
    swiftLanguageModes: [.v5]
)
