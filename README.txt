SCREEN APP INSPECT 2.2

Requirements
------------
Windows 10/11; Python 3.10+ with Tcl/Tk; psutil 6.0+ and comtypes 1.4.8+.
64-bit Python is recommended on 64-bit Windows.

Quick start
-----------
1. Extract the entire ZIP. Keep all three Python files in the same folder.
2. Open Terminal in the extracted folder.
3. Install the dependency:
       py -m pip install -r requirements.txt
4. Start:
       py screen_app_inspect.py
   You can also double-click run.cmd after installing the dependency.

Controls
--------
Move the mouse over a visible application window.
LEFT CLICK:   Freeze the selection and collect its full report in the background.
LEFT AGAIN:   Resume live inspection.
RIGHT CLICK:  Copy a full report. While frozen, copy the saved frozen report.

While inspection is active, left/right button presses and releases are captured
globally and are NOT delivered to the underlying applications. Movement, wheel,
and middle-button events pass through. The tooltip itself is click-through.

Ctrl+Alt+M: Pause/resume inspection. While paused, the tooltip is hidden and
            normal mouse clicks are restored.
Ctrl+Alt+F: Freeze/resume (keyboard alternative).
Ctrl+Alt+C: Copy the full report (keyboard alternative).
Ctrl+Alt+Q: Quit and restore normal mouse behavior.

If a mouse hook cannot be installed, the tool falls back to the keyboard controls.
If a hotkey is unavailable, close another inspector instance or the app using it.

Compact tooltip
---------------
The tooltip always shows identification only: title, process, PID/thread,
topmost status, and executable path. There is no expanded-tooltip mode.
Its text is shortened to fit the screen; the copied report is not shortened.
Desktop, taskbar, and Explorer windows are excluded by default.

Full reports and freezing
-------------------------
Copied reports are formatted JSON text. Collection runs on a separate thread.
If you copy while collection is running, copying is queued and happens when that
specific report is ready. A frozen report is collected once and retained in
memory. Re-copying it never re-reads the application, even if it has changed or
closed. If you resume before a requested copy completes, that request still copies
the originally selected report. Only one collection runs at a time.

Included where available:
- Window title/class/handles, process/thread identity, window/client/frame bounds,
  placement, styles, extended flags, owner/parent/root, visibility, enabled state,
  menu/help IDs, class values, window properties, GUI-thread state, native child
  windows, layered-window settings, and DWM attributes.
- Process identity, arguments, executable, working directory, account, creation
  time, status, parent/children, threads, handles, CPU information, affinity,
  priority, memory counters/maps, I/O, open files, network connections, environment,
  and other supported psutil fields.
- Session, architecture, token elevation/integrity/UIAccess, package/application
  identity, executable file metadata and version-resource strings.
- Monitor/device identity, physical monitor/work-area rectangles, monitor scale,
  window-effective DPI, awareness and 96-DPI window size.
- Loaded EXE/DLL addresses, sizes, paths and versions, GDI/USER current/peak counts,
  configured power-throttling masks and raw window/class extra-storage DWORDs.
- UI Automation window/point/focused-element properties: Control Type, Automation
  ID, Name, Value, Toggle state and exposed document text where available.

Reports can include environment variables, open file paths, and network endpoints.
The tool only places reports on the local clipboard when you request a copy.

There is no universal API for every private property of every application. The
report contains all returned values from its implemented queries, with no display
truncation. Unsupported/denied/exited fields have individual availability/error
records. It does not dump arbitrary process memory, read browser DOMs or perform
OCR. UI Automation probes only the selected window, saved pointer element and
focused element within that window; it does not crawl the full accessibility tree.
Password values/text are not queried. Native child titles use GetWindowText;
custom-drawn control text may be unavailable.

UI Automation is isolated in a cancellable helper process. A provider timeout
does not hold the tooltip or native report hostage. The default budget is four
seconds; first-time COM type-library generation may be slower. --uia-timeout 10
allows more time; --no-uia skips UIA. Missing/incomplete app accessibility support
is reported explicitly. Electron content is only as rich as its exposed provider.
See CAPABILITIES.md for sources, semantics and limits of every added group.

