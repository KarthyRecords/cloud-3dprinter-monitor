# printer_monitor_aws_v2 - Linux side
#
# Receives accelerometer blocks from the sketch over the Bridge and serves
# a live web page on port 7001:
#     http://<board-ip>:7001/
#
#   /              the page (index.html next to this file)
#   /events        Server-Sent Events: raw accel blocks + 1 s status
#   /capture.csv   last N seconds of both sensors in g  (?s=10)
#   /status        JSON counters (incl. camera state)
#   /stream        camera MJPEG stream
#   /snapshot.jpg  latest camera frame
#   /printer.json  printer state, settings, mesh, history, path, log
#   /printer.csv   printer log as in phase 1 (?min=60 ; 0 = all)
#   /aws.json      AWS IoT Core publisher state + one example of each message
#   /settings      app settings: ?cam=480|720|1080 &cam_fps=N &pos_hz=N
#                  (app-side only - nothing is sent to the printer)
#
# aws_v2: web_status is sent to the MCU only while its accel stream is
#     arriving (data in the last 2 s). Sending while the sketch restarts (Run
#     in App Lab re-flashes it) could leave a half message in its receive
#     buffer and hang Bridge.begin() at "1 starting Bridge" until a reboot.
#
# aws_v1: everything of v13, plus aws_publisher.py streams the data to AWS
#     IoT Core (MQTT/TLS, device certificate). Configure aws_config.json and
#     put the certificate files in python/certs/ - see README.
#
# v12: camera frames are passed through as the camera's own MJPEG (no
#      decode/re-encode), so 1080p costs almost no CPU; resolution is
#      selectable on the page and saved in settings.json. Re-encode is
#      only used if the camera/OpenCV can't deliver raw MJPEG.
#
# v9: the 3D printer's USB cable on the hub is read by printer.py (same
#     query-only commands and outputs as phase 1 printer_monitor_v05 /
#     printer_gateway_v1). Accel #1 = table, #2 = toolhead.
#
# v8: the USB camera is opened by this app (OpenCV, as App Lab's own
#     Camera peripheral does), so pressing Run in App Lab starts both the
#     vibration stream and the camera - no SSH / cam_stream.py needed.
#     If OpenCV or the camera is not available inside the app, /status
#     says why and the page falls back to cam_stream.py on port 8080.
#     Stop cam_stream.py if it is still running - only one program can
#     hold the camera.
#
# v6: one Bridge message per sensor ("accel": seq, sensor, odr, b64);
#     sends "web_status"(viewers) to the MCU every second instead of the
#     MCU calling us (a lost reply to Bridge.call froze v5's sketch);
#     prints a 5 s receive summary to the Python log.

import base64
import json
import os
import queue
import struct
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sys

from arduino.app_utils import App, Bridge

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import printer as printer_mod  # noqa: E402
import aws_publisher  # noqa: E402

WEB_PORT = 7001
CAM_RES = {"480": (640, 480), "720": (1280, 720), "1080": (1920, 1080)}
CAM_QUALITY = 80           # only used in re-encode fallback
SETTINGS_FILE = None       # set below, next to this file
DEFAULT_SETTINGS = {"cam": "1080", "cam_fps": 15, "pos_hz": 8}
G_PER_LSB = 0.0039
HISTORY_S = 120            # seconds kept for CSV capture
MAX_ODR = 800

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")


def load_settings():
    st = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            st.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    if str(st["cam"]) not in CAM_RES:
        st["cam"] = DEFAULT_SETTINGS["cam"]
    st["cam"] = str(st["cam"])
    return st


def save_settings(st):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(st, f)
    except OSError as e:
        print("settings not saved:", e, flush=True)


settings = load_settings()

lock = threading.Lock()
history = {0: deque(maxlen=HISTORY_S * MAX_ODR), 1: deque(maxlen=HISTORY_S * MAX_ODR)}
clients = set()            # one queue.Queue per connected browser
stats = {"blocks": 0, "lost": 0, "odr": 0, "mask": 0,
         "last_rx": 0.0, "started": time.time()}
