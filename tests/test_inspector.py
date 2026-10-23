import argparse
import ctypes as C
from concurrent.futures import Future
from collections import namedtuple
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import screen_app_inspect as app
from inspect_support import Capture, MouseGate, collect_process, collect_report
import inspect_support as support


class FakePS:
    class Error(Exception):
        pass

    class AccessDenied(Error):
        pass

    class NoSuchProcess(Error):
        pass

    def __init__(self, names=None):
        self.names = names or {}

    def Process(self, pid):
        if pid not in self.names:
            raise self.NoSuchProcess()
        return SimpleNamespace(name=lambda: self.names[pid])


class FakeAPI:
    enum_type = staticmethod(lambda fn: fn)
    class_name = "class"
    title = "title"

    def __init__(self, windows):
        self.windows = windows

    def enum(self, callback, _):
        for window in self.windows:
            if not callback(window["hwnd"], 0):
                break

    def get(self, hwnd):
        return next(w for w in self.windows if w["hwnd"] == hwnd)

    def desktop(self):
        return 1

    def shell(self):
        return 2

    def visible(self, hwnd):
        return self.get(hwnd).get("visible", True)

    def iconic(self, hwnd):
        return self.get(hwnd).get("iconic", False)

    def valid(self, hwnd):
        return self.get(hwnd).get("valid", True)

    def covers(self, hwnd, x, y):
        w = self.get(hwnd)
        return w.get("covers", True) and not w.get("cloaked", False)

    def process_id(self, hwnd):
        return self.get(hwnd).get("pid", 100), 456

    def text(self, method, hwnd):
        return self.get(hwnd).get(method, "")

    def bounds(self, hwnd, frame):
        return (-1920, -200, -920, 600)

    def get_long(self, hwnd, field):
        return self.get(hwnd).get("exstyle", 0) if field == -20 else 0

    def at_point(self, point):
        return self.windows[-1]["hwnd"]

    def ancestor(self, hwnd, mode):
        return hwnd

    def foreground(self):
        return self.windows[-1]["hwnd"]

    def enabled(self, hwnd):
        return self.get(hwnd).get("enabled", True)

    def owner(self, hwnd, mode):
        return None


def inspector(windows, include_explorer=False, names=None):
    obj = app.Inspector.__new__(app.Inspector)
    obj.api = FakeAPI(windows)
    obj.psutil = FakePS(names)
    obj.own_pid = 999
    obj.names = {}
    obj.args = argparse.Namespace(include_explorer=include_explorer)
    return obj


class WindowSelectionTests(unittest.TestCase):
    def test_own_tooltip_and_shell_are_ignored(self):
        obj = inspector([{"hwnd": 1}, {"hwnd": 2}, {"hwnd": 3, "pid": 999},
                         {"hwnd": 5}])
        self.assertEqual(obj.find(50, 50), 5)

    def test_taskbar_does_not_select_covered_window(self):
        obj = inspector([{"hwnd": 4, "class": "Shell_TrayWnd"}, {"hwnd": 5}])
        self.assertIsNone(obj.find(50, 50))

    def test_disabled_untitled_popup_wins_over_normal_window(self):
        obj = inspector([{"hwnd": 10, "enabled": False, "title": "", "exstyle": 0x20},
                         {"hwnd": 11, "title": "Covered app"}])
        self.assertEqual(obj.find(50, 50), 10)

    def test_invisible_minimized_cloaked_and_outside_are_ignored(self):
        windows = [{"hwnd": 10, "visible": False}, {"hwnd": 11, "iconic": True},
                   {"hwnd": 12, "cloaked": True}, {"hwnd": 13, "covers": False}]
        self.assertIsNone(inspector(windows).find(50, 50))

    def test_explorer_is_excluded_without_selecting_covered_window(self):
        windows = [{"hwnd": 10, "pid": 123}, {"hwnd": 11, "pid": 124}]
        obj = inspector(windows, names={123: "explorer.exe", 124: "notepad.exe"})
        self.assertIsNone(obj.find(50, 50))
        obj.args.include_explorer = True
        self.assertEqual(obj.find(50, 50), 10)

    def test_access_denied_does_not_remove_unknown_process(self):
        obj = inspector([{"hwnd": 10, "pid": 123}])
        def denied(pid):
            raise obj.psutil.AccessDenied()
        obj.psutil.Process = denied
        self.assertEqual(obj.find(50, 50), 10)

    def test_callback_exception_is_raised_outside_native_callback(self):
        obj = inspector([{"hwnd": 10}])
        obj.api.covers = lambda *args: 1 / 0
        with self.assertRaises(ZeroDivisionError):
            obj.find(0, 0)


