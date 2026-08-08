// mac-ax-helper — macOS Accessibility Tree capture for context awareness
//
// Captures the AX element tree from running applications and outputs
// filtered, semantic JSON. Designed for LLM context injection — strips
// coordinates, visual chrome, and other noise that has no semantic value.
//
// Usage:
//   mac-ax-helper                       → frontmost app only
//   mac-ax-helper --all-visible         → all visible apps
//   mac-ax-helper --depth 100           → max traversal depth (default: 100)
//   mac-ax-helper --timeout 3           → per-app timeout in seconds (default: 3)
//
// Exit codes:
//   0 = success (JSON on stdout)
//   1 = general error
//   2 = accessibility not authorized
//
// Compile:
//   swiftc resources/mac-ax-helper.swift -o resources/mac-ax-helper -O -target arm64-apple-macos12.0 -swift-version 5

import AppKit
import ApplicationServices
import Foundation

// MARK: - Configuration

struct Config {
    enum Operation {
        case accessibilityTree
        case frontmostWindowMetadata
        case captureFrontmostWindow
    }

    var operation: Operation = .accessibilityTree
    var allVisible = false
    var appName: String? = nil  // --app-name: capture a specific app by name
    var focusedWindowOnly = false  // --focused-window-only: only capture the focused window
    var requireCompleteTree = false  // fail instead of silently pruning at --depth
    var raw = false  // --raw: preserve the unfiltered AX tree for parser debugging
    var maxDepth = 100   // 0 = unlimited
    var timeout: TimeInterval = 3
    var screenshotMaxWidth = 1920
    var screenshotJPEGQuality = 80
}

private let maximumTraversalNodes = 20_000
private let maximumTraversalDepth = 128
private let maximumCapturedStrings = 50_000
private let maximumSingleStringBytes = 16 * 1024
private let maximumCapturedStringBytes = 4 * 1024 * 1024
private let maximumAXJSONBytes = 8 * 1024 * 1024
private let axCaptureSchemaVersion = 1
private let resourceLimitsVersion = 1

private enum CaptureLimitError: Error {
    case exceeded
}

/// One budget is shared by every app/window in a helper invocation.  Limits
/// never produce a partial tree: callers propagate this error to a non-zero
/// process exit so downstream privacy policy cannot mistake truncation for a
/// complete Accessibility result.
private final class CaptureBudget {
    private var nodeCount = 0
    private var stringCount = 0
    private var stringBytes = 0
    private(set) var treeComplete = true

    func markIncomplete() {
        treeComplete = false
    }

    func consumeNode(depth: Int) throws {
        guard depth <= maximumTraversalDepth, nodeCount < maximumTraversalNodes else {
            throw CaptureLimitError.exceeded
        }
        nodeCount += 1
    }

    func consumeString(_ value: String?) throws -> String? {
        guard let value else { return nil }
        let byteCount = value.utf8.count
        guard byteCount <= maximumSingleStringBytes,
              stringCount < maximumCapturedStrings,
              byteCount <= maximumCapturedStringBytes - stringBytes
        else {
            throw CaptureLimitError.exceeded
        }
        stringCount += 1
        stringBytes += byteCount
        return value
    }

    func consumeStrings(_ values: [String]) throws -> [String] {
        var checked: [String] = []
        checked.reserveCapacity(min(values.count, maximumCapturedStrings))
        for value in values {
            guard let value = try consumeString(value) else { continue }
            checked.append(value)
        }
        return checked
    }
}

// MARK: - Filtered AX Node

/// Roles that are pure visual chrome — drop entirely (including children)
private let dropRoles: Set<String> = [
    "AXImage", "AXScrollBar", "AXValueIndicator", "AXSplitter",
    "AXColumn", "AXMenuBar", "AXGrowArea", "AXRuler",
    "AXMatte", "AXLayoutArea", "AXLayoutItem",
]

/// Roles that carry semantic text when they have a title or value
private let textBearingRoles: Set<String> = [
    "AXStaticText", "AXTextField", "AXTextArea", "AXLink",
    "AXButton", "AXMenuItem", "AXRadioButton", "AXCheckBox",
    "AXTab", "AXHeading", "AXCell", "AXRow",
    "AXWebArea", "AXPopUpButton", "AXMenuButton",
    "AXDisclosureTriangle", "AXComboBox", "AXSlider",
    "AXTabGroup",
]

/// Container roles that should be collapsed if they add no semantic value
private let containerRoles: Set<String> = [
    "AXGroup", "AXSplitGroup", "AXScrollArea", "AXList",
    "AXOutline", "AXBrowser", "AXDrawer", "AXSheet",
    "AXToolbar",
]

// MARK: - AX Helpers

private enum AXCaptureReadError: Error {
    case unexpectedAXError(AXError)
    case successWithoutValue
}

/// Interpret one Accessibility read without weakening legacy, non-strict
/// captures.  A strict complete-tree scan may treat an explicitly missing or
/// unsupported attribute as absent; every other AX failure is fatal because it
/// could otherwise hide a subtree or policy-relevant string behind a receipt.
private func axReadSucceeded(
    _ error: AXError,
    requireCompleteTree: Bool
) throws -> Bool {
    switch error {
    case .success:
        return true
    case .noValue, .attributeUnsupported:
        return false
    default:
        if requireCompleteTree {
            throw AXCaptureReadError.unexpectedAXError(error)
        }
        return false
    }
}

private func axCaptureValue(
    _ element: AXUIElement,
    _ attribute: String,
    requireCompleteTree: Bool
) throws -> CFTypeRef? {
    var ref: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(element, attribute as CFString, &ref)
    guard try axReadSucceeded(err, requireCompleteTree: requireCompleteTree) else {
        return nil
    }
    guard let ref else {
        if requireCompleteTree {
            throw AXCaptureReadError.successWithoutValue
        }
        return nil
    }
    return ref
}

