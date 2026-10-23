"""Integration tests against real Windows APIs and synthetic controls."""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import inspect_support as support
from screen_app_inspect import Inspector, Windows


@unittest.skipUnless(sys.platform == "win32", "Native integration requires Windows")
class WindowsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psutil
        cls.psutil = psutil
        cls.temp = tempfile.TemporaryDirectory()
        ready = Path(cls.temp.name) / "ready.json"
        fixture = Path(__file__).with_name("native_target.py")
        # The synthetic target does not inherit CI secrets or arbitrary user environment.
        allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env["INSPECTOR_SYNTHETIC_FIXTURE"] = "1"
        cls.child = subprocess.Popen([sys.executable, str(fixture), str(ready)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        deadline = time.monotonic() + 20
        while not ready.exists():
            if cls.child.poll() is not None:
                error = cls.child.stderr.read().decode("utf-8", errors="replace")
                raise RuntimeError("Synthetic target exited: " + error)
            if time.monotonic() >= deadline:
                cls.child.kill()
                raise RuntimeError("Synthetic target startup timed out")
            time.sleep(0.1)
        cls.target = json.loads(ready.read_text(encoding="utf-8"))
        cls.api = Windows()
        cls.api.dpi_aware()
        cls.native = support.NativeDetails(cls.api)

    @classmethod
    def tearDownClass(cls):
        if cls.child.poll() is None:
            cls.child.terminate()
        cls.child.communicate(timeout=5)
        cls.temp.cleanup()

    def test_native_window_monitor_dpi_focus_capture(self):
        # Reacquire on the target's own thread, then compare with the cross-thread
        # inspector result. OS capture state may have changed since fixture startup.
        send = self.api.user.SendMessageW
        send.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        send.restype = C.c_ssize_t
        captured = send(self.target["hwnd"], 0x800A, 0, 0)
        result = self.native.window(self.target["hwnd"], self.target["thread"])
        monitor = result["monitor_and_DPI"]
        self.assertTrue(monitor["device_name"].startswith("\\\\.\\DISPLAY"), monitor)
        self.assertGreaterEqual(monitor["window_dpi"], 96)
        self.assertEqual(len(monitor["work_area_rect_physical_pixels"]), 4)
        gui = result["GUI_thread"]
        self.assertEqual(gui["capture"], hex(captured))
        self.assertEqual(gui["capture_within_selected_window"], captured == self.target["hwnd"])
        self.assertIn("extra_storage", result)

    def test_native_process_modules_resources_power(self):
        result = self.native.process(self.target["pid"])
        self.assertIn("token", result)
        self.assertIn("integrity", result["token"])
        self.assertIsInstance(result["GUI_resources"]["GDI"], int)
        self.assertIsInstance(result["GUI_resources"]["USER"], int)
        modules = result["loaded_modules"]
        self.assertTrue(modules["enumeration_complete"], modules)
        self.assertGreater(modules["module_count"], 0)
        self.assertTrue(any(str(module.get("path", "")).lower().endswith(".dll") for module in modules["modules"]))
        self.assertTrue(all("base_address" in module for module in modules["modules"]))
        power = result["power_throttling"]
        if power.get("available") is False:
            self.assertIn(power["status"], ("not_supported", "query_unavailable"))
            self.assertIn(power.get("winerror"), (50, 87, 120))
        else:
            self.assertIsInstance(power["control_mask"], int)
            self.assertIsInstance(power["state_mask"], int)

    def test_uia_checkbox_and_edit_values(self):
        capture = support.Capture(self.target["hwnd"], self.target["pid"], self.target["thread"], {},
            point=tuple(self.target["point"]), expected_created=self.psutil.Process(self.target["pid"]).create_time(),
            uia_timeout=15)
        result = support.collect_uia(capture)
        self.assertIn("window_element", result, result)
        element = result["element_at_capture_point"]
        self.assertEqual(element["control_type"], "CheckBox", element)
        self.assertEqual(element["toggle_state"], 1)
        self.assertEqual(element["name"], "Verify checkbox")
        self.assertEqual(element["automation_id"], "101")
        capture.point = tuple(self.target["edit_point"])
        edit_result = support.collect_uia(capture)
        edit = edit_result["element_at_capture_point"]
        self.assertEqual(edit["control_type"], "Edit", edit)
        self.assertEqual(edit["value"], "Fixture text")
        self.assertEqual(edit["automation_id"], "102")

    def test_compact_gui_startup_and_hook_cleanup(self):
        import tkinter as tk
        args = argparse.Namespace(start_paused=True, keyboard_only=True, include_explorer=False,
                                  width=480, interval=50, no_uia=True, uia_timeout=4)
        app = Inspector(self.api, self.psutil, tk, args)
        try:
            app.root.update_idletasks()
            app.toggle_pause()
            app.root.update()
            self.assertFalse(app.paused)
            self.assertTrue(self.api.valid(app.hwnd))
        finally:
            app.close()
        self.assertFalse(self.api.valid(app.hwnd))
        import queue
        hook = support.MouseHook(queue.SimpleQueue())
        hook.gate.enabled = False
        hook.start()
        hook.stop()
        self.assertFalse(hook.thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
