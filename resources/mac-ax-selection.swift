// mac-ax-selection — one-shot, fail-closed selected-text receipt for Prompt Rescue.
//
// This helper reads only AXSelectedText. It never falls back to AXValue,
// clipboard contents, neighboring text, paste, replacement, or submission.

import AppKit
import ApplicationServices
import Foundation

let maxIdentityCharacters = 512
let maxRoleCharacters = 128
let maxSelectionCharacters = 20_000
let maxOutputBytes = 128 * 1024

enum SelectionFailure: String, Error {
    case accessibilityUntrusted = "accessibility_untrusted"
    case noFocusedApplication = "no_focused_application"
    case noFocusedWindow = "no_focused_window"
    case noFocusedElement = "no_focused_element"
    case selfSelection = "self_selection"
    case secureField = "secure_field"
    case noSelection = "no_selection"
    case multipleSelection = "multiple_selection"
    case selectionTooLarge = "selection_too_large"
    case invalidIdentity = "invalid_identity"
    case focusChanged = "focus_changed"
    case outputUnavailable = "output_unavailable"
}

struct FocusedIdentity {
    let appElement: AXUIElement
    let window: AXUIElement
    let pid: pid_t
    let appName: String
    let bundleID: String
    let windowTitle: String
}

func copyValue(_ element: AXUIElement, _ attribute: CFString) throws -> CFTypeRef {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
          let value
    else {
        throw SelectionFailure.invalidIdentity
    }
    return value
}

func copyElement(
    _ element: AXUIElement,
    _ attribute: CFString,
    failure: SelectionFailure
) throws -> AXUIElement {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
          let value,
          CFGetTypeID(value) == AXUIElementGetTypeID()
    else {
        throw failure
    }
    return unsafeBitCast(value, to: AXUIElement.self)
}

func copyString(
    _ element: AXUIElement,
    _ attribute: CFString,
    required: Bool = false
) throws -> String {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    if !required, error == .attributeUnsupported || error == .noValue {
        return ""
    }
    guard error == .success, let string = value as? String else {
        throw SelectionFailure.invalidIdentity
    }
    return string
}

func checkedIdentity(_ value: String, maximum: Int) throws -> String {
    guard !value.contains("\0"), value.count <= maximum else {
        throw SelectionFailure.invalidIdentity
    }
    return value
}

func selectedRange(_ element: AXUIElement) throws -> CFRange {
    let value = try copyValue(element, kAXSelectedTextRangeAttribute as CFString)
    guard CFGetTypeID(value) == AXValueGetTypeID() else {
        throw SelectionFailure.noSelection
    }
    let axValue = unsafeBitCast(value, to: AXValue.self)
    guard AXValueGetType(axValue) == .cfRange else {
        throw SelectionFailure.noSelection
    }
    var range = CFRange()
    guard AXValueGetValue(axValue, .cfRange, &range),
          range.location != kCFNotFound,
          range.location >= 0,
          range.length > 0
    else {
        throw SelectionFailure.noSelection
    }
    return range
}

func rejectMultipleSelection(_ element: AXUIElement) throws {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(
        element,
        kAXSelectedTextRangesAttribute as CFString,
        &value
    )
    if error == .attributeUnsupported || error == .noValue { return }
    guard error == .success, let ranges = value as? [AXValue] else {
        throw SelectionFailure.multipleSelection
    }
    guard ranges.count == 1 else { throw SelectionFailure.multipleSelection }
}

func isSecure(_ element: AXUIElement, within window: AXUIElement) throws -> Bool {
    var current = element
    for _ in 0 ..< 32 {
        let subrole = try copyString(current, kAXSubroleAttribute as CFString)
        if subrole == (kAXSecureTextFieldSubrole as String) { return true }
        if CFEqual(current, window) { return false }
        current = try copyElement(
            current,
            kAXParentAttribute as CFString,
            failure: .invalidIdentity
        )
    }
    throw SelectionFailure.invalidIdentity
}

func focusedIdentity() throws -> FocusedIdentity {
    guard AXIsProcessTrusted() else { throw SelectionFailure.accessibilityUntrusted }

    guard let running = NSWorkspace.shared.frontmostApplication else {
        throw SelectionFailure.noFocusedApplication
    }
    let pid = running.processIdentifier
    guard pid > 0,
          pid != ProcessInfo.processInfo.processIdentifier,
          !running.isTerminated
    else {
        throw pid == ProcessInfo.processInfo.processIdentifier
            ? SelectionFailure.selfSelection
            : SelectionFailure.noFocusedApplication
    }
    let appElement = AXUIElementCreateApplication(pid)

    let window = try copyElement(
        appElement,
        kAXFocusedWindowAttribute as CFString,
        failure: .noFocusedWindow
    )
    let windowTitle = try checkedIdentity(
        try copyString(window, kAXTitleAttribute as CFString),
        maximum: maxIdentityCharacters
    )
    let appName = try checkedIdentity(
        running.localizedName ?? "",
        maximum: maxIdentityCharacters
    )
    let bundleID = try checkedIdentity(
        running.bundleIdentifier ?? "",
        maximum: maxIdentityCharacters
    )
    guard !bundleID.isEmpty else { throw SelectionFailure.invalidIdentity }
    return FocusedIdentity(
        appElement: appElement,
        window: window,
        pid: pid,
        appName: appName,
        bundleID: bundleID,
        windowTitle: windowTitle
    )
}