func axValue(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    // Non-tree identity and screenshot operations retain their historical
    // best-effort behavior. Complete-tree traversal uses axCaptureValue.
    return try? axCaptureValue(element, attribute, requireCompleteTree: false)
}

func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    guard let ref = axValue(element, attribute) else { return nil }
    return ref as? String
}

private func axCaptureString(
    _ element: AXUIElement,
    _ attribute: String,
    config: Config
) throws -> String? {
    guard let ref = try axCaptureValue(
        element, attribute, requireCompleteTree: config.requireCompleteTree)
    else {
        return nil
    }
    return ref as? String
}

private func axCaptureStringList(
    _ element: AXUIElement,
    _ attribute: String,
    config: Config
) throws -> [String] {
    guard let ref = try axCaptureValue(
        element, attribute, requireCompleteTree: config.requireCompleteTree)
    else {
        return []
    }

    if let values = ref as? [String] {
        return values
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    if let value = ref as? String {
        return value
            .split(whereSeparator: \.isWhitespace)
            .map(String.init)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    return []
}

private func axCaptureBool(
    _ element: AXUIElement,
    _ attribute: String,
    config: Config
) throws -> Bool? {
    guard let ref = try axCaptureValue(
        element, attribute, requireCompleteTree: config.requireCompleteTree)
    else {
        return nil
    }
    return ref as? Bool
}

private func axCaptureChildren(
    _ element: AXUIElement,
    config: Config
) throws -> [AXUIElement] {
    guard let ref = try axCaptureValue(
        element,
        kAXChildrenAttribute as String,
        requireCompleteTree: config.requireCompleteTree)
    else {
        return []
    }
    guard let children = ref as? [AXUIElement] else {
        if config.requireCompleteTree {
            throw AXCaptureReadError.successWithoutValue
        }
        return []
    }
    return children
}

private func axCaptureAttributeNames(
    _ element: AXUIElement,
    config: Config
) throws -> [String] {
    var namesRef: CFArray?
    let err = AXUIElementCopyAttributeNames(element, &namesRef)
    guard try axReadSucceeded(err, requireCompleteTree: config.requireCompleteTree) else {
        return []
    }
    guard let names = namesRef as? [String] else {
        if config.requireCompleteTree {
            throw AXCaptureReadError.successWithoutValue
        }
        return []
    }
    return names.sorted()
}

// MARK: - Exact Focused Window Identity

private let windowIdentitySchemaVersion = 1
private let geometryTolerance: CGFloat = 0.01
private let maximumBoundMagnitude: CGFloat = 10_000_000
private let maximumBoundSide: CGFloat = 12_288
private let maximumBoundArea: CGFloat = 40_000_000
private let maximumSourceImageSide = 8192
private let maximumSourceImageArea = 12_000_000
private let maximumOutputImageSide = 8192
private let maximumOutputImageArea = 12_000_000
private let maximumScreenshotWidth = 4096
private let maximumJPEGBytes = 12 * 1024 * 1024
private let maximumBase64Characters = 4 * ((maximumJPEGBytes + 2) / 3)
private let maximumScreenshotJSONBytes = 17 * 1024 * 1024
private let maximumMetadataJSONBytes = 64 * 1024
private let maximumWindowRequestBytes = 64 * 1024
private let maximumIdentityStringBytes = 16 * 1024
private let maximumIdentityTotalBytes = 20 * 1024

struct WindowBounds: Equatable {
    let x: CGFloat
    let y: CGFloat
    let width: CGFloat
    let height: CGFloat

    init(rect: CGRect) {
        x = rect.origin.x
        y = rect.origin.y
        width = rect.size.width
        height = rect.size.height
    }

    var rect: CGRect {
        CGRect(x: x, y: y, width: width, height: height)
    }

    var valid: Bool {
        let values = [x, y, width, height]
        return values.allSatisfy { $0.isFinite && abs($0) <= maximumBoundMagnitude }
            && width > 0 && height > 0
            && width <= maximumBoundSide && height <= maximumBoundSide
            && width * height <= maximumBoundArea
    }

    func approximatelyEquals(_ other: WindowBounds) -> Bool {
        abs(x - other.x) <= geometryTolerance
            && abs(y - other.y) <= geometryTolerance
            && abs(width - other.width) <= geometryTolerance
            && abs(height - other.height) <= geometryTolerance
    }

    func toDict() -> [String: Any] {
        [
            "x": Double(x),
            "y": Double(y),
            "width": Double(width),
            "height": Double(height),
        ]
    }
}

struct FocusedWindowIdentity {
    let pid: pid_t
    let windowID: CGWindowID
    let bounds: WindowBounds
    let appName: String
    let bundleID: String
    let title: String

    var valid: Bool {
        let strings = [appName, bundleID, title]
        return pid > 0 && windowID > 0 && bounds.valid && !appName.isEmpty && !bundleID.isEmpty
            && strings.allSatisfy { $0.utf8.count <= maximumIdentityStringBytes }
            && strings.reduce(0) { $0 + $1.utf8.count } <= maximumIdentityTotalBytes
    }

    func matches(_ other: FocusedWindowIdentity) -> Bool {
        valid && other.valid
            && pid == other.pid
            && windowID == other.windowID
            && bounds.approximatelyEquals(other.bounds)
            && appName == other.appName
            && bundleID == other.bundleID
            && title == other.title
    }

    func toDict() -> [String: Any] {
        [
            "schema_version": windowIdentitySchemaVersion,
            "pid": Int(pid),
            "window_id": UInt64(windowID),
            "bounds": bounds.toDict(),
            "app_name": appName,
            "bundle_id": bundleID,
            "title": title,
        ]
    }
}

private struct CoreGraphicsWindow {
    let windowID: CGWindowID
    let bounds: WindowBounds
    let title: String?
}

func axPoint(_ element: AXUIElement, _ attribute: String) -> CGPoint? {
    guard let ref = axValue(element, attribute), CFGetTypeID(ref) == AXValueGetTypeID() else {
        return nil
    }
    let value = unsafeBitCast(ref, to: AXValue.self)
    guard AXValueGetType(value) == .cgPoint else { return nil }
    var point = CGPoint.zero
    guard AXValueGetValue(value, .cgPoint, &point) else { return nil }
    return point
}

func axSize(_ element: AXUIElement, _ attribute: String) -> CGSize? {
    guard let ref = axValue(element, attribute), CFGetTypeID(ref) == AXValueGetTypeID() else {
        return nil
    }
    let value = unsafeBitCast(ref, to: AXValue.self)
    guard AXValueGetType(value) == .cgSize else { return nil }
    var size = CGSize.zero
    guard AXValueGetValue(value, .cgSize, &size) else { return nil }
    return size
}

private func cgBounds(_ value: Any?) -> WindowBounds? {
    guard let dictionary = value as? NSDictionary else { return nil }
    var rect = CGRect.zero
    guard CGRectMakeWithDictionaryRepresentation(dictionary as CFDictionary, &rect) else {
        return nil
    }
    let bounds = WindowBounds(rect: rect)
    return bounds.valid ? bounds : nil
}

private func cgWindows(for pid: pid_t) -> [CoreGraphicsWindow]? {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID),
          let dictionaries = raw as? [[String: Any]]
    else {
        return nil
    }

    var windows: [CoreGraphicsWindow] = []
    for dictionary in dictionaries {
        guard let ownerPID = dictionary[kCGWindowOwnerPID as String] as? NSNumber,
              ownerPID.int32Value == pid,
              let onscreen = dictionary[kCGWindowIsOnscreen as String] as? NSNumber,
              onscreen.boolValue,
              let number = dictionary[kCGWindowNumber as String] as? NSNumber,
              number.uint32Value > 0,
              let bounds = cgBounds(dictionary[kCGWindowBounds as String])
        else {
            continue
        }
        windows.append(
            CoreGraphicsWindow(
                windowID: number.uint32Value,
                bounds: bounds,
                title: dictionary[kCGWindowName as String] as? String
            ))
    }
    return windows
}

/// Select one geometry match without ignoring contradictory title evidence.
/// CoreGraphics may omit a title without Screen Recording access, so a unique
/// nil/empty title remains usable. A present mismatching title is never safe.
private func selectFocusedCGWindow(
    _ geometryMatches: [CoreGraphicsWindow], axTitle: String
) -> CoreGraphicsWindow? {
    if geometryMatches.count == 1, let candidate = geometryMatches.first {
        guard candidate.title == nil || candidate.title?.isEmpty == true
            || candidate.title == axTitle
        else {
            return nil
        }
        return candidate
    }
    guard geometryMatches.allSatisfy({ candidate in
        guard let title = candidate.title else { return false }
        return !title.isEmpty
    }) else {
        return nil
    }
    let exactTitleMatches = geometryMatches.filter { $0.title == axTitle }
    guard exactTitleMatches.count == 1 else { return nil }
    return exactTitleMatches[0]
}

/// Resolve Accessibility's focused window to one exact, ordered CoreGraphics
/// window. Ambiguity is an error; there is intentionally no "first window"
/// fallback because that could bind pixels to a sibling window.
func focusedWindowIdentity() -> FocusedWindowIdentity? {
    let workspace = NSWorkspace.shared
    guard let app = workspace.frontmostApplication else { return nil }
    let pid = app.processIdentifier
    guard pid > 0,
          let appName = app.localizedName, !appName.isEmpty,
          let bundleID = app.bundleIdentifier, !bundleID.isEmpty
    else {
        return nil
    }

    let appRef = AXUIElementCreateApplication(pid)
    guard let focusedRef = axValue(appRef, kAXFocusedWindowAttribute as String),
          CFGetTypeID(focusedRef) == AXUIElementGetTypeID()
    else {
        return nil
    }
    let focusedWindow = unsafeBitCast(focusedRef, to: AXUIElement.self)
    guard axString(focusedWindow, kAXRoleAttribute as String) == (kAXWindowRole as String),
          let position = axPoint(focusedWindow, kAXPositionAttribute as String),
          let size = axSize(focusedWindow, kAXSizeAttribute as String)
    else {
        return nil
    }
    let axBounds = WindowBounds(
        rect: CGRect(x: position.x, y: position.y, width: size.width, height: size.height))
    guard axBounds.valid, let allCandidates = cgWindows(for: pid) else { return nil }

    let geometryMatches = allCandidates.filter { $0.bounds.approximatelyEquals(axBounds) }
    let axTitle = axString(focusedWindow, kAXTitleAttribute as String) ?? ""
    guard let candidate = selectFocusedCGWindow(geometryMatches, axTitle: axTitle) else {
        return nil
    }

    // The frontmost process can change while AX/CG calls are in flight.
    guard workspace.frontmostApplication?.processIdentifier == pid else { return nil }
    let identity = FocusedWindowIdentity(
        pid: pid,
        windowID: candidate.windowID,
        bounds: candidate.bounds,
        appName: appName,
        bundleID: bundleID,
        title: axTitle
    )
    return identity.valid ? identity : nil
}

private func axWindow(_ window: AXUIElement, matches identity: FocusedWindowIdentity) -> Bool {
    guard axString(window, kAXRoleAttribute as String) == (kAXWindowRole as String),
          let position = axPoint(window, kAXPositionAttribute as String),
          let size = axSize(window, kAXSizeAttribute as String)
    else {
        return false
    }
    let bounds = WindowBounds(
        rect: CGRect(x: position.x, y: position.y, width: size.width, height: size.height))
    let title = axString(window, kAXTitleAttribute as String) ?? ""
    return bounds.approximatelyEquals(identity.bounds) && title == identity.title
}

private func jsonNumber(_ value: Any?) -> NSNumber? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else {
        return nil
    }
    return number
}

