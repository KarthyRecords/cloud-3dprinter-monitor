# printer.py - Kobra 2 Neo USB serial monitor for the UNO Q app (READ-ONLY)
#
# Ports the monitoring side of printer_monitor_v05.py / printer_gateway_v1.py
# (phase 1, PC) to the board:
#   * finds the printer's CH340 port (1A86:7523) on the USB hub
#   * opens it with DTR/RTS low (capability report: board does not reset)
#   * temperatures via M155 auto-report, M105 fallback
#   * 1 s poll: M114, M27, M31, M220, M221; every 5th poll M119 + M27 C
#   * position (M114) at POS_HZ between polls for the path view
#   * once per connection: M115, M503, M412, M211, M290, M420 V, M20 L (idle)
#   * derived: motor position from step counts, mesh Z correction, lead,
#     filament used, slicer estimate from filename, ETA (2 methods), finish time
#
# OUTPUTS ONLY: send() refuses anything that is not on QUERY_ALLOWLIST, so
# this module can never heat, move, print or change a setting. (M111 echo
# is left out on purpose - it changes a printer flag.)
#
# Standard library only (termios) - no pyserial needed in the app.
#
# v10: the UNO Q kernel has no ch341 driver, so there is no /dev/ttyUSB0.
#      If no tty is found, this connects to ch340_bridge (a small program on
#      the board that drives the CH340 over raw USB) at PRINTER_BRIDGE,
#      default msgpack-rpc-router:7010 (= the board, seen from the app).
# v11: bed mesh is checked for completeness (rows 0..n-1 against the
#      header) and re-read up to 3 times if lines were lost.
# v12: an unreadable M27 reply no longer counts as "not printing" (that
#      cleared the nozzle path and filament counter on every glitch); a
#      print only ends after 3 explicit "Not SD printing" replies in a row.
#      Position rate adjustable (set_pos_hz, 1-20 Hz, default 8 as phase 1),
#      achieved rate and M114 reply time reported.

import fcntl
import socket
import glob
import math
import os
import re
import select
import struct
import termios
import threading
import time
from collections import deque
from datetime import datetime

BAUDS = (115200, 250000)
POLL_INTERVAL = 1.0          # s, main status poll (as phase 1)
POS_HZ = 8.0                 # default M114 rate for the path view (phase 1: 8 Hz)
POS_HZ_RANGE = (1.0, 20.0)
PRINT_END_POLLS = 3          # explicit idle replies needed to end a print
SLOW_EVERY = 5               # endstops / filename every N polls
SILENCE_RECONNECT_S = 60
HISTORY_S = 4 * 3600         # 1 Hz rows kept (CSV + charts)
POS_HISTORY = 20 * 60 * 20   # 20 min of path at up to 20 Hz
LOG_LINES = 300
STEPS_DEFAULT = {"x": 80.0, "y": 80.0, "z": 400.0}
FILAMENT_DIA = 1.75          # mm
FILAMENT_DENSITY = 1.24      # g/cm3 (PLA)
RETRACT_MAX = 3.0            # E drop larger than this = counter reset (G92 E0)
FILAMENT_TRIGGERED_MEANS_PRESENT = True   # as phase 1
CH340 = ("1a86", "7523")
BRIDGE = os.environ.get("PRINTER_BRIDGE", "msgpack-rpc-router:7010")

# Every command this module may send. All are pure queries / reports.
QUERY_ALLOWLIST = {
    "M105", "M114", "M27", "M27 C", "M31", "M220", "M221", "M119",
    "M115", "M503", "M412", "M211", "M290", "M420 V", "M20 L",
    "M155 S1", "M155 S2", "M155 S0",   # temperature auto-report on/off (phase 1 too)
}

ALERT_WORDS = ("error", "done printing", "file opened", "file selected",
               "runout", "thermal", "halted", "kill", "paused", "resend")
ALERT_IGNORE = re.compile(r"(?i)^cap:|filament runout (on|off)|M412\s+S\d|runout sensor")

NUM = r"[-+]?\d+(?:\.\d+)?"
GCODE_ECHO_RE = re.compile(r"^echo:((?:N\d+\s+)?[GMT]\d+.*)$")

