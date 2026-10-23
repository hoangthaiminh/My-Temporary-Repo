"""Read-only UI Automation probe, isolated so a slow provider can be terminated."""
from __future__ import annotations

import argparse
import ctypes as C
import json
import sys


CONTROL_TYPES = dict(enumerate((
    "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image", "ListItem", "List",
    "Menu", "MenuBar", "MenuItem", "ProgressBar", "RadioButton", "ScrollBar", "Slider", "Spinner",
    "StatusBar", "Tab", "TabItem", "Text", "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom", "Group",
    "Thumb", "DataGrid", "DataItem", "Document", "SplitButton", "Window", "Pane", "Header", "HeaderItem",
    "Table", "TitleBar", "Separator", "SemanticZoom", "AppBar"), 50000))

PROPERTIES = {
    "runtime_id": 30000, "bounding_rectangle_physical_pixels": 30001, "process_id": 30002,
    "control_type_id": 30003, "localized_control_type": 30004, "name": 30005,
    "accelerator_key": 30006, "access_key": 30007, "has_keyboard_focus": 30008,
    "keyboard_focusable": 30009, "enabled": 30010, "automation_id": 30011, "class_name": 30012,
    "help_text": 30013, "culture": 30015, "is_control_element": 30016, "is_content_element": 30017,
    "is_password": 30019, "native_window_handle": 30020, "item_type": 30021, "offscreen": 30022,
    "orientation": 30023, "framework_id": 30024, "required_for_form": 30025, "item_status": 30026,
    "invoke_pattern_available": 30031, "selection_item_pattern_available": 30036,
    "text_pattern_available": 30040, "toggle_pattern_available": 30041, "value_pattern_available": 30043,
}


def absent(reason, status="query_error", **extra):
    return {"available": False, "status": status, "reason": reason, **extra}


def failure(exc):
    hresult = getattr(exc, "hresult", None)
    code = hresult & 0xFFFFFFFF if isinstance(hresult, int) else None
    status = {0x80070005: "access_denied", 0x80040200: "query_unavailable", 0x80040204: "not_supported",
              0x80040201: "window_gone", 0x80131505: "timeout"}.get(code, "query_error")
    extra = {"HRESULT": f"0x{code:08X}"} if code is not None else {}
    return absent(f"{type(exc).__name__}: {exc}", status, **extra)


def value_property(automation, element, property_id):
    try:
        value = element.GetCurrentPropertyValueEx(property_id, True)
        if hasattr(value, "QueryInterface"):
            if automation.CheckNotSupported(value):
                return absent("The accessibility provider does not support this property", "not_supported")
            return absent("This property returns a COM object rather than a scalar value", "not_supported")
        if isinstance(value, tuple):
            value = list(value)
        if value is None or isinstance(value, (str, int, float, bool, list)):
            return value
        return absent("The accessibility provider returned a non-scalar property value", "not_supported")
    except Exception as exc:
        return failure(exc)


def describe(automation, element, interfaces):
    if not element:
        return absent("No accessibility element was returned", "query_unavailable")
    result = {name: value_property(automation, element, identifier) for name, identifier in PROPERTIES.items()}
    kind = result["control_type_id"]
    result["control_type"] = CONTROL_TYPES.get(kind, "Unknown") if isinstance(kind, int) else kind
    # Providers do not consistently reject value/text queries for password controls.
    if result["is_password"] is True:
        result["value"] = absent("Value/text are not queried for password controls", "not_collected")
        result["document_text"] = result["value"]
    else:
        result["value"] = (value_property(automation, element, 30045)
                           if result["value_pattern_available"] is True else
                           absent("Value pattern is not available", "not_supported"))
        result["value_read_only"] = (value_property(automation, element, 30046)
                                     if result["value_pattern_available"] is True else
                                     absent("Value pattern is not available", "not_supported"))
        if result["text_pattern_available"] is True:
            try:
                pattern = element.GetCurrentPattern(10014).QueryInterface(interfaces.IUIAutomationTextPattern)
                result["document_text"] = pattern.DocumentRange.GetText(-1)
            except Exception as exc:
                result["document_text"] = failure(exc)
        else:
            result["document_text"] = absent("Text pattern is not available", "not_supported")
    result["toggle_state"] = (value_property(automation, element, 30086)
                              if result["toggle_pattern_available"] is True else
                              absent("Toggle pattern is not available", "not_supported"))
    result["toggle_state_name"] = {0: "Off", 1: "On", 2: "Indeterminate"}.get(
        result["toggle_state"], "Unavailable") if isinstance(result["toggle_state"], int) else "Unavailable"
    return result


def belongs_to(automation, element, root):
    walker = automation.RawViewWalker
    for _ in range(64):
        if not element:
            return False
        if automation.CompareElements(element, root):
            return True
        element = walker.GetParentElement(element)
    return False


def probe(hwnd, pid, point=None, expected_created=None):
    if sys.platform != "win32":
        return absent("UI Automation requires Windows", "not_supported")
    # Initialize COM as MTA before comtypes performs its automatic initialization.
    sys.coinit_flags = 0
    try:
        import psutil
        import comtypes.client
        interfaces = comtypes.client.GetModule("UIAutomationCore.dll")
    except ImportError as exc:
        return absent(f"UI Automation dependency is missing: {exc.name}. Install requirements.txt.", "not_supported")
    except Exception as exc:
        return failure(exc)
    try:
        process = psutil.Process(pid)
        if expected_created is not None and process.create_time() != expected_created:
            return absent("PID was reused before UI Automation started", "identity_changed")
        cls = getattr(interfaces, "CUIAutomation8", interfaces.CUIAutomation)
        automation = comtypes.client.CreateObject(cls, interface=interfaces.IUIAutomation)
        if hasattr(interfaces, "IUIAutomation2"):
            options = automation.QueryInterface(interfaces.IUIAutomation2)
            options.ConnectionTimeout = 750
            options.TransactionTimeout = 1000
        root = automation.ElementFromHandle(hwnd)
        root_pid = value_property(automation, root, 30002)
        if root_pid != pid:
            return absent("The selected HWND no longer belongs to the captured process", "identity_changed")
        result = {"window_element": describe(automation, root, interfaces),
                  "scope": "Selected window, element at the saved pointer position, and focused element within that window. No full-tree crawl.",
                  "read_only": True}
        if point is not None:
            native_point = interfaces.tagPOINT(*point)
            element = automation.ElementFromPoint(native_point)
            result["element_at_capture_point"] = (describe(automation, element, interfaces)
                if belongs_to(automation, element, root) else
                absent("The point currently resolves to an element outside the selected window", "not_applicable"))
        else:
            result["element_at_capture_point"] = absent("No saved pointer position was supplied", "not_collected")
        element = automation.GetFocusedElement()
        result["focused_element"] = (describe(automation, element, interfaces)
            if belongs_to(automation, element, root) else
            absent("Keyboard focus belongs to another window", "not_applicable"))
        return result
    except Exception as exc:
        return failure(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hwnd", type=int, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--created", type=float)
    args = parser.parse_args()
    point = (args.x, args.y) if args.x is not None and args.y is not None else None
    # ASCII JSON keeps the child protocol independent of Windows console encoding.
    print(json.dumps(probe(args.hwnd, args.pid, point, args.created), ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
