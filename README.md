# Screen App Inspect 2.0

A mouse-operated window inspector for Windows. All interface text, code comments,
and included instructions are in English.

**[Download screen-app-inspect.zip](https://github.com/hoangthaiminh/My-Temporary-Repo/raw/refs/heads/main/screen-app-inspect.zip)**

Extract the entire ZIP, open Terminal in the extracted folder, and run:

```powershell
py -m pip install -r requirements.txt
py screen_app_inspect.py
```

Requires Windows 10/11 and Python 3.10+ with Tcl/Tk. Keep `screen_app_inspect.py`
and `inspect_support.py` together. You can also use `run.cmd` after installation.

| Control | Action |
| --- | --- |
| Left-click | Freeze the selected window; click again to resume |
| Right-click | Copy an unabridged JSON report; frozen mode uses the saved report |
| Ctrl+Alt+M | Pause inspection and restore normal mouse clicks; press again to resume |
| Ctrl+Alt+F / Ctrl+Alt+C | Keyboard alternatives for freeze / copy |
| Ctrl+Alt+Q | Quit |

**While inspection is active, left/right clicks are consumed so the underlying
application is not clicked.** Use `--keyboard-only` to leave mouse buttons alone,
or `--start-paused` to enable inspection later with Ctrl+Alt+M.

The tooltip always contains basic identification only. Full reports are collected
in the background and include window/class/child-window properties, process
arguments and environment, executable/version metadata, token/session details,
CPU/memory/I/O information, threads, files, and network connections where readable.
Reports are not truncated. A frozen report is collected once and reused for later
copies. Unavailable/unsupported fields include error records. There is no general
API for every application's private internal properties; the report covers the
queries implemented by this tool.

The click-through, non-activating overlay reasserts its topmost position periodically
and responds to foreground and window-order events. Windows still reserves higher
system/security bands, so an ordinary Python overlay cannot guarantee being above
every UIAccess/system surface or exclusive full-screen application. Administrator
rights alone do not grant UIAccess. See `README.txt` inside the ZIP for details.

Desktop/taskbar and Explorer windows are excluded by default. Use
`--include-explorer` to inspect Explorer app windows too. System-hosted notifications
may identify their rendering host rather than their sender.

Python syntax and **29 mocked logic tests passed on Linux**. The actual native
Windows integration and interactive GUI have not been run in this environment.