class MetadataTests(unittest.TestCase):
    def test_topmost_long_command_and_64bit_handle(self):
        hwnd = 0x123456789ABC
        obj = inspector([{"hwnd": hwnd, "exstyle": 0x080800A8, "enabled": False}])
        obj.process_pid, obj.process_at = 100, 99
        long_command = "C:/App/app.exe " + "argument " * 1000
        obj.process = {"Process": "app.exe", "Command line": long_command}
        obj.data = obj.inspect(hwnd, -1000, 0, 100)
        self.assertIn("Yes", obj.data["Always on top"])
        self.assertEqual(obj.data["Enabled"], "No")
        self.assertEqual(obj.data["HWND"], "0x0000123456789ABC")
        self.assertIn("NOACTIVATE", obj.data["Extended flags"])
        self.assertEqual(obj.data["Size"], "1000 x 800 pixels")
        self.assertEqual(obj.data["Command line"], long_command)

    def test_field_access_denied_keeps_other_fields(self):
        ps = FakePS()
        def denied():
            raise ps.AccessDenied()
        process = SimpleNamespace(name=lambda: "app.exe", exe=denied,
                                  cmdline=lambda: ["C:\\Program Files\\App.exe", "a b"],
                                  cwd=lambda: "C:\\Work", username=denied, ppid=lambda: 42)
        ps.Process = lambda pid: process
        data = app.process_details(ps, 100)
        self.assertEqual(data["Process"], "app.exe")
        self.assertEqual(data["Executable"], "Unavailable (access denied)")
        self.assertIn('"a b"', data["Command line"])
        self.assertEqual(data["Parent PID"], "42")


class PlacementTests(unittest.TestCase):
    def test_monitor_layouts_and_edges(self):
        areas = [(0, 0, 1920, 1040), (-1920, -200, 0, 840), (0, -1080, 1920, 0)]
        for area in areas:
            for x in range(area[0], area[2], 97):
                for y in range(area[1], area[3], 91):
                    tx, ty = app.place(x, y, 570, 400, area)
                    self.assertGreaterEqual(tx, area[0])
                    self.assertGreaterEqual(ty, area[1])
                    self.assertLessEqual(tx + 570, area[2])
                    self.assertLessEqual(ty + 400, area[3])

    def test_small_monitor_clamp(self):
        self.assertEqual(app.place(-15, -15, 280, 200, (-300, -220, 0, 0)), (-300, -220))


class MouseTests(unittest.TestCase):
    def test_left_freezes_and_right_copies(self):
        gate = MouseGate()
        self.assertEqual(gate.handle(0x0201), (True, "freeze"))
        self.assertEqual(gate.handle(0x0202), (True, None))
        self.assertEqual(gate.handle(0x0204), (True, "copy"))
        self.assertEqual(gate.handle(0x0205), (True, None))
        self.assertEqual(gate.blocked, set())

    def test_pause_during_press_still_consumes_its_release(self):
        gate = MouseGate()
        gate.handle(0x0201)
        gate.enabled = False
        self.assertEqual(gate.handle(0x0202), (True, None))
        self.assertEqual(gate.handle(0x0201), (False, None))
        self.assertEqual(gate.handle(0x0202), (False, None))

    def test_enabling_mid_press_does_not_consume_unmatched_release(self):
        gate = MouseGate()
        gate.enabled = False
        self.assertEqual(gate.handle(0x0204), (False, None))
        gate.enabled = True
        self.assertEqual(gate.handle(0x0205), (False, None))

    def test_move_scroll_and_middle_button_pass_through(self):
        gate = MouseGate()
        for message in (0x0200, 0x0207, 0x0208, 0x020A, 0x020E):
            self.assertEqual(gate.handle(message), (False, None))


