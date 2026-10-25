printer_monitor_v13 - printer data + vibration + camera web page (home network)

v13 = v12 + heater power plot (hotend / bed, % of 127 from M105 @: / B@:)
under the temperature chart, same time window, with window averages.
Only index.html changed (python/ and sketch/ otherwise as v12 / v9).

v12 = v11 +
  * nozzle path: an unreadable M27 reply no longer counts as "print ended"
    (it cleared the path + filament counter); print ends after 3 explicit
    idle replies. M114 rate selectable on the page (2/5/8/15 Hz, default 8
    as phase 1) with achieved rate and reply time. Path and temperature
    windows use the board's clock, not the viewing device's.
  * camera: the camera's own MJPEG frames are passed through (no decode /
    re-encode), so 1080p costs almost no CPU. Resolution 480p/720p/1080p
    selectable on the page; missing Huffman tables are added for browsers
    that need them; live board-time clock on the video. Re-encode is used
    only if raw MJPEG is not available.
  * settings (camera resolution, fps, M114 rate) saved in python/settings.json.
Sketch unchanged from v9; use with ch340_bridge_v3.

v11 = v10 + the bed mesh is checked for completeness (rows 0..n-1 against
the M420 V header) and re-read up to 3 times; the page marks a partial mesh
instead of showing NaN. Use with ch340_bridge_v3 (v2 lost bytes in bursts).
Sketch unchanged from v9.

v10 = v9 + printer.py talks to ch340_bridge_v1 (TCP 7010 on the board) when
there is no /dev/ttyUSB* - the UNO Q kernel has no CH340 driver. Install the
bridge once (see ch340_bridge_v1/README.txt), then restart the app.
Override the bridge address with environment PRINTER_BRIDGE=host:port.
Sketch unchanged from v9.

v9 = v8 + the 3D printer's USB cable (on the hub) is read by python/printer.py:
the same READ-ONLY outputs as phase 1 (printer_monitor_v05 / gateway_v1):
temperatures + targets + heater power, planned vs motor X/Y/Z/E, mesh Z
correction, lead, SD job/progress/file, print time, slicer estimate, ETA
(2 methods) + finish time, speed/flow, filament used, endstop + filament +
runout / soft-endstop / power-loss / levelling lamps, Z offset, bed mesh with
tilt fit, settings (M115/M503), SD file list (idle), serial log, alerts.
Only query commands are sent (allow-list in printer.py; G28 etc. refused).
Printer CSV (/printer.csv) uses the phase 1 column layout.
Accel #1 = table, #2 = toolhead; OLED shows TABLE / HEAD.
PSDs mark the input shaper frequencies read from M593.
Only one program can own the printer's serial port: close the phase 1
monitor/gateway if they run elsewhere on the same cable.
Force a port with environment PRINTER_PORT=/dev/ttyUSB0 (default: auto CH340).

v8 = v7 + the camera runs inside the app: pressing Run in App Lab starts
the vibration stream AND the camera (served on /stream, port 7001).
Stop cam_stream.py first - only one program can hold the camera.
If the app cannot open the camera, the page shows why and falls back to
cam_stream.py on port 8080.

v7 = v6 + Bridge.begin() restored before Monitor.begin() (v6 hung at
"1 starting Bridge"). v6 fixed the v5 freeze (no blocking Bridge.call,
loop yields, smaller messages). OLED bottom row "tx.. rx.. |/-\\" spins
while the sketch is alive; during start-up row 3 shows the step number.

Files
  app.yaml               app definition, exposes port 7001
  sketch/sketch.ino      MCU: streams both ADXL345 FIFOs over the Bridge
  sketch/stream_types.h  types used by the sketch
  sketch/oled_text.h     OLED text driver (same as v3/v4)
  python/main.py         Linux: receives blocks, camera, serves the web page
  python/printer.py      Linux: printer USB serial monitor (read-only)
  python/index.html      the page (traces, Grms, PSD, camera, CSV)

Install (copies the whole app to the board, no pasting)
  From PowerShell in ...\arduino_uno_q :
    scp -r printer_monitor_v13 arduino@<board-name>.local:~/ArduinoApps/
  Then in App Lab -> My Apps -> printer_monitor_v13 -> Run.
  Library needed: Arduino_RouterBridge only (no Adafruit libraries).

Camera
  Opened by the app itself (first USB camera), MJPEG passthrough.
  Resolution 480p / 720p / 1080p on the page (saved); 1080p MJPEG is
  roughly 2-5 MB/s to each viewer - drop to 720p on weak Wi-Fi.
  Snapshot: http://<board-ip>:7001/snapshot.jpg

Open
  http://<board-ip>:7001/

Settings
  ODR_HZ in sketch.ino: 100 / 200 / 400 (default 400).
  800 Hz exceeds the Bridge's 115200-baud link with two sensors.
  BLOCK is set in stream_types.h only (Python/page derive it from the data).