last_seq = {0: None, 1: None}
last_rx_k = {0: 0.0, 1: 0.0}


# ---------------------------------------------------------------- Bridge
def on_accel(seq, k, odr, b64):
    try:
        raw = base64.b64decode(b64)
        vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
        now = time.time()
        with lock:
            last = last_seq[k]
            if last is not None and seq > last + 1:
                stats["lost"] += seq - last - 1
            last_seq[k] = seq
            stats["blocks"] += 1
            stats["odr"] = odr
            last_rx_k[k] = now
            stats["last_rx"] = now
            stats["mask"] = sum(1 << j for j in (0, 1) if now - last_rx_k[j] < 2.0)

            h = history[k]
            for i in range(0, len(vals) - 2, 3):
                h.append((vals[i], vals[i + 1], vals[i + 2]))
        aws.on_accel(k, seq, odr, raw)
        with lock:

            msg = json.dumps({"t": "a", "k": k, "seq": seq, "odr": odr, "b64": b64})
            for q in list(clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass       # slow browser: skip this block for it
    except Exception as e:  # never let a bad block kill the handler thread
        print("accel error:", e, flush=True)


Bridge.provide("accel", on_accel)


def status_to_mcu():
    """Tell the MCU we are alive and how many browsers are watching -
    but only while it is streaming, never while it is (re)starting."""
    warned = False
    last_log, last_blocks = time.time(), 0
    while True:
        with lock:
            n = len(clients)
            blocks, lost, odr, mask = stats["blocks"], stats["lost"], stats["odr"], stats["mask"]
            mcu_streaming = stats["last_rx"] and time.time() - stats["last_rx"] < 2.0
        if mcu_streaming:
            try:
                Bridge.notify("web_status", n)
            except Exception as e:
                if not warned:
                    print("web_status notify failed:", e, flush=True)
                    warned = True
        now = time.time()
        if now - last_log >= 5.0:
            rate = (blocks - last_blocks) / (now - last_log)
            ps = pm.snapshot()
            print("rx %d blocks (%.1f/s) lost %d odr %s sensors %s viewers %d | printer: %s"
                  % (blocks, rate, lost, odr or "-", bin(mask), n, ps["status"]["state"]),
                  flush=True)
            last_log, last_blocks = now, blocks
        time.sleep(1.0)


# ---------------------------------------------------------------- camera
cam = {"state": "starting", "device": None, "fps": 0.0, "jpeg": None, "frames": 0,
       "width": None, "height": None, "req": None, "mode": None, "mbps": 0.0}
cam_cond = threading.Condition()
cam_reopen = threading.Event()


def find_usb_cameras():
    """Primary /dev/videoN nodes that sit on a USB bus (webcams make 2+ nodes)."""
    found = []
    base = "/sys/class/video4linux"
    try:
        names = sorted(os.listdir(base), key=lambda n: int(n[5:]) if n[5:].isdigit() else 99)
    except OSError:
        return found
    for name in names:
        node = os.path.join(base, name)
        try:
            with open(os.path.join(node, "index")) as f:
                if f.read().strip() != "0":
                    continue
            if "/usb" in os.path.realpath(os.path.join(node, "device")):
                found.append("/dev/" + name)
        except OSError:
            continue
    return found


def set_cam(**kw):
    with cam_cond:
        cam.update(kw)


def std_dht(cv2, np):
    """Standard JPEG Huffman tables (DHT segments) taken from a tiny encode.
    Many webcams send MJPEG frames without them; some browsers then fail."""
    ok, enc = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
    b = enc.tobytes()
    out, i = b"", 2
    while i + 4 <= len(b) and b[i] == 0xFF:
        marker, seglen = b[i + 1], (b[i + 2] << 8) | b[i + 3]
        if marker == 0xDA:                       # start of scan: headers end
            break
        if marker == 0xC4:
            out += b[i:i + 2 + seglen]
        i += 2 + seglen
    return out


def fix_mjpeg(frame_bytes, dht):
    """Insert standard Huffman tables if the frame has none."""
    sos = frame_bytes.find(b"\xff\xda")
    if sos < 0 or frame_bytes.find(b"\xff\xc4", 0, sos) >= 0:
        return frame_bytes
    return frame_bytes[:2] + dht + frame_bytes[2:]


def camera_loop():
    try:
        import cv2
        import numpy as np
    except Exception as e:
        set_cam(state="OpenCV not available in app: %s" % e)
        print("camera: OpenCV not available (%s) - page will try port 8080" % e, flush=True)
        return
    dht = std_dht(cv2, np)

    last_msg = None
    while True:
        cam_reopen.clear()
        devs = find_usb_cameras()
        if not devs:
            msg = "no USB camera visible to the app"
            set_cam(state=msg, device=None)
            if msg != last_msg:
                print("camera:", msg, flush=True)
                last_msg = msg
            cam_reopen.wait(5)
            continue

        dev = devs[0]
        req = settings["cam"]
        want_w, want_h = CAM_RES[req]
        fps_req = int(settings.get("cam_fps", 15))
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)
        cap.set(cv2.CAP_PROP_FPS, fps_req)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)          # ask for the raw MJPEG frames
        if not cap.isOpened():
            cap.release()
            msg = "cannot open %s (in use by cam_stream.py?)" % dev
            set_cam(state=msg, device=dev)
            if msg != last_msg:
                print("camera:", msg, flush=True)
                last_msg = msg
            cam_reopen.wait(5)
            continue

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        last_msg = None
        mode = None
        t0, n, nbytes, fails = time.time(), 0, 0, 0
        while not cam_reopen.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                fails += 1
                if fails >= 10:
                    print("camera: frame grab failing, reopening", flush=True)
                    break
                time.sleep(0.1)
                continue
            fails = 0
            raw = frame.tobytes() if frame.ndim <= 2 and frame.dtype == np.uint8 and \
                (frame.ndim == 1 or min(frame.shape) == 1) else None
            if raw is not None and raw[:2] == b"\xff\xd8":
                jpg = fix_mjpeg(raw, dht)
                if mode != "passthrough":
                    mode = "passthrough"
                    print("camera: %s %dx%d MJPEG passthrough (requested %s)" % (dev, w, h, req), flush=True)
            else:
                if frame.ndim == 3:
                    img = frame
                else:                                   # raw but not JPEG: decode
                    img = cv2.imdecode(frame, cv2.IMREAD_COLOR)
                    if img is None:
                        continue
                h, w = img.shape[:2]
                cv2.putText(img, time.strftime("%Y-%m-%d %H:%M:%S"), (8, h - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5 * max(1, w / 640), (255, 255, 255), 1, cv2.LINE_AA)
                ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, CAM_QUALITY])
                if not ok:
                    continue
                jpg = enc.tobytes()
                if mode != "re-encode":
                    mode = "re-encode"
                    print("camera: %s %dx%d re-encode mode (raw MJPEG not available)" % (dev, w, h), flush=True)
            with cam_cond:
                cam.update(jpeg=jpg, state="ok", device=dev, width=w, height=h, req=req, mode=mode)
                cam["frames"] += 1
                cam_cond.notify_all()
            n += 1
            nbytes += len(jpg)
            dt = time.time() - t0
            if dt >= 2.0:
                set_cam(fps=n / dt, mbps=nbytes / dt / 1e6)
                t0, n, nbytes = time.time(), 0, 0
            if mode == "re-encode":                    # pace the CPU-heavy path
                time.sleep(max(0.0, 1.0 / fps_req - 0.005))

        cap.release()
        set_cam(state="reopening", jpeg=None)
        time.sleep(0.5)