class FakeRoot:
    def __init__(self):
        self.clipboard = None
        self.copies = 0

    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, value):
        self.clipboard += value
        self.copies += 1


def state():
    obj = inspector([{"hwnd": 10}])
    obj.root = FakeRoot()
    obj.frozen, obj.frozen_capture, obj.job = False, None, None
    obj.data = {"Title": "Original", "PID / thread": "100 / 456"}
    obj.target = 10
    obj.notice, obj.notice_until = "", 0
    return obj


class CaptureTests(unittest.TestCase):
    def test_pause_releases_mouse_and_hides_tooltip(self):
        obj = state()
        obj.paused = False
        obj.mouse = SimpleNamespace(gate=MouseGate())
        hidden = []
        obj.root.withdraw = lambda: hidden.append(True)
        obj.root.deiconify = lambda: hidden.append(False)
        obj.toggle_pause()
        self.assertTrue(obj.paused)
        self.assertFalse(obj.mouse.gate.enabled)
        self.assertEqual(hidden, [True])
        obj.toggle_pause()
        self.assertFalse(obj.paused)
        self.assertTrue(obj.mouse.gate.enabled)
        self.assertEqual(hidden, [True, False])

    def test_paused_mouse_actions_do_not_freeze_or_copy(self):
        obj = state()
        obj.paused = True
        obj.copy_report = lambda *a: self.fail("Paused copy should do nothing")
        obj.toggle_freeze = lambda *a: self.fail("Paused freeze should do nothing")
        obj.handle_action("copy", (0, 0))
        obj.handle_action("freeze", (0, 0))

    def test_frozen_copy_never_requeries_target(self):
        obj = state()
        capture = Capture(10, 100, 456, dict(obj.data), report="immutable frozen report")
        obj.frozen, obj.frozen_capture = True, capture
        obj.select = lambda *a, **k: self.fail("Frozen copy must not select or re-read anything")
        obj.data = {"Title": "Changed live title"}
        obj.copy_report((999, 999))
        obj.copy_report((-100, -200))
        self.assertEqual(obj.root.clipboard, "immutable frozen report")
        self.assertEqual(obj.root.copies, 2)

    def test_copy_pending_snapshot_queues_and_copies_once_ready(self):
        obj = state()
        capture = Capture(10, 100, 456, dict(obj.data), future=Future())
        obj.job, obj.frozen, obj.frozen_capture = capture, True, capture
        obj.copy_report((0, 0))
        self.assertTrue(capture.copy_requested)
        self.assertIsNone(obj.root.clipboard)
        capture.future.set_result("frozen report")
        obj.poll_capture()
        obj.poll_capture()
        self.assertEqual(obj.root.clipboard, "frozen report")
        self.assertEqual(obj.root.copies, 1)

    def test_queued_copy_keeps_its_original_snapshot_after_resume(self):
        obj = state()
        capture = Capture(10, 100, 456, dict(obj.data), future=Future(), copy_requested=True)
        obj.job, obj.frozen, obj.frozen_capture = capture, True, capture
        obj.toggle_freeze((0, 0))
        self.assertFalse(obj.frozen)
        capture.future.set_result("original target report")
        obj.poll_capture()
        self.assertEqual(obj.root.clipboard, "original target report")

    def test_busy_capture_does_not_queue_unlimited_work(self):
        obj = state()
        obj.job = Capture(10, 100, 456, dict(obj.data), future=Future())
        self.assertIsNone(obj.start_capture())
        self.assertIn("being collected", obj.notice)

    def test_live_copy_uses_click_coordinates(self):
        obj = state()
        selected = []
        obj.select = lambda point, force: selected.append(point) or obj.data
        obj.start_capture = lambda **kwargs: True
        obj.copy_report((-123, 456))
        self.assertEqual(selected, [(-123, 456)])

    def test_capture_failure_is_explicit(self):
        obj = state()
        capture = Capture(10, 100, 456, dict(obj.data), future=Future(), copy_requested=True)
        capture.future.set_exception(RuntimeError("collector failed"))
        obj.job = capture
        obj.poll_capture()
        report = json.loads(obj.root.clipboard)
        self.assertIn("collector failed", report["capture_error"])
        self.assertEqual(report["selected_window"]["Title"], "Original")


