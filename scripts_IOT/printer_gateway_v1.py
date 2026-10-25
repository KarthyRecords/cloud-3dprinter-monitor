#!/usr/bin/env python3
"""
printer_gateway_v1.py  -  Phase 1 gateway
Anycubic Kobra 2 Neo (USB serial)  ->  local SQLite buffer  ->  AWS IoT Core (MQTT/TLS, QoS 1)

What it does
  * Finds the printer's COM port automatically (by USB VID:PID or description),
    so a changed COM number after replugging doesn't break anything.
  * Opens the port with DTR/RTS held low to avoid resetting the printer.
  * Asks the printer to auto-report temperatures (M155) and SD progress (M27 S),
    and falls back to polling (M105 / M27) if the firmware ignores those.
  * Writes every sample to a local SQLite queue first. A sample is deleted only
    after AWS IoT Core acknowledges it (PUBACK), so Wi-Fi/internet drops cause
    delay, not data loss.
  * Every message carries device id, UTC timestamp, a monotonically increasing
    sequence number (survives restarts) and a schema version.
  * Publishes a retained online/offline status, with an MQTT Last Will so the
    dashboard sees "offline" if the PC or script dies.

Usage
  python printer_gateway_v1.py --list-ports       # show serial ports with VID:PID
  python printer_gateway_v1.py --dry-run          # serial only, prints JSON, no AWS
  python printer_gateway_v1.py                    # full run (uses gateway_config.json)
  python printer_gateway_v1.py --config other.json

Install
  pip install pyserial "paho-mqtt>=2.0"

Same script is intended to run unchanged on the UNO Q (Linux) in phase 2:
only the config file changes.
"""

import argparse
import copy
import json
import logging
import logging.handlers
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import serial
import serial.tools.list_ports

SCHEMA_VERSION = 1
GATEWAY_VERSION = "1.0"

log = logging.getLogger("gateway")

# --------------------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "device_id": "kobra2neo",
    "serial": {
        "port": "",                       # leave empty for autodetect, or e.g. "COM5"
        "match_vid_pid": ["1A86:7523"],   # CH340; check yours with --list-ports
        "match_description": ["CH340", "USB-SERIAL", "USB Serial"],
        "baudrates": [115200, 250000],    # tried in order until the printer answers
        "open_settle_s": 2.0,             # wait after opening before talking
        "auto_report_interval_s": 2,      # M155 S<n>
        "progress_report_interval_s": 5,  # M27 S<n>
        "fallback_poll_after_s": 8,       # poll if no auto-report seen for this long
        "silence_reconnect_s": 60,        # reopen the port if nothing received
    },
    "publish_interval_s": 2,
    "stale_after_s": 10,                  # don't publish temps older than this
    "queue": {
        "db_path": "gateway_queue.db",
        "max_rows": 200000,               # oldest dropped beyond this (~4.6 days at 2 s)
    },
    "aws": {
        "endpoint": "REPLACE-ME-ats.iot.eu-west-2.amazonaws.com",
        "port": 8883,
        "client_id": "kobra2neo-pc-gateway",
        "ca_cert": "certs/AmazonRootCA1.pem",
        "cert": "certs/device.pem.crt",
        "key": "certs/private.pem.key",
        "topic_telemetry": "printer/kobra/telemetry",
        "topic_status": "printer/kobra/status",
        "keepalive_s": 30,
        "max_inflight": 20,
        "ack_timeout_s": 30,
    },
    "log_file": "gateway.log",
}


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    p = Path(path)
    if not p.exists():
        log.warning("Config %s not found, using built-in defaults", p)
        return copy.deepcopy(DEFAULT_CONFIG)
    with p.open() as f:
        return deep_merge(DEFAULT_CONFIG, json.load(f))


def setup_logging(log_file):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-9s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file:
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- printer state