# ---------------------------------------------------------------- printer
def broadcast(obj):
    msg = json.dumps(obj, separators=(",", ":"), default=str)
    with lock:
        for q in list(clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


_log_cursor = [0]


def on_printer_event(kind, data):
    try:
        if kind == "pos":
            broadcast(dict(data, t="pos"))
        elif kind == "live":
            snap = pm.snapshot()
            lines, _log_cursor[0] = pm.log_since(_log_cursor[0])
            snap["log_new"] = lines[-50:]
            snap["t"] = "p"
            broadcast(snap)
        elif kind == "pstatus":
            broadcast({"t": "ps", "status": pm.snapshot()["status"]})
        elif kind == "alert":
            broadcast(dict(data, t="alert", at=data["t"]))
            aws.on_alert(data["t"], data["line"])
        elif kind == "setup":
            broadcast({"t": "setup"})
    except Exception as e:  # never break the serial thread
        print("printer event error:", e, flush=True)


# PRINTER_PORT (environment) forces a device, e.g. /dev/ttyUSB0; default = auto (CH340)
pm = printer_mod.PrinterMonitor(on_event=on_printer_event,
                                port_override=os.environ.get("PRINTER_PORT") or None)


def _latest_jpeg():
    with cam_cond:
        return cam["jpeg"]


aws = aws_publisher.AwsPublisher(HERE, {
    "printer": lambda: pm.snapshot(),
    "setup": pm.setup_info,
    "jpeg": _latest_jpeg,
})


def printer_csv(minutes):
    rows = pm.csv_rows(minutes)
    out = [",".join(printer_mod.CSV_FIELDS)]
    for r in rows:
        vals = []
        for k in printer_mod.CSV_FIELDS:
            v = r.get(k)
            if v is None:
                vals.append("")
            elif isinstance(v, str):
                vals.append('"%s"' % v.replace('"', "'") if "," in v else v)
            else:
                vals.append(str(v))
        out.append(",".join(vals))
    return ("\n".join(out) + "\n").encode()


# ---------------------------------------------------------------- web
def status_json():
    with lock:
        s = dict(stats)
        s["viewers"] = len(clients)
        s["link"] = (time.time() - s["last_rx"]) < 2.0
    s.pop("last_rx", None)
    with cam_cond:
        s["cam"] = {"state": cam["state"], "device": cam["device"],
                    "fps": round(cam["fps"], 1), "frames": cam["frames"],
                    "width": cam["width"], "height": cam["height"], "req": cam["req"],
                    "mode": cam["mode"], "mbps": round(cam["mbps"], 2)}
    s["settings"] = dict(settings)
    s["pos_stats"] = pm.snapshot()["pos_stats"]
    s["printer"] = pm.snapshot()["status"]["state"]
    s["aws"] = aws.snapshot()
    return s


def capture_csv(seconds):
    with lock:
        odr = stats["odr"] or 400
        n = int(max(0.1, min(seconds, HISTORY_S)) * odr)
        h0 = list(history[0])[-n:]
        h1 = list(history[1])[-n:]
    rows = max(len(h0), len(h1))
    # align both sensors on the most recent sample
    pad0 = rows - len(h0)
    pad1 = rows - len(h1)
    out = ["t_s,s1_x_g,s1_y_g,s1_z_g,s2_x_g,s2_y_g,s2_z_g"]
    for i in range(rows):
        t = (i - rows + 1) / odr
        a = h0[i - pad0] if i >= pad0 else None
        b = h1[i - pad1] if i >= pad1 else None
        fa = ",".join("%.4f" % (v * G_PER_LSB) for v in a) if a else ",,"
        fb = ",".join("%.4f" % (v * G_PER_LSB) for v in b) if b else ",,"
        out.append("%.5f,%s,%s" % (t, fa, fb))
    return ("\n".join(out) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
                self._send(200, "text/html; charset=utf-8", body)
            except OSError:
                self._send(500, "text/plain", b"index.html missing next to main.py")
        elif url.path == "/status":
            self._send(200, "application/json", json.dumps(status_json()).encode())
        elif url.path == "/capture.csv":
            secs = float(parse_qs(url.query).get("s", ["10"])[0])
            name = time.strftime("accel_%Y%m%d_%H%M%S.csv")
            self._send(200, "text/csv", capture_csv(secs),
                       {"Content-Disposition": 'attachment; filename="%s"' % name})
        elif url.path == "/events":
            self._events()
        elif url.path == "/snapshot.jpg":
            with cam_cond:
                jpg = cam["jpeg"]
            if jpg:
                self._send(200, "image/jpeg", jpg)
            else:
                self._send(503, "text/plain", ("camera: %s" % cam["state"]).encode())
        elif url.path == "/stream":
            self._stream()
        elif url.path == "/settings":
            q = parse_qs(url.query)
            changed = False
            if "cam" in q and q["cam"][0] in CAM_RES and q["cam"][0] != settings["cam"]:
                settings["cam"] = q["cam"][0]
                changed = True
                cam_reopen.set()
            if "cam_fps" in q:
                try:
                    f = max(5, min(30, int(q["cam_fps"][0])))
                    if f != settings["cam_fps"]:
                        settings["cam_fps"] = f
                        changed = True
                        cam_reopen.set()
                except ValueError:
                    pass
            if "pos_hz" in q:
                try:
                    settings["pos_hz"] = pm.set_pos_hz(float(q["pos_hz"][0]))
                    changed = True
                except ValueError:
                    pass
            if changed:
                save_settings(settings)
            self._send(200, "application/json", json.dumps(settings).encode())
        elif url.path == "/aws.json":
            self._send(200, "application/json",
                       json.dumps({"stats": aws.snapshot(), "examples": aws.examples()},
                                  indent=1, default=str).encode())
        elif url.path == "/printer.json":
            self._send(200, "application/json",
                       json.dumps(pm.snapshot(with_history=True), default=str).encode())
        elif url.path == "/printer.csv":
            mins = float(parse_qs(url.query).get("min", ["60"])[0])
            name = time.strftime("printer_log_%Y%m%d_%H%M%S.csv")
            self._send(200, "text/csv", printer_csv(mins),
                       {"Content-Disposition": 'attachment; filename="%s"' % name})
        else:
            self._send(404, "text/plain", b"not found")

    def _stream(self):
        with cam_cond:
            if cam["state"] != "ok":
                body = ("camera: %s" % cam["state"]).encode()
                self._send(503, "text/plain", body)
                return
        self.send_response(200)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        last = None
        try:
            while True:
                with cam_cond:
                    cam_cond.wait_for(lambda: cam["jpeg"] is not None and cam["jpeg"] is not last,
                                      timeout=5)
                    jpg = cam["jpeg"]
                if jpg is None or jpg is last:
                    if cam["state"] != "ok":
                        return          # camera gone: end stream, page retries
                    continue
                last = jpg
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(jpg))
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _events(self):
        q = queue.Queue(maxsize=200)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with lock:
            clients.add(q)
        last_status = 0.0
        try:
            while True:
                try:
                    msg = q.get(timeout=1.0)
                    self.wfile.write(b"data: " + msg.encode() + b"\n\n")
                except queue.Empty:
                    pass
                now = time.time()
                if now - last_status >= 1.0:
                    last_status = now
                    st = dict(status_json(), t="s")
                    self.wfile.write(b"data: " + json.dumps(st).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with lock:
                clients.discard(q)


def run_web():
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    server.daemon_threads = True
    print("Web page on port %d" % WEB_PORT, flush=True)
    server.serve_forever()


threading.Thread(target=run_web, daemon=True).start()
threading.Thread(target=status_to_mcu, daemon=True).start()
threading.Thread(target=camera_loop, daemon=True).start()
pm.set_pos_hz(settings.get("pos_hz", 8))
pm.start()
aws.start()

App.run()
