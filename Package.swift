// swift-tools-version: 6.0

// The Swift package at the repository root, so apps can depend on it by URL:
//   .package(url: "https://github.com/ugurcandede/beacon", from: "1.0.0")
// The sources live under swift/.

import PackageDescription

let package = Package(
    name: "Beacon",
    platforms: [.macOS(.v13)],
    products: [.library(name: "Beacon", targets: ["Beacon"])],
    targets: [
        .target(name: "Beacon", path: "swift/Sources/Beacon"),
        .testTarget(name: "BeaconTests", dependencies: ["Beacon"], path: "swift/Tests/BeaconTests"),
    ],
    swiftLanguageModes: [.v5]
)