private func parseExpectedWindowIdentity(_ data: Data) -> FocusedWindowIdentity? {
    guard let raw = try? JSONSerialization.jsonObject(with: data),
          let dictionary = raw as? [String: Any],
          jsonNumber(dictionary["schema_version"])?.intValue == windowIdentitySchemaVersion,
          let pidNumber = jsonNumber(dictionary["pid"]),
          pidNumber.int64Value > 0, pidNumber.int64Value <= Int64(Int32.max),
          let windowNumber = jsonNumber(dictionary["window_id"]),
          windowNumber.uint64Value > 0, windowNumber.uint64Value <= UInt64(UInt32.max),
          let rawBounds = dictionary["bounds"] as? [String: Any],
          let x = jsonNumber(rawBounds["x"])?.doubleValue,
          let y = jsonNumber(rawBounds["y"])?.doubleValue,
          let width = jsonNumber(rawBounds["width"])?.doubleValue,
          let height = jsonNumber(rawBounds["height"])?.doubleValue,
          let appName = dictionary["app_name"] as? String,
          let bundleID = dictionary["bundle_id"] as? String,
          let title = dictionary["title"] as? String
    else {
        return nil
    }
    let identity = FocusedWindowIdentity(
        pid: pid_t(pidNumber.int32Value),
        windowID: CGWindowID(windowNumber.uint32Value),
        bounds: WindowBounds(
            rect: CGRect(x: x, y: y, width: width, height: height)),
        appName: appName,
        bundleID: bundleID,
        title: title
    )
    return identity.valid ? identity : nil
}