CSV_FIELDS = ["time", "elapsed_s", "hotend", "hotend_target", "hotend_pwm",
              "bed", "bed_target", "bed_pwm", "x", "y", "z", "e",
              "x_act", "y_act", "z_act", "z_corr", "lead",
              "sd_done", "sd_total", "progress", "print_time_s",
              "speed", "flow", "filament_mm", "file_long", "slicer_est_s",
              "eta_slicer_s", "eta_progress_s"]


# --------------------------------------------------------------------------- parsers
# (same regexes as printer_monitor_v05.py, numpy-free)
def parse_m105(text):
    out = {}
    m = re.search(rf"T:({NUM})\s*/\s*({NUM})", text)
    if m:
        out["hotend"], out["hotend_target"] = map(float, m.groups())
    m = re.search(rf"B:({NUM})\s*/\s*({NUM})", text)
    if m:
        out["bed"], out["bed_target"] = map(float, m.groups())
    m = re.search(rf"(?:^|\s)@:({NUM})", text)
    if m:
        out["hotend_pwm"] = float(m.group(1))
    m = re.search(rf"B@:({NUM})", text)
    if m:
        out["bed_pwm"] = float(m.group(1))
    return out


def parse_m114(text):
    m = re.search(rf"X:({NUM})\s*Y:({NUM})\s*Z:({NUM})\s*E:({NUM})", text)
    if not m:
        return {}
    out = dict(zip(("x", "y", "z", "e"), map(float, m.groups())))
    c = re.search(r"Count\s+X:\s*(-?\d+)\s*Y:\s*(-?\d+)\s*Z:\s*(-?\d+)", text)
    if c:
        out["cx"], out["cy"], out["cz"] = map(int, c.groups())
    return out


def parse_m92(text):
    m = re.search(rf"M92\s+X({NUM})\s+Y({NUM})\s+Z({NUM})", text)
    return dict(zip(("x", "y", "z"), map(float, m.groups()))) if m else {}


def parse_slicer_time(name):
    m = re.search(r"_((?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?)\.g(?:code|co)?$", name or "", re.I)
    if not m or not m.group(1):
        return None
    secs = sum(int(v) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
               for v, u in re.findall(r"(\d+)([dhms])", m.group(1)))
    return float(secs) if secs else None


def parse_m27(text):
    m = re.search(r"SD printing byte (\d+)\s*/\s*(\d+)", text)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        pct = 100.0 * done / total if total else 0.0
        return {"sd_done": done, "sd_total": total, "progress": pct, "printing": total > 0}
    return {"sd_done": 0, "sd_total": 0, "progress": 0.0, "printing": False}


def parse_m27_safe(text):
    """Like parse_m27, but {} when the reply could not be read (lost bytes)."""
    if re.search(r"SD printing byte (\d+)\s*/\s*(\d+)", text):
        return parse_m27(text)
    if re.search(r"(?i)not sd printing", text):
        return parse_m27(text)
    return {}


def parse_m27c(text):
    m = re.search(r"Current file:\s*(.*)", text)
    if not m:
        return ""
    name = m.group(1).strip()
    return "" if "no file" in name.lower() else name


