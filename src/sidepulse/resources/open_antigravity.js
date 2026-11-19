// Run by macOS osascript. Use the app's own conversation links, which carry
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

function run(argv) {
    const path = "/c/" + argv[0];
    if (!$.AXIsProcessTrustedWithOptions($({AXTrustedCheckOptionPrompt: true}))) {
        return "Allow the SidePulse status bar launcher under Privacy & Security → Accessibility, then click the session again.";
    }
    const deadline = Date.now() + 8000;
    let pressed = false;
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
                $.AXUIElementPerformAction(window, $("AXRaise"));
                process.activateWithOptions($.NSApplicationActivateIgnoringOtherApps);
                if (match.selected) return "";
                if (!pressed) {
                    $.AXUIElementPerformAction(match.link, $("AXPress"));
                    pressed = true;
                }
            }
            if (!pressed && !revealed && hiddenSidebar) {
                $.AXUIElementPerformAction(hiddenSidebar, $("AXPress"));
                revealed = true;
            }
        }
        delay(0.2);
    }
    return "Antigravity could not select this conversation. Check that it is visible in the sidebar.";
}