private func resizedImage(_ image: CGImage, maxWidth: Int) -> CGImage? {
    guard image.width > 0, image.height > 0,
          image.width <= maximumSourceImageSide, image.height <= maximumSourceImageSide,
          image.width <= maximumSourceImageArea / image.height,
          (1 ... maximumScreenshotWidth).contains(maxWidth)
    else {
        return nil
    }
    let sourceArea = Double(image.width) * Double(image.height)
    let scale = min(
        1.0,
        Double(maxWidth) / Double(image.width),
        Double(maximumOutputImageSide) / Double(image.height),
        sqrt(Double(maximumOutputImageArea) / sourceArea)
    )
    let targetWidth = max(1, Int((Double(image.width) * scale).rounded(.down)))
    let targetHeight = max(1, Int((Double(image.height) * scale).rounded(.down)))
    guard targetWidth <= maxWidth,
          targetWidth <= maximumOutputImageSide,
          targetHeight <= maximumOutputImageSide,
          targetWidth <= maximumOutputImageArea / targetHeight
    else {
        return nil
    }
    if targetWidth == image.width && targetHeight == image.height { return image }
    guard let context = CGContext(
        data: nil,
        width: targetWidth,
        height: targetHeight,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return nil
    }
    context.interpolationQuality = .high
    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: targetWidth, height: targetHeight))
    context.draw(image, in: CGRect(x: 0, y: 0, width: targetWidth, height: targetHeight))
    return context.makeImage()
}

private func jpegData(_ image: CGImage, quality: Int) -> Data? {
    guard (1 ... 100).contains(quality) else { return nil }
    let bitmap = NSBitmapImageRep(cgImage: image)
    return bitmap.representation(
        using: .jpeg,
        properties: [.compressionFactor: Double(quality) / 100.0]
    )
}

private func writeJSON(
    _ object: [String: Any],
    pretty: Bool = false,
    maximumBytes: Int
) -> Bool {
    let options: JSONSerialization.WritingOptions = pretty ? [.prettyPrinted, .sortedKeys] : [.sortedKeys]
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: options),
          data.count <= maximumBytes
    else {
        return false
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
    return true
}

private func readBoundedStdin(maximumBytes: Int) -> Data? {
    let input = FileHandle.standardInput
    var data = Data()
    while true {
        let remainingWithSentinel = maximumBytes - data.count + 1
        guard remainingWithSentinel > 0 else { return nil }
        let chunk = input.readData(ofLength: min(64 * 1024, remainingWithSentinel))
        if chunk.isEmpty { return data }
        data.append(chunk)
        if data.count > maximumBytes { return nil }
    }
}

