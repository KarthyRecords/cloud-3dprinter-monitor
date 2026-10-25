# aws_publisher.py - publishes the board's data streams to AWS IoT Core
#
# MQTT 3.1.1 over TLS 1.2 (port 8883) with the device's X.509 certificate,
# using paho-mqtt (python/requirements.txt). Builds on the phase 1
# printer_gateway_v1.py design: same envelope (v, dev, ts, gw, seq), retained
# online/offline status with a Last Will, and a SQLite store-and-forward
# queue so network drops delay data instead of losing it.
#
# Topics (prefix from aws_config.json, default printer/kobra):
#   <p>/telemetry           printer state every 2 s           QoS1, queued
#   <p>/vibration           Grms / peaks / PSD bands, 2 s      QoS1, queued
#   <p>/event               alerts, print started / finished   QoS1, queued
#   <p>/accel_raw           400 Hz samples, 1 s per message    QoS0, live only
#   <p>/snapshot/<unix_ms>  binary JPEG, every 30 s            QoS0, live only
#   <p>/setup               settings + bed mesh                retained
#   <p>/status              online / offline (Last Will)       retained
#
# Nothing here ever talks to the printer; it only reads what the app has.

import base64
import copy
import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

import vib_summary

SCHEMA_VERSION = 2          # v1 = phase 1 gateway (fields kept, more added)
GW = "unoq-aws-1"

DEFAULT_CONFIG = {
    "enabled": True,
    "dry_run": False,                 # build + count messages, no connection
    "endpoint": "REPLACE-ME-ats.iot.eu-west-2.amazonaws.com",
    "port": 8883,
    "client_id": "kobra2neo-unoq",    # must match the IoT policy
    "device_id": "kobra2neo",
    "topic_prefix": "printer/kobra",
    "ca_cert": "certs/AmazonRootCA1.pem",
    "cert": "certs/device.pem.crt",
    "key": "certs/private.pem.key",
    "keepalive_s": 30,
    "telemetry_interval_s": 2,
    "vibration_interval_s": 2,
    "stale_after_s": 10,
    "raw_accel": True,
    "raw_block_s": 1.0,
    "snapshot_interval_s": 30,
    "snapshot_max_kb": 120,           # IoT Core payload limit is 128 KB
    "snapshot_max_width": 1280,
    "sensor_names": ["table", "toolhead"],
    "queue": {"db_path": "aws_queue.db", "max_rows": 200000},
    "max_inflight": 20,
    "ack_timeout_s": 30,
}