class RichProcess:
    pid = 100

    def create_time(self):
        return 1000.0

    def is_running(self):
        return True

    def name(self):
        return "app.exe"

    def exe(self):
        return "C:\\app.exe"

    def cmdline(self):
        return ["C:\\app.exe", "x" * 50000]

    def environ(self):
        return {"EXAMPLE_VALUE": "y" * 50000}

    def memory_maps(self, grouped):
        return [namedtuple("Map", "path rss")("C:\\module.dll", index) for index in range(2000)]

    def net_connections(self, kind):
        return [namedtuple("Connection", "fd status")(-1, "ESTABLISHED")]

    def cpu_percent(self, interval):
        return 1.25

    def parent(self):
        return None

    def children(self, recursive):
        return []


class ReportTests(unittest.TestCase):
    def setUp(self):
        probe = patch.object(support, "collect_uia", return_value=support.unavailable("Synthetic test", status="not_collected"))
        probe.start()
        self.addCleanup(probe.stop)

    def native(self):
        return SimpleNamespace(window=lambda *a: {"property": "value"}, process=lambda *a: {"token": "High"},
                               executable=lambda *a: {"version": "1.0"})

    def test_reports_do_not_truncate_command_environment_or_arrays(self):
        process = RichProcess()
        ps = SimpleNamespace(Process=lambda pid: process)
        obj = inspector([{"hwnd": 10}])
        capture = Capture(10, 100, 456, {"Title": "App"}, expected_created=1000)
        report = json.loads(collect_report(obj.api, self.native(), ps, capture))
        self.assertEqual(len(report["process"]["cmdline"][1]), 50000)
        self.assertEqual(len(report["process"]["environ"]["EXAMPLE_VALUE"]), 50000)
        self.assertEqual(len(report["process"]["memory_maps_ungrouped"]), 2000)
        self.assertEqual(report["process"]["network_connections"][0]["status"], "ESTABLISHED")
        self.assertEqual(report["capture_started"], capture.started)
        self.assertIn("capture_completed", report)

    def test_unavailable_fields_do_not_hide_accessible_data(self):
        process = RichProcess()
        def denied():
            raise PermissionError("denied")
        process.environ = denied
        report = collect_process(None, process)
        self.assertEqual(report["name"], "app.exe")
        self.assertFalse(report["environ"]["available"])
        self.assertIn("PermissionError", report["environ"]["reason"])
        self.assertFalse(report["num_handles"]["available"])

    def test_pid_reuse_does_not_inspect_replacement(self):
        obj = inspector([{"hwnd": 10}])
        ps = SimpleNamespace(Process=lambda pid: RichProcess())
        native = self.native()
        native.window = lambda *a: self.fail("Reused process must not be inspected")
        capture = Capture(10, 100, 456, {}, expected_created=999.0)
        report = json.loads(collect_report(obj.api, native, ps, capture))
        self.assertIn("PID was reused", report["process"]["reason"])
        self.assertNotIn("native_process", report)

    def test_window_metadata_survives_denied_process_creation_time(self):
        obj = inspector([{"hwnd": 10}])
        process = RichProcess()
        def denied():
            raise PermissionError("denied")
        process.create_time = denied
        ps = SimpleNamespace(Process=lambda pid: process)
        report = json.loads(collect_report(obj.api, self.native(), ps, Capture(10, 100, 456, {})))
        self.assertEqual(report["window"]["property"], "value")
        self.assertEqual(report["process"]["name"], "app.exe")


    def test_missing_window_preserves_process_and_selection(self):
        obj = inspector([{"hwnd": 10, "valid": False}])
        ps = SimpleNamespace(Process=lambda pid: RichProcess())
        report = json.loads(collect_report(obj.api, self.native(), ps, Capture(10, 100, 456, {"Title": "Gone"})))
        self.assertEqual(report["selected_window"]["Title"], "Gone")
        self.assertFalse(report["window"]["available"])
        self.assertEqual(report["process"]["name"], "app.exe")