private func captureExpectedFrontmostWindow(config: Config) -> [String: Any]? {
    guard let requestData = readBoundedStdin(maximumBytes: maximumWindowRequestBytes) else {
        return nil
    }
    guard let expected = parseExpectedWindowIdentity(requestData),
          let before = focusedWindowIdentity(),
          expected.matches(before)
    else {
        return nil
    }

    // Do not trigger a consent prompt from this background helper. The caller
    // can explain how to grant Screen Recording access; absent access is a
    // normal fail-closed result.
    guard CGPreflightScreenCaptureAccess() else { return nil }
    guard before.bounds.width <= CGFloat(maximumSourceImageSide),
          before.bounds.height <= CGFloat(maximumSourceImageSide),
          before.bounds.width * before.bounds.height <= CGFloat(maximumSourceImageArea)
    else {
        return nil
    }

    let windowNumbers = [NSNumber(value: before.windowID)] as CFArray
    guard let captured = CGImage(
        windowListFromArrayScreenBounds: .null,
        windowArray: windowNumbers,
        imageOption: [.boundsIgnoreFraming, .nominalResolution]
    ),
          captured.width > 0, captured.height > 0,
          let afterCapture = focusedWindowIdentity(),
          before.matches(afterCapture),
          let scaled = resizedImage(captured, maxWidth: config.screenshotMaxWidth),
          let encoded = jpegData(scaled, quality: config.screenshotJPEGQuality),
          encoded.count <= maximumJPEGBytes,
          let finalIdentity = focusedWindowIdentity(),
          afterCapture.matches(finalIdentity)
    else {
        return nil
    }

    let base64 = encoded.base64EncodedString()
    guard base64.utf8.count <= maximumBase64Characters else { return nil }

    return [
        "schema_version": windowIdentitySchemaVersion,
        "mime_type": "image/jpeg",
        "image_base64": base64,
        "width": scaled.width,
        "height": scaled.height,
        "window_meta": finalIdentity.toDict(),
    ]
}

// MARK: - Tree Traversal with Filtering

struct AXNode {
    var role: String?
    var subrole: String?
    var title: String?
    var description: String?
    var value: String?
    var identifier: String?
    var domIdentifier: String?
    var domClassList: [String]
    var attributeNames: [String]
    var children: [AXNode]

    var isEmpty: Bool {
        return subrole == nil
            && title == nil
            && description == nil
            && value == nil
            && identifier == nil
            && domIdentifier == nil
            && domClassList.isEmpty
            && attributeNames.isEmpty
            && children.isEmpty
    }

    func toDict() -> [String: Any]? {
        if isEmpty { return nil }

        var dict: [String: Any] = [:]
        if let r = role { dict["role"] = r }
        if let sr = subrole { dict["subrole"] = sr }
        if let t = title { dict["title"] = t }
        if let d = description { dict["description"] = d }
        if let v = value { dict["value"] = v }
        if let id = identifier { dict["identifier"] = id }
        if let domID = domIdentifier { dict["domIdentifier"] = domID }
        if !domClassList.isEmpty { dict["domClassList"] = domClassList }
        if !attributeNames.isEmpty { dict["attributeNames"] = attributeNames }
        if !children.isEmpty {
            let childDicts = children.compactMap { $0.toDict() }
            if !childDicts.isEmpty {
                dict["children"] = childDicts
            }
        }
        // A node with only a role and no text and no children is noise
        if dict.count == 1 && dict.keys.first == "role" { return nil }
        return dict
    }
}

