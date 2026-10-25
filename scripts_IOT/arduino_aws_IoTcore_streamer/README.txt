printer_monitor_aws_v1 - local web page (as v13) + AWS IoT Core publishing

Everything printer_monitor_v13 does, plus python/aws_publisher.py sends the
data to AWS IoT Core over MQTT/TLS (port 8883) with the board's X.509
certificate. Spec for the AWS side (thing, policy, topics, payloads): the
"Kobra 2 Neo to AWS IoT Core: device spec" doc shared with Karthika.

Topics (prefix printer/kobra)
  telemetry      every 2 s      QoS 1, buffered on disk while offline
  vibration      every 2 s      QoS 1, buffered   (Grms, peaks, PSD bands)
  event          when it occurs QoS 1, buffered   (alerts, print start/end)
  accel_raw      1 s / sensor   QoS 0, live only  (400 Hz int16, base64)
  snapshot/<ms>  every 30 s     QoS 0, live only  (binary JPEG <= 120 kB)
  setup          on change      retained          (settings + bed mesh)
  status         on change      retained          (online / offline Last Will)
The board never subscribes: nothing in AWS can command it or the printer.

New files in python/
  aws_publisher.py   MQTT client, disk queue (aws_queue.db), Last Will
  vib_summary.py     Grms / PSD summaries (same maths as the web page)
  aws_config.json    endpoint, client id, intervals, raw/snapshot on/off
  requirements.txt   paho-mqtt (App Lab installs it on the next start)
  certs/             put the 3 certificate files here (see certs/README.txt)
main.py, printer.py and index.html are updated (AWS status card + /aws.json).
Sketch unchanged from v9; still needs ch340_bridge_v3 on the board.

Install
  1. From PowerShell in ...\arduino_uno_q :
       scp -r printer_monitor_aws_v1\python\* arduino@192.168.0.42:~/ArduinoApps/3dprinter-dualaccel-camera-oled/python/
  2. Stop + Run the app. The first start installs paho-mqtt (needs
     internet, takes a minute). The page's AWS card then says
     "waiting for certificates".
  3. When Karthika sends the files and the endpoint:
       scp AmazonRootCA1.pem device.pem.crt private.pem.key arduino@192.168.0.42:~/ArduinoApps/3dprinter-dualaccel-camera-oled/python/certs/
     then on the board set "endpoint" in python/aws_config.json:
       nano ~/ArduinoApps/3dprinter-dualaccel-camera-oled/python/aws_config.json
     and restart the app. The AWS card turns green and counts messages.

Notes
  - TLS needs the board's clock right: `timedatectl` should say
    "System clock synchronized: yes".
  - private.pem.key is a secret: keep it only on the board.
  - "dry_run": true builds every message without connecting; examples show
    at http://<board-ip>:7001/aws.json (handy for Karthika before the certs).
  - "raw_accel": false stops the raw stream (two thirds of all messages);
    "snapshot_interval_s": 0 stops snapshots.
  - Tested against a mutual-TLS MQTT broker: all 7 topics, correct rates and
    sizes, 108/108 queued messages delivered across a 32 s outage, retained
    status/setup, Last Will "offline" after a hard kill.
