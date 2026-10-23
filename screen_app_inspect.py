#!/usr/bin/env python3
"""Screen App Inspect - Windows 10/11, Python 3.10+, Tkinter, psutil.

Install: py -m pip install psutil
Run: py screen_app_inspect.py
Left-click: freeze/resume; Right-click: copy full report.
Ctrl+Alt+M: normal mouse/pause; Ctrl+Alt+F: freeze; Ctrl+Alt+C: copy; Ctrl+Alt+Q: quit.
"""
from __future__ import annotations
import argparse
import copy
from concurrent.futures import Future
import json
import queue
import threading
from inspect_support import Capture, MouseHook, NativeDetails, VERSION, cancel_capture, collect_report
import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import sys
import time


def clipped(value, limit=150):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def place(x, y, width, height, area):
    left, top, right, bottom = area
    tx = x + 20 if x + 20 + width <= right else x - width - 20
    ty = y + 20 if y + 20 + height <= bottom else y - height - 20
    return max(left, min(tx, right - width)), max(top, min(ty, bottom - height))


def handle_text(hwnd):
    return f"0x{hwnd:0{C.sizeof(C.c_void_p) * 2}X}" if hwnd else "None"


class Windows:
    def __init__(self):
        self.user = C.WinDLL("user32", use_last_error=True)
        dwm = C.WinDLL("dwmapi", use_last_error=True)
        gdi = C.WinDLL("gdi32", use_last_error=True)

        def bind(dll, name, result, *arguments):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(arguments)
            return fn

        self.enum_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.proc_type = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        self.enum = bind(self.user, "EnumWindows", W.BOOL, self.enum_type, W.LPARAM)
        self.valid = bind(self.user, "IsWindow", W.BOOL, W.HWND)
        self.visible = bind(self.user, "IsWindowVisible", W.BOOL, W.HWND)
        self.iconic = bind(self.user, "IsIconic", W.BOOL, W.HWND)
        self.enabled = bind(self.user, "IsWindowEnabled", W.BOOL, W.HWND)
        self.rect = bind(self.user, "GetWindowRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
        self.title = bind(self.user, "GetWindowTextW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
        self.title_length = bind(self.user, "GetWindowTextLengthW", C.c_int, W.HWND)
        self.class_name = bind(self.user, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
        self.pid = bind(self.user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
        self.ancestor = bind(self.user, "GetAncestor", W.HWND, W.HWND, W.UINT)
        self.owner = bind(self.user, "GetWindow", W.HWND, W.HWND, W.UINT)
        self.foreground = bind(self.user, "GetForegroundWindow", W.HWND)
        self.desktop = bind(self.user, "GetDesktopWindow", W.HWND)
        self.shell = bind(self.user, "GetShellWindow", W.HWND)
        self.cursor = bind(self.user, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
        self.at_point = bind(self.user, "WindowFromPoint", W.HWND, W.POINT)
        self.attr = bind(dwm, "DwmGetWindowAttribute", W.LONG,
                         W.HWND, W.DWORD, C.c_void_p, W.DWORD)
        self.region = bind(self.user, "GetWindowRgn", C.c_int, W.HWND, W.HRGN)
        self.new_region = bind(gdi, "CreateRectRgn", W.HRGN, C.c_int, C.c_int, C.c_int, C.c_int)
        self.in_region = bind(gdi, "PtInRegion", W.BOOL, W.HRGN, C.c_int, C.c_int)
        self.delete = bind(gdi, "DeleteObject", W.BOOL, W.HANDLE)
        suffix = "PtrW" if C.sizeof(C.c_void_p) == 8 else "W"
        self.get_long = bind(self.user, "GetWindowLong" + suffix, C.c_ssize_t, W.HWND, C.c_int)
        self.set_long = bind(self.user, "SetWindowLong" + suffix, C.c_ssize_t,
                             W.HWND, C.c_int, C.c_ssize_t)
        self.call_proc = bind(self.user, "CallWindowProcW", C.c_ssize_t,
                              C.c_void_p, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        self.position = bind(self.user, "SetWindowPos", W.BOOL, W.HWND, W.HWND,
                             C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
        self.register = bind(self.user, "RegisterHotKey", W.BOOL, W.HWND, C.c_int, W.UINT, W.UINT)
        self.unregister = bind(self.user, "UnregisterHotKey", W.BOOL, W.HWND, C.c_int)
        self.monitor = bind(self.user, "MonitorFromPoint", W.HMONITOR, W.POINT, W.DWORD)

        class MonitorInfo(C.Structure):
            _fields_ = [("size", W.DWORD), ("monitor", W.RECT), ("work", W.RECT), ("flags", W.DWORD)]

        self.monitor_type = MonitorInfo
        self.monitor_info = bind(self.user, "GetMonitorInfoW", W.BOOL,
                                 W.HMONITOR, C.POINTER(MonitorInfo))
        self.metric = bind(self.user, "GetSystemMetrics", C.c_int, C.c_int)

    def dpi_aware(self):
        try:
            fn = self.user.SetProcessDpiAwarenessContext
            fn.argtypes, fn.restype = [C.c_void_p], W.BOOL
            if fn(C.c_void_p(-4)):
                return
        except AttributeError:
            pass
        try:
            shcore = C.WinDLL("shcore")
            shcore.SetProcessDpiAwareness.argtypes = [C.c_int]
            shcore.SetProcessDpiAwareness.restype = W.LONG
            if shcore.SetProcessDpiAwareness(2) == 0:
                return
        except (AttributeError, OSError):
            pass
        self.user.SetProcessDPIAware()

    def process_id(self, hwnd):
        pid = W.DWORD()
        thread = self.pid(hwnd, C.byref(pid))
        return pid.value, thread

    def text(self, fn, hwnd):
        length = max(512, self.title_length(hwnd) + 1) if fn is self.title else 512
        buffer = C.create_unicode_buffer(length)
        fn(hwnd, buffer, len(buffer))
        return buffer.value

    def bounds(self, hwnd, frame=True):
        rect = W.RECT()
        if not self.rect(hwnd, C.byref(rect)):
            return None
        if frame:
            other = W.RECT()
            if self.attr(hwnd, 9, C.byref(other), C.sizeof(other)) == 0:
                if other.right > other.left and other.bottom > other.top:
                    rect = other
        return rect.left, rect.top, rect.right, rect.bottom

    def covers(self, hwnd, x, y):
        box = self.bounds(hwnd)
        if not box or not (box[0] <= x < box[2] and box[1] <= y < box[3]):
            return False
        cloaked = W.DWORD()
        if self.attr(hwnd, 14, C.byref(cloaked), C.sizeof(cloaked)) == 0 and cloaked.value:
            return False
        region = self.new_region(0, 0, 0, 0)
        if not region:
            return True
        try:
            if self.region(hwnd, region) == 0:
                return True  # No explicitly assigned window region.
            raw = self.bounds(hwnd, False)
            return bool(raw and self.in_region(region, x - raw[0], y - raw[1]))
        finally:
            self.delete(region)

    def work_area(self, x, y):
        info = self.monitor_type()
        info.size = C.sizeof(info)
        if self.monitor_info(self.monitor(W.POINT(x, y), 2), C.byref(info)):
            r = info.work
            return r.left, r.top, r.right, r.bottom
        return 0, 0, self.metric(0), self.metric(1)


def process_details(psutil, pid):
    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return {"Process": "Unavailable (process ended)"}
    result = {}
    fields = {"Process": process.name, "Executable": process.exe,
              "Command line": lambda: subprocess.list2cmdline(process.cmdline()),
              "Working directory": process.cwd, "User": process.username,
              "Parent PID": process.ppid,
              "Process creation time": getattr(process, "create_time", lambda: "Unavailable")}
    for key, getter in fields.items():
        try:
            result[key] = str(getter()) or "(Empty)"
        except psutil.AccessDenied:
            result[key] = "Unavailable (access denied)"
        except psutil.NoSuchProcess:
            result[key] = "Unavailable (process ended)"
        except (psutil.Error, OSError) as exc:
            result[key] = f"Unavailable ({type(exc).__name__})"
    return result


class Inspector:
    def __init__(self, api, psutil, tk, args):
        self.api, self.psutil, self.tk, self.args = api, psutil, tk, args
        self.native = NativeDetails(api)
        self.own_pid = os.getpid()
        self.frozen, self.closed, self.paused = False, False, args.start_paused
        self.data, self.target, self.process_pid = None, None, None
        self.process, self.names = {}, {}
        self.read_at, self.process_at = 0.0, 0.0
        self.actions, self.keys, self.event_hooks = queue.SimpleQueue(), [], []
        self.notice, self.notice_until = "", 0.0
        self.last_view, self.job, self.frozen_capture = None, None, None
        self.mouse, self.original_proc = None, None
        self.last_raise = 0.0
        self.after_id = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Screen App Inspect")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.configure(bg="#111827")
        self.labels = []
        for color, font in [("#7dd3fc", ("Segoe UI", 11, "bold")),
                            ("#f3f4f6", ("Segoe UI", 10)), ("#a7b0c0", ("Segoe UI", 8))]:
            label = tk.Label(self.root, bg="#111827", fg=color, font=font, anchor="w", justify="left")
            label.pack(fill="x", padx=12, pady=5)
            self.labels.append(label)
        self.root.update_idletasks()
        self.hwnd = api.ancestor(self.root.winfo_id(), 2) or self.root.winfo_id()
        # Native click-through, layered, tool-window and no-activation styles.
        api.set_long(self.hwnd, -20, api.get_long(self.hwnd, -20) | 0x080800A0)
        api.position(self.hwnd, W.HWND(-1), 0, 0, 0, 0, 0x0037)

        @api.proc_type
        def window_proc(hwnd, message, wparam, lparam):
            if message == 0x0312:
                action = {1: "freeze", 2: "copy", 3: "pause", 4: "quit"}.get(int(wparam))
                if action:
                    self.actions.put((action, None))
                return 0
            return api.call_proc(self.original_proc, hwnd, message, wparam, lparam)

        self.window_proc = window_proc
        self.original_proc = api.set_long(self.hwnd, -4, C.cast(window_proc, C.c_void_p).value)
        if not self.original_proc:
            self.root.destroy()
            raise RuntimeError(f"Cannot install window handler: {C.get_last_error()}")
        try:
            for key_id, letter in enumerate("FCMQ", 1):
                if not api.register(self.hwnd, key_id, 0x4003, ord(letter)):
                    raise RuntimeError(f"Ctrl+Alt+{letter} is unavailable (Windows error {C.get_last_error()}). "
                                       "Close another inspector instance or the app using this shortcut.")
                self.keys.append(key_id)
            self.install_overlay_events()
            if not args.keyboard_only:
                self.mouse = MouseHook(self.actions)
                self.mouse.gate.enabled = not self.paused
                try:
                    self.mouse.start()
                except RuntimeError as exc:
                    self.mouse.stop()
                    self.mouse = None
                    self.set_notice(f"{exc} Use Ctrl+Alt+F / Ctrl+Alt+C.", 10)
            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.render(api.work_area(0, 0))
            if not self.paused:
                self.root.deiconify()
            self.after_id = self.root.after(0, self.tick)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        for capture in (self.job, self.frozen_capture):
            if capture is not None:
                cancel_capture(capture)
        try:
            if self.after_id is not None:
                self.root.after_cancel(self.after_id)
            if self.mouse:
                self.mouse.stop()
        finally:
            try:
                for hook in self.event_hooks:
                    self.native.unhook_event(hook)
                for key_id in self.keys:
                    self.api.unregister(self.hwnd, key_id)
            finally:
                if self.original_proc:
                    self.api.set_long(self.hwnd, -4, self.original_proc)
                self.root.destroy()

    def install_overlay_events(self):
        @self.native.event_type
        def event_callback(hook, event, hwnd, object_id, child_id, thread, event_time):
            if self.closed or self.paused or not hwnd:
                return
            now = time.monotonic()
            if now - self.last_raise >= 0.01:
                self.last_raise = now
                self.api.position(self.hwnd, W.HWND(-1), 0, 0, 0, 0, 0x0213)

        self.event_callback = event_callback
        for first, last in [(3, 3), (0x8002, 0x8004)]:
            # Out-of-context events; skip this process to avoid feedback loops.
            hook = self.native.event_hook(first, last, None, event_callback, 0, 0, 2)
            if hook:
                self.event_hooks.append(hook)

    def find(self, x, y):
        api, found, errors = self.api, [], []
        excluded = {api.desktop(), api.shell()}

        @api.enum_type
        def visit(hwnd, _):
            try:
                if hwnd in excluded or not api.visible(hwnd) or api.iconic(hwnd):
                    return True
                pid, _ = api.process_id(hwnd)
                if not pid or pid == self.own_pid or not api.covers(hwnd, x, y):
                    return True
                if api.text(api.class_name, hwnd) in {
                        "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
                    found.append(None)
                    return False  # Do not inspect an app covered by the shell.
                found.append(hwnd)
                return False
            except Exception as exc:
                errors.append(exc)
                return False

        # Top-level windows in Z order, including disabled/click-through popups.
        api.enum(visit, 0)
        if errors:
            raise errors[0]
        if not found or found[0] is None:
            return None
        hwnd = found[0]
        if not self.args.include_explorer:
            pid, _ = api.process_id(hwnd)
            cached = self.names.get(pid)
            if not cached or time.monotonic() - cached[0] > 2:
                try:
                    name = self.psutil.Process(pid).name().lower()
                except self.psutil.Error:
                    name = ""
                if len(self.names) > 256:
                    self.names.clear()
                cached = self.names[pid] = (time.monotonic(), name)
            if cached[1] == "explorer.exe":
                return None
        return hwnd

    def inspect(self, hwnd, x, y, now):
        api = self.api
        pid, thread = api.process_id(hwnd)
        if not pid or not api.valid(hwnd):
            return None
        if pid != self.process_pid or now - self.process_at > 1.5:
            self.process = process_details(self.psutil, pid)
            self.process_pid, self.process_at = pid, now
        bounds = api.bounds(hwnd, False)
        if not bounds:
            return None
        style, exstyle = api.get_long(hwnd, -16) & 0xFFFFFFFF, api.get_long(hwnd, -20) & 0xFFFFFFFF
        child = api.at_point(W.POINT(x, y))
        child_info = "None / no separate child HWND detected"
        if child and child != hwnd and api.ancestor(child, 2) == hwnd:
            child_info = f"{handle_text(child)} | {api.text(api.class_name, child)}"
        data = {"Title": api.text(api.title, hwnd) or "(Untitled window)",
                "Process": self.process.get("Process", "Unavailable"),
                "PID / thread": f"{pid} / {thread}",
                "Always on top": "Yes (WS_EX_TOPMOST)" if exstyle & 8 else "No",
                "Executable": self.process.get("Executable", "Unavailable"),
                "Window class": api.text(api.class_name, hwnd), "HWND": handle_text(hwnd)}
        data.update({k: v for k, v in self.process.items() if k not in data})
        data.update({"Bounds": str(bounds),
                     "Size": f"{bounds[2] - bounds[0]} x {bounds[3] - bounds[1]} pixels",
                     "Enabled": "Yes" if api.enabled(hwnd) else "No",
                     "Foreground": "Yes" if api.foreground() == hwnd else "No",
                     "Owner HWND": handle_text(api.owner(hwnd, 4)), "Native child": child_info,
                     "Style / exstyle": f"0x{style:08X} / 0x{exstyle:08X}",
                     "Extended flags": ", ".join(name for bit, name in [
                         (8, "TOPMOST"), (0x20, "TRANSPARENT"), (0x80, "TOOLWINDOW"),
                         (0x80000, "LAYERED"), (0x8000000, "NOACTIVATE")] if exstyle & bit) or "None",
                     "Captured at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                     "Selection point": [x, y]})
        return data if api.valid(hwnd) and api.process_id(hwnd)[0] == pid else None

    def set_notice(self, text, seconds=3):
        self.notice, self.notice_until = text, time.monotonic() + seconds

    def pointer(self):
        point = W.POINT()
        return (point.x, point.y) if self.api.cursor(C.byref(point)) else None

    def select(self, point, force=False):
        if point is None:
            self.data, self.target = None, None
            return None
        now = time.monotonic()
        hwnd = self.find(*point)
        if hwnd is None:
            self.data, self.target = None, None
        elif force or hwnd != self.target or now - self.read_at > 0.25:
            if force:
                self.process_pid = None
            self.data = self.inspect(hwnd, *point, now)
            self.target, self.read_at = hwnd, now
        return self.data

    def start_capture(self, copy_requested=False):
        if not self.data or not self.target:
            self.set_notice("No window found.")
            return None
        if self.job is not None:
            self.set_notice("A report is being collected. Please try again shortly.")
            return None
        summary = copy.deepcopy(self.data)
        pid, thread = self.api.process_id(self.target)
        if f"{pid} / {thread}" != summary["PID / thread"]:
            self.set_notice("The selected window changed. Please try again.")
            return None
        created = summary.get("Process creation time")
        try:
            created = float(created)
        except (TypeError, ValueError):
            created = None
        capture = Capture(self.target, pid, thread, summary, expected_created=created,
                          copy_requested=copy_requested, future=Future(),
                          point=tuple(summary["Selection point"]) if "Selection point" in summary else None,
                          uia_timeout=getattr(self.args, "uia_timeout", 4.0),
                          uia_enabled=not getattr(self.args, "no_uia", False))
        self.job = capture

        def collect():
            try:
                capture.future.set_result(collect_report(self.api, self.native, self.psutil, capture))
            except Exception as exc:
                capture.future.set_exception(exc)

        threading.Thread(target=collect, name="InspectorReport", daemon=True).start()
        return capture

    def toggle_freeze(self, point):
        if self.frozen:
            self.frozen, self.frozen_capture, self.target = False, None, None
            self.set_notice("Live inspection resumed.")
            return
        if self.select(point, force=True) is None:
            self.set_notice("No window found.")
            return
        capture = self.start_capture()
        if capture:
            self.frozen, self.frozen_capture = True, capture
            self.data = capture.summary
            self.set_notice("Frozen. Collecting the full snapshot...")

    def copy_report(self, point):
        if self.frozen:
            capture = self.frozen_capture
            if capture.report is not None:
                self.write_clipboard(capture.report)
            else:
                capture.copy_requested = True
                self.set_notice("Collecting the frozen report; copy is queued.")
            return
        if self.select(point, force=True) is None:
            self.set_notice("No window found.")
            return
        if self.start_capture(copy_requested=True):
            self.set_notice("Collecting the full report; it will be copied when ready.")

    def write_clipboard(self, report):
        self.root.clipboard_clear()
        self.root.clipboard_append(report)
        self.set_notice("Full report copied.")

    def poll_capture(self):
        capture = self.job
        if capture is None or not capture.future.done():
            return
        try:
            capture.report = capture.future.result()
        except Exception as exc:
            capture.report = json.dumps({"tool": f"Screen App Inspect {VERSION}", "capture_started": capture.started,
                "selected_window": capture.summary, "capture_error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False, indent=2)
            self.set_notice("Report collection failed. The report contains the error.")
        else:
            if self.frozen_capture is capture:
                self.set_notice("Frozen snapshot ready. Right-click to copy.")
        self.job = None
        if capture.copy_requested:
            self.write_clipboard(capture.report)

    def toggle_pause(self):
        self.paused = not self.paused
        if self.mouse:
            self.mouse.gate.enabled = not self.paused
        if self.paused:
            self.root.withdraw()
            print("Inspection paused. Mouse clicks work normally. Ctrl+Alt+M resumes.")
        else:
            self.last_view, self.target = None, None
            self.root.deiconify()
            self.set_notice("Inspection resumed. Ctrl+Alt+M restores normal mouse clicks.")

    def handle_action(self, action, point):
        if action == "quit":
            self.close()
        elif action == "pause":
            self.toggle_pause()
        elif action == "hook_error":
            if self.mouse:
                self.mouse.gate.enabled = False
            self.set_notice(str(point) + " Use Ctrl+Alt+F / Ctrl+Alt+C.", 10)
        elif not self.paused:
            if action == "freeze":
                self.toggle_freeze(point if point is not None else self.pointer())
            elif action == "copy":
                self.copy_report(point if point is not None else self.pointer())

    def render(self, area):
        width = max(120, min(self.args.width, area[2] - area[0] - 24))
        heading = f"Screen App Inspect | {'FROZEN' if self.frozen else 'LIVE'}"
        lines = ["No window found.", "Desktop, taskbar, and excluded windows are ignored."]
        if self.data:
            # The tooltip always contains identification only. No details mode.
            keys = ["Title", "Process", "PID / thread", "Always on top", "Executable"]
            lines = [f"{key}: {clipped(self.data[key], 130)}" for key in keys]
        if self.mouse and self.mouse.gate.enabled:
            footer = "Left-click Freeze/Resume | Right-click Copy\nCtrl+Alt+M Normal mouse | Ctrl+Alt+Q Quit"
        else:
            footer = "Ctrl+Alt+F Freeze | Ctrl+Alt+C Copy\nCtrl+Alt+M Pause/Resume | Ctrl+Alt+Q Quit"
        if time.monotonic() < self.notice_until:
            footer = self.notice + "\n" + footer
        view = heading, tuple(lines), footer, width, area[3] - area[1]
        if view == self.last_view:
            return
        for label, text in zip(self.labels, [heading, "\n".join(lines), footer]):
            label.configure(text=text, wraplength=max(80, width - 24))
        self.root.update_idletasks()
        maximum = max(1, area[3] - area[1] - 16)
        while self.root.winfo_reqheight() > maximum and len(lines) > 2:
            lines.pop()
            self.labels[1].configure(text="\n".join(lines) + "\nFull report: right-click / Ctrl+Alt+C")
            self.root.update_idletasks()
        self.root.geometry(f"{width}x{min(maximum, self.root.winfo_reqheight())}")
        self.root.update_idletasks()
        self.last_view = view

    def tick(self):
        try:
            self.poll_capture()
            for _ in range(100):
                try:
                    action, point = self.actions.get_nowait()
                except queue.Empty:
                    break
                self.handle_action(action, point)
                if self.closed:
                    return
            if not self.paused:
                point = self.pointer()
                if point:
                    if not self.frozen:
                        self.select(point)
                    area = self.api.work_area(*point)
                    self.render(area)
                    width, height = self.root.winfo_width(), self.root.winfo_height()
                    x, y = place(*point, width, height, area)
                    # Reassert the top of the normal topmost band without activation.
                    self.api.position(self.hwnd, W.HWND(-1), x, y, width, height, 0x0250)
                elif not self.frozen:
                    self.data, self.target = None, None
                    self.render(self.api.work_area(0, 0))
        except self.tk.TclError:
            self.close()
            raise
        except Exception as exc:
            if not self.frozen:
                self.target, self.data = None, None
            self.set_notice(f"Inspection error: {clipped(exc, 90)}")
            print(self.notice, file=sys.stderr)
        if not self.closed:
            self.after_id = self.root.after(self.args.interval, self.tick)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--include-explorer", action="store_true", help="Also inspect Explorer app windows.")
    parser.add_argument("--keyboard-only", action="store_true", help="Use hotkeys; do not capture mouse buttons.")
    parser.add_argument("--start-paused", action="store_true", help="Start paused; Ctrl+Alt+M enables inspection.")
    parser.add_argument("--interval", type=int, default=50, help="Polling interval: 25-1000 ms (default: 50).")
    parser.add_argument("--width", type=int, default=480, help="Tooltip width: 320-1000 pixels (default: 480).")
    parser.add_argument("--no-uia", action="store_true", help="Skip UI Automation properties.")
    parser.add_argument("--uia-timeout", type=float, default=4.0, help="UI Automation probe time budget: 1-30 seconds (default: 4).")
    args = parser.parse_args()
    if not 25 <= args.interval <= 1000 or not 320 <= args.width <= 1000 or not 1 <= args.uia_timeout <= 30:
        parser.error("Use --interval 25..1000, --width 320..1000 and --uia-timeout 1..30.")
    if sys.platform != "win32":
        parser.exit(1, "Screen App Inspect requires Windows 10 or Windows 11.\n")
    try:
        import psutil
        import tkinter as tk
    except ImportError as exc:
        parser.exit(1, f"Missing dependency: {exc.name}. Run: py -m pip install -r requirements.txt\n"
                    "If Tkinter is missing, install Python with Tcl/Tk enabled.\n")
    api = Windows()
    api.dpi_aware()
    try:
        app = Inspector(api, psutil, tk, args)
        print(f"Screen App Inspect {VERSION} is running. Hover over a visible application window.")
        print("Left-click: freeze/resume | Right-click: copy the full report")
        print("Ctrl+Alt+F: freeze | Ctrl+Alt+C: copy | Ctrl+Alt+M: normal mouse/pause | Ctrl+Alt+Q: quit")
        print("Full reports are collected in the background; frozen reports are never re-read.")
        app.run()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError, tk.TclError) as exc:
        parser.exit(1, f"Screen App Inspect could not continue: {exc}\n")


if __name__ == "__main__":
    main()
