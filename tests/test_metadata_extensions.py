"""Regression tests for display, process and isolated UI Automation metadata."""
from __future__ import annotations
import ctypes as C
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import inspect_support as support
import inspect_uia as uia


class DisplayTests(unittest.TestCase):
    def native(self, dpi=144):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.api = SimpleNamespace(bounds=lambda *args: (-1800, -50, -900, 550))
        native.window_monitor = lambda *args: 100
        def monitor(handle, pointer):
            value = pointer._obj
            value.device = "\\\\.\\DISPLAY2"
            value.monitor.left, value.monitor.top, value.monitor.right, value.monitor.bottom = -1920, -100, 0, 980
            value.work.left, value.work.top, value.work.right, value.work.bottom = -1920, -100, 0, 940
            return True
        native.monitor_details = monitor
        def device(name, index, pointer, flags):
            pointer._obj.description = "Synthetic display"
            pointer._obj.name = "DISPLAY_DEVICE_2"
            return True
        native.display_device = device
        native.monitor_scale = lambda handle, factor: setattr(factor._obj, "value", 150) or 0
        native.window_dpi = lambda hwnd: dpi
        native.dpi_context = lambda hwnd: 1
        native.dpi_awareness = lambda context: 2 if dpi != 96 else 0
        return native

    def test_monitor_rects_and_dpi_size_conversion(self):
        result = self.native().monitor_and_dpi(123)
        self.assertEqual(result["device_name"], "\\\\.\\DISPLAY2")
        self.assertEqual(result["work_area_rect_physical_pixels"], [-1920, -100, 0, 940])
        self.assertEqual(result["window_scale_percent"], 150)
        self.assertEqual(result["window_size_at_96_dpi"], [600, 400])
        self.assertEqual(result["monitor_scale_percent"], 150)

    def test_unaware_window_dpi_is_not_confused_with_monitor_scale(self):
        result = self.native(96).monitor_and_dpi(123)
        self.assertEqual(result["window_scale_percent"], 100)
        self.assertEqual(result["monitor_scale_percent"], 150)
        self.assertEqual(result["window_dpi_awareness"], "Unaware")

    def test_unavailable_dpi_api_does_not_invent_a_scale(self):
        native = self.native()
        native.window_dpi = None
        result = native.monitor_and_dpi(123)
        self.assertEqual(result["window_dpi"]["status"], "not_supported")
        self.assertNotIn("window_scale_percent", result)


