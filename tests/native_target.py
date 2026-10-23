"""Synthetic Win32 controls for native integration tests; not an inspected user app."""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import os
import sys
import tkinter as tk
project = Path(__file__).resolve().parent
if not (project / "screen_app_inspect.py").exists():
    project = project.parent
sys.path.insert(0, str(project))
from screen_app_inspect import Windows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ready_file")
    args = parser.parse_args()
    api = Windows()
    api.dpi_aware()
    root = tk.Tk()
    root.title("Inspector Synthetic Test Window")
    root.geometry("480x260+100+100")
    root.update()
    hwnd = api.ancestor(root.winfo_id(), 2)
    user = C.WinDLL("user32", use_last_error=True)
    create = user.CreateWindowExW
    create.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int,
                       C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
    create.restype = W.HWND
    parent = root.winfo_id()
    checkbox = create(0, "BUTTON", "Verify checkbox", 0x50000003, 20, 50, 180, 30, parent, W.HMENU(101), None, None)
    edit = create(0, "EDIT", "Fixture text", 0x50800000, 20, 100, 250, 30, parent, W.HMENU(102), None, None)
    if not checkbox or not edit:
        raise OSError(C.get_last_error(), "Synthetic controls could not be created")
    user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.SendMessageW.restype = C.c_ssize_t
    user.SetFocus.argtypes = [W.HWND]
    user.SetFocus.restype = W.HWND
    user.SetCapture.argtypes = [W.HWND]
    user.SetCapture.restype = W.HWND
    user.GetCapture.argtypes = []
    user.GetCapture.restype = W.HWND
    original = None
    @api.proc_type
    def procedure(window, message, wparam, lparam):
        if message == 0x800A:
            user.SetFocus(edit)
            user.SetCapture(hwnd)
            return user.GetCapture() or 0
        return api.call_proc(original, window, message, wparam, lparam)
    original = api.set_long(hwnd, -4, C.cast(procedure, C.c_void_p).value)
    if not original:
        raise OSError(C.get_last_error(), "Synthetic target handler could not be installed")
    user.SendMessageW(checkbox, 0x00F1, 1, 0)
    root.update()
    user.SetFocus(edit)
    user.SetCapture(hwnd)
    box = api.bounds(checkbox, False)
    edit_box = api.bounds(edit, False)
    payload = {"hwnd": hwnd, "pid": os.getpid(), "thread": api.process_id(hwnd)[1],
               "checkbox": checkbox, "edit": edit, "point": [(box[0]+box[2])//2, (box[1]+box[3])//2],
               "edit_point": [(edit_box[0]+edit_box[2])//2, (edit_box[1]+edit_box[3])//2]}
    Path(args.ready_file).write_text(json.dumps(payload), encoding="utf-8")
    root.mainloop()


if __name__ == "__main__":
    main()