def parse_m31(text):
    m = re.search(r"Print time:\s*(.+)", text)
    if not m:
        return "", None
    s = m.group(1).replace("ok", "").strip()
    secs = 0
    for val, unit in re.findall(r"(\d+)\s*([dhms])", s):
        secs += int(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return s, float(secs)


def parse_m119(lines):
    out = {}
    for line in lines:
        line = line.replace("echo:", "").strip()
        if ":" in line and not line.startswith("ok") and "Reporting" not in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_percent(text, key):
    m = re.search(rf"{key}\s*:?\s*(\d+)%", text)
    return float(m.group(1)) if m else None


def parse_mesh_ex(lines):
    """-> (rows, row indices, columns announced by the header line or None)."""
    rows, idx, ncols = [], [], None
    for line in lines:
        t = line.replace("echo:", "").strip()
        if re.fullmatch(r"(\d+\s+)+\d+", t):          # header "0 1 2 3 4"
            ncols = len(t.split())
            continue
        m = re.match(r"^(\d+)\s+(.+)$", t)
        if not m:
            continue
        vals, has_float = [], False
        for tok in m.group(2).split():
            if re.fullmatch(r"[-+]?\d+\.\d+", tok):
                vals.append(float(tok))
                has_float = True
            elif tok in (".", "=", "-"):
                vals.append(None)
            else:
                vals = None
                break
        if vals and has_float and (ncols is None or len(vals) == ncols):
            rows.append(vals)
            idx.append(int(m.group(1)))
    return rows, idx, ncols


def mesh_complete(rows, idx, ncols):
    return (len(rows) >= 2 and idx == list(range(len(rows)))
            and (ncols is None or len(rows) == ncols)
            and len({len(r) for r in rows}) == 1)


def parse_mesh(lines):
    rows = []
    for line in lines:
        line = line.replace("echo:", "").strip()
        m = re.match(r"^(\d+)\s+(.+)$", line)
        if not m:
            continue
        vals, has_float = [], False
        for tok in m.group(2).split():
            if re.fullmatch(r"[-+]?\d+\.\d+", tok):
                vals.append(float(tok))
                has_float = True
            elif tok in (".", "=", "-"):
                vals.append(None)
            else:
                vals = None
                break
        if vals and has_float:
            rows.append(vals)
    if not rows or len({len(r) for r in rows}) != 1:
        return None
    return rows


def parse_files(lines):
    files, inside = [], False
    for line in lines:
        if "Begin file list" in line:
            inside = True
            continue
        if "End file list" in line:
            break
        if inside and line.strip():
            parts = line.strip().split(" ", 2)
            short = parts[0]
            size = parts[1] if len(parts) > 1 else ""
            long_name = parts[2] if len(parts) > 2 else short
            files.append({"name": long_name, "size": size, "short": short})
    return files


def parse_settings(m503, extra):
    """Pick the values phase 1 showed on the Settings tab / lamps."""
    t = m503
    s = {}

    def grab(key, pat):
        m = re.search(pat, t)
        if m:
            s[key] = m.group(0).replace("echo:", "").strip()

    grab("steps", r"M92\s+X\S+\s+Y\S+\s+Z\S+(?:\s+E\S+)?")
    grab("max_feed", r"M203\s+X\S+\s+Y\S+\s+Z\S+(?:\s+E\S+)?")
    grab("max_accel", r"M201\s+X\S+\s+Y\S+\s+Z\S+(?:\s+E\S+)?")
    grab("accel", r"M204\s+P\S+(?:\s+R\S+)?(?:\s+T\S+)?")
    grab("jerk", r"M205\s+[^\n]+")
    grab("hotend_pid", r"M301\s+P\S+\s+I\S+\s+D\S+")
    grab("bed_pid", r"M304\s+P\S+\s+I\S+\s+D\S+")
    grab("probe_offset", r"M851\s+X\S+\s+Y\S+\s+Z\S+")
    grab("linear_advance", r"M900\s+K\S+")
    grab("filament_unload", r"M603\s+[^\n;]+")
    presets = re.findall(r"M145\s+S\d\s+H\S+\s+B\S+(?:\s+F\S+)?", t)
    if presets:
        s["presets"] = "  |  ".join(presets)
    shaping = {}
    for axis, f, d in re.findall(rf"M593\s+([XY])\s+F({NUM})\s+D({NUM})", t):
        shaping[axis.lower()] = {"f": float(f), "d": float(d)}
    if shaping:
        s["input_shaping"] = shaping
    m = re.search(r"M413\s+S(\d)", t)
    if m:
        s["power_loss"] = m.group(1) == "1"
    m = re.search(r"M420\s+S(\d)", t)
    if m:
        s["leveling"] = m.group(1) == "1"
    # lamps from separate queries (same regexes as phase 1)
    m = re.search(r"(?i)runout\s*:?\s*(ON|OFF)|M412\s+S(\d)", extra.get("M412", ""))
    if m:
        s["runout"] = (m.group(1) or "").upper() == "ON" or m.group(2) == "1"
    m = re.search(r"M211\s+S(\d)|(?i:soft endstops:?\s*(on|off))", extra.get("M211", ""))
    if m:
        s["soft_endstops"] = m.group(1) == "1" or (m.group(2) or "").lower() == "on"
    m = re.search(rf"(?i)offset\s+Z\s*:?\s*({NUM})", extra.get("M290", ""))
    if m:
        s["z_offset"] = float(m.group(1))
    fw = extra.get("M115", "")
    m = re.search(r"FIRMWARE_NAME:(.*?)(?:\s+SOURCE_CODE_URL|$)", fw)
    if m:
        s["firmware"] = m.group(1).strip()
    m = re.search(r"MACHINE_TYPE:(\S+)", fw)
    if m:
        s["machine"] = m.group(1)
    caps_on = re.findall(r"Cap:(\w+):1", fw)
    if caps_on:
        s["caps_on"] = caps_on
    return s


# --------------------------------------------------------------------------- serial port
BAUD_CONST = {115200: termios.B115200, 250000: getattr(termios, "B250000", None)}


class SerialPort:
    """Minimal raw tty (8N1, no flow control, DTR/RTS held low)."""

    def __init__(self, dev, baud):
        self.dev = dev
        self.fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._lines_low()
            attrs = termios.tcgetattr(self.fd)
            speed = BAUD_CONST.get(baud)
            if speed is None:
                raise OSError("baud %d not supported by termios here" % baud)
            attrs[0] = 0                                       # iflag
            attrs[1] = 0                                       # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL   # no HUPCL
            attrs[3] = 0                                       # lflag (raw)
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            self._lines_low()
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self.fd)
            raise
        self.buf = b""

    def _lines_low(self):
        try:
            fcntl.ioctl(self.fd, termios.TIOCMBIC,
                        struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
        except OSError:
            pass                    # not a real UART (e.g. test pty)

    def readline(self, timeout):
        end = time.monotonic() + timeout
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i], self.buf[i + 1:]
                return line.decode("ascii", "replace").strip()
            left = end - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    raise OSError("port closed")
                self.buf += chunk

    def in_waiting(self):
        r, _, _ = select.select([self.fd], [], [], 0)
        return bool(r) or b"\n" in self.buf

    def write(self, data):
        view = memoryview(data)
        end = time.monotonic() + 2.0
        while view:
            if time.monotonic() > end:
                raise OSError("write timeout")
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                select.select([], [self.fd], [], 0.1)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class SocketPort:
    """Same interface as SerialPort, over TCP to ch340_bridge."""

    def __init__(self, hostport):
        host, _, port = hostport.rpartition(":")
        self.dev = "bridge " + hostport
        self.sock = socket.create_connection((host, int(port)), timeout=3)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setblocking(False)
        self.buf = b""

    def readline(self, timeout):
        end = time.monotonic() + timeout
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i], self.buf[i + 1:]
                return line.decode("ascii", "replace").strip()
            left = end - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.sock], [], [], left)
            if r:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise OSError("bridge closed the connection")
                self.buf += chunk

    def in_waiting(self):
        r, _, _ = select.select([self.sock], [], [], 0)
        return bool(r) or b"\n" in self.buf

    def write(self, data):
        self.sock.setblocking(True)
        try:
            self.sock.settimeout(2.0)
            self.sock.sendall(data)
        finally:
            self.sock.setblocking(False)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def find_printer_port():
    """Return (device, 'vid:pid' or None, reason). Prefers the CH340."""
    cands = []
    for path in sorted(glob.glob("/sys/class/tty/ttyUSB*") + glob.glob("/sys/class/tty/ttyACM*")):
        name = os.path.basename(path)
        vid = pid = None
        d = os.path.realpath(os.path.join(path, "device"))
        for up in (d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
            try:
                with open(os.path.join(up, "idVendor")) as f:
                    vid = f.read().strip().lower()
                with open(os.path.join(up, "idProduct")) as f:
                    pid = f.read().strip().lower()
                break
            except OSError:
                continue
        cands.append(("/dev/" + name, vid, pid))
    for dev, vid, pid in cands:
        if (vid, pid) == CH340:
            return dev, "%s:%s" % (vid, pid), None
    if cands:
        dev, vid, pid = cands[0]
        return dev, ("%s:%s" % (vid, pid)) if vid else None, None
    devs = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if devs:
        return devs[0], None, None
    return None, None, "no USB serial device visible to the app"


# --------------------------------------------------------------------------- monitor
class PrinterMonitor(threading.Thread):
    def __init__(self, on_event=None, port_override=None):
        super().__init__(name="printer", daemon=True)
        self.on_event = on_event or (lambda kind, data: None)
        self.port_override = port_override
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.ser = None
        self.status = {"state": "starting", "port": None, "baud": None, "usb": None,
                       "since": time.time(), "reset_seen": False}
        self.live = {}
        self.setup = {}
        self.setup_seq = 0
        self.mesh = None
        self.mesh_info = {}
        self.files = None
        self.history = deque(maxlen=HISTORY_S)
        self.path = deque(maxlen=POS_HISTORY)
        self.log = deque(maxlen=LOG_LINES)
        self.log_seq = 0
        self.alerts = deque(maxlen=30)
        self.steps = dict(STEPS_DEFAULT)
        self.sd_map = {}
        self.auto_temp = False
        self.last_temp, self.last_temp_t = {}, 0.0
        self.last_rx = time.monotonic()
        self.fil_mm, self.fil_last_e = 0.0, None
        self.was_printing = False
        self.idle_polls = 0
        self.pos_hz = POS_HZ
        self.pos_times = deque(maxlen=2000)      # (monotonic t, reply ms)
        self.print_id = 0
        self.t0 = time.time()
        self.cmds_sent = 0
        self.refused = 0

    # ---- public snapshot helpers (thread-safe) ----
    def snapshot(self, with_history=False):
        with self.lock:
            now = time.monotonic()
            recent = [(t, ms) for t, ms in self.pos_times if now - t <= 5.0]
            pos_stats = {"set_hz": self.pos_hz,
                         "hz": round(len(recent) / 5.0, 1),
                         "lat_avg_ms": round(sum(m for _, m in recent) / len(recent), 1) if recent else None,
                         "lat_max_ms": round(max(m for _, m in recent), 1) if recent else None}
            out = {"status": dict(self.status), "live": dict(self.live), "pos_stats": pos_stats,
                   "setup_seq": self.setup_seq, "print_id": self.print_id,
                   "alerts": list(self.alerts)[-10:], "log_seq": self.log_seq}
            if with_history:
                out["setup"] = dict(self.setup)
                out["mesh"] = self.mesh
                out["mesh_info"] = dict(self.mesh_info)
                out["files"] = self.files
                out["history"] = [{k: r.get(k) for k in ("t", "hotend", "hotend_target",
                                   "bed", "bed_target", "hotend_pwm", "bed_pwm", "progress")}
                                  for r in list(self.history)[-3600:]]
                out["path"] = list(self.path)
                out["log"] = [t for _, t in list(self.log)[-200:]]
        return out

    def set_pos_hz(self, hz):
        lo, hi = POS_HZ_RANGE
        self.pos_hz = max(lo, min(hi, float(hz)))
        return self.pos_hz

    def log_since(self, seq):
        with self.lock:
            return [t for s, t in self.log if s > seq], self.log_seq

    def csv_rows(self, minutes):
        with self.lock:
            rows = list(self.history)
        if minutes:
            cut = time.time() - minutes * 60
            rows = [r for r in rows if r["t"] >= cut]
        return rows

    # ---- internals ----
    def _set_status(self, **kw):
        with self.lock:
            if "state" in kw and kw["state"] != self.status.get("state"):
                kw.setdefault("since", time.time())
            self.status.update(kw)
        self.on_event("pstatus", None)

    def _log(self, direction, line):
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log_seq += 1
            self.log.append((self.log_seq, "%s %s %s" % (stamp, direction, line)))

    def _readline(self, timeout):
        line = self.ser.readline(timeout)
        if line is None or line == "":
            return None
        self.last_rx = time.monotonic()
        # M155 auto-report line
        if not line.startswith("ok") and re.match(rf"^T:\s*{NUM}\s*/", line):
            self.last_temp = parse_m105(line)
            self.last_temp_t = time.time()
            return None
        if GCODE_ECHO_RE.match(line):           # echo left on by another program
            return None
        if line.startswith("start"):
            with self.lock:
                self.status["reset_seen"] = True
            self._log("!", "printer reported 'start' (board reset/boot)")
        self._log("<", line)
        low = line.lower()
        if any(w in low for w in ALERT_WORDS) and not ALERT_IGNORE.search(line):
            with self.lock:
                self.alerts.append({"t": time.time(), "line": line})
            self.on_event("alert", {"t": time.time(), "line": line})
        return line

    def send(self, cmd, timeout=3.0):
        if cmd not in QUERY_ALLOWLIST:          # outputs only - never control
            self.refused += 1
            self._log("!", "REFUSED (not a query): %s" % cmd)
            return []
        self.ser.write((cmd + "\n").encode("ascii"))
        self.cmds_sent += 1
        self._log(">", cmd)
        lines, end = [], time.monotonic() + timeout
        while time.monotonic() < end and not self.stop_flag.is_set():
            line = self._readline(min(0.2, max(0.01, end - time.monotonic())))
            if line is None:
                continue
            end = time.monotonic() + timeout     # still talking, keep waiting
            if line.startswith("echo:busy"):
                continue
            lines.append(line)
            if line.startswith("ok"):
                return lines
        self._log("!", "no 'ok' after %s (timeout)" % cmd)
        return lines

    def drain(self, max_s):
        end = time.monotonic() + max_s
        while time.monotonic() < end and not self.stop_flag.is_set():
            if not self.ser.in_waiting():
                time.sleep(min(0.01, max(0.0, end - time.monotonic())))
                continue
            self._readline(0.05)

    def _probe(self):
        for _ in range(2):
            lines = self.send("M105", timeout=2.0)
            if any(l.startswith("ok") or "T:" in l for l in lines):
                return True
        return False

    def _connect_bridge(self):
        try:
            self.ser = SocketPort(BRIDGE)
        except OSError as e:
            self._set_status(state="no /dev/ttyUSB* (kernel has no CH340 driver) and "
                                   "ch340_bridge not reachable at %s: %s" % (BRIDGE, e),
                             port=None, baud=None)
            self.ser = None
            return False
        self._set_status(state="bridge connected, waiting for printer", port=self.ser.dev,
                         baud=None, usb="1a86:7523 via ch340_bridge")
        self.drain(1.0)
        if self._probe():
            return True
        self.ser.close()
        self.ser = None
        self._set_status(state="ch340_bridge reachable but the printer did not answer "
                               "(printer off, or bridge has no USB device)")
        return False

    def _connect(self, dev):
        for baud in BAUDS:
            if BAUD_CONST.get(baud) is None:
                continue
            try:
                self.ser = SerialPort(dev, baud)
            except OSError as e:
                self._set_status(state="cannot open %s: %s" % (dev, e.strerror or e), port=dev)
                return False
            self._set_status(state="opened %s @ %d, waiting" % (dev, baud), port=dev, baud=baud)
            self.drain(2.0)                      # boot messages if it did reset
            if self._probe():
                return True
            self.ser.close()
            self.ser = None
            self._log("!", "no answer at %d baud" % baud)
        self._set_status(state="printer on %s did not answer" % dev)
        return False

    def read_setup(self):
        extra = {}
        for cmd in ("M115", "M503", "M412", "M211", "M290"):
            if self.stop_flag.is_set():
                return
            extra[cmd] = "\n".join(self.send(cmd, timeout=4.0))
        self.steps.update(parse_m92(extra["M503"]))
        setup = parse_settings(extra["M503"], extra)
        best = None
        for attempt in range(1, 4):                       # re-read if lines were lost
            rows, idx, ncols = parse_mesh_ex(self.send("M420 V", timeout=4.0))
            ok = mesh_complete(rows, idx, ncols)
            if best is None or len(rows) > len(best[0]) or ok:
                best = (rows, idx, ncols, ok, attempt)
            if ok:
                break
            self._log("!", "bed mesh incomplete (%d rows, header %s) - re-reading"
                      % (len(rows), ncols))
        rows, idx, ncols, ok, _ = best
        tries = attempt
        mesh = rows or None
        mesh_info = {"complete": ok, "tries": tries, "rows": len(rows),
                     "rows_expected": ncols, "row_idx": idx}
        printing = parse_m27("\n".join(self.send("M27")))["printing"]
        files = None
        if not printing:
            files = parse_files(self.send("M20 L", timeout=6.0))
            self.sd_map = {f["short"].upper(): f["name"] for f in files}
        with self.lock:
            self.setup = setup
            self.setup["steps_used"] = dict(self.steps)
            self.mesh = mesh
            self.mesh_info = mesh_info
            if files is not None:
                self.files = files
            self.setup_seq += 1
        self.on_event("setup", None)

    def start_auto_temp(self):
        reply = "\n".join(self.send("M155 S1"))
        self.auto_temp = "unknown command" not in reply.lower()
        self._log("!", "temperatures via M155 auto-report" if self.auto_temp
                  else "M155 not supported, polling M105")

    def actual(self, pos):
        if "cx" in pos:
            st = self.steps
            pos["x_act"] = pos["cx"] / st["x"]
            pos["y_act"] = pos["cy"] / st["y"]
            pos["z_act"] = pos["cz"] / st["z"]
            pos["z_corr"] = round(pos["z_act"] - pos["z"], 4)
            pos["lead"] = round(math.hypot(pos["x"] - pos["x_act"], pos["y"] - pos["y_act"]), 3)
        return pos

    def _update_filament(self, e):
        if e is None:
            return
        last, self.fil_last_e = self.fil_last_e, e
        if last is None:
            return
        de = e - last
        if de < -RETRACT_MAX:
            return
        self.fil_mm += de

    def _pos_sample(self, pos):
        if "x" not in pos:
            return
        self._update_filament(pos.get("e"))
        if "lat_ms" in pos:
            with self.lock:
                self.pos_times.append((time.monotonic(), pos["lat_ms"]))
        s = {"t": round(time.time(), 2), "x": pos["x"], "y": pos["y"], "z": pos["z"],
             "xa": pos.get("x_act", pos["x"]), "ya": pos.get("y_act", pos["y"]),
             "za": pos.get("z_act", pos["z"])}
        with self.lock:
            self.path.append(s)
        self.on_event("pos", s)

    def _long_name(self, file_field):
        parts = file_field.split(" ", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
        return self.sd_map.get(parts[0].upper(), parts[0])

    def poll(self, cycle):
        d = {"t": time.time()}
        if self.auto_temp and time.time() - self.last_temp_t < 3.0:
            d.update(self.last_temp)
        else:
            d.update(parse_m105("\n".join(self.send("M105"))))
        t_sent = time.monotonic()
        pos = self.actual(parse_m114("\n".join(self.send("M114"))))
        if pos:
            pos["lat_ms"] = round((time.monotonic() - t_sent) * 1000.0, 1)
            self._pos_sample(pos)
        d.update(pos)
        d.update(parse_m27_safe("\n".join(self.send("M27"))))   # {} if unreadable
        d["print_time_str"], d["print_time_s"] = parse_m31("\n".join(self.send("M31")))
        d["speed"] = parse_percent("\n".join(self.send("M220")), "FR")
        d["flow"] = parse_percent("\n".join(self.send("M221")), "Flow")
        if cycle % SLOW_EVERY == 0:
            d["endstops"] = parse_m119(self.send("M119"))
            d["file"] = parse_m27c("\n".join(self.send("M27 C")))
        self._derive(d)

    def _derive(self, d):
        """Same derived values as the phase 1 Live tab (on_live)."""
        if "printing" not in d:                  # M27 unreadable: keep state
            printing = self.was_printing
        elif d["printing"]:
            self.idle_polls = 0
            printing = True
        else:
            self.idle_polls += 1
            printing = self.was_printing and self.idle_polls < PRINT_END_POLLS
        new_print = printing and not self.was_printing
        if new_print:
            self.fil_mm = 0.0
        self.was_printing = printing
        with self.lock:
            if new_print:
                self.print_id += 1
                self.path.clear()
            L = self.live
            L.update(d)
            L["printing"] = printing
            L["state"] = "Printing from SD" if printing else "Idle"
            mm = max(self.fil_mm, 0.0)
            L["filament_mm"] = round(mm, 1)
            area = math.pi * (FILAMENT_DIA / 2) ** 2
            L["filament_g"] = round(mm * area / 1000.0 * FILAMENT_DENSITY, 1)
            L["filament_scope"] = "this print" if printing else "since connect"
            long_name = self._long_name(L.get("file") or "") if printing else ""
            slicer = parse_slicer_time(long_name)
            el = L.get("print_time_s")
            p = (L.get("progress") or 0.0) / 100.0
            eta_s = max(slicer - el, 0.0) if (printing and slicer and el is not None) else None
            eta_p = el * (1 - p) / p if (printing and el and p >= 0.02) else None
            L.update(file_long=long_name, slicer_est_s=slicer,
                     eta_slicer_s=None if eta_s is None else round(eta_s),
                     eta_progress_s=None if eta_p is None else round(eta_p),
                     finish_slicer=None if eta_s is None else d["t"] + eta_s,
                     finish_progress=None if eta_p is None else d["t"] + eta_p)
            row = {k: L.get(k) for k in CSV_FIELDS}
            row["t"] = d["t"]
            row["time"] = datetime.fromtimestamp(d["t"]).isoformat(timespec="seconds")
            row["elapsed_s"] = round(d["t"] - self.t0, 1)
            self.history.append(row)
        self.on_event("live", None)

    def _session(self):
        self.last_rx = time.monotonic()
        self._set_status(state="reading settings", usb=self.status.get("usb"))
        self.read_setup()
        self.start_auto_temp()
        self._set_status(state="connected")
        cycle, next_t, next_pos = 0, time.monotonic(), time.monotonic()
        while not self.stop_flag.is_set():
            pos_iv = 1.0 / self.pos_hz
            now = time.monotonic()
            if now >= next_t:
                self.poll(cycle)
                cycle += 1
                next_t = max(next_t + POLL_INTERVAL, time.monotonic())
                next_pos = time.monotonic() + pos_iv
            elif now >= next_pos:
                t_sent = time.monotonic()
                pos = self.actual(parse_m114("\n".join(self.send("M114"))))
                if pos:
                    pos["lat_ms"] = round((time.monotonic() - t_sent) * 1000.0, 1)
                    self._pos_sample(pos)
                next_pos = max(next_pos + pos_iv, time.monotonic())
            else:
                self.drain(max_s=min(0.05, max(0.0, min(next_pos, next_t) - time.monotonic())))
            if time.monotonic() - self.last_rx > SILENCE_RECONNECT_S:
                raise OSError("nothing received for %d s" % SILENCE_RECONNECT_S)

    def run(self):
        backoff = 2.0
        while not self.stop_flag.is_set():
            if self.port_override:
                dev, usb, why = self.port_override, None, None
            else:
                dev, usb, why = find_printer_port()
            try:
                if not dev:                      # no tty: use ch340_bridge
                    ok = self._connect_bridge()
                else:
                    self._set_status(usb=usb)
                    ok = self._connect(dev)
                if ok:
                    backoff = 2.0
                    self._session()
            except Exception as e:                       # noqa: BLE001
                self._set_status(state="link lost: %s" % e)
                self._log("!", "link lost: %s" % e)
            finally:
                if self.ser:
                    try:
                        if self.auto_temp:
                            self.ser.write(b"M155 S0\n")  # restore, as phase 1 did
                    except OSError:
                        pass
                    self.ser.close()
                    self.ser = None
            self.stop_flag.wait(backoff)
            backoff = min(backoff * 2, 30.0)