def utc_iso(t=None):
    dt = datetime.fromtimestamp(time.time() if t is None else t, timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class DiskQueue:
    """Append-only SQLite queue (as gateway v1). Row id = message seq."""

    def __init__(self, path, max_rows):
        self.lock = threading.Lock()
        self.max_rows = int(max_rows)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        " topic TEXT NOT NULL, payload TEXT NOT NULL)")

    def put(self, topic, payload):
        with self.lock:
            cur = self.db.execute("INSERT INTO q (topic, payload) VALUES (?, ?)",
                                  (topic, json.dumps(payload, separators=(",", ":"))))
            rid = cur.lastrowid
            if rid % 500 == 0:
                self.db.execute("DELETE FROM q WHERE id <= ?", (rid - self.max_rows,))
            return rid

    def peek(self, n, exclude=()):
        with self.lock:
            rows = self.db.execute("SELECT id, topic, payload FROM q ORDER BY id LIMIT ?",
                                   (n + len(exclude),)).fetchall()
        return [(rid, t, json.loads(p)) for rid, t, p in rows if rid not in exclude][:n]

    def delete(self, rid):
        with self.lock:
            self.db.execute("DELETE FROM q WHERE id = ?", (rid,))

    def depth(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM q").fetchone()[0]


class AwsPublisher(threading.Thread):
    """sources: dict of callables supplied by main.py
         printer()   -> printer snapshot (with history=False)
         setup()     -> (setup_seq, setup dict, mesh, mesh_info)
         jpeg()      -> latest camera JPEG bytes or None
    """

    def __init__(self, here, sources, config_name="aws_config.json"):
        super().__init__(name="aws", daemon=True)
        self.here = here
        self.src = sources
        self.cfg_path = os.path.join(here, config_name)
        self.cfg = self._load_config()
        self.p = self.cfg["topic_prefix"].rstrip("/")
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.connected = threading.Event()
        self.client = None
        self.queue = None
        self.inflight = {}                    # mid -> (row id, sent time)
        self.raw = {0: [], 1: []}             # pending raw blocks per sensor
        self.raw_meta = {0: None, 1: None}    # [seq_first, seq_last, gaps, odr, t_first]
        self.win = {0: deque(), 1: deque()}   # (t, bytes) for vibration summaries
        self.odr = 400
        self.stats = {"state": "starting", "endpoint": self.cfg["endpoint"],
                      "client_id": self.cfg["client_id"], "dry_run": self.cfg["dry_run"],
                      "connects": 0, "last_error": None, "queued": 0,
                      "sent": {}, "dropped": {}, "bytes": {}}
        self.last_payload = {}                # stream -> example (for /aws.json)
        self.was_printing = None
        self.print_id = None
        self.setup_seq_sent = None
        self.printer_state_sent = None

    # ------------------------------------------------------------ config
    def _load_config(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        try:
            with open(self.cfg_path) as f:
                cfg = deep_merge(cfg, json.load(f))
        except FileNotFoundError:
            pass
        except ValueError as e:
            print("aws: aws_config.json is not valid JSON (%s) - using defaults" % e, flush=True)
        for k in ("ca_cert", "cert", "key"):
            if not os.path.isabs(cfg[k]):
                cfg[k] = os.path.join(self.here, cfg[k])
        if not os.path.isabs(cfg["queue"]["db_path"]):
            cfg["queue"]["db_path"] = os.path.join(self.here, cfg["queue"]["db_path"])
        return cfg

    def _count(self, kind, stream, nbytes=0):
        with self.lock:
            self.stats[kind][stream] = self.stats[kind].get(stream, 0) + 1
            if kind == "sent":
                self.stats["bytes"][stream] = self.stats["bytes"].get(stream, 0) + nbytes

    def _env(self, **body):
        return dict({"v": SCHEMA_VERSION, "dev": self.cfg["device_id"], "ts": utc_iso(), "gw": GW}, **body)

    def snapshot(self):
        with self.lock:
            s = copy.deepcopy(self.stats)
        s["connected"] = self.connected.is_set()
        s["queued"] = self.queue.depth() if self.queue else 0
        return s

    def examples(self):
        with self.lock:
            return copy.deepcopy(self.last_payload)

    # ------------------------------------------------------------ inputs
    def on_accel(self, k, seq, odr, raw):
        """Called from the Bridge handler thread - must stay quick."""
        now = time.time()
        with self.lock:
            self.odr = odr
            w = self.win[k]
            w.append((now, raw))
            while w and now - w[0][0] > 3.0:
                w.popleft()
            if not self.cfg["raw_accel"]:
                return
            m = self.raw_meta[k]
            if m is None:
                self.raw_meta[k] = [seq, seq, 0, odr, now]
            else:
                if seq > m[1] + 1:
                    m[2] += seq - m[1] - 1
                m[1] = seq
            self.raw[k].append(raw)

    def on_alert(self, t, line):
        self._queue_msg("event", self._env(type="printer_alert", at=utc_iso(t), line=line))

    # ------------------------------------------------------------ message building
    def _queue_msg(self, stream, payload):
        topic = "%s/%s" % (self.p, stream)
        with self.lock:
            self.last_payload[stream] = payload
        if self.cfg["dry_run"]:
            self._count("sent", stream, len(json.dumps(payload)))
            return
        if self.queue:
            self.queue.put(topic, payload)

    def _live_qos0(self, stream, topic, payload_bytes, example=None):
        with self.lock:
            if example is not None:
                self.last_payload[stream] = example
        if self.cfg["dry_run"]:
            self._count("sent", stream, len(payload_bytes))
            return
        if not self.connected.is_set():
            self._count("dropped", stream)
            return
        info = self.client.publish(topic, payload_bytes, qos=0)
        if info.rc == 0:
            self._count("sent", stream, len(payload_bytes))
        else:
            self._count("dropped", stream)

    def build_telemetry(self):
        snap = self.src["printer"]()
        st, L = snap["status"], snap["live"]
        connected = st.get("state") == "connected"
        if not connected or not L.get("t") or time.time() - L["t"] > self.cfg["stale_after_s"]:
            return None, snap
        pct = lambda v: None if v is None else round(v / 127 * 100, 1)
        printing = bool(L.get("printing"))
        msg = self._env(
            # --- phase 1 gateway v1 fields (unchanged meaning) ---
            nozzle={"act": L.get("hotend"), "tgt": L.get("hotend_target"),
                    "pwm": L.get("hotend_pwm"), "pwm_pct": pct(L.get("hotend_pwm"))},
            bed={"act": L.get("bed"), "tgt": L.get("bed_target"),
                 "pwm": L.get("bed_pwm"), "pwm_pct": pct(L.get("bed_pwm"))},
            progress={"printing": printing, "bytes": L.get("sd_done"),
                      "total": L.get("sd_total"),
                      "pct": None if L.get("progress") is None else round(L["progress"], 2)},
            # --- added in v2 (phase 1 monitor Live tab) ---
            pos={k: L.get(k) for k in ("x", "y", "z", "e", "x_act", "y_act", "z_act", "z_corr", "lead")},
            job={"file": L.get("file_long") or None, "print_time_s": L.get("print_time_s"),
                 "slicer_est_s": L.get("slicer_est_s"), "eta_slicer_s": L.get("eta_slicer_s"),
                 "eta_progress_s": L.get("eta_progress_s"), "speed_pct": L.get("speed"),
                 "flow_pct": L.get("flow"), "filament_mm": L.get("filament_mm"),
                 "filament_g": L.get("filament_g")},
            endstops=L.get("endstops") or None,
            sample_t=utc_iso(L["t"]),
        )
        return msg, snap

    def build_vibration(self):
        now = time.time()
        sensors, names = {}, self.cfg["sensor_names"]
        with self.lock:
            odr = self.odr
            wins = {k: [b for t, b in self.win[k] if now - t <= self.cfg["vibration_interval_s"] + 0.05]
                    for k in (0, 1)}
        for k in (0, 1):
            raw = b"".join(wins[k])
            if len(raw) >= 6 * 32:
                sensors[names[k]] = vib_summary.summarize(raw, odr)
        if not sensors:
            return None
        return self._env(odr=odr, window_s=self.cfg["vibration_interval_s"], sensors=sensors)

    def flush_raw(self, force=False):
        names = self.cfg["sensor_names"]
        for k in (0, 1):
            with self.lock:
                m = self.raw_meta[k]
                if m is None or not self.raw[k]:
                    continue
                n = sum(len(b) for b in self.raw[k]) // 6
                if not force and n < m[3] * self.cfg["raw_block_s"]:
                    continue
                data = b"".join(self.raw[k])
                self.raw[k] = []
                self.raw_meta[k] = None
            payload = self._env(sensor=names[k], odr=m[3], n=n, fmt="int16le_xyz",
                                scale_g_per_lsb=vib_summary.G_PER_LSB,
                                t_first_rx=utc_iso(m[4]), seq_first=m[0], seq_last=m[1],
                                gaps=m[2], b64=base64.b64encode(data).decode())
            body = json.dumps(payload, separators=(",", ":")).encode()
            example = dict(payload, b64=payload["b64"][:48] + "...")
            self._live_qos0("accel_raw", "%s/accel_raw" % self.p, body, example)

    def snapshot_jpeg(self):
        jpg = self.src["jpeg"]()
        if not jpg:
            return None
        limit = int(self.cfg["snapshot_max_kb"]) * 1024
        try:
            import cv2
            import numpy as np
        except Exception:
            return jpg if len(jpg) <= limit else None
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return jpg if len(jpg) <= limit else None
        h, w = img.shape[:2]
        maxw = int(self.cfg["snapshot_max_width"])
        if len(jpg) <= limit and w <= maxw:
            return jpg
        if w > maxw:
            img = cv2.resize(img, (maxw, int(h * maxw / w)), interpolation=cv2.INTER_AREA)
        for q in (85, 75, 65, 55, 45, 35):
            ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
            if ok and len(enc) <= limit:
                return enc.tobytes()
        return None

    def publish_setup(self, force=False):
        seq, setup, mesh, mesh_info = self.src["setup"]()
        if not setup or (seq == self.setup_seq_sent and not force):
            return
        payload = self._env(setup=setup, mesh=mesh, mesh_info=mesh_info)
        with self.lock:
            self.last_payload["setup"] = payload
        if self.cfg["dry_run"]:
            self.setup_seq_sent = seq
            self._count("sent", "setup")
            return
        if self.connected.is_set():
            body = json.dumps(payload, separators=(",", ":"), default=str)
            info = self.client.publish("%s/setup" % self.p, body, qos=1, retain=True)
            if info.rc == 0:
                self.setup_seq_sent = seq
                self._count("sent", "setup", len(body))

    def _status_payload(self, state, printer_state=None):
        return json.dumps(self._env(state=state, printer=printer_state), separators=(",", ":"))

    # ------------------------------------------------------------ mqtt
    def _make_client(self):
        import paho.mqtt.client as mqtt
        self.mqtt = mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg["client_id"],
                        clean_session=True, protocol=mqtt.MQTTv311)
        c.tls_set(ca_certs=self.cfg["ca_cert"], certfile=self.cfg["cert"], keyfile=self.cfg["key"])
        c.will_set("%s/status" % self.p, self._status_payload("offline"), qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.max_inflight_messages_set(int(self.cfg["max_inflight"]))
        c.max_queued_messages_set(500)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_publish = self._on_publish
        return c

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            with self.lock:
                self.stats["state"] = "connect refused: %s" % reason_code
                self.stats["last_error"] = str(reason_code)
            print("aws: connect refused:", reason_code, flush=True)
            return
        with self.lock:
            self.stats["state"] = "connected"
            self.stats["connects"] += 1
        print("aws: connected to %s as %s" % (self.cfg["endpoint"], self.cfg["client_id"]), flush=True)
        st = self.src["printer"]()["status"].get("state")
        client.publish("%s/status" % self.p, self._status_payload("online", st), qos=1, retain=True)
        self.printer_state_sent = st
        self.connected.set()
        self.publish_setup(force=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        with self.lock:
            self.inflight.clear()        # unacked rows stay on disk and are re-sent
            self.stats["state"] = "disconnected (%s) - reconnecting" % reason_code
        print("aws: disconnected:", reason_code, flush=True)

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        with self.lock:
            entry = self.inflight.pop(mid, None)
        if entry:
            rid, stream, nbytes = entry[0], entry[2], entry[3]
            self.queue.delete(rid)
            self._count("sent", stream, nbytes)

    def pump_queue(self):
        if not self.connected.is_set():
            return
        now = time.monotonic()
        with self.lock:
            for mid, e in list(self.inflight.items()):
                if now - e[1] > self.cfg["ack_timeout_s"]:
                    del self.inflight[mid]
            room = int(self.cfg["max_inflight"]) - len(self.inflight)
            busy = {e[0] for e in self.inflight.values()}
        if room <= 0:
            return
        for rid, topic, payload in self.queue.peek(room, exclude=busy):
            payload["seq"] = rid
            body = json.dumps(payload, separators=(",", ":"), default=str)
            info = self.client.publish(topic, body, qos=1)
            if info.rc != 0:
                break
            stream = topic[len(self.p) + 1:]
            with self.lock:
                self.inflight[info.mid] = (rid, now, stream, len(body))

    # ------------------------------------------------------------ main loop
    def _certs_missing(self):
        return [k for k in ("ca_cert", "cert", "key") if not os.path.exists(self.cfg[k])]

    def run(self):
        cfg = self.cfg
        if not cfg["enabled"]:
            with self.lock:
                self.stats["state"] = "disabled in aws_config.json"
            return
        if not cfg["dry_run"]:
            try:
                import paho.mqtt.client  # noqa: F401
            except Exception as e:
                with self.lock:
                    self.stats["state"] = "paho-mqtt missing (python/requirements.txt): %s" % e
                print("aws: paho-mqtt not installed:", e, flush=True)
                return
            while True:
                missing = self._certs_missing()
                if not missing and "REPLACE-ME" not in cfg["endpoint"]:
                    break
                why = ("endpoint not set in aws_config.json" if not missing else
                       "waiting for certificates: " + ", ".join(os.path.relpath(cfg[k], self.here) for k in missing))
                with self.lock:
                    self.stats["state"] = why
                if self.stop_flag.wait(30):
                    return
                self.cfg = cfg = self._load_config()
            self.queue = DiskQueue(cfg["queue"]["db_path"], cfg["queue"]["max_rows"])
            self.client = self._make_client()
            with self.lock:
                self.stats["state"] = "connecting to %s" % cfg["endpoint"]
            self.client.connect_async(cfg["endpoint"], int(cfg["port"]), keepalive=int(cfg["keepalive_s"]))
            self.client.loop_start()
        else:
            with self.lock:
                self.stats["state"] = "dry run (nothing sent)"

        t0 = time.monotonic()
        next_tel = next_vib = t0 + 2
        next_snap = t0 + 5
        while not self.stop_flag.is_set():
            now = time.monotonic()
            try:
                if now >= next_tel:
                    next_tel = now + cfg["telemetry_interval_s"]
                    msg, snap = self.build_telemetry()
                    if msg:
                        self._queue_msg("telemetry", msg)
                    self._print_events(snap)
                    self._printer_status(snap)
                if now >= next_vib:
                    next_vib = now + cfg["vibration_interval_s"]
                    msg = self.build_vibration()
                    if msg:
                        self._queue_msg("vibration", msg)
                if cfg["raw_accel"]:
                    self.flush_raw()
                if cfg["snapshot_interval_s"] and now >= next_snap:
                    next_snap = now + cfg["snapshot_interval_s"]
                    jpg = self.snapshot_jpeg()
                    if jpg:
                        ms = int(time.time() * 1000)
                        self._live_qos0("snapshot", "%s/snapshot/%d" % (self.p, ms), jpg,
                                        {"topic": "%s/snapshot/%d" % (self.p, ms), "bytes": len(jpg),
                                         "content": "binary JPEG"})
                self.publish_setup()
                if self.queue:
                    self.pump_queue()
            except Exception as e:                     # keep publishing whatever happens
                with self.lock:
                    self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
                print("aws: error:", e, flush=True)
            self.stop_flag.wait(0.1)

    def _print_events(self, snap):
        L = snap["live"]
        if snap["status"].get("state") != "connected" or "printing" not in L:
            return
        printing = bool(L["printing"])
        if self.was_printing is not None and printing != self.was_printing:
            if printing:
                self._queue_msg("event", self._env(type="print_started", file=L.get("file_long") or None))
            else:
                self._queue_msg("event", self._env(type="print_finished", print_time_s=L.get("print_time_s"),
                                                   filament_mm=L.get("filament_mm")))
        self.was_printing = printing

    def _printer_status(self, snap):
        st = snap["status"].get("state")
        if st != self.printer_state_sent and self.connected.is_set() and not self.cfg["dry_run"]:
            self.client.publish("%s/status" % self.p, self._status_payload("online", st), qos=1, retain=True)
            self.printer_state_sent = st

    def stop(self):
        self.stop_flag.set()
        if self.client and self.connected.is_set():
            try:
                info = self.client.publish("%s/status" % self.p, self._status_payload("offline"), qos=1, retain=True)
                info.wait_for_publish(timeout=3)
                self.client.disconnect()
            finally:
                self.client.loop_stop()
