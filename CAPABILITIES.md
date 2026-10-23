# Inspection coverage

Reports contain unabridged values from the implemented read-only APIs. A field's
absence, unsupported status, denied access, or timeout is recorded explicitly.

| Requested metadata | Source | Interpretation and limits |
| --- | --- | --- |
| Monitor device/name and work area | MonitorFromWindow, GetMonitorInfoW, EnumDisplayDevicesW | Physical screen rectangles, including negative origins; taskbar-excluded work area |
| DPI/scaling | GetDpiForWindow, GetScaleFactorForMonitor, DPI-awareness APIs | Window-effective DPI and monitor scale are separate; unaware apps can return 96 on a scaled display |
| UIA Control Type, Automation ID, Name | IUIAutomation window, saved-point and focused elements | Provider-dependent; only elements inside the selected window are described |
| UIA Value/Toggle/document text | Value/Toggle/Text properties and TextPattern | No invoke/set/toggle actions; unsupported patterns are recorded; password value/text are not queried |
| Corner preference and backdrop | DwmGetWindowAttribute | Numeric value plus decoded name, with OS-build support checks; not a guarantee of the final visual result |
| Layer alpha/color-key | WS_EX_LAYERED then GetLayeredWindowAttributes | Non-layered windows are not_applicable; compositor/per-pixel opacity is not inferred |
| Focus and mouse capture | GetGUIThreadInfo(selected GUI thread) | Handles and membership in the selected window; not a system-wide exclusivity claim |
| Loaded EXE/DLL modules | EnumProcessModulesEx, GetModuleInformation, GetModuleFileNameExW | Base address, image size, entry point, path and version resource; loader changes/cross-bitness/access restrictions may limit results |
| Package full name/application ID | GetPackageFullName, GetApplicationUserModelId | Unpackaged/no-application identity is expected absence; shell/window AUMID properties are a separate mechanism |
| Integrity and token elevation | OpenProcessToken, GetTokenInformation | Integrity RID/level, elevation type, elevated and UIAccess flags where readable |
| GDI/USER object counts | GetGuiResources | Current and peak counts; a single sample does not establish a resource leak |
| Power throttling/EcoQoS | GetProcessInformation(ProcessPowerThrottling) | Raw control/state masks and configured execution-speed policy; does not measure all effective OS throttling |
| User data and extra storage | GetWindowLongPtrW, GetWindowLongW, GetClassLongW | Raw API-accessible values only, no pointer dereference; extra storage read as DWORDs with offsets/unread trailing-byte count |
| Process/resources/files/connections | psutil plus Win32 | Existing full process metadata, arguments, environment, memory maps, handles, CPU/I/O and network endpoints |

UI Automation runs in a short-lived, separate process. A slow/hung provider is
terminated after its configurable budget (default four seconds), while the native
report remains available. Closing the inspector cancels its owned UIA probe.
This prevents provider hangs from blocking the tooltip or creating an unbounded
report queue. Provider startup/type-library generation can be slower on the first
run; `--uia-timeout 10` allows more time and `--no-uia` skips this section.

UIA text/value metadata describes exposed accessibility elements, not an Electron
DOM or arbitrary application-private objects. A minimal accessibility provider can
return only a top-level element even when the screen contains custom-drawn content.
No full accessibility-tree crawl, OCR, code injection, arbitrary memory dump, or
application mutation is performed.

Frozen reports are captures over a recorded time interval, not atomic OS snapshots.
The chosen HWND/PID/thread/creation time and pointer location are pinned. Subsequent
copies reuse exactly the stored JSON, including text/value metadata and timestamps.