func windowSnapshot() throws -> [String: Any] {
    let before = try focusedIdentity()
    let after = try focusedIdentity()
    guard before.pid == after.pid,
          before.appName == after.appName,
          before.bundleID == after.bundleID,
          before.windowTitle == after.windowTitle,
          CFEqual(before.window, after.window)
    else {
        throw SelectionFailure.focusChanged
    }
    return [
        "schema_version": 1,
        "app_name": before.appName,
        "bundle_id": before.bundleID,
        "pid": Int(before.pid),
        "window_title": before.windowTitle,
    ]
}

func snapshot() throws -> [String: Any] {
    let identity = try focusedIdentity()
    let element = try copyElement(
        identity.appElement,
        kAXFocusedUIElementAttribute as CFString,
        failure: .noFocusedElement
    )
    let elementWindow = try copyElement(
        element,
        kAXWindowAttribute as CFString,
        failure: .noFocusedWindow
    )
    guard CFEqual(identity.window, elementWindow) else { throw SelectionFailure.focusChanged }
    let focusedValue = try copyValue(element, kAXFocusedAttribute as CFString)
    guard let focused = focusedValue as? Bool, focused else {
        throw SelectionFailure.focusChanged
    }
    guard try !isSecure(element, within: identity.window) else {
        throw SelectionFailure.secureField
    }
    try rejectMultipleSelection(element)

    let rangeBefore = try selectedRange(element)
    let selectedBefore = try copyString(
        element,
        kAXSelectedTextAttribute as CFString,
        required: true
    )
    guard !selectedBefore.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        throw SelectionFailure.noSelection
    }
    guard selectedBefore.count <= maxSelectionCharacters else {
        throw SelectionFailure.selectionTooLarge
    }

    let role = try checkedIdentity(
        try copyString(element, kAXRoleAttribute as CFString, required: true),
        maximum: maxRoleCharacters
    )
    let subrole = try checkedIdentity(
        try copyString(element, kAXSubroleAttribute as CFString),
        maximum: maxRoleCharacters
    )

    let finalIdentity = try focusedIdentity()
    let finalElement = try copyElement(
        finalIdentity.appElement,
        kAXFocusedUIElementAttribute as CFString,
        failure: .focusChanged
    )
    let rangeAfter = try selectedRange(finalElement)
    let selectedAfter = try copyString(
        finalElement,
        kAXSelectedTextAttribute as CFString,
        required: true
    )
    guard identity.pid == finalIdentity.pid,
          identity.appName == finalIdentity.appName,
          identity.bundleID == finalIdentity.bundleID,
          identity.windowTitle == finalIdentity.windowTitle,
          CFEqual(identity.window, finalIdentity.window),
          CFEqual(element, finalElement),
          rangeBefore.location == rangeAfter.location,
          rangeBefore.length == rangeAfter.length,
          selectedBefore == selectedAfter
    else {
        throw SelectionFailure.focusChanged
    }

    return [
        "schema_version": 1,
        "source_kind": "macos_selection",
        "selected_text": selectedBefore,
        "captured_at": ISO8601DateFormatter().string(from: Date()),
        "app_name": identity.appName,
        "bundle_id": identity.bundleID,
        "pid": Int(identity.pid),
        "window_title": identity.windowTitle,
        "element_role": role,
        "element_subrole": subrole,
        "selection_location": rangeBefore.location,
        "selection_length": rangeBefore.length,
    ]
}

func emit(_ value: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    guard data.count <= maxOutputBytes else { throw SelectionFailure.outputUnavailable }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

do {
    if CommandLine.arguments.count == 1 {
        try emit(["ok": true, "selection": try snapshot()])
    } else if CommandLine.arguments == [CommandLine.arguments[0], "--frontmost-window-metadata"] {
        try emit(["ok": true, "window": try windowSnapshot()])
    } else {
        throw SelectionFailure.invalidIdentity
    }
} catch let failure as SelectionFailure {
    try? emit(["ok": false, "error_code": failure.rawValue])
    exit(2)
} catch {
    try? emit(["ok": false, "error_code": SelectionFailure.outputUnavailable.rawValue])
    exit(1)
}