Reading availability statuses (2.1)
----------------------------------
An unavailable field is not automatically a failed capture. Each structured
record includes a status, reason and any available original Windows/HRESULT code:
- not_applicable: the property does not apply, such as MSIX identity on an
  unpackaged process or constant-alpha/color-key values on a non-layered window.
- not_supported: the installed OS build or psutil API does not support the field.
- access_denied: permissions prevented the query.
- query_unavailable: the API rejected a query but its exact cause is unresolved.
- query_error: an unexpected query/code error; this is not silently relabeled as
  unsupported. window_gone/process_exited/identity_changed describe lifecycle changes.

The report includes a field_status_summary and runtime information: Windows
version/build, Python, psutil, machine type and inspector pointer width.
psutil.Process.terminal is not implemented on Windows and is reported as
not_supported without calling a missing method. Known Windows 11-only DWM
attributes are skipped on older detected builds. An E_INVALIDARG on an otherwise
supported/unknown build is retained as query_unavailable, with no guessed cause.
APPMODEL_ERROR_NO_PACKAGE (15700) and APPMODEL_ERROR_NO_APPLICATION (15703) are
expected absence of the identity exposed by those APIs; they are not permissions
errors. Shell/window AppUserModelID properties are a separate identity mechanism.
GetLayeredWindowAttributes is queried only for windows with WS_EX_LAYERED. This
API does not measure arbitrary per-pixel/compositor transparency.

A report is a best-effort capture over the interval recorded in capture_started
and capture_completed, not an atomic OS snapshot. Freeze pins the target and its
report at selection time, but individual APIs return values as they are queried.
Metadata already inaccessible when collection starts cannot be reconstructed.
Process creation times help reject a reused PID.

Overlay behavior
----------------
The overlay is layered, click-through, non-activating, and topmost. It reasserts
its position periodically and reacts to other processes' foreground/show/hide/
reorder events. It does not activate or change the inspected application.

This improves visibility above ordinary always-on-top windows. Another application
may briefly cover it between updates. Windows has higher privileged/system Z-order
bands: this ordinary Python process cannot guarantee visibility above UIAccess
windows, secure/UAC desktops, protected system surfaces, hardware overlays, or
exclusive full-screen rendering. Running as administrator does not grant UIAccess.
A properly packaged, signed, trusted-location UIAccess executable is a separate
deployment option; this ZIP does not change UIAccess, registry policy, or security.
Drawing directly onto the desktop is not a reliable compositor-independent fix.

Options
-------
py screen_app_inspect.py --keyboard-only
py screen_app_inspect.py --start-paused
py screen_app_inspect.py --include-explorer
py screen_app_inspect.py --interval 50 --width 480
py screen_app_inspect.py --uia-timeout 10
py screen_app_inspect.py --no-uia

--keyboard-only leaves all mouse buttons available to applications.
--start-paused waits for Ctrl+Alt+M before showing the overlay/capturing clicks.
--include-explorer includes Explorer app windows; desktop/taskbar stay excluded.

Other limits
------------
Elevated or protected processes can deny metadata access. Starting Terminal as
administrator can expose more fields, but protected data may remain inaccessible.
Notification windows can belong to a system host that renders another app's
notification; the owning process does not always identify the original sender.
Hit detection uses Z order, DWM bounds, and window regions. Per-pixel alpha holes
and unusual compositor overlays can be approximate. Other sessions and secure
desktops are outside the supported inspection surface.

Validation
----------
The included suite has 64 logic/package tests and four Windows-native integration
tests. Native tests start synthetic controls and verify monitor/DPI, GUI-thread
capture, process modules/resources/power metadata, UIA checkbox/edit values, GUI
startup and hook cleanup. Logic tests also cover provider timeout/cancellation,
frozen-copy correctness, API applicability and unabridged metadata.
Run verify.cmd on Windows after dependency installation. Linux runs skip the four
native tests. GitHub Actions runs the suite on Python 3.10/3.13 and Ubuntu,
Windows Server 2022 and Windows Server 2025; inspect Actions for actual results.
These checks do not guarantee behavior with every app/provider or Windows desktop.
The package builder verifies a fixed file inventory, every source payload, Python
syntax and ZIP integrity. No user's exported report is included.
