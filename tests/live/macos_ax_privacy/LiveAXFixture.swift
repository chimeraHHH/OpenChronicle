// OpenChronicle live macOS AX/privacy audit fixture.
//
// The runner passes random canaries through the environment.  This process
// never prints those values: stdout contains only fixed command/event names
// and counts so it is safe to retain if a caller redirects it accidentally.

import AppKit
import Foundation

private let publicTitle = "OpenChronicle AX Audit — Public"
private let bundleIdentifier = "app.openchronicle.LiveAXFixture"
private let publicVisualCanary = NSColor(
    srgbRed: 0.08, green: 0.76, blue: 0.30, alpha: 1.0
)
private let privateVisualCanary = NSColor(
    srgbRed: 0.88, green: 0.08, blue: 0.70, alpha: 1.0
)

private enum FixtureError: Error {
    case missingEnvironment
}

private final class FixtureDelegate: NSObject, NSApplicationDelegate {
    private var publicWindow: NSWindow!
    private var privateWindow: NSWindow!
    private var normalField: NSTextField!
    private var publicURLField: NSTextField!
    private var secureField: NSSecureTextField!
    private var privateURLField: NSTextField!
    private var privateTitle = ""
    private var publicURLValue = ""
    private var privateURLValue = ""
    private var rapidGeneration = 0
    private var fixtureValues: [String: String] = [:]
    private var commandFile: URL?
    private var eventFile: URL?
    private var commandOffset: UInt64 = 0
    private var commandRemainder = ""
    private var commandTimer: Timer?

    func applicationDidFinishLaunching(_: Notification) {
        do {
            try loadFixtureValues()
            try buildWindows()
        } catch {
            emit(event: "configuration_error")
            NSApplication.shared.terminate(nil)
            return
        }

        NSApplication.shared.setActivationPolicy(.regular)
        publicWindow.orderFront(nil)
        privateWindow.orderFront(nil)
        focusPublicNormal()
        startCommandTransport()
        emit(event: "ready", extra: ["window_count": 2, "protocol_version": 1])
    }

    func applicationShouldTerminateAfterLastWindowClosed(_: NSApplication) -> Bool {
        false
    }

    private func requiredEnvironment(_ name: String) throws -> String {
        guard let value = fixtureValues[name], !value.isEmpty else {
            throw FixtureError.missingEnvironment
        }
        return value
    }

    private func loadFixtureValues() throws {
        let environment = ProcessInfo.processInfo.environment
        if let markerPath = environment["OC_LIVE_AX_MARKER_FILE"], !markerPath.isEmpty {
            let data = try Data(contentsOf: URL(fileURLWithPath: markerPath))
            guard let values = try JSONSerialization.jsonObject(with: data) as? [String: String]
            else { throw FixtureError.missingEnvironment }
            fixtureValues = values
            return
        }
        fixtureValues = environment
    }

    private func buildWindows() throws {
        let normal = try requiredEnvironment("OC_LIVE_AX_NORMAL")
        let publicURL = try requiredEnvironment("OC_LIVE_AX_PUBLIC_URL")
        let secure = try requiredEnvironment("OC_LIVE_AX_SECURE")
        let excludedTitle = try requiredEnvironment("OC_LIVE_AX_EXCLUDED_TITLE")
        let privateURL = try requiredEnvironment("OC_LIVE_AX_PRIVATE_URL")
        publicURLValue = publicURL
        privateURLValue = privateURL
        privateTitle = "OpenChronicle AX Audit — Private \(excludedTitle)"

        publicWindow = makeWindow(
            title: publicTitle,
            origin: NSPoint(x: 180, y: 520),
            identifier: "oc-live-public-window",
            backgroundColor: publicVisualCanary
        )
        let publicStack = makeStack(in: publicWindow)
        publicStack.addArrangedSubview(label("Normal text field"))
        normalField = textField(normal, identifier: "oc-live-normal-field")
        publicStack.addArrangedSubview(normalField)
        publicStack.addArrangedSubview(label("URL-like text field"))
        publicURLField = textField(publicURL, identifier: "oc-live-public-url-field")
        publicStack.addArrangedSubview(publicURLField)
        publicStack.addArrangedSubview(colorSwatch(publicVisualCanary))

        privateWindow = makeWindow(
            title: privateTitle,
            origin: NSPoint(x: 760, y: 520),
            identifier: "oc-live-private-window",
            backgroundColor: privateVisualCanary
        )
        let privateStack = makeStack(in: privateWindow)
        privateStack.addArrangedSubview(label("Secure text field"))
        secureField = NSSecureTextField(string: secure)
        configureField(secureField, identifier: "oc-live-secure-field")
        privateStack.addArrangedSubview(secureField)
        privateStack.addArrangedSubview(label("Excluded URL-like text field"))
        privateURLField = textField(privateURL, identifier: "oc-live-private-url-field")
        privateStack.addArrangedSubview(privateURLField)
        privateStack.addArrangedSubview(colorSwatch(privateVisualCanary))
    }

