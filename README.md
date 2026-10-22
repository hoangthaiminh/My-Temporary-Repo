# Screen App Inspect

A hover inspector for visible Windows application windows, including native popups and notifications. The script and its interface are in English.

[Download screen-app-inspect.zip](https://github.com/hoangthaiminh/My-Temporary-Repo/raw/refs/heads/main/screen-app-inspect.zip)

Extract the ZIP, open Terminal in the extracted folder, then run:

```powershell
py -m pip install -r requirements.txt
py screen_app_inspect.py
```

Requires Windows 10/11 and Python 3.10+ with Tcl/Tk. After installing the dependency, you can also double-click `run.cmd`.

- **Ctrl+Alt+D:** toggle expanded details.
- **Ctrl+Alt+F:** freeze or resume the displayed snapshot.
- **Ctrl+Alt+C:** copy the full report.
- **Ctrl+Alt+Q:** quit.

The tooltip shows the title, process, PID/thread, executable path, window class, and topmost status. Expanded details include the reconstructed command line, working directory, owner HWND, bounds, and window styles. Desktop, taskbar, and Explorer windows are excluded by default. Use `--include-explorer` to include Explorer application windows.

See `README.txt` inside the ZIP for options and limitations. Some process fields require elevated permissions; system-hosted notifications may identify their rendering host instead of their sender. Per-pixel transparent overlays can produce approximate results.

Python syntax and 12 mocked logic tests passed on Linux. The native Windows integration and interactive GUI have not been run in this environment.
