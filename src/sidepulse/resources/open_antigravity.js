ObjC.import("AppKit");
ObjC.import("ApplicationServices");
// Pass NSString's UTF-16 buffer as a pointer. JXA's inferred UniChar binding
// otherwise drops the text and sends the physical key printed on the keyboard.
ObjC.bindFunction("CGEventKeyboardSetUnicodeString", ["void", ["void *", "unsigned long", "void *"]]);

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

function textAttr(element, name) {
    return String(ObjC.unwrap(attribute(element, name)) || "");
}

function findElement(root, predicate) {
    const stack = root ? [root] : [];
    while (stack.length) {
        const element = stack.pop();
        if (predicate(element, textAttr(element, "AXRole"))) return element;
        stack.push(...children(element, "AXChildren").reverse());
    }
    return null;
}

function chatPath(element) {
    const url = attribute(element, "AXURL");
    return url && ObjC.unwrap(url.host) === "127.0.0.1" ? ObjC.unwrap(url.path) : null;
}

function waitFor(read, milliseconds) {
    const end = Date.now() + milliseconds;
    do {
        const value = read();
        if (value) return value;
        delay(0.08);
    } while (Date.now() < end);
    return null;
}

function key(pid, code, flags, text) {
    for (const down of [true, false]) {
        const event = $.CGEventCreateKeyboardEvent(null, code, down);
        $.CGEventSetFlags(event, flags);
        if (text) {
            const data = $(text).dataUsingEncoding($.NSUTF16LittleEndianStringEncoding);
            $.CGEventKeyboardSetUnicodeString(event, text.length, data.bytes);
        }
        $.CGEventPostToPid(pid, event);
    }
}

function searchConversation(process, sessionId, title, duplicate) {
    const pid = process.processIdentifier;
    const app = $.AXUIElementCreateApplication(pid);
    $.AXUIElementSetAttributeValue(app, $("AXManualAccessibility"), $.kCFBooleanTrue);
    process.activateWithOptions($.NSApplicationActivateIgnoringOtherApps);
    const window = waitFor(() => {
        const focused = process.active && attribute(app, "AXFocusedWindow");
        return focused && findElement(focused, (el, role) => role === "AXWebArea" && chatPath(el)) && focused;
    }, 2500);
    if (!window) return "Antigravity did not bring its chat window forward.";
    const path = "/c/" + sessionId;
    const isCurrent = () => findElement(window, (el, role) => role === "AXWebArea" && chatPath(el) === path);
    if (isCurrent()) return "";
    if (!title) {
        const link = findElement(window, (el, role) => role === "AXLink" && chatPath(el) === path);
        title = link && textAttr(link, "AXDescription");
    }
    const searchBox = () => findElement(window, (el, role) => role === "AXComboBox" &&
        /^Search for conversations/i.test(textAttr(el, "AXPlaceholderValue") || textAttr(el, "AXDescription")));
    let box = searchBox();
    if (!box) {
        key(pid, 40, $.kCGEventFlagMaskCommand); // ⌘K
        box = waitFor(searchBox, 2500);
    }
    if (!box) return "Antigravity’s conversation search did not open.";
    if (!title) return "SidePulse could not find the chat title. Choose the chat in the open search.";
    // The search field can expose its highlighted result as the focused AX element.
    const focusedSearch = () => {
        if (!process.active) return null;
        const activeWindow = attribute(app, "AXFocusedWindow");
        if (!activeWindow || !$.CFEqual(window, activeWindow)) return null;
        const field = searchBox();
        const focus = attribute(app, "AXFocusedUIElement");
        if (!field || !focus) return null;
        if ($.CFEqual(field, focus) || ObjC.unwrap(attribute(focus, "AXSelected"))) return field;
        return null;
    };
    if (!focusedSearch()) return "Conversation search lost focus.";
    if (textAttr(box, "AXValue")) key(pid, 0, $.kCGEventFlagMaskCommand); // ⌘A
    key(pid, 0, 0, title);
    const ready = waitFor(() => {
        const field = focusedSearch();
        if (!field || textAttr(field, "AXValue") !== title) return false;
        const row = attribute(app, "AXFocusedUIElement");
        if (!row || !ObjC.unwrap(attribute(row, "AXSelected"))) return false;
        const label = textAttr(row, "AXValue");
        return label === title || label.startsWith(title + " ");
    }, 3500);
    if (!ready || duplicate) return "Choose the matching chat in Antigravity’s open search.";
    key(pid, 36, 0); // Return
    return waitFor(isCurrent, 3500) ? "" : "Antigravity did not open the selected chat.";
}

function openChat(action, sessionId, title, duplicate) {
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
    const process = waitFor(() => $.NSRunningApplication
        .runningApplicationsWithBundleIdentifier("com.google.antigravity").firstObject, 2500);
    return process ? searchConversation(process, sessionId, title, duplicate) : "Antigravity did not open.";
}


function run() {
    const argv = ObjC.deepUnwrap($.NSProcessInfo.processInfo.arguments);
    const result = openChat(argv[1], argv[2], argv[3], argv[4] === "duplicate");
    const data = $(JSON.stringify(result)).dataUsingEncoding($.NSUTF8StringEncoding);
    $.NSFileHandle.fileHandleWithStandardOutput.writeData(data);
}
