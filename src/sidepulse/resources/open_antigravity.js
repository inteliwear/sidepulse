// Bundled as SidePulse Chat Opener. Use the app's own conversation links, which carry
// the conversation ID even when a title changes or two titles are identical.
ObjC.import("AppKit");
ObjC.import("ApplicationServices");

function attribute(element, name) {
    const result = Ref();
    if ($.AXUIElementCopyAttributeValue(element, $(name), result) !== 0) return null;
    return ObjC.castRefToObject(result[0]);
}

function children(element, name) {
    const list = attribute(element, name);
    const result = [];
    if (list) {
        for (let i = 0; i < list.count; i++) result.push(list.objectAtIndex(i));
    }
    return result;
}

function conversationInWindow(window, path) {
    const stack = [{element: window, port: null, sidebar: false}];
    let sidebarVisible = false;
    let toggle = null;
    while (stack.length) {
        const item = stack.pop();
        const element = item.element;
        const role = ObjC.unwrap(attribute(element, "AXRole"));
        let port = item.port;
        let sidebar = item.sidebar;
        if (role === "AXWebArea" || role === "AXLink") {
            const url = attribute(element, "AXURL");
            const local = url && ObjC.unwrap(url.host) === "127.0.0.1";
            if (role === "AXWebArea") {
                if (port !== null || !local) continue;
                port = ObjC.unwrap(url.port);
                if (ObjC.unwrap(url.path) === path) return {selected: true};
            } else if (sidebar && local && port !== null && ObjC.unwrap(url.port) === port &&
                       ObjC.unwrap(url.path) === path) {
                return {link: element};
            }
        }
        if (port !== null && (role === "AXGroup" || role === "AXButton")) {
            const label = ObjC.unwrap(attribute(element, "AXDescription")) ||
                          ObjC.unwrap(attribute(element, "AXTitle"));
            if (role === "AXGroup" && label === "Sidebar") {
                sidebar = true;
                sidebarVisible = true;
            }
            if (role === "AXButton" && label === "Toggle Sidebar") toggle = element;
        }
        for (const child of children(element, "AXChildren").reverse()) {
            stack.push({element: child, port, sidebar});
        }
    }
    return {toggle: !sidebarVisible && toggle};
}

function openChat(action, sessionId) {
    if (!["check", "select", "request-and-select"].includes(action)) return "";
    if (action !== "check" && !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(sessionId)) {
        return "This session has no valid Antigravity conversation ID.";
    }
    const trusted = $.AXIsProcessTrusted();
    if (action === "check") return trusted ? "granted" : "missing";
    if (!trusted) {
        if (action === "request-and-select") {
            // Let the user choose this exact bundle, even on macOS versions that
            // attribute an automatic system prompt to the process that launched it.
            $.NSWorkspace.sharedWorkspace.activateFileViewerSelectingURLs([$.NSBundle.mainBundle.bundleURL]);
            const settings = $.NSURL.URLWithString("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility");
            $.NSWorkspace.sharedWorkspace.openURL(settings);
        }
        return "";
    }
    const path = "/c/" + sessionId;
    const deadline = Date.now() + 8000;
    let nextPress = 0;
    let targetWindow = null;
    let revealed = false;
    while (Date.now() < deadline) {
        const apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.google.antigravity");
        if (apps.count) {
            const process = apps.firstObject;
            const app = $.AXUIElementCreateApplication(process.processIdentifier);
            $.AXUIElementSetAttributeValue(app, $("AXManualAccessibility"), $.kCFBooleanTrue);
            let hiddenSidebar = null;
            for (const window of children(app, "AXWindows")) {
                const match = conversationInWindow(window, path);
                if (!match.selected && !match.link) {
                    if (match.toggle) hiddenSidebar = match.toggle;
                    continue;
                }
                if (!targetWindow || !$.CFEqual(targetWindow, window) || !process.active) {
                    process.activateWithOptions($.NSApplicationActivateIgnoringOtherApps);
                    $.AXUIElementPerformAction(window, $("AXRaise"));
                    targetWindow = window;
                    nextPress = Date.now() + 350;
                    break;
                }
                if (match.selected) return "";
                // Electron can acknowledge AXPress before the window is ready.
                // Confirm the destination on the next pass and retry if necessary.
                if (Date.now() >= nextPress) {
                    $.AXUIElementPerformAction(match.link, $("AXScrollToVisible"));
                    $.AXUIElementPerformAction(match.link, $("AXPress"));
                    nextPress = Date.now() + 1000;
                }
                break;
            }
            if (!targetWindow && !revealed && hiddenSidebar) {
                $.AXUIElementPerformAction(hiddenSidebar, $("AXPress"));
                revealed = true;
            }
        }
        delay(0.2);
    }
    return "Antigravity could not select this conversation. Press ⌘K in Antigravity to find the chat.";
}


function run() {
    const argv = ObjC.deepUnwrap($.NSProcessInfo.processInfo.arguments);
    const result = openChat(argv[1], argv[2]);
    const data = $(JSON.stringify(result)).dataUsingEncoding($.NSUTF8StringEncoding);
    $.NSFileHandle.fileHandleWithStandardOutput.writeData(data);
}