class TooltipTests(unittest.TestCase):
    def test_tooltip_never_shows_report_fields(self):
        obj = state()
        obj.args.width = 480
        obj.mouse = None
        obj.last_view = None
        obj.notice_until = 0
        obj.data.update({"Process": "app.exe", "Always on top": "Yes", "Executable": "C:\\app.exe",
                         "Command line": "COMMAND_SENTINEL", "User": "USER_SENTINEL",
                         "Window class": "CLASS_SENTINEL", "HWND": "HANDLE_SENTINEL"})
        class Label:
            text = ""
            def configure(self, **kwargs):
                self.text = kwargs.get("text", self.text)
        obj.labels = [Label(), Label(), Label()]
        obj.root.update_idletasks = lambda: None
        obj.root.winfo_reqheight = lambda: 190
        obj.root.geometry = lambda value: None
        obj.render((0, 0, 1920, 1040))
        visible = "\n".join(label.text for label in obj.labels)
        for marker in ("COMMAND_SENTINEL", "USER_SENTINEL", "CLASS_SENTINEL", "HANDLE_SENTINEL"):
            self.assertNotIn(marker, visible)
        self.assertIn("app.exe", visible)
        self.assertIn("Always on top", visible)
        self.assertNotIn("Details", visible)


class AvailabilityTests(unittest.TestCase):
    def test_missing_terminal_is_unsupported_without_attribute_error(self):
        fields = collect_process(None, RichProcess())
        self.assertEqual(fields["terminal"]["status"], "not_supported")
        self.assertNotIn("AttributeError", fields["terminal"]["reason"])
        self.assertEqual(fields["name"], "app.exe")

    def test_attribute_error_inside_supported_method_remains_a_query_error(self):
        def broken():
            raise AttributeError("internal implementation failed")
        result = support.process_query(SimpleNamespace(name=broken), "name")
        self.assertEqual(result["status"], "query_error")
        self.assertEqual(result["exception_type"], "AttributeError")

    def test_permission_failure_is_not_treated_as_unsupported(self):
        def denied():
            raise PermissionError("denied")
        self.assertEqual(support.read(denied)["status"], "access_denied")

    def test_not_implemented_is_unsupported(self):
        def missing():
            raise NotImplementedError("not available here")
        self.assertEqual(support.read(missing)["status"], "not_supported")

    def test_missing_package_identity_is_expected_absence(self):
        result = support.NativeDetails.identity(lambda *args: 15700, 100, "package_full_name")
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["error_name"], "APPMODEL_ERROR_NO_PACKAGE")
        self.assertEqual(result["winerror"], 15700)

    def test_missing_application_identity_is_expected_absence(self):
        result = support.NativeDetails.identity(lambda *args: 15703, 100, "application_user_model_id")
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["error_name"], "APPMODEL_ERROR_NO_APPLICATION")
        self.assertIn("separate mechanism", result["reason"])

    def test_identity_access_denied_preserves_failure_code(self):
        result = support.NativeDetails.identity(lambda *args: 5, 100, "package_full_name")
        self.assertEqual(result["status"], "access_denied")
        self.assertEqual(result["winerror"], 5)

    def test_identity_buffer_growth_and_success_preserve_full_value(self):
        value = "Example.Package.Application"
        calls = []
        def query(handle, length, buffer):
            calls.append(buffer is None)
            if buffer is None:
                length._obj.value = 2
                return 122
            if length._obj.value < len(value) + 1:
                length._obj.value = len(value) + 1
                return 122
            buffer.value = value
            return 0
        result = support.NativeDetails.identity(query, 100, "application_user_model_id")
        self.assertEqual(result, value)
        self.assertEqual(len(calls), 3)

    def layer(self, exstyle):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.api = SimpleNamespace(valid=lambda hwnd: True, get_long=lambda *args: exstyle)
        native.long_value = lambda getter, hwnd, index: hex(getter(hwnd, index))
        return native

    def test_plain_window_skips_inapplicable_layer_api(self):
        native = self.layer(8)
        native.layer_attributes = lambda *args: self.fail("Must not query a non-layered window")
        result = native.layered_window(100)
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["extended_style"], "0x8")
        self.assertIn("WS_EX_LAYERED", result["reason"])
        self.assertNotIn("winerror", result)

    def test_layered_error_87_does_not_claim_a_known_cause(self):
        native = self.layer(0x80008)
        native.layer_attributes = lambda *args: False
        with patch.object(C, "set_last_error", create=True), patch.object(C, "get_last_error", return_value=87, create=True):
            result = native.layered_window(100)
        self.assertEqual(result["status"], "query_unavailable")
        self.assertEqual(result["winerror"], 87)
        self.assertIn("exact cause is not determined", result["reason"])

    def test_layered_alpha_success_retains_values_and_applicability(self):
        native = self.layer(0x80008)
        def attributes(hwnd, color, alpha, flags):
            color._obj.value, alpha._obj.value, flags._obj.value = 123, 220, 2
            return True
        native.layer_attributes = attributes
        with patch.object(C, "set_last_error", create=True):
            result = native.layered_window(100)
        self.assertEqual(result["alpha"], 220)
        self.assertTrue(result["alpha_applies"])
        self.assertFalse(result["color_key_applies"])

    def test_windows_10_skips_windows_11_only_dwm_attributes(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        calls = []
        native.api = SimpleNamespace(attr=lambda hwnd, index, *args: calls.append(index) or 0)
        with patch.object(support, "windows_build", return_value=19045):
            result = native.dwm_attributes(100)
        self.assertEqual(calls, [1, 14, 15, 20])
        self.assertEqual(result["corner_preference"]["status"], "not_supported")
        self.assertEqual(result["corner_preference"]["required_windows_build"], 22000)
        self.assertEqual(result["system_backdrop_type"]["required_windows_build"], 22621)

    def test_windows_11_early_build_skips_backdrop_only(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        calls = []
        native.api = SimpleNamespace(attr=lambda hwnd, index, *args: calls.append(index) or 0)
        with patch.object(support, "windows_build", return_value=22000):
            result = native.dwm_attributes(100)
        self.assertIn(37, calls)
        self.assertNotIn(38, calls)
        self.assertEqual(result["corner_preference"], 0)

    def test_dwm_invalid_argument_retains_uncertainty_on_supported_build(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.api = SimpleNamespace(attr=lambda *args: -2147024809)
        with patch.object(support, "windows_build", return_value=22631):
            result = native.dwm_attributes(100)
        self.assertEqual(result["corner_preference"]["status"], "query_unavailable")
        self.assertEqual(result["corner_preference"]["HRESULT"], "0x80070057")

    def test_dwm_unknown_build_does_not_assume_unsupported(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        calls = []
        native.api = SimpleNamespace(attr=lambda hwnd, index, *args: calls.append(index) or 0x80070057)
        with patch.object(support, "windows_build", return_value=None):
            result = native.dwm_attributes(100)
        self.assertIn(38, calls)
        self.assertEqual(result["system_backdrop_type"]["status"], "query_unavailable")

    def test_status_summary_distinguishes_expected_absence_and_failures(self):
        report = {"fields": [support.unavailable("normal absence", status="not_applicable"),
                             support.unavailable("unsupported", status="not_supported"),
                             support.unavailable("denied", status="access_denied"),
                             support.unavailable("unexpected")], "success": [1, 2, 3]}
        result = support.field_status_summary(report)["unavailable_fields_by_status"]
        self.assertEqual(result, {"not_applicable": 1, "not_supported": 1, "access_denied": 1, "query_error": 1})

    def test_runtime_context_reports_platform_and_versions(self):
        result = support.runtime_context(SimpleNamespace(__version__="test-version"))
        self.assertEqual(result["psutil_version"], "test-version")
        self.assertIn("python", result)
        self.assertIn("windows", result)



if __name__ == "__main__":
    unittest.main(verbosity=2)