class ProcessExtensionTests(unittest.TestCase):
    def test_power_policy_system_managed_is_not_reported_as_disabled(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.process_information = lambda *args: True
        result = native.power_throttling(10)
        self.assertFalse(result["execution_speed_controlled"])
        self.assertIsNone(result["execution_speed_throttling_enabled"])
        self.assertIn("System-managed", result["execution_speed_policy"])

    def test_power_execution_speed_mask_is_decoded(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        def information(handle, kind, pointer, length):
            self.assertEqual(kind, 4)
            pointer._obj.control_mask, pointer._obj.state_mask = 5, 1
            return True
        native.process_information = information
        result = native.power_throttling(10)
        self.assertTrue(result["execution_speed_throttling_enabled"])
        self.assertTrue(result["ignore_timer_resolution_controlled"])
        self.assertFalse(result["ignore_timer_resolution_enabled"])

    def test_zero_gui_resource_count_can_be_valid(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.open_process = lambda *args: 100
        closed, flags = [], []
        native.close_handle = lambda handle: closed.append(handle)
        native.gui_resources = lambda handle, flag: flags.append(flag) or 0
        with patch.object(C, "set_last_error", create=True), patch.object(C, "get_last_error", return_value=0, create=True):
            result = native.resource_counts(1)
        self.assertEqual(result["GDI"], 0)
        self.assertEqual(result["USER_peak"], 0)
        self.assertEqual(flags, [0, 1, 2, 4])
        self.assertEqual(closed, [100])

    def test_extra_storage_preserves_offsets_and_unread_tail(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.class_long = lambda hwnd, index: {-18: 6, -20: 4}[index]
        native.long_value = lambda fn, hwnd, index: hex(fn(hwnd, index))
        native.window_long32 = lambda hwnd, offset: 0xFFFFFFFF
        native.class_long32 = lambda hwnd, offset: 0x1234
        with patch.object(C, "set_last_error", create=True), patch.object(C, "get_last_error", return_value=0, create=True):
            result = native.extra_storage(10)
        self.assertEqual(result["window"]["DWORD_values"], [{"byte_offset": 0, "DWORD": "0xFFFFFFFF"}])
        self.assertEqual(result["window"]["trailing_bytes_not_read"], 2)
        self.assertEqual(result["class"]["DWORD_values"][0]["DWORD"], "0x00001234")

    def test_module_buffer_growth_and_version_cache(self):
        native = support.NativeDetails.__new__(support.NativeDetails)
        native.open_process = lambda *args: 1
        native.close_handle = Mock()
        calls, versions = [], []
        def enumerate_modules(handle, array, size, needed, flags):
            calls.append(len(array))
            needed._obj.value = 300 * C.sizeof(C.c_void_p)
            for index in range(min(300, len(array))):
                array[index] = index + 4096
            return True
        native.enum_modules = enumerate_modules
        def module_path(handle, module, buffer, length):
            buffer.value = "C:\\Synthetic.dll"
            return len(buffer.value)
        native.module_path = module_path
        def info(handle, module, pointer, length):
            pointer._obj.base, pointer._obj.image_bytes = module, 4096
            return True
        native.module_info = info
        native.file_version = lambda path: versions.append(path) or {"file_version": "1.0"}
        result = native.loaded_modules(10)
        self.assertTrue(result["enumeration_complete"])
        self.assertEqual(result["module_count"], 300)
        self.assertEqual(calls, [256, 300])
        self.assertEqual(len(versions), 1)
        self.assertEqual(result["modules"][0]["base_address"], "0x1000")
        native.close_handle.assert_called_once_with(1)


class UIAPropertyTests(unittest.TestCase):
    def test_disabled_and_unsupported_com_errors_are_distinct(self):
        class ProviderError(Exception):
            pass
        error = ProviderError("Provider state")
        error.hresult = 0x80040200
        self.assertEqual(uia.failure(error)["status"], "query_unavailable")
        error.hresult = 0x80040204
        self.assertEqual(uia.failure(error)["status"], "not_supported")

    def element(self, values):
        element = Mock()
        element.GetCurrentPropertyValueEx.side_effect = lambda identifier, ignore: values.get(identifier, False)
        return element

    def test_value_toggle_and_control_type(self):
        element = self.element({30003: 50004, 30005: "Example", 30011: "102", 30019: False,
                                30043: True, 30041: True, 30045: "Complete\ntext", 30086: 2})
        result = uia.describe(Mock(), element, SimpleNamespace())
        self.assertEqual(result["control_type"], "Edit")
        self.assertEqual(result["value"], "Complete\ntext")
        self.assertEqual(result["toggle_state_name"], "Indeterminate")

    def test_password_value_is_never_queried(self):
        element = self.element({30003: 50004, 30019: True, 30043: True, 30040: True})
        result = uia.describe(Mock(), element, SimpleNamespace())
        identifiers = [call.args[0] for call in element.GetCurrentPropertyValueEx.call_args_list]
        self.assertNotIn(30045, identifiers)
        self.assertEqual(result["value"]["status"], "not_collected")
        element.GetCurrentPattern.assert_not_called()

    def test_parent_cycle_does_not_loop_forever(self):
        element, root = object(), object()
        automation = SimpleNamespace(CompareElements=lambda a, b: a is b,
                                     RawViewWalker=SimpleNamespace(GetParentElement=lambda value: element))
        self.assertFalse(uia.belongs_to(automation, element, root))

    def test_descendant_scope_does_not_accept_another_window(self):
        child, root, other = object(), object(), object()
        parent = {child: root, root: None, other: None}
        automation = SimpleNamespace(CompareElements=lambda a, b: a is b,
                                     RawViewWalker=SimpleNamespace(GetParentElement=lambda value: parent[value]))
        self.assertTrue(uia.belongs_to(automation, child, root))
        self.assertFalse(uia.belongs_to(automation, other, root))


class UIAIsolationTests(unittest.TestCase):
    def test_timeout_kills_helper_and_cleans_registry(self):
        child = Mock()
        child.communicate.side_effect = [subprocess.TimeoutExpired("probe", 1), ("", "")]
        capture = support.Capture(10, 20, 30, {}, uia_timeout=1)
        with patch.object(support.sys, "platform", "win32"), patch.object(support.subprocess, "Popen", return_value=child):
            result = support.collect_uia(capture)
        self.assertEqual(result["status"], "timeout")
        child.kill.assert_called_once()
        self.assertEqual(child.communicate.call_count, 2)
        self.assertEqual(support._PROBES, {})

    def test_cancelled_capture_does_not_spawn_a_helper(self):
        capture = support.Capture(10, 20, 30, {})
        capture.cancelled.set()
        with patch.object(support.sys, "platform", "win32"), patch.object(support.subprocess, "Popen") as spawn:
            result = support.collect_uia(capture)
        spawn.assert_not_called()
        self.assertEqual(result["status"], "not_collected")

    def test_helper_result_and_saved_coordinates(self):
        child = Mock(returncode=0)
        child.communicate.return_value = ('{"window_element":{"control_type":"Pane"}}', "")
        capture = support.Capture(10, 20, 30, {}, point=(-123, 456), expected_created=1000)
        with patch.object(support.sys, "platform", "win32"), patch.object(support.subprocess, "Popen", return_value=child) as spawn:
            result = support.collect_uia(capture)
        self.assertEqual(result["window_element"]["control_type"], "Pane")
        arguments = spawn.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--x") + 1], "-123")
        self.assertEqual(arguments[arguments.index("--created") + 1], "1000")

    def test_cancellation_only_kills_the_owned_probe(self):
        first, second = support.Capture(1, 1, 1, {}), support.Capture(2, 2, 2, {})
        owned, other = Mock(), Mock()
        owned.poll.return_value = other.poll.return_value = None
        support._PROBES.update({owned: first, other: second})
        try:
            support.cancel_capture(first)
            owned.kill.assert_called_once()
            other.kill.assert_not_called()
        finally:
            support._PROBES.clear()


class PackagingTests(unittest.TestCase):
    def builder(self):
        path = Path(__file__).resolve().parents[1] / "tools/build_package.py"
        spec = importlib.util.spec_from_file_location("inspector_package_builder", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_package_is_repeatable_and_has_no_private_attachments(self):
        builder = self.builder()
        first, second = builder.build(), builder.build()
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "package.zip"
            path.write_bytes(first)
            builder.verify(path)
        with ZipFile(BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), set(builder.FILES))
            self.assertFalse(any("attachment" in name or "Zalo" in name for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
