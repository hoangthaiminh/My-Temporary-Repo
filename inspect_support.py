"""Native mouse controls and comprehensive, read-only inspection reports."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import platform
import queue
import subprocess
import sys
import threading
from typing import Any


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


VERSION = "2.2"
_PROBE_LOCK = threading.Lock()
_PROBES = {}


def unavailable(reason, status="query_error", **details):
    return {"available": False, "status": status, "reason": str(reason), **details}


def exception_result(exc):
    name = type(exc).__name__
    code = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError) or name == "AccessDenied" or code == 5:
        status = "access_denied"
    elif name in ("NoSuchProcess", "ZombieProcess"):
        status = "process_exited"
    elif isinstance(exc, NotImplementedError):
        status = "not_supported"
    else:
        status = "query_error"
    details = {"exception_type": name}
    if code is not None:
        details["winerror"] = code
    return unavailable(f"{name}: {exc}", status=status, **details)


def read(getter):
    try:
        return json_value(getter())
    except Exception as exc:
        return exception_result(exc)


def win32_failure(reason, code, **details):
    status = {5: "access_denied", 50: "not_supported", 120: "not_supported",
              1400: "window_gone"}.get(code, "query_error")
    return unavailable(reason, status=status, winerror=code, **details)


def windows_version():
    if not hasattr(sys, "getwindowsversion"):
        return unavailable("Windows version API is unavailable on this platform", status="not_supported")
    value = sys.getwindowsversion()
    fields = ("major", "minor", "build", "platform", "service_pack", "service_pack_major",
              "service_pack_minor", "suite_mask", "product_type", "platform_version")
    return {name: json_value(getattr(value, name)) for name in fields if hasattr(value, name)}


def windows_build():
    version = windows_version()
    actual = version.get("platform_version")
    return actual[2] if actual and len(actual) >= 3 else version.get("build")


def runtime_context(psutil):
    return {"system": platform.system(), "release": platform.release(), "version": platform.version(),
            "machine": platform.machine(), "python": sys.version,
            "inspector_pointer_bits": C.sizeof(C.c_void_p) * 8,
            "psutil_version": getattr(psutil, "__version__", "Unknown"),
            "windows": read(windows_version)}


def field_status_summary(report):
    counts = {}
    def visit(value):
        if isinstance(value, dict):
            if value.get("available") is False:
                status = value.get("status", "query_error")
                counts[status] = counts.get(status, 0) + 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(report)
    return {"unavailable_fields_by_status": counts,
            "note": "not_applicable and not_supported are informational; they do not mean the report failed."}


def json_value(value):
    if hasattr(value, "_asdict"):
        return {k: json_value(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [json_value(v) for v in value]
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class MouseGate:
    """Match swallowed button releases even if inspection is paused mid-click."""

    def __init__(self):
        self.enabled = True
        self.blocked = set()

    def handle(self, message):
        down = {0x0201: "freeze", 0x0204: "copy"}
        up = {0x0202: "freeze", 0x0205: "copy"}
        if message in down:
            action = down[message]
            if self.enabled:
                self.blocked.add(action)
                return True, action
        elif message in up:
            action = up[message]
            if action in self.blocked:
                self.blocked.remove(action)
                return True, None
        return False, None


class MouseHook:
    """A dedicated message-pump thread keeps the low-level hook responsive."""

    def __init__(self, actions):
        self.actions = actions
        self.gate = MouseGate()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.thread_id = None
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.callback_type = C.WINFUNCTYPE(C.c_ssize_t, C.c_int, W.WPARAM, W.LPARAM)

        class MouseInfo(C.Structure):
            _fields_ = [("point", W.POINT), ("mouse_data", W.DWORD),
                        ("flags", W.DWORD), ("time", W.DWORD), ("extra", C.c_size_t)]

        self.info_type = MouseInfo
        self.user.SetWindowsHookExW.argtypes = [C.c_int, self.callback_type, W.HINSTANCE, W.DWORD]
        self.user.SetWindowsHookExW.restype = W.HANDLE
        self.user.CallNextHookEx.argtypes = [W.HANDLE, C.c_int, W.WPARAM, W.LPARAM]
        self.user.CallNextHookEx.restype = C.c_ssize_t
        self.user.UnhookWindowsHookEx.argtypes = [W.HANDLE]
        self.user.UnhookWindowsHookEx.restype = W.BOOL
        self.user.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        self.user.GetMessageW.restype = C.c_int
        self.user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        self.user.PeekMessageW.restype = W.BOOL
        self.user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
        self.user.PostThreadMessageW.restype = W.BOOL
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = W.DWORD
        self.kernel.GetModuleHandleW.argtypes = [W.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = W.HMODULE
        self.thread = threading.Thread(target=self._run, name="InspectorMouseHook", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            self.stop()
            raise RuntimeError("Timed out while starting the mouse hook.")
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        hook = None
        try:
            self.thread_id = self.kernel.GetCurrentThreadId()

            @self.callback_type
            def callback(code, wparam, lparam):
                if code >= 0:
                    try:
                        swallowed, action = self.gate.handle(int(wparam))
                        if swallowed:
                            if action:
                                data = C.cast(lparam, C.POINTER(self.info_type)).contents
                                self.actions.put((action, (data.point.x, data.point.y)))
                            return 1
                    except Exception:
                        # Never let a Python exception cross a native hook boundary.
                        self.gate.enabled = False
                return self.user.CallNextHookEx(hook, code, wparam, lparam)

            self.callback = callback
            hook = self.user.SetWindowsHookExW(14, callback, self.kernel.GetModuleHandleW(None), 0)
            if not hook:
                raise OSError(C.get_last_error(), "Cannot install the mouse hook")
            message = W.MSG()
            # PeekMessage creates the thread queue before ready is signaled.
            self.user.PeekMessageW(C.byref(message), None, 0, 0, 0)
            self.ready.set()
            while not self.stopped.is_set():
                result = self.user.GetMessageW(C.byref(message), None, 0, 0)
                if result == -1:
                    raise OSError(C.get_last_error(), "Mouse hook message loop failed")
                if result == 0:
                    break
                # Low-level hook callbacks run while GetMessage dispatches sent messages.
        except Exception as exc:
            self.error = f"Mouse controls unavailable: {exc}"
            self.actions.put(("hook_error", self.error))
        finally:
            self.gate.enabled = False
            self.ready.set()
            if hook:
                self.user.UnhookWindowsHookEx(hook)

    def stop(self):
        self.gate.enabled = False
        self.stopped.set()
        if self.thread_id:
            self.user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        if self.thread.is_alive():
            self.thread.join(timeout=1)


@dataclass
class Capture:
    hwnd: int
    pid: int
    thread: int
    summary: dict[str, Any]
    started: str = field(default_factory=timestamp)
    expected_created: float | None = None
    report: str | None = None
    copy_requested: bool = False
    future: Any = None
    point: tuple[int, int] | None = None
    uia_timeout: float = 4.0
    uia_enabled: bool = True
    cancelled: Any = field(default_factory=threading.Event, repr=False)


class NativeDetails:
    def __init__(self, api):
        self.api = api
        # Keep independent ctypes declarations for extended native structures.
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.version = C.WinDLL("version", use_last_error=True)

        def bind(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(args)
            return fn

        self.client_rect = bind(self.user, "GetClientRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
        self.map_points = bind(self.user, "MapWindowPoints", C.c_int, W.HWND, W.HWND, C.c_void_p, W.UINT)
        self.parent = bind(self.user, "GetParent", W.HWND, W.HWND)
        self.zoomed = bind(self.user, "IsZoomed", W.BOOL, W.HWND)
        self.unicode = bind(self.user, "IsWindowUnicode", W.BOOL, W.HWND)
        self.menu = bind(self.user, "GetMenu", W.HMENU, W.HWND)
        self.help_id = bind(self.user, "GetWindowContextHelpId", W.DWORD, W.HWND)
        self.placement = bind(self.user, "GetWindowPlacement", W.BOOL, W.HWND, C.c_void_p)
        self.gui_info = bind(self.user, "GetGUIThreadInfo", W.BOOL, W.DWORD, C.c_void_p)
        self.enum_children = bind(self.user, "EnumChildWindows", W.BOOL, W.HWND, api.enum_type, W.LPARAM)
        self.props_type = C.WINFUNCTYPE(C.c_int, W.HWND, C.c_void_p, W.HANDLE, C.c_size_t)
        self.enum_props = bind(self.user, "EnumPropsExW", C.c_int, W.HWND, self.props_type, W.LPARAM)
        suffix = "PtrW" if C.sizeof(C.c_void_p) == 8 else "W"
        self.class_long = bind(self.user, "GetClassLong" + suffix, C.c_size_t, W.HWND, C.c_int)
        self.layer_attributes = bind(self.user, "GetLayeredWindowAttributes", W.BOOL,
                                     W.HWND, C.POINTER(W.DWORD), C.POINTER(C.c_ubyte), C.POINTER(W.DWORD))
        self.open_process = bind(self.kernel, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
        self.close_handle = bind(self.kernel, "CloseHandle", W.BOOL, W.HANDLE)
        self.priority = bind(self.kernel, "GetPriorityClass", W.DWORD, W.HANDLE)
        self.session = bind(self.kernel, "ProcessIdToSessionId", W.BOOL, W.DWORD, C.POINTER(W.DWORD))
        self.query_image = bind(self.kernel, "QueryFullProcessImageNameW", W.BOOL,
                                W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
        self.open_token = bind(self.advapi, "OpenProcessToken", W.BOOL,
                               W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
        self.token_info = bind(self.advapi, "GetTokenInformation", W.BOOL,
                               W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
        self.sid_count = bind(self.advapi, "GetSidSubAuthorityCount", C.POINTER(C.c_ubyte), C.c_void_p)
        self.sid_part = bind(self.advapi, "GetSidSubAuthority", C.POINTER(W.DWORD), C.c_void_p, W.DWORD)
        self.package = bind(self.kernel, "GetPackageFullName", W.LONG,
                            W.HANDLE, C.POINTER(W.UINT), W.LPWSTR)
        self.app_id = bind(self.kernel, "GetApplicationUserModelId", W.LONG,
                           W.HANDLE, C.POINTER(W.UINT), W.LPWSTR)
        self.event_type = C.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND,
                                        W.LONG, W.LONG, W.DWORD, W.DWORD)
        self.event_hook = bind(self.user, "SetWinEventHook", W.HANDLE,
                               W.DWORD, W.DWORD, W.HMODULE, self.event_type,
                               W.DWORD, W.DWORD, W.DWORD)
        self.unhook_event = bind(self.user, "UnhookWinEvent", W.BOOL, W.HANDLE)
        self.version_size = bind(self.version, "GetFileVersionInfoSizeW", W.DWORD,
                                 W.LPCWSTR, C.POINTER(W.DWORD))
        self.version_info = bind(self.version, "GetFileVersionInfoW", W.BOOL,
                                 W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p)
        self.query_version = bind(self.version, "VerQueryValueW", W.BOOL,
                                  C.c_void_p, W.LPCWSTR, C.POINTER(C.c_void_p), C.POINTER(W.UINT))
        self.window_monitor = bind(self.user, "MonitorFromWindow", W.HMONITOR, W.HWND, W.DWORD)
        self.monitor_details = bind(self.user, "GetMonitorInfoW", W.BOOL, W.HMONITOR, C.c_void_p)
        self.display_device = bind(self.user, "EnumDisplayDevicesW", W.BOOL, W.LPCWSTR, W.DWORD, C.c_void_p, W.DWORD)
        self.window_dpi = (bind(self.user, "GetDpiForWindow", W.UINT, W.HWND)
                           if hasattr(self.user, "GetDpiForWindow") else None)
        self.dpi_context = (bind(self.user, "GetWindowDpiAwarenessContext", W.HANDLE, W.HWND)
                            if hasattr(self.user, "GetWindowDpiAwarenessContext") else None)
        self.dpi_awareness = (bind(self.user, "GetAwarenessFromDpiAwarenessContext", C.c_int, W.HANDLE)
                              if hasattr(self.user, "GetAwarenessFromDpiAwarenessContext") else None)
        shcore = C.WinDLL("shcore", use_last_error=True)
        self.monitor_scale = bind(shcore, "GetScaleFactorForMonitor", W.LONG,
                                  W.HMONITOR, C.POINTER(C.c_int))
        self.gui_resources = bind(self.user, "GetGuiResources", W.DWORD, W.HANDLE, W.DWORD)
        self.process_information = (bind(self.kernel, "GetProcessInformation", W.BOOL,
                                         W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
                                    if hasattr(self.kernel, "GetProcessInformation") else None)
        self.window_long32 = bind(self.user, "GetWindowLongW", W.LONG, W.HWND, C.c_int)
        self.class_long32 = bind(self.user, "GetClassLongW", W.DWORD, W.HWND, C.c_int)
        psapi = C.WinDLL("psapi", use_last_error=True)
        self.enum_modules = bind(psapi, "EnumProcessModulesEx", W.BOOL,
                                 W.HANDLE, C.POINTER(W.HMODULE), W.DWORD, C.POINTER(W.DWORD), W.DWORD)
        self.module_path = bind(psapi, "GetModuleFileNameExW", W.DWORD,
                                W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD)
        self.module_info = bind(psapi, "GetModuleInformation", W.BOOL,
                                W.HANDLE, W.HMODULE, C.c_void_p, W.DWORD)

    @staticmethod
    def checked(ok):
        if not ok:
            raise OSError(C.get_last_error(), "Windows API call failed")

    @staticmethod
    def rect_value(rect):
        return [rect.left, rect.top, rect.right, rect.bottom]

    @staticmethod
    def long_value(getter, hwnd, index):
        C.set_last_error(0)
        value = getter(hwnd, index)
        error = C.get_last_error()
        if not value and error:
            raise OSError(error, "Window/class value query failed")
        return hex(value & ((1 << (C.sizeof(C.c_void_p) * 8)) - 1))

    def dwm_attributes(self, hwnd):
        build = read(windows_build)
        if not isinstance(build, int):
            build = None
        result = {}
        attributes = [(1, "nonclient_rendering_enabled", None), (14, "cloaked", None),
                      (15, "freeze_representation", None), (20, "immersive_dark_mode", None),
                      (33, "corner_preference", 22000), (34, "border_color", 22000),
                      (35, "caption_color", 22000), (36, "text_color", 22000),
                      (37, "visible_frame_border_thickness", 22000), (38, "system_backdrop_type", 22621)]
        for index, name, minimum in attributes:
            if minimum is not None and build is not None and build < minimum:
                result[name] = unavailable(
                    f"This DWM attribute requires Windows build {minimum} or later; detected build is {build}.",
                    status="not_supported", required_windows_build=minimum, detected_windows_build=build)
                continue
            value = W.DWORD()
            hresult = self.api.attr(hwnd, index, C.byref(value), C.sizeof(value)) & 0xFFFFFFFF
            if hresult == 0:
                result[name] = value.value
            elif hresult == 0x80070057:
                result[name] = unavailable(
                    "DWM returned E_INVALIDARG. The attribute may be unsupported for this Windows build/window; the exact cause is not determined.",
                    status="query_unavailable", HRESULT="0x80070057", error_name="E_INVALIDARG")
            else:
                status = {0x80070005: "access_denied", 0x80004001: "not_supported",
                          0x80263001: "not_applicable", 0x80070578: "window_gone"}.get(hresult, "query_error")
                result[name] = unavailable("DwmGetWindowAttribute failed", status=status,
                                           HRESULT=f"0x{hresult:08X}")
        return result

    def layered_window(self, hwnd):
        if not self.api.valid(hwnd):
            return unavailable("The selected window no longer exists", status="window_gone")
        extended = read(lambda: self.long_value(self.api.get_long, hwnd, -20))
        if not isinstance(extended, str):
            return extended
        if not int(extended, 16) & 0x80000:
            return unavailable(
                "The window does not have WS_EX_LAYERED, so GetLayeredWindowAttributes does not apply. This does not determine compositor transparency.",
                status="not_applicable", extended_style=extended)
        color, alpha, flags = W.DWORD(), C.c_ubyte(), W.DWORD()
        C.set_last_error(0)
        if self.layer_attributes(hwnd, C.byref(color), C.byref(alpha), C.byref(flags)):
            return {"color_key": color.value, "alpha": alpha.value, "flags": flags.value,
                    "color_key_applies": bool(flags.value & 1), "alpha_applies": bool(flags.value & 2)}
        error = C.get_last_error()
        if error == 87:
            return unavailable(
                "The window is layered, but GetLayeredWindowAttributes returned ERROR_INVALID_PARAMETER. Per-pixel layering or a window-state change may prevent this query; the exact cause is not determined.",
                status="query_unavailable", winerror=87, error_name="ERROR_INVALID_PARAMETER")
        return win32_failure("GetLayeredWindowAttributes failed", error)

    @staticmethod
    def identity(fn, handle, kind):
        missing = {
            15700: ("APPMODEL_ERROR_NO_PACKAGE", "The process has no MSIX/AppX package identity."),
            15703: ("APPMODEL_ERROR_NO_APPLICATION",
                    "GetApplicationUserModelId returned no application identity. Shell/window AppUserModelID properties are a separate mechanism."),
        }
        length = W.UINT()
        code = fn(handle, C.byref(length), None)
        for _ in range(3):
            if code != 122:
                break
            if not length.value:
                return unavailable("Identity query returned an invalid empty buffer length", winerror=code)
            text = C.create_unicode_buffer(length.value)
            code = fn(handle, C.byref(length), text)
            if code == 0:
                return text.value
        if code in missing:
            name, reason = missing[code]
            return unavailable(reason, status="not_applicable", winerror=code,
                               error_name=name, identity_kind=kind)
        if code == 0:
            return ""
        return win32_failure("Process identity query failed", code, identity_kind=kind)

    def monitor_and_dpi(self, hwnd):
        class MonitorInfoEx(C.Structure):
            _fields_ = [("size", W.DWORD), ("monitor", W.RECT), ("work", W.RECT),
                        ("flags", W.DWORD), ("device", W.WCHAR * 32)]

        class DisplayDevice(C.Structure):
            _fields_ = [("size", W.DWORD), ("name", W.WCHAR * 32), ("description", W.WCHAR * 128),
                        ("state_flags", W.DWORD), ("device_id", W.WCHAR * 128), ("device_key", W.WCHAR * 128)]

        monitor = self.window_monitor(hwnd, 2)
        self.checked(monitor)
        info = MonitorInfoEx()
        info.size = C.sizeof(info)
        self.checked(self.monitor_details(monitor, C.byref(info)))
        result = {"monitor_handle": hex(monitor), "device_name": info.device,
                  "monitor_rect_physical_pixels": self.rect_value(info.monitor),
                  "work_area_rect_physical_pixels": self.rect_value(info.work),
                  "primary": bool(info.flags & 1),
                  "selection": "Monitor with the largest window intersection; nearest monitor if off-screen."}
        device = DisplayDevice()
        device.size = C.sizeof(device)
        result["display_device"] = ({"name": device.name, "description": device.description,
            "state_flags": device.state_flags, "device_id": device.device_id} if
            self.display_device(info.device, 0, C.byref(device), 0) else
            unavailable("No monitor-device description was returned", status="query_unavailable"))
        factor = C.c_int()
        hresult = self.monitor_scale(monitor, C.byref(factor)) & 0xFFFFFFFF
        result["monitor_scale_percent"] = (factor.value if hresult == 0 else
            unavailable("GetScaleFactorForMonitor failed", HRESULT=f"0x{hresult:08X}"))
        if self.window_dpi:
            dpi = self.window_dpi(hwnd)
            result["window_dpi"] = dpi if dpi else win32_failure("GetDpiForWindow failed", C.get_last_error())
            if dpi:
                result["window_scale_percent"] = dpi * 100.0 / 96.0
                bounds = self.api.bounds(hwnd, False)
                if bounds:
                    result["window_size_at_96_dpi"] = [(bounds[2] - bounds[0]) * 96.0 / dpi,
                                                       (bounds[3] - bounds[1]) * 96.0 / dpi]
        else:
            result["window_dpi"] = unavailable("GetDpiForWindow requires Windows 10 version 1607+", status="not_supported")
        if self.dpi_context and self.dpi_awareness:
            context = self.dpi_context(hwnd)
            awareness = self.dpi_awareness(context) if context else -1
            result["window_dpi_awareness"] = {0: "Unaware", 1: "System aware", 2: "Per-monitor aware"}.get(
                awareness, unavailable("Window DPI-awareness query failed", status="query_unavailable"))
        else:
            result["window_dpi_awareness"] = unavailable("Window DPI-awareness APIs are unavailable", status="not_supported")
        result["DPI_note"] = ("GetDpiForWindow reflects the target's awareness: an unaware window may return 96 even on a scaled monitor. "
                              "Screen rectangles stay in physical pixels; only size is converted to 96-DPI units.")
        return result

    def extra_storage(self, hwnd):
        result = {}
        for name, size_index, getter in [("window", -18, self.window_long32), ("class", -20, self.class_long32)]:
            size = read(lambda: self.long_value(self.class_long, hwnd, size_index))
            if not isinstance(size, str):
                result[name] = size
                continue
            count = int(size, 16)
            if count > 65536:
                result[name] = unavailable("Declared extra storage exceeds the supported 65536-byte metadata bound",
                                           status="not_collected", declared_bytes=count)
                continue
            words = []
            for offset in range(0, count - 3, 4):
                def value(offset=offset):
                    C.set_last_error(0)
                    raw = getter(hwnd, offset)
                    if raw == 0 and C.get_last_error():
                        raise OSError(C.get_last_error(), "Extra-storage query failed")
                    return f"0x{raw & 0xFFFFFFFF:08X}"
                words.append({"byte_offset": offset, "DWORD": read(value)})
            result[name] = {"declared_bytes": count, "DWORD_values": words,
                            "trailing_bytes_not_read": count % 4,
                            "interpretation": "Raw DWORD metadata via documented window/class APIs; pointer values are not dereferenced."}
        return result

    def power_throttling(self, handle):
        if not self.process_information:
            return unavailable("GetProcessInformation is not supported", status="not_supported")
        class PowerState(C.Structure):
            _fields_ = [("version", W.DWORD), ("control_mask", W.DWORD), ("state_mask", W.DWORD)]
        value = PowerState(1, 0, 0)
        if not self.process_information(handle, 4, C.byref(value), C.sizeof(value)):
            error = C.get_last_error()
            if error == 87:
                return unavailable("ProcessPowerThrottling is not accepted on this Windows version/process",
                                   status="query_unavailable", winerror=error)
            return win32_failure("GetProcessInformation(ProcessPowerThrottling) failed", error)
        controlled = bool(value.control_mask & 1)
        return {"version": value.version, "control_mask": value.control_mask, "state_mask": value.state_mask,
                "execution_speed_controlled": controlled,
                "execution_speed_throttling_enabled": bool(value.state_mask & 1) if controlled else None,
                "execution_speed_policy": ("EcoQoS requested" if value.state_mask & 1 else "Explicitly disabled")
                                          if controlled else "System-managed / not determined by this mask",
                "ignore_timer_resolution_controlled": bool(value.control_mask & 4),
                "ignore_timer_resolution_enabled": bool(value.state_mask & 4) if value.control_mask & 4 else None,
                "scope": "Configured power-throttling policy, not measurement of effective CPU speed or all OS scheduling decisions."}

    def resource_counts(self, pid):
        handle = self.open_process(0x400, False, pid)
        if not handle:
            return win32_failure("OpenProcess(PROCESS_QUERY_INFORMATION) failed for GUI resources", C.get_last_error())
        try:
            result = {}
            for name, flag in [("GDI", 0), ("USER", 1), ("GDI_peak", 2), ("USER_peak", 4)]:
                C.set_last_error(0)
                count = self.gui_resources(handle, flag)
                error = C.get_last_error()
                result[name] = win32_failure("GetGuiResources failed", error) if count == 0 and error else count
            result["scope"] = "Current and peak process counts; a single sample does not prove a resource leak."
            return result
        finally:
            self.close_handle(handle)

    def loaded_modules(self, pid):
        handle = self.open_process(0x410, False, pid)
        if not handle:
            return win32_failure("OpenProcess(QUERY_INFORMATION | VM_READ) failed for module enumeration", C.get_last_error())
        try:
            class ModuleInfo(C.Structure):
                _fields_ = [("base", C.c_void_p), ("image_bytes", W.DWORD), ("entry_point", C.c_void_p)]
            capacity, needed, complete = 256, W.DWORD(), False
            for _ in range(5):
                modules = (W.HMODULE * capacity)()
                if not self.enum_modules(handle, modules, C.sizeof(modules), C.byref(needed), 3):
                    error = C.get_last_error()
                    if error == 299:
                        return unavailable("Module enumeration returned ERROR_PARTIAL_COPY. Cross-bitness or process/module changes are possible causes; use 64-bit Python on 64-bit Windows.",
                                           status="query_unavailable", winerror=error)
                    return win32_failure("EnumProcessModulesEx failed", error)
                if needed.value <= C.sizeof(modules):
                    complete = True
                    break
                if needed.value > 64 * 1024 * 1024:
                    return unavailable("Module enumeration returned an implausibly large buffer requirement", needed_bytes=needed.value)
                capacity = (needed.value + C.sizeof(W.HMODULE) - 1) // C.sizeof(W.HMODULE)
            count = min(needed.value // C.sizeof(W.HMODULE), len(modules))
            items, versions = [], {}
            for module in modules[:count]:
                entry = {"module_handle": hex(module or 0)}
                buffer = C.create_unicode_buffer(32768)
                size = self.module_path(handle, module, buffer, len(buffer))
                if size:
                    path = buffer.value
                    entry["path"] = path
                    key = os.path.normcase(path)
                    if key not in versions:
                        versions[key] = read(lambda path=path: self.file_version(path))
                    entry["version"] = versions[key]
                else:
                    entry["path"] = win32_failure("GetModuleFileNameExW failed", C.get_last_error())
                info = ModuleInfo()
                if self.module_info(handle, module, C.byref(info), C.sizeof(info)):
                    entry.update({"base_address": hex(info.base or 0), "image_bytes": info.image_bytes,
                                  "entry_point": hex(info.entry_point or 0)})
                else:
                    entry["image_information"] = win32_failure("GetModuleInformation failed", C.get_last_error())
                items.append(entry)
            return {"enumeration_complete": complete, "module_count": len(items), "modules": items,
                    "scope": "Loader modules (EXE and DLL), not every memory mapping; modules loaded as data files may be absent. The loader list can change during capture."}
        finally:
            self.close_handle(handle)

    def window(self, hwnd, thread):
        api = self.api
        result = {
            "hwnd": hex(hwnd), "title": api.text(api.title, hwnd),
            "class": api.text(api.class_name, hwnd), "pid_and_thread": api.process_id(hwnd),
            "window_rect": api.bounds(hwnd, False), "DWM_frame_rect": api.bounds(hwnd),
            "style": f"0x{api.get_long(hwnd, -16) & 0xFFFFFFFF:08X}",
            "extended_style": f"0x{api.get_long(hwnd, -20) & 0xFFFFFFFF:08X}",
            "visible": bool(api.visible(hwnd)), "enabled": bool(api.enabled(hwnd)),
            "minimized": bool(api.iconic(hwnd)), "maximized": bool(self.zoomed(hwnd)),
            "unicode": bool(self.unicode(hwnd)), "foreground_hwnd": hex(api.foreground() or 0),
            "parent_hwnd": hex(self.parent(hwnd) or 0), "owner_hwnd": hex(api.owner(hwnd, 4) or 0),
            "root_hwnd": hex(api.ancestor(hwnd, 2) or 0),
            "root_owner_hwnd": hex(api.ancestor(hwnd, 3) or 0),
            "menu_handle": hex(self.menu(hwnd) or 0), "context_help_id": self.help_id(hwnd),
            "window_id_or_userdata": {str(i): read(lambda i=i: self.long_value(api.get_long, hwnd, i))
                                      for i in (-12, -21)},
        }

        def client():
            rect = W.RECT()
            self.checked(self.client_rect(hwnd, C.byref(rect)))
            local = self.rect_value(rect)
            self.map_points(hwnd, None, C.byref(rect), 2)
            return {"local": local, "screen": self.rect_value(rect)}

        result["client_rect"] = read(client)
        result["monitor_and_DPI"] = read(lambda: self.monitor_and_dpi(hwnd))

        class Placement(C.Structure):
            _fields_ = [("length", W.UINT), ("flags", W.UINT), ("show_command", W.UINT),
                        ("min_position", W.POINT), ("max_position", W.POINT), ("normal", W.RECT)]

        def placement():
            value = Placement()
            value.length = C.sizeof(value)
            self.checked(self.placement(hwnd, C.byref(value)))
            return {"flags": value.flags, "show_command": value.show_command,
                    "min_position": [value.min_position.x, value.min_position.y],
                    "max_position": [value.max_position.x, value.max_position.y],
                    "normal_rect_workspace_coordinates": self.rect_value(value.normal)}

        result["placement"] = read(placement)

        class GuiInfo(C.Structure):
            _fields_ = [("size", W.DWORD), ("flags", W.DWORD), ("active", W.HWND),
                        ("focus", W.HWND), ("capture", W.HWND), ("menu_owner", W.HWND),
                        ("move_size", W.HWND), ("caret", W.HWND), ("caret_rect", W.RECT)]

        def gui():
            value = GuiInfo()
            value.size = C.sizeof(value)
            self.checked(self.gui_info(thread, C.byref(value)))
            focus, capture = value.focus or 0, value.capture or 0
            belongs = lambda handle: bool(handle and (handle == hwnd or api.ancestor(handle, 2) == hwnd))
            return {**{name: hex(getattr(value, name) or 0)
                       for name in ("active", "focus", "capture", "menu_owner", "move_size", "caret")},
                    "flags": value.flags, "caret_rect": self.rect_value(value.caret_rect),
                    "focus_within_selected_window": belongs(focus),
                    "capture_within_selected_window": belongs(capture),
                    "source": "GetGUIThreadInfo for the selected window's GUI thread/input queue.",
                    "scope": "Focus/capture are thread/input-queue state, not proof of system-wide exclusive mouse ownership."}

        result["GUI_thread"] = read(gui)
        properties = []

        @self.props_type
        def prop_callback(_, name, value, __):
            try:
                key = f"ATOM {name or 0}" if not name or name < 65536 else C.wstring_at(name)
                properties.append({"name": key, "value_handle": hex(value or 0)})
            except Exception as exc:
                properties.append(unavailable(exc))
            return 1

        self.enum_props(hwnd, prop_callback, 0)
        result["window_properties"] = properties
        result["class_values"] = {name: read(lambda index=index: self.long_value(self.class_long, hwnd, index)) for name, index in [
            ("style", -26), ("atom", -32), ("module", -16), ("window_proc", -24),
            ("icon", -14), ("small_icon", -34), ("cursor", -12), ("background_brush", -10),
            ("class_extra_bytes", -20), ("window_extra_bytes", -18)]}
        result["extra_storage"] = read(lambda: self.extra_storage(hwnd))
        children = []

        @api.enum_type
        def child_callback(child, _):
            try:
                children.append({"hwnd": hex(child), "class": api.text(api.class_name, child),
                                 "title_via_GetWindowText": api.text(api.title, child),
                                 "pid_and_thread": api.process_id(child),
                                 "rect": api.bounds(child, False), "visible": bool(api.visible(child)),
                                 "enabled": bool(api.enabled(child)),
                                 "style": f"0x{api.get_long(child, -16) & 0xFFFFFFFF:08X}",
                                 "extended_style": f"0x{api.get_long(child, -20) & 0xFFFFFFFF:08X}"})
            except Exception as exc:
                children.append(unavailable(exc))
            return True

        self.enum_children(hwnd, child_callback, 0)
        result["native_descendant_windows"] = children
        result["DWM_attributes"] = self.dwm_attributes(hwnd)
        corner = result["DWM_attributes"]["corner_preference"]
        backdrop = result["DWM_attributes"]["system_backdrop_type"]
        result["DWM_interpretation"] = {
            "corner_preference": ({0: "Default", 1: "Do not round", 2: "Round", 3: "Round small"}.get(corner, "Unknown")
                                  if isinstance(corner, int) else corner),
            "system_backdrop_type": ({0: "Auto", 1: "None", 2: "Main window (Mica)",
                                      3: "Transient window (Desktop Acrylic)", 4: "Tabbed window (Mica Alt)"}.get(backdrop, "Unknown")
                                     if isinstance(backdrop, int) else backdrop),
            "scope": "DWM configuration/preference; the actual visual result can depend on Windows policy and window state."}
        result["layer_attributes"] = self.layered_window(hwnd)
        return result

    def process(self, pid):
        session = W.DWORD()
        result = {"session_id": session.value if self.session(pid, C.byref(session)) else
                  win32_failure("ProcessIdToSessionId failed", C.get_last_error())}
        handle = self.open_process(0x1000, False, pid)
        if not handle:
            result["query_handle"] = win32_failure("OpenProcess failed", C.get_last_error())
            return result
        try:
            priority = self.priority(handle)
            result["priority_class"] = priority if priority else win32_failure("GetPriorityClass failed", C.get_last_error())
            size = W.DWORD(32768)
            buffer = C.create_unicode_buffer(size.value)
            result["image_path"] = buffer.value if self.query_image(handle, 0, buffer, C.byref(size)) else win32_failure(
                "QueryFullProcessImageName failed", C.get_last_error())
            if hasattr(self.kernel, "IsWow64Process2"):
                fn = self.kernel.IsWow64Process2
                fn.argtypes, fn.restype = [W.HANDLE, C.POINTER(W.WORD), C.POINTER(W.WORD)], W.BOOL
                process_machine, native_machine = W.WORD(), W.WORD()
                result["architecture"] = ({"process_machine": hex(process_machine.value),
                    "native_machine": hex(native_machine.value)} if fn(handle, C.byref(process_machine), C.byref(native_machine))
                    else win32_failure("IsWow64Process2 failed", C.get_last_error()))
            else:
                result["architecture"] = unavailable("IsWow64Process2 is unsupported by this Windows version", status="not_supported")
            for name, fn in [("package_full_name", self.package), ("application_user_model_id", self.app_id)]:
                result[name] = self.identity(fn, handle, name)
            result["power_throttling"] = read(lambda: self.power_throttling(handle))
            token = W.HANDLE()
            if self.open_token(handle, 8, C.byref(token)):
                try:
                    result["token"] = {}
                    for info_class, name in [(18, "elevation_type"), (20, "elevated"), (26, "UIAccess")]:
                        value, returned = W.DWORD(), W.DWORD()
                        result["token"][name] = (value.value if self.token_info(token, info_class, C.byref(value),
                            C.sizeof(value), C.byref(returned)) else win32_failure("Token query failed", C.get_last_error()))
                    result["token"]["integrity"] = read(lambda: self.integrity(token))
                finally:
                    self.close_handle(token)
            else:
                result["token"] = win32_failure("OpenProcessToken failed", C.get_last_error())
        finally:
            self.close_handle(handle)
        result["GUI_resources"] = read(lambda: self.resource_counts(pid))
        result["loaded_modules"] = read(lambda: self.loaded_modules(pid))
        return result

    def integrity(self, token):
        length = W.DWORD()
        self.token_info(token, 25, None, 0, C.byref(length))
        if not length.value:
            raise OSError(C.get_last_error(), "Token integrity length query failed")
        buffer = C.create_string_buffer(length.value)
        self.checked(self.token_info(token, 25, buffer, length, C.byref(length)))
        sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
        count = self.sid_count(sid)[0]
        if not count:
            raise RuntimeError("Integrity SID has no sub-authorities")
        rid = self.sid_part(sid, count - 1)[0]
        names = {0: "Untrusted", 4096: "Low", 8192: "Medium", 8448: "Medium Plus",
                 12288: "High", 16384: "System", 20480: "Protected"}
        return {"RID": rid, "level": names.get(rid, "Other")}

    def executable(self, path):
        result = {"path": path, "stat": read(lambda: self.file_stat(path)),
                  "version": read(lambda: self.file_version(path))}
        return result

    @staticmethod
    def file_stat(path):
        stat = os.stat(path)
        return {key: getattr(stat, key) for key in dir(stat) if key.startswith("st_")}

    def file_version(self, path):
        unused = W.DWORD()
        size = self.version_size(path, C.byref(unused))
        if not size and C.get_last_error() in (1812, 1813, 1814, 1815):
            return unavailable("The file has no matching version resource", status="not_applicable", winerror=C.get_last_error())
        self.checked(size)
        data = C.create_string_buffer(size)
        self.checked(self.version_info(path, 0, size, data))
        pointer, length = C.c_void_p(), W.UINT()
        self.checked(self.query_version(data, "\\", C.byref(pointer), C.byref(length)))
        fixed = C.cast(pointer, C.POINTER(C.c_uint32))
        values = [fixed[i] for i in range(min(13, length.value // 4))]
        result = {"fixed_info_DWORDs": values}
        if len(values) >= 6:
            result["file_version"] = ".".join(str(v) for v in [values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535])
            result["product_version"] = ".".join(str(v) for v in [values[4] >> 16, values[4] & 65535, values[5] >> 16, values[5] & 65535])
        translations = [(0x0409, 0x04B0)]
        if self.query_version(data, "\\VarFileInfo\\Translation", C.byref(pointer), C.byref(length)):
            words = C.cast(pointer, C.POINTER(C.c_uint16))
            translations = [(words[i], words[i + 1]) for i in range(0, length.value // 2 - 1, 2)]
        result["strings"] = {}
        for language, codepage in translations:
            strings = {}
            for key in ("CompanyName", "FileDescription", "FileVersion", "InternalName", "LegalCopyright",
                        "LegalTrademarks", "OriginalFilename", "ProductName", "ProductVersion", "Comments",
                        "PrivateBuild", "SpecialBuild"):
                query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
                if self.query_version(data, query, C.byref(pointer), C.byref(length)):
                    strings[key] = C.wstring_at(pointer, max(0, length.value - 1))
            result["strings"][f"{language:04x}{codepage:04x}"] = strings
        return result


def process_query(process, name, *args, **kwargs):
    missing = object()
    try:
        getter = getattr(process, name, missing)
    except Exception as exc:
        return exception_result(exc)
    if getter is missing:
        return unavailable(f"The installed psutil.Process API does not expose {name} on this platform.",
                           status="not_supported", api=f"psutil.Process.{name}")
    return read(lambda: getter(*args, **kwargs) if callable(getter) else getter)


def collect_process(psutil, process):
    getters = ["pid", "name", "exe", "cmdline", "cwd", "username", "ppid", "create_time", "status",
               "terminal", "num_threads", "threads", "num_handles", "cpu_times", "cpu_affinity",
               "memory_info", "memory_full_info", "memory_percent", "io_counters", "open_files",
               "nice", "ionice", "environ", "num_ctx_switches", "is_running"]
    data = {}
    for name in getters:
        data[name] = process_query(process, name)
    data["cpu_percent_100ms_sample"] = process_query(process, "cpu_percent", interval=0.1)
    data["memory_maps_ungrouped"] = process_query(process, "memory_maps", grouped=False)
    data["network_connections"] = process_query(process, "net_connections", kind="all")

    def related(item):
        return {"pid": item.pid, "name": read(item.name), "exe": read(item.exe),
                "create_time": read(item.create_time)} if item else None

    data["parent"] = read(lambda: related(process.parent()))
    data["children"] = read(lambda: [related(child) for child in process.children(recursive=False)])
    return data


def cancel_capture(capture):
    """Only terminate UIA helper processes owned by this capture."""
    with _PROBE_LOCK:
        capture.cancelled.set()
        for child, owner in list(_PROBES.items()):
            if owner is capture and child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass


def collect_uia(capture):
    if not capture.uia_enabled:
        return unavailable("UI Automation was disabled for this capture", status="not_collected")
    if sys.platform != "win32":
        return unavailable("UI Automation requires Windows", status="not_supported")
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inspect_uia.py")
    if not os.path.isfile(helper):
        return unavailable("inspect_uia.py is missing from the installation")
    command = [sys.executable, helper, "--hwnd", str(capture.hwnd), "--pid", str(capture.pid)]
    if capture.point is not None:
        command += ["--x", str(capture.point[0]), "--y", str(capture.point[1])]
    if capture.expected_created is not None:
        command += ["--created", str(capture.expected_created)]
    child = None
    try:
        with _PROBE_LOCK:
            if capture.cancelled.is_set():
                return unavailable("The capture was cancelled", status="not_collected")
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", creationflags=0x08000000)
            _PROBES[child] = capture
        stdout, stderr = child.communicate(timeout=capture.uia_timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        return unavailable("The isolated UI Automation probe exceeded its time budget and was terminated; native/process report data is retained.",
                           status="timeout", timeout_seconds=capture.uia_timeout)
    except Exception as exc:
        if child is not None and child.poll() is None:
            try:
                child.kill()
                child.communicate()
            except OSError:
                pass
        return exception_result(exc)
    finally:
        if child is not None:
            with _PROBE_LOCK:
                _PROBES.pop(child, None)
    if capture.cancelled.is_set():
        return unavailable("The capture was cancelled", status="not_collected")
    if child.returncode != 0:
        return unavailable("The isolated UI Automation probe failed", returncode=child.returncode,
                           diagnostic=stderr.strip())
    try:
        value = json.loads(stdout)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object from the UI Automation probe")
        return value
    except (ValueError, TypeError) as exc:
        return unavailable(f"Invalid UI Automation probe response: {exc}")


def collect_report(api, native, psutil, capture):
    """Collect once. Re-copying a frozen report never calls these APIs again."""
    data = {"tool": f"Screen App Inspect {VERSION}", "capture_started": capture.started,
            "runtime": runtime_context(psutil),
            "selected_window": dict(capture.summary), "window": None, "process": None}
    process = None
    identity_mismatch = False
    try:
        process = psutil.Process(capture.pid)
    except Exception as exc:
        data["process"] = exception_result(exc)
    if process is not None:
        created = read(process.create_time)
        if isinstance(created, (int, float)) and capture.expected_created is not None and created != capture.expected_created:
            identity_mismatch = True
            process = None
            data["process"] = unavailable("PID was reused; replacement process was not inspected", status="identity_changed")
        elif not isinstance(created, (int, float)):
            data["process_identity_validation"] = created
    if not identity_mismatch and api.valid(capture.hwnd) and api.process_id(capture.hwnd) == (capture.pid, capture.thread):
        data["window"] = read(lambda: native.window(capture.hwnd, capture.thread))
        if not api.valid(capture.hwnd) or api.process_id(capture.hwnd) != (capture.pid, capture.thread):
            data["window_identity_after_collection"] = unavailable("Window disappeared or handle was reused during collection", status="window_gone")
    else:
        data["window"] = unavailable("Selected window disappeared or its handle/PID was reused", status="window_gone")
    if process is not None:
        data["process"] = collect_process(psutil, process)
        running = read(process.is_running)
        if running is True:
            data["native_process"] = read(lambda: native.process(capture.pid))
            executable = data["process"].get("exe")
            if isinstance(executable, str) and executable:
                data["executable_file"] = read(lambda: native.executable(executable))
        else:
            data["identity_after_collection"] = (running if isinstance(running, dict) else
                unavailable("Process exited while the report was being collected", status="process_exited"))
    if not identity_mismatch and api.valid(capture.hwnd) and api.process_id(capture.hwnd) == (capture.pid, capture.thread):
        data["UI_Automation"] = read(lambda: collect_uia(capture))
        if not api.valid(capture.hwnd) or api.process_id(capture.hwnd) != (capture.pid, capture.thread):
            data["UI_Automation_identity_after_collection"] = unavailable("Window disappeared or changed during UI Automation collection", status="window_gone")
    else:
        data["UI_Automation"] = unavailable("The selected window no longer has a validated identity", status="window_gone")
    if data["window"] is None:
        data["window"] = unavailable("Window metadata could not be queried safely after process identity validation")
    data["capture_completed"] = timestamp()
    data["field_status_summary"] = field_status_summary(data)
    data["scope"] = [
        "Unabridged values returned by the implemented Windows and psutil queries.",
        "Reads take place between capture_started and capture_completed; this is not an atomic OS snapshot.",
        "This report can include process environment variables, open file paths, and network endpoints.",
        "Unavailable, unsupported, exited, or denied fields are recorded separately.",
        "Application-private objects, arbitrary process memory, OCR, and browser DOM data are outside this report.",
        "UI Automation covers the selected window, saved pointer element and focused element within it; unsupported providers/timeout are recorded. Password values are not queried.",
        "Native child titles use GetWindowText; custom control text may be unavailable.",
        "System-hosted notifications may identify their renderer rather than their sender.",
    ]
    return json.dumps(json_value(data), ensure_ascii=False, indent=2, allow_nan=False)
