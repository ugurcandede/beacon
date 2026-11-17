// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "BeaconDemo",
    platforms: [.macOS(.v13)],
    dependencies: [.package(path: "../..")],  // the repository root
    targets: [
        .executableTarget(name: "BeaconDemo", dependencies: [.product(name: "Beacon", package: "beacon")]),
    ],
    swiftLanguageModes: [.v5]
)