private func traverseElement(
    _ element: AXUIElement,
    depth: Int,
    config: Config,
    budget: CaptureBudget
) throws -> AXNode? {
    if config.maxDepth > 0 && depth > config.maxDepth {
        if config.requireCompleteTree { throw CaptureLimitError.exceeded }
        budget.markIncomplete()
        return nil
    }
    try budget.consumeNode(depth: depth)

    let role = try budget.consumeString(
        axCaptureString(element, kAXRoleAttribute as String, config: config))

    // Ordinary filtered captures may drop pure visual chrome. Strict complete-
    // tree mode is a privacy scan: it must still inspect the node's strings and
    // descendants, otherwise a filtered wrapper could hide deny evidence while
    // receiving a completeness receipt.
    if !config.raw, !config.requireCompleteTree,
       let role = role, dropRoles.contains(role)
    {
        return nil
    }

    // Check for secure text field — redact value
    let subrole = try budget.consumeString(
        axCaptureString(element, kAXSubroleAttribute as String, config: config))
    let isSecure = role == "AXTextField" && subrole == "AXSecureTextField"
    let rawDescription = try budget.consumeString(
        try axCaptureString(element, kAXDescriptionAttribute as String, config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
    let rawIdentifier = try budget.consumeString(
        try axCaptureString(element, kAXIdentifierAttribute as String, config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
    let rawDOMIdentifier = try budget.consumeString(
        try axCaptureString(element, "AXDOMIdentifier", config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
    let domClassList = try budget.consumeStrings(
        axCaptureStringList(element, "AXDOMClassList", config: config))
    let attributeNames = try budget.consumeStrings(
        config.raw ? try axCaptureAttributeNames(element, config: config) : [])

    // Get text content
    var title = try budget.consumeString(
        try axCaptureString(element, kAXTitleAttribute as String, config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
    var value: String?

    if isSecure {
        value = try budget.consumeString("[REDACTED]")
    } else {
        if let rawValue = try axCaptureString(
            element, kAXValueAttribute as String, config: config)
        {
            let v = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
            _ = try budget.consumeString(v)
            if !v.isEmpty { value = v }
        }
    }

    // Clean up empty strings
    if title?.isEmpty == true { title = nil }
    let description = rawDescription?.isEmpty == true ? nil : rawDescription
    let identifier = rawIdentifier?.isEmpty == true ? nil : rawIdentifier
    let domIdentifier = rawDOMIdentifier?.isEmpty == true ? nil : rawDOMIdentifier

    // AXGroup titles are always Obj-C class names (BrowserUserView, ContentsView, …)
    // — never semantic content. Strip them so the single-child promotion logic fires
    // correctly and container chains collapse properly.
    if !config.raw && !config.requireCompleteTree && role == "AXGroup" {
        title = nil
    }

    // Get description as fallback for title (skip for AXGroup — same noise issue)
    if !config.raw && !config.requireCompleteTree
        && title == nil && value == nil && role != "AXGroup"
    {
        if let desc = description, !desc.isEmpty
        {
            title = desc
        }
    }

    // Recursively process children
    let childElements = try axCaptureChildren(element, config: config)
    var childNodes: [AXNode] = []
    for child in childElements {
        if let node = try traverseElement(
            child, depth: depth + 1, config: config, budget: budget)
        {
            childNodes.append(node)
        }
    }

    // A strict privacy scan must preserve every selected AX field that was
    // inspected. Semantic cleanup below is useful for ordinary captures, but
    // applying it here could erase deny evidence while still issuing a
    // complete-tree receipt (for example an AXLink identifier containing a URL).
    if config.requireCompleteTree {
        return AXNode(
            role: role,
            subrole: subrole,
            title: title,
            description: description,
            value: value,
            identifier: identifier,
            domIdentifier: domIdentifier,
            domClassList: domClassList,
            attributeNames: attributeNames,
            children: childNodes
        )
    }

    let hasText = title != nil || value != nil
    let hasMetadata = subrole != nil
        || description != nil
        || identifier != nil
        || domIdentifier != nil
        || !domClassList.isEmpty

    // For text-bearing roles: keep if they have text or meaningful children
    if let role = role, textBearingRoles.contains(role) {
        if hasText || description != nil || !childNodes.isEmpty {
            return AXNode(
                role: role,
                subrole: subrole,
                title: title,
                description: description,
                value: value,
                identifier: identifier,
                domIdentifier: domIdentifier,
                domClassList: domClassList,
                attributeNames: attributeNames,
                children: childNodes
            )
        }
        return nil
    }

    // For container roles: collapse if no text and single child or no semantic content
    if let role = role, containerRoles.contains(role) {
        if !config.raw && !hasText && !hasMetadata {
            // Single child → promote it
            if childNodes.count == 1 {
                return childNodes[0]
            }
            // No children → drop
            if childNodes.isEmpty {
                return nil
            }
        }
        // Multiple children or has text → keep as container
        return AXNode(
            role: role,
            subrole: subrole,
            title: title,
            description: description,
            value: value,
            identifier: identifier,
            domIdentifier: domIdentifier,
            domClassList: domClassList,
            attributeNames: attributeNames,
            children: childNodes
        )
    }

    // For window and application roles: always keep
    if let role = role, (role == "AXWindow" || role == "AXApplication") {
        return AXNode(
            role: role,
            subrole: subrole,
            title: title,
            description: description,
            value: value,
            identifier: identifier,
            domIdentifier: domIdentifier,
            domClassList: domClassList,
            attributeNames: attributeNames,
            children: childNodes
        )
    }

    // For unknown roles: keep if they have text or children
    if hasText || hasMetadata || !childNodes.isEmpty {
        return AXNode(
            role: role,
            subrole: subrole,
            title: title,
            description: description,
            value: value,
            identifier: identifier,
            domIdentifier: domIdentifier,
            domClassList: domClassList,
            attributeNames: attributeNames,
            children: childNodes
        )
    }

    return nil
}

// MARK: - Window Processing

/// Process a window with full element traversal.
private func processWindow(
    _ window: AXUIElement,
    config: Config,
    budget: CaptureBudget
) throws -> [String: Any]? {
    try budget.consumeNode(depth: 1)
    let title = try budget.consumeString(
        try axCaptureString(window, kAXTitleAttribute as String, config: config) ?? "") ?? ""
    let focused = try axCaptureBool(
        window, kAXFocusedAttribute as String, config: config) ?? false
    let subrole = try budget.consumeString(
        try axCaptureString(window, kAXSubroleAttribute as String, config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
    let description = try budget.consumeString(
        try axCaptureString(window, kAXDescriptionAttribute as String, config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
    let identifier = try budget.consumeString(
        try axCaptureString(window, kAXIdentifierAttribute as String, config: config)?
            .trimmingCharacters(in: .whitespacesAndNewlines))

    let children = try axCaptureChildren(window, config: config)
    var elements: [[String: Any]] = []

    for child in children {
        if let node = try traverseElement(
            child, depth: 2, config: config, budget: budget),
           let dict = node.toDict()
        {
            elements.append(dict)
        }
    }

    // Skip windows with no title and no content
    if title.isEmpty && elements.isEmpty { return nil }

    var windowDict: [String: Any] = [
        "title": title,
    ]
    if let subrole, !subrole.isEmpty { windowDict["subrole"] = subrole }
    if let description, !description.isEmpty { windowDict["description"] = description }
    if let identifier, !identifier.isEmpty { windowDict["identifier"] = identifier }
    if focused { windowDict["focused"] = true }
    if !elements.isEmpty { windowDict["elements"] = elements }
    return windowDict
}


// MARK: - App Processing

private func processApp(
    pid: pid_t,
    name: String,
    bundleID: String?,
    isFrontmost: Bool,
    config: Config,
    budget: CaptureBudget,
    expectedFocusedIdentity: FocusedWindowIdentity? = nil
)
    throws -> [String: Any]?
{
    let appRef = AXUIElementCreateApplication(pid)

    // Identify the focused window so we can mark it in the output.
    let focusedWindowRef = try axCaptureValue(
        appRef,
        kAXFocusedWindowAttribute as String,
        requireCompleteTree: config.requireCompleteTree)
    let focusedElement: AXUIElement?
    if let focusedWindowRef,
       CFGetTypeID(focusedWindowRef) == AXUIElementGetTypeID()
    {
        focusedElement = unsafeBitCast(focusedWindowRef, to: AXUIElement.self)
    } else {
        focusedElement = nil
    }

    if let expected = expectedFocusedIdentity {
        guard expected.pid == pid,
              expected.appName == name,
              expected.bundleID == bundleID,
              isFrontmost,
              let focusedElement,
              axWindow(focusedElement, matches: expected)
        else {
            return nil
        }
    }

    // Get all children (AXChildren includes windows across all Spaces,
    // unlike kAXWindowsAttribute which only returns the current Space).
    var childrenRef: CFTypeRef?
    var childrenReadError: Error?
    let semaphore = DispatchSemaphore(value: 0)
    var timedOut = false

    DispatchQueue.global(qos: .userInitiated).async {
        do {
            childrenRef = try axCaptureValue(
                appRef,
                kAXChildrenAttribute as String,
                requireCompleteTree: config.requireCompleteTree)
        } catch {
            childrenReadError = error
        }
        semaphore.signal()
    }

    if semaphore.wait(timeout: .now() + config.timeout) == .timedOut {
        timedOut = true
        budget.markIncomplete()
    }
    if !timedOut, let childrenReadError {
        throw childrenReadError
    }

    var windowDicts: [[String: Any]] = []

    if !timedOut, let ref = childrenRef, let children = ref as? [AXUIElement] {
        var foundFocused = false
        for child in children {
            let role = try axCaptureString(
                child, kAXRoleAttribute as String, config: config)
            guard role == "AXWindow" else { continue }

            let isFocusedWindow = focusedElement != nil && CFEqual(child, focusedElement!)

            // If --focused-window-only, skip non-focused windows
            if config.focusedWindowOnly && !isFocusedWindow {
                continue
            }

            if var dict = try processWindow(child, config: config, budget: budget) {
                if isFocusedWindow {
                    dict["focused"] = true
                    foundFocused = true
                }
                windowDicts.append(dict)
            }
        }

        // Focused-window mode is a privacy boundary. If AX did not return the
        // focused element from this exact application, fail closed instead of
        // substituting an arbitrary sibling window.
        if config.focusedWindowOnly && !foundFocused { return nil }
    }

    if windowDicts.isEmpty { return nil }

    if let expected = expectedFocusedIdentity {
        guard let focusedElement, axWindow(focusedElement, matches: expected) else { return nil }
    }

    guard let checkedName = try budget.consumeString(name) else { return nil }
    let checkedBundleID = try budget.consumeString(bundleID)
    var appDict: [String: Any] = [
        "pid": pid,
        "name": checkedName,
        "is_frontmost": isFrontmost,
    ]
    if let bid = checkedBundleID { appDict["bundle_id"] = bid }
    appDict["windows"] = windowDicts
    return appDict
}

// MARK: - Main

func parseArgs() -> Config {
    var config = Config()
    var args = CommandLine.arguments.dropFirst()

    while let arg = args.first {
        args = args.dropFirst()
        switch arg {
        case "--frontmost-window-metadata":
            config.operation = .frontmostWindowMetadata
        case "--capture-frontmost-window":
            config.operation = .captureFrontmostWindow
        case "--all-visible":
            config.allVisible = true
        case "--app-name":
            if let next = args.first {
                config.appName = next
                args = args.dropFirst()
            }
        case "--depth":
            if let next = args.first, let val = Int(next) {
                config.maxDepth = val  // 0 = unlimited
                args = args.dropFirst()
            }
        case "--timeout":
            if let next = args.first, let val = Double(next) {
                config.timeout = val
                args = args.dropFirst()
            }
        case "--max-width":
            if let next = args.first, let val = Int(next),
               (1 ... maximumScreenshotWidth).contains(val)
            {
                config.screenshotMaxWidth = val
                args = args.dropFirst()
            } else {
                fputs("Invalid screenshot width.\n", stderr)
                exit(1)
            }
        case "--jpeg-quality":
            if let next = args.first, let val = Int(next), (1 ... 100).contains(val) {
                config.screenshotJPEGQuality = val
                args = args.dropFirst()
            } else {
                fputs("Invalid JPEG quality.\n", stderr)
                exit(1)
            }
        case "--focused-window-only":
            config.focusedWindowOnly = true
        case "--require-complete-tree":
            config.requireCompleteTree = true
        case "--raw":
            config.raw = true
        case "--help", "-h":
            fputs(
                """
                Usage: mac-ax-helper [--all-visible] [--app-name NAME] [--focused-window-only] [--require-complete-tree] [--depth N] [--timeout SECS] [--raw]
                  (default)           Capture frontmost app only
                  --all-visible       Capture all visible apps
                  --app-name NAME     Capture a specific app by name (case-insensitive)
                  --focused-window-only
                                      Capture only the AX focused window; never fall back
                  --require-complete-tree
                                      Fail on pruning, limits, or non-benign AX read errors
                  --frontmost-window-metadata
                                      Emit exact focused PID/CGWindowID/bounds JSON
                  --capture-frontmost-window
                                      Read expected identity JSON on stdin and emit an exact-window JPEG
                  --max-width N       Maximum screenshot pixel width (default: 1920)
                  --jpeg-quality N    Screenshot JPEG quality, 1-100 (default: 80)
                  --depth N           Max traversal depth (default: 100; hard cap: 128)
                  --timeout SECS      Per-app timeout in seconds (default: 3)
                  --raw               Preserve the unfiltered AX tree for debugging/parser work
                \n
                """, stderr)
            exit(0)
        default:
            fputs("Unknown argument.\n", stderr)
            exit(1)
        }
    }
    return config
}

func runMain() throws {
    let config = parseArgs()

    // Check accessibility permission
    let trusted = AXIsProcessTrustedWithOptions(
        [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
    )
    if !trusted {
        fputs("Accessibility permission not granted. Please enable in System Settings.\n", stderr)
        exit(2)
    }

    switch config.operation {
    case .frontmostWindowMetadata:
        guard let identity = focusedWindowIdentity(),
              writeJSON(identity.toDict(), maximumBytes: maximumMetadataJSONBytes)
        else {
            fputs("Focused window identity unavailable.\n", stderr)
            exit(1)
        }
        return
    case .captureFrontmostWindow:
        guard let result = captureExpectedFrontmostWindow(config: config),
              writeJSON(result, maximumBytes: maximumScreenshotJSONBytes)
        else {
            fputs("Exact focused-window capture unavailable.\n", stderr)
            exit(3)
        }
        return
    case .accessibilityTree:
        break
    }

    let workspace = NSWorkspace.shared
    let runningApps = workspace.runningApplications

    // Use the dedicated API — runningApplications order is unspecified,
    // so filtering with .first { $0.isActive } is unreliable.
    let frontmostApp = workspace.frontmostApplication
    let frontmostPID = frontmostApp?.processIdentifier ?? -1
    let budget = CaptureBudget()

    let focusedFenceBefore: FocusedWindowIdentity?
    if config.focusedWindowOnly && config.appName == nil && !config.allVisible {
        guard let identity = focusedWindowIdentity() else {
            fputs("Focused window identity unavailable.\n", stderr)
            exit(1)
        }
        focusedFenceBefore = identity
    } else {
        focusedFenceBefore = nil
    }

    var appDicts: [[String: Any]] = []

    if let targetName = config.appName {
        // Capture a specific app by name.
        // Match against localizedName (e.g. "飞书") and process name
        // (e.g. "Feishu") since Electron's AppleScript reports the
        // process name, not the localized display name.
        let targetLower = targetName.lowercased()
        guard let app = runningApps.first(where: { runApp in
            let localized = (runApp.localizedName ?? "").lowercased()
            let process = (runApp.executableURL?.lastPathComponent ?? "").lowercased()
            let bundle = (runApp.bundleIdentifier ?? "").lowercased()
            return localized == targetLower
                || process == targetLower
                || bundle == targetLower
                || bundle.hasSuffix(".\(targetLower)")
        }) else {
            fputs("No running app matching '\(targetName)' found.\n", stderr)
            exit(1)
        }

        let pid = app.processIdentifier
        let name = app.localizedName ?? "Unknown"
        let bundleID = app.bundleIdentifier
        let isFrontmost = pid == frontmostPID

        if let dict = try processApp(
            pid: pid, name: name, bundleID: bundleID,
            isFrontmost: isFrontmost, config: config, budget: budget)
        {
            appDicts.append(dict)
        }
    } else if config.allVisible {
        // Capture all regular, visible apps
        for app in runningApps {
            guard app.activationPolicy == .regular else { continue }

            let pid = app.processIdentifier
            let name = app.localizedName ?? "Unknown"
            let bundleID = app.bundleIdentifier
            let isFrontmost = pid == frontmostPID

            if let dict = try processApp(
                pid: pid, name: name, bundleID: bundleID,
                isFrontmost: isFrontmost, config: config, budget: budget)
            {
                appDicts.append(dict)
            }
        }
    } else {
        // Capture frontmost app only
        guard let app = frontmostApp else {
            fputs("No frontmost application found.\n", stderr)
            exit(1)
        }

        let pid = app.processIdentifier
        let name = app.localizedName ?? "Unknown"
        let bundleID = app.bundleIdentifier

        if let dict = try processApp(
            pid: pid, name: name, bundleID: bundleID,
            isFrontmost: true, config: config, budget: budget,
            expectedFocusedIdentity: focusedFenceBefore)
        {
            appDicts.append(dict)
        }
    }

    // Build output
    let iso8601Formatter = ISO8601DateFormatter()
    iso8601Formatter.formatOptions = [.withInternetDateTime]

    var output: [String: Any] = [
        "timestamp": iso8601Formatter.string(from: Date()),
        "apps": appDicts,
    ]

    // This receipt belongs exclusively to the AX-tree protocol. Metadata and
    // screenshot operations return above and must never inherit these fields.
    // Legacy non-strict captures may still emit a partial tree, but a partial
    // result never receives a completeness claim.
    if budget.treeComplete && !appDicts.isEmpty {
        let effectiveMaxDepth = config.maxDepth > 0
            ? min(config.maxDepth, maximumTraversalDepth)
            : maximumTraversalDepth
        output["ax_capture_schema_version"] = axCaptureSchemaVersion
        output["tree_complete"] = true
        output["focused_window_only"] = config.focusedWindowOnly
        output["effective_max_depth"] = effectiveMaxDepth
        output["resource_limits_version"] = resourceLimitsVersion
    }

    if let before = focusedFenceBefore {
        guard appDicts.count == 1,
              let after = focusedWindowIdentity(),
              before.matches(after)
        else {
            fputs("Focused window changed during AX capture.\n", stderr)
            exit(1)
        }
        output["window_meta"] = after.toDict()
    }

    // Serialize to JSON
    guard let jsonData = try? JSONSerialization.data(
        withJSONObject: output, options: [.prettyPrinted, .sortedKeys]),
          jsonData.count <= maximumAXJSONBytes
    else {
        fputs("Failed to serialize JSON output.\n", stderr)
        exit(1)
    }

    FileHandle.standardOutput.write(jsonData)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func main() {
    do {
        try runMain()
    } catch CaptureLimitError.exceeded {
        fputs("AX capture resource limit exceeded.\n", stderr)
        exit(4)
    } catch AXCaptureReadError.unexpectedAXError {
        fputs("AX capture attribute read failed.\n", stderr)
        exit(5)
    } catch AXCaptureReadError.successWithoutValue {
        fputs("AX capture attribute read returned no value.\n", stderr)
        exit(5)
    } catch {
        fputs("AX capture failed.\n", stderr)
        exit(1)
    }
}

main()