    private func makeWindow(
        title: String,
        origin: NSPoint,
        identifier: String,
        backgroundColor: NSColor
    ) -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(origin: origin, size: NSSize(width: 520, height: 260)),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = title
        window.identifier = NSUserInterfaceItemIdentifier(identifier)
        window.isReleasedWhenClosed = false
        window.collectionBehavior = [.canJoinAllSpaces]
        window.contentView?.wantsLayer = true
        window.contentView?.layer?.backgroundColor = backgroundColor.cgColor
        return window
    }

    private func makeStack(in window: NSWindow) -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        guard let content = window.contentView else { return stack }
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
        ])
        return stack
    }

    private func label(_ value: String) -> NSTextField {
        let field = NSTextField(labelWithString: value)
        field.setAccessibilityIdentifier("oc-live-label")
        return field
    }

    private func textField(_ value: String, identifier: String) -> NSTextField {
        let field = NSTextField(string: value)
        configureField(field, identifier: identifier)
        return field
    }

    private func configureField(_ field: NSTextField, identifier: String) {
        field.identifier = NSUserInterfaceItemIdentifier(identifier)
        field.setAccessibilityIdentifier(identifier)
        field.widthAnchor.constraint(equalToConstant: 455).isActive = true
    }

    private func colorSwatch(_ color: NSColor) -> NSView {
        let view = NSView()
        view.wantsLayer = true
        view.layer?.backgroundColor = color.cgColor
        view.setAccessibilityElement(false)
        view.widthAnchor.constraint(equalToConstant: 455).isActive = true
        view.heightAnchor.constraint(equalToConstant: 72).isActive = true
        return view
    }

    private func activate(_ window: NSWindow, responder: NSResponder) {
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(responder)
    }

    private func focusPublicNormal() {
        activate(publicWindow, responder: normalField)
    }

    private func focusPublicURL() {
        publicURLField.stringValue = publicURLValue
        activate(publicWindow, responder: publicURLField)
    }

    private func focusPublicForbiddenURL() {
        publicURLField.stringValue = privateURLValue
        activate(publicWindow, responder: publicURLField)
    }

    private func focusPrivateSecure() {
        activate(privateWindow, responder: secureField)
    }

    private func focusPrivateURL() {
        activate(privateWindow, responder: privateURLField)
    }

    private func startCommandTransport() {
        let environment = ProcessInfo.processInfo.environment
        if let commandPath = environment["OC_LIVE_AX_COMMAND_FILE"],
           let eventPath = environment["OC_LIVE_AX_EVENT_FILE"]
        {
            commandFile = URL(fileURLWithPath: commandPath)
            eventFile = URL(fileURLWithPath: eventPath)
            commandTimer = Timer.scheduledTimer(
                withTimeInterval: 0.04,
                repeats: true
            ) { [weak self] _ in
                self?.pollCommandFile()
            }
            return
        }
        startStdinReader()
    }

    private func startStdinReader() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while let line = readLine() {
                let command = line.trimmingCharacters(in: .whitespacesAndNewlines)
                DispatchQueue.main.async {
                    self?.handle(command)
                }
            }
        }
    }

    private func pollCommandFile() {
        guard let commandFile,
              let fileHandle = try? FileHandle(forReadingFrom: commandFile)
        else { return }
        defer { try? fileHandle.close() }
        do {
            try fileHandle.seek(toOffset: commandOffset)
            let data = fileHandle.readDataToEndOfFile()
            commandOffset += UInt64(data.count)
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            commandRemainder += text
            let parts = commandRemainder.split(separator: "\n", omittingEmptySubsequences: false)
            commandRemainder = String(parts.last ?? "")
            for part in parts.dropLast() {
                handle(String(part).trimmingCharacters(in: .whitespacesAndNewlines))
            }
        } catch {
            return
        }
    }

    private func handle(_ command: String) {
        switch command {
        case "public.normal":
            focusPublicNormal()
            emit(event: "ack", extra: ["command": command])
        case "public.url":
            focusPublicURL()
            emit(event: "ack", extra: ["command": command])
        case "public.forbidden-url":
            focusPublicForbiddenURL()
            emit(event: "ack", extra: ["command": command])
        case "private.secure":
            focusPrivateSecure()
            emit(event: "ack", extra: ["command": command])
        case "private.url":
            focusPrivateURL()
            emit(event: "ack", extra: ["command": command])
        case "rapid":
            startRapidSwitches()
        case "state":
            emit(event: "state", extra: ["window_count": 2])
        case "quit":
            emit(event: "bye")
            NSApplication.shared.terminate(nil)
        default:
            emit(event: "unknown_command")
        }
    }

    private func startRapidSwitches() {
        rapidGeneration += 1
        let generation = rapidGeneration
        let switchCount = 48
        emit(event: "rapid_started", extra: ["switch_count": switchCount])
        for index in 0 ..< switchCount {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.04 * Double(index)) { [weak self] in
                guard let self, self.rapidGeneration == generation else { return }
                switch index % 4 {
                case 0: self.focusPublicNormal()
                case 1: self.focusPrivateSecure()
                case 2: self.focusPublicURL()
                default: self.focusPrivateURL()
                }
                if index == switchCount - 1 {
                    self.emit(
                        event: "rapid_finished",
                        extra: ["switch_count": switchCount]
                    )
                }
            }
        }
    }

    private func emit(event: String, extra: [String: Any] = [:]) {
        var payload = extra
        payload["event"] = event
        payload["bundle_id"] = bundleIdentifier
        guard
            let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
            let line = String(data: data, encoding: .utf8)
        else { return }
        if let eventFile,
           let handle = try? FileHandle(forWritingTo: eventFile),
           let data = (line + "\n").data(using: .utf8)
        {
            defer { try? handle.close() }
            do {
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
                try handle.synchronize()
            } catch {
                return
            }
        } else {
            print(line)
            fflush(stdout)
        }
    }
}

let app = NSApplication.shared
private let delegate = FixtureDelegate()
app.delegate = delegate
app.run()
