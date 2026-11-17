// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "Beacon",
    platforms: [.macOS(.v13)],
    products: [.library(name: "Beacon", targets: ["Beacon"])],
    targets: [
        .target(name: "Beacon"),
        .testTarget(name: "BeaconTests", dependencies: ["Beacon"]),
    ],
    swiftLanguageModes: [.v5]
)
