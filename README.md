# Screen App Inspect 2.2

A mouse-operated window inspector for Windows. All interface text, code comments,
and included instructions are in English.

**[Download screen-app-inspect.zip](https://github.com/hoangthaiminh/My-Temporary-Repo/raw/refs/heads/main/screen-app-inspect.zip)**

Extract the entire ZIP, open Terminal in the extracted folder, and run:

```powershell
py -m pip install -r requirements.txt
py screen_app_inspect.py
```

Requires Windows 10/11 and Python 3.10+ with Tcl/Tk. Keep all three Python modules
together. `psutil` and Windows-only `comtypes` are installed by requirements.txt.
You can also use `run.cmd` after installation.

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
Version 2.2 adds monitor/work-area/DPI metadata, decoded DWM preferences, DLL module
addresses and versions, GDI/USER counts, configured power-throttling policy, extra
window/class storage, and read-only UI Automation properties/text/value/toggle
state. Focus/capture use the selected window's GUI thread, not the inspector's.
See [CAPABILITIES.md](CAPABILITIES.md) for exact sources and limitations.
Reports are not truncated. A frozen report is collected once and reused for later
copies. Unavailable/unsupported fields include error records. There is no general
API for every application's private internal properties; the report covers the
queries implemented by this tool.

Version 2.1 distinguishes `not_applicable`, `not_supported`, `access_denied`,
`query_unavailable` and unexpected `query_error` results. It skips unsupported
psutil getters and inapplicable layered-window queries, recognizes normal missing
MSIX/application identities, and checks OS-build requirements for recent DWM
attributes. Reports retain raw error codes and add runtime/build details plus a
field-status summary. An unavailable field does not automatically mean capture
failed. See `CHANGELOG.txt` and `README.txt` in the ZIP.

The click-through, non-activating overlay reasserts its topmost position periodically
and responds to foreground and window-order events. Windows still reserves higher
system/security bands, so an ordinary Python overlay cannot guarantee being above
every UIAccess/system surface or exclusive full-screen application. Administrator
rights alone do not grant UIAccess. See `README.txt` inside the ZIP for details.

Desktop/taskbar and Explorer windows are excluded by default. Use
`--include-explorer` to inspect Explorer app windows too. System-hosted notifications
may identify their rendering host rather than their sender.

UI Automation runs in an isolated, cancellable helper with a four-second budget.
Use `--uia-timeout 10` for a slower provider or `--no-uia` to skip this section.
The tooltip remains compact; copied/frozen JSON contains the collected details.

The repository includes source, tests, a package builder and GitHub Actions checks
for Python 3.10/3.13 on Ubuntu, Windows Server 2022 and Windows Server 2025.
The suite contains 64 logic/package tests plus four Windows-native integration
tests using synthetic controls (not user reports). See
[verification runs](https://github.com/hoangthaiminh/My-Temporary-Repo/actions/workflows/verify-inspector.yml)
for actual run status. Native tests cover Win32 metadata, UI Automation values,
GUI startup and hook cleanup. They do not certify every third-party application,
interactive mouse sequence, Windows edition, or privileged surface.

Run `verify.cmd` on Windows, or `python -m unittest discover -s tests -v` from a
terminal. Build the ZIP with `python tools/build_package.py`.