class PrinterState:
    """Latest values parsed from the printer, shared between threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._temps = {}
        self._temps_t = 0.0
        self._progress = None
        self._progress_t = 0.0

    def set_temps(self, temps):
        with self._lock:
            self._temps = temps
            self._temps_t = time.monotonic()

    def set_progress(self, progress):
        with self._lock:
            self._progress = progress
            self._progress_t = time.monotonic()

    def snapshot(self, stale_after_s):
        with self._lock:
            now = time.monotonic()
            if not self._temps or now - self._temps_t > stale_after_s:
                return None
            snap = copy.deepcopy(self._temps)
            if self._progress is not None and now - self._progress_t < 3 * stale_after_s:
                snap["progress"] = dict(self._progress)
            return snap


# --------------------------------------------------------------------------- parsing

# Marlin temperature report, e.g. "ok T:205.1 /205.0 B:60.2 /60.0 @:127 B@:0"
TEMP_RE = re.compile(r"(?<![\w@])(T\d?|B|C)\s*:\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)")
SD_RE = re.compile(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", re.I)
HEATER_NAMES = {"T": "nozzle", "T0": "nozzle", "T1": "nozzle1", "B": "bed", "C": "chamber"}


def parse_temps(line):
    matches = TEMP_RE.findall(line)
    if not matches:
        return None
    keys = {m[0] for m in matches}
    temps = {}
    for key, act, tgt in matches:
        if key == "T0" and "T" in keys:
            continue  # "T" already describes the active hotend
        name = HEATER_NAMES.get(key, key)
        temps[name] = {"act": float(act), "tgt": float(tgt)}
    return temps


def parse_progress(line):
    m = SD_RE.search(line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        pct = round(100.0 * done / total, 1) if total else None
        return {"printing": True, "bytes": done, "total": total, "pct": pct}
    if "not sd printing" in line.lower():
        return {"printing": False}
    return None


# --------------------------------------------------------------------------- serial side

def list_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
    for p in ports:
        vp = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
        print(f"{p.device:<20} {vp}  {p.description}  {p.manufacturer or ''}")


def find_port(scfg):
    if scfg.get("port"):
        return scfg["port"]
    ports = list(serial.tools.list_ports.comports())
    wanted = {s.upper() for s in scfg.get("match_vid_pid", [])}
    for p in ports:
        if p.vid is not None and f"{p.vid:04X}:{p.pid:04X}" in wanted:
            return p.device
    keys = [k.lower() for k in scfg.get("match_description", [])]
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in desc for k in keys):
            return p.device
    return None


def open_serial(port, baud):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 1
    ser.write_timeout = 2
    ser.dsrdtr = False
    ser.rtscts = False
    ser.dtr = False   # applied on open: avoids the auto-reset pulse on many boards
    ser.rts = False
    ser.open()
    return ser


def probe(ser, timeout=4.0):
    """True if the printer answers M105 at the current baud rate."""
    ser.reset_input_buffer()
    ser.write(b"M105\n")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = ser.readline().decode("ascii", "replace").strip()
        if line.startswith("ok") or TEMP_RE.search(line):
            return True
    return False


class SerialReader(threading.Thread):
    def __init__(self, scfg, state, stop_evt):
        super().__init__(name="serial", daemon=True)
        self.s = scfg
        self.state = state
        self.stop_evt = stop_evt
        self.connected = threading.Event()

    def run(self):
        backoff = 1.0
        while not self.stop_evt.is_set():
            port = find_port(self.s)
            if not port:
                log.warning("Printer serial port not found; retrying in %.0f s", backoff)
            else:
                ser = None
                try:
                    ser = self._connect(port)
                    if ser:
                        backoff = 1.0
                        self.connected.set()
                        self._session(ser)
                except (serial.SerialException, OSError, TimeoutError) as e:
                    log.warning("Serial link lost on %s: %s", port, e)
                finally:
                    self.connected.clear()
                    if ser is not None:
                        try:
                            ser.close()
                        except Exception:
                            pass
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _connect(self, port):
        for baud in self.s["baudrates"]:
            if self.stop_evt.is_set():
                return None
            ser = open_serial(port, baud)
            self.stop_evt.wait(self.s["open_settle_s"])  # let a reset/boot finish if one happened
            if probe(ser):
                log.info("Connected to printer on %s @ %d baud", port, baud)
                return ser
            ser.close()
            log.info("No answer on %s @ %d baud", port, baud)
        log.warning("Printer on %s did not answer at any configured baud rate", port)
        return None

    def _send(self, ser, cmd):
        ser.write((cmd + "\n").encode("ascii"))

    def _session(self, ser):
        s = self.s
        temp_iv = int(s["auto_report_interval_s"])
        prog_iv = int(s["progress_report_interval_s"])
        self._send(ser, f"M155 S{temp_iv}")
        self._send(ser, f"M27 S{prog_iv}")

        start = time.monotonic()
        last_rx = last_temp = last_prog = start
        next_temp_poll = next_prog_poll = 0.0
        warned_temp = warned_prog = False

        while not self.stop_evt.is_set():
            raw = ser.readline()  # 1 s timeout
            now = time.monotonic()
            if raw:
                last_rx = now
                line = raw.decode("ascii", "replace").strip()
                if not line:
                    continue
                temps = parse_temps(line)
                if temps:
                    self.state.set_temps(temps)
                    last_temp = now
                    continue
                prog = parse_progress(line)
                if prog:
                    self.state.set_progress(prog)
                    last_prog = now
                    continue
                if line.lower().startswith("error"):
                    log.warning("Printer: %s", line)

            # Fallback polling if the firmware isn't auto-reporting
            if now - last_temp > s["fallback_poll_after_s"] and now >= next_temp_poll:
                if not warned_temp:
                    log.warning("No temperature auto-reports; polling with M105")
                    warned_temp = True
                self._send(ser, "M105")
                next_temp_poll = now + temp_iv
            if now - last_prog > max(3 * prog_iv, s["fallback_poll_after_s"]) and now >= next_prog_poll:
                if not warned_prog:
                    log.info("No progress auto-reports; polling with M27")
                    warned_prog = True
                self._send(ser, "M27")
                next_prog_poll = now + prog_iv

            if now - last_rx > s["silence_reconnect_s"]:
                raise TimeoutError(f"nothing received for {s['silence_reconnect_s']} s")


# --------------------------------------------------------------------------- disk queue

class DiskQueue:
    """Append-only SQLite queue. Row id doubles as the message sequence number."""

    def __init__(self, path, max_rows):
        self._lock = threading.Lock()
        self.max_rows = int(max_rows)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # AUTOINCREMENT: ids are never reused, so seq keeps increasing across restarts
        self.db.execute("CREATE TABLE IF NOT EXISTS q ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")

    def put(self, payload):
        with self._lock:
            cur = self.db.execute("INSERT INTO q (payload) VALUES (?)",
                                  (json.dumps(payload, separators=(",", ":")),))
            rid = cur.lastrowid
            if rid % 500 == 0:
                cur = self.db.execute("DELETE FROM q WHERE id <= ?", (rid - self.max_rows,))
                if cur.rowcount:
                    log.warning("Queue over %d rows; dropped %d oldest samples",
                                self.max_rows, cur.rowcount)
            return rid

    def peek(self, n, exclude=()):
        with self._lock:
            rows = self.db.execute("SELECT id, payload FROM q ORDER BY id LIMIT ?",
                                   (n + len(exclude),)).fetchall()
        return [(rid, json.loads(p)) for rid, p in rows if rid not in exclude][:n]

    def delete(self, rid):
        with self._lock:
            self.db.execute("DELETE FROM q WHERE id = ?", (rid,))

    def depth(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM q").fetchone()[0]


# --------------------------------------------------------------------------- publishers

class DryRunPublisher:
    """Prints messages instead of sending them. For testing the serial side alone."""

    def __init__(self, queue, topic):
        self.queue = queue
        self.topic = topic
        self.connected = threading.Event()
        self.connected.set()

    def start(self):
        log.info("DRY RUN: messages are printed, nothing is sent to AWS")

    def pump(self):
        for rid, payload in self.queue.peek(50):
            payload["seq"] = rid
            print(f"[{self.topic}] {json.dumps(payload, separators=(',', ':'))}", flush=True)
            self.queue.delete(rid)

    def stop(self):
        pass


class IoTCorePublisher:
    def __init__(self, acfg, queue, device_id):
        import paho.mqtt.client as mqtt  # imported here so --dry-run works without paho

        self.mqtt = mqtt
        self.a = acfg
        self.queue = queue
        self.device_id = device_id
        self.connected = threading.Event()
        self._lock = threading.Lock()
        self._inflight = {}  # mid -> (row id, sent time)

        for key in ("ca_cert", "cert", "key"):
            if not Path(acfg[key]).exists():
                raise FileNotFoundError(f"aws.{key} not found: {acfg[key]}")

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id=acfg["client_id"], clean_session=True)
        c.tls_set(ca_certs=acfg["ca_cert"], certfile=acfg["cert"], keyfile=acfg["key"])
        c.will_set(acfg["topic_status"], self._status("offline"), qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.max_inflight_messages_set(int(acfg["max_inflight"]))
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_publish = self._on_publish
        self.client = c

    def _status(self, state):
        return json.dumps({"v": SCHEMA_VERSION, "dev": self.device_id,
                           "state": state, "ts": utc_now_iso(), "gw": GATEWAY_VERSION})

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("AWS IoT connect refused: %s", reason_code)
            return
        log.info("Connected to AWS IoT Core")
        client.publish(self.a["topic_status"], self._status("online"), qos=1, retain=True)
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        with self._lock:
            # Unacknowledged rows stay on disk and are re-sent after reconnect.
            # The cloud side may therefore see a duplicate seq: de-duplicate on (dev, seq).
            self._inflight.clear()
        log.warning("Disconnected from AWS IoT Core (%s); will reconnect", reason_code)

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        with self._lock:
            entry = self._inflight.pop(mid, None)
        if entry:
            self.queue.delete(entry[0])

    def start(self):
        self.client.connect_async(self.a["endpoint"], int(self.a["port"]),
                                  keepalive=int(self.a["keepalive_s"]))
        self.client.loop_start()

    def pump(self):
        if not self.connected.is_set():
            return
        now = time.monotonic()
        with self._lock:
            # Forget sends that were never acknowledged so they get retried
            for mid, (rid, t) in list(self._inflight.items()):
                if now - t > self.a["ack_timeout_s"]:
                    del self._inflight[mid]
            room = int(self.a["max_inflight"]) - len(self._inflight)
            if room <= 0:
                return
            busy = {rid for rid, _ in self._inflight.values()}
            for rid, payload in self.queue.peek(room, exclude=busy):
                payload["seq"] = rid
                info = self.client.publish(self.a["topic_telemetry"],
                                           json.dumps(payload, separators=(",", ":")), qos=1)
                if info.rc != self.mqtt.MQTT_ERR_SUCCESS:
                    break
                self._inflight[info.mid] = (rid, now)

    def stop(self):
        try:
            if self.connected.is_set():
                info = self.client.publish(self.a["topic_status"], self._status("offline"),
                                           qos=1, retain=True)
                info.wait_for_publish(timeout=3)
            self.client.disconnect()
        finally:
            self.client.loop_stop()


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Kobra 2 Neo serial -> AWS IoT Core gateway")
    ap.add_argument("--config", default="gateway_config.json")
    ap.add_argument("--dry-run", action="store_true", help="print messages, don't send to AWS")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--run-for", type=float, default=0, help=argparse.SUPPRESS)  # testing
    args = ap.parse_args()

    if args.list_ports:
        list_ports()
        return

    cfg = load_config(args.config)
    setup_logging(cfg.get("log_file"))
    log.info("printer_gateway v%s starting (device_id=%s)", GATEWAY_VERSION, cfg["device_id"])

    stop_evt = threading.Event()
    state = PrinterState()
    queue = DiskQueue(cfg["queue"]["db_path"], cfg["queue"]["max_rows"])
    if args.dry_run:
        pub = DryRunPublisher(queue, cfg["aws"]["topic_telemetry"])
    else:
        pub = IoTCorePublisher(cfg["aws"], queue, cfg["device_id"])

    reader = SerialReader(cfg["serial"], state, stop_evt)
    reader.start()
    pub.start()

    interval = float(cfg["publish_interval_s"])
    started = time.monotonic()
    next_sample = started + interval
    next_stats = started + 60
    try:
        while not stop_evt.is_set():
            now = time.monotonic()
            if now >= next_sample:
                snap = state.snapshot(cfg["stale_after_s"])
                if snap:
                    queue.put({"v": SCHEMA_VERSION, "dev": cfg["device_id"],
                               "ts": utc_now_iso(), "gw": GATEWAY_VERSION, **snap})
                next_sample += interval
                if next_sample < now:  # we fell behind (e.g. PC was asleep)
                    next_sample = now + interval
            pub.pump()
            if now >= next_stats:
                log.info("serial=%s cloud=%s queued=%d",
                         "up" if reader.connected.is_set() else "DOWN",
                         "up" if pub.connected.is_set() else "DOWN",
                         queue.depth())
                next_stats = now + 60
            if args.run_for and now - started > args.run_for:
                break
            stop_evt.wait(0.2)
    except KeyboardInterrupt:
        log.info("Stopping...")
    finally:
        stop_evt.set()
        pub.stop()
        reader.join(timeout=5)
        log.info("Stopped. %d samples still queued for next run.", queue.depth())


if __name__ == "__main__":
    main()
