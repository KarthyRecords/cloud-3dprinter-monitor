"""Quick local dashboard: shows what the printer is sending to AWS IoT Core.

Reads the DynamoDB tables and S3 bucket created by iot-ingest/ with your own
AWS credentials. Read-only. Run it on your laptop; it listens on localhost only.

    python app.py            # then open http://127.0.0.1:8080
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import jwt
from boto3.dynamodb.conditions import Key
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "eu-west-1")
# the "dev" field the board puts in every message (device_id in aws_config.json),
# not the MQTT client id
DEVICE = os.environ.get("DEVICE_ID", "kobra2neo")
TELEMETRY_TABLE = os.environ.get("TELEMETRY_TABLE", "printer-monitor-telemetry")
STATE_TABLE = os.environ.get("STATE_TABLE", "printer-monitor-state")
BUCKET = os.environ.get("DATA_BUCKET", "printer-monitor-data-045774533738-eu-west-1")
PORT = int(os.environ.get("PORT", "8080"))
# localhost for a laptop run; the container image sets HOST=0.0.0.0 so the ALB can reach it
HOST = os.environ.get("HOST", "127.0.0.1")

session = boto3.Session(region_name=REGION)
ddb = session.resource("dynamodb")
# SigV4 presigned URLs also work with the temporary credentials of an ECS task role
s3 = session.client("s3", config=Config(signature_version="s3v4"))

# Cognito login. Off for a laptop run; the ECS task sets AUTH_ENABLED=1 and the rest.
AUTH_ENABLED = os.environ.get("AUTH_ENABLED") == "1"
SESSION_SECONDS = 3600
if AUTH_ENABLED:
    PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")  # https://<id>.cloudfront.net
    COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"].rstrip("/")
    CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
    CLIENT_SECRET = os.environ["COGNITO_CLIENT_SECRET"]
    SESSION_KEY = os.environ["SESSION_KEY"].encode()
    ISSUER = "https://cognito-idp.%s.amazonaws.com/%s" % (REGION, os.environ["COGNITO_USER_POOL_ID"])
    jwks = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json")  # caches the signing keys


def sign(value):
    return hmac.new(SESSION_KEY, value.encode(), hashlib.sha256).hexdigest()


def make_session(email, lifetime=SESSION_SECONDS):
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "exp": int(time.time()) + lifetime}).encode()
    ).decode()
    return payload + "." + sign(payload)


def read_session(value):
    """The session dict if the cookie is genuine and not expired, else None."""
    try:
        payload, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, sign(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if data["exp"] > time.time() else None
    except Exception:
        return None


def exchange_code(code):
    """Swap the one-time login code for tokens, server to server, with the client secret."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "redirect_uri": PUBLIC_URL + "/callback",
    }).encode()
    basic = base64.b64encode(("%s:%s" % (CLIENT_ID, CLIENT_SECRET)).encode()).decode()
    req = urllib.request.Request(
        COGNITO_DOMAIN + "/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": "Basic " + basic},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def verify_id_token(token):
    """Signature, issuer, audience and expiry are all checked by PyJWT."""
    key = jwks.get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=CLIENT_ID, issuer=ISSUER)
    if claims.get("token_use") != "id":
        raise ValueError("not an ID token")
    return claims


NOT_APPROVED = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Waiting for approval</title>
<body style="font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px">
<h1>Waiting for approval</h1>
<p>Your account is registered, but it has not been approved for this dashboard yet.
Once it is, sign in again.</p>
<p><a href="/logout">Sign out</a></p></body>"""


def clean(value):
    """DynamoDB returns Decimal; JSON needs int/float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def latest(kind, limit=20):
    """Newest items of one stream (telemetry, vibration, event), newest first."""
    resp = ddb.Table(TELEMETRY_TABLE).query(
        KeyConditionExpression=Key("pk").eq("%s#%s" % (DEVICE, kind)),
        ScanIndexForward=False,
        Limit=limit,
    )
    return clean(resp["Items"])


def state():
    out = {}
    for sk in ("status", "setup"):
        item = ddb.Table(STATE_TABLE).get_item(Key={"pk": DEVICE, "sk": sk}).get("Item")
        out[sk] = clean(item) if item else None
    return out


def snapshot():
    """Presigned URL of the newest snapshot, or None.

    Keys are kobra/snapshots/<unix ms>.jpg and S3 lists them in ascending order,
    1000 per page, so a plain list returns the OLDEST. Start the listing just
    before "now" instead, and fall back to a full listing if the board was quiet.
    """
    prefix = "kobra/snapshots/"
    newest = None
    for window_ms in (10 * 60 * 1000, None):
        kwargs = {"Bucket": BUCKET, "Prefix": prefix}
        if window_ms:
            kwargs["StartAfter"] = "%s%d.jpg" % (prefix, time.time() * 1000 - window_ms)
        for page in s3.get_paginator("list_objects_v2").paginate(**kwargs):
            for o in page.get("Contents", []):
                if newest is None or o["LastModified"] > newest["LastModified"]:
                    newest = o
        if newest:
            break
    if not newest:
        return None
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": newest["Key"]}, ExpiresIn=300
    )
    return {"url": url, "key": newest["Key"], "at": newest["LastModified"].isoformat()}


def collect():
    data = {"device": DEVICE, "region": REGION, "errors": {}}
    for name, fn in (
        ("state", state),
        ("telemetry", lambda: latest("telemetry", 120)),  # ~4 min at one reading / 2 s
        ("vibration", lambda: latest("vibration", 60)),
        ("events", lambda: latest("event", 10)),
        ("snapshot", snapshot),
    ):
        try:
            data[name] = fn()
        except Exception as exc:  # show the problem in the page instead of a blank screen
            data[name] = None
            data["errors"][name] = str(exc)
    return data


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Printer monitor</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2128;--mute:#667085;--line:#e4e7ec;--ok:#0a7d33;--bad:#b42318;
  --c1:#2563eb;--c2:#ea580c;--c3:#0d9488}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c23;--fg:#e6e9ee;--mute:#98a2b3;--line:#2a323d;--ok:#4ade80;--bad:#f87171;
  --c1:#60a5fa;--c2:#fb923c;--c3:#2dd4bf}}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-bottom:6px;font-size:12px;color:var(--mute)}
.legend i{display:inline-block;width:16px;height:0;border-top:2px solid;vertical-align:middle;margin-right:5px}
.chart text{fill:var(--mute);font-size:13px}.chart .grid{stroke:var(--line)}
.chart svg{width:100%;height:auto;display:block}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:13px;margin:0 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--mute);margin-bottom:16px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px}.kv b{color:var(--mute);font-weight:500}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:500}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-weight:600}
.ok{color:var(--ok)}.bad{color:var(--bad)}img{max-width:100%;border-radius:6px}
.err{color:var(--bad);white-space:pre-wrap}
</style></head><body><main>
<h1>Printer monitor <a href="/logout" style="float:right;font-size:13px;font-weight:400;color:var(--mute)">Sign out</a></h1>
<div class="sub" id="sub">loading…</div>
<div class="grid">
  <div class="card"><h2>Status</h2><div id="status"></div></div>
  <div class="card"><h2>Latest snapshot</h2><div id="snap"></div></div>
</div>
<div class="grid" style="margin-top:12px">
  <div class="card"><h2>Temperatures (°C)</h2><div class="chart" id="ch-temp"></div></div>
  <div class="card"><h2>Vibration, total Grms (g)</h2><div class="chart" id="ch-vib"></div></div>
  <div class="card"><h2>Spectrum by band: toolhead (Grms g, Hz)</h2><div class="chart" id="ch-spec-toolhead"></div></div>
  <div class="card"><h2>Spectrum by band: table (Grms g, Hz)</h2><div class="chart" id="ch-spec-table"></div></div>
</div>
<div class="card" style="margin-top:12px"><h2>Latest telemetry, all fields</h2><div id="latest"></div></div>
<div class="card" style="margin-top:12px"><h2>Recent telemetry</h2><div id="tel"></div></div>
<div class="grid" style="margin-top:12px">
  <div class="card"><h2>Vibration</h2><div id="vib"></div></div>
  <div class="card"><h2>Events</h2><div id="evt"></div></div>
</div>
<div class="card err" id="errs" style="margin-top:12px;display:none"></div>
</main>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const val=v=>typeof v==="object"&&v!==null?esc(JSON.stringify(v)):esc(v);
// {nozzle:{act:22}} -> {"nozzle.act":22}, so nested payloads read as flat fields
function flat(o,p=""){const r={};for(const [k,v] of Object.entries(o||{})){
  if(v&&typeof v==="object"&&!Array.isArray(v))Object.assign(r,flat(v,p+k+"."));else r[p+k]=v}return r}
function kv(o,skip=[]){return '<div class="kv">'+Object.entries(flat(o)).filter(([k])=>!skip.includes(k))
  .map(([k,v])=>'<b>'+esc(k)+'</b><span>'+val(v)+'</span>').join('')+'</div>'}
function table(rows){
  if(!rows||!rows.length)return '<span style="color:var(--mute)">no data yet</span>';
  rows=rows.map(r=>flat(r));
  const cols=[...new Set(rows.flatMap(r=>Object.keys(r)))].filter(c=>!["pk","ttl","v","gw","dev"].includes(c));
  return '<table><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'+
    rows.map(r=>'<tr>'+cols.map(c=>'<td>'+(c in r?val(r[c]):'')+'</td>').join('')+'</tr>').join('')+'</table>'}
const num=v=>typeof v==="number"&&isFinite(v);
const fmt=v=>Math.abs(v)>=100?v.toFixed(0):Math.abs(v)>=1?v.toFixed(1):v.toFixed(3);
// Plain SVG line chart. series: [{name,color,data:[...],dash?}]; null values leave a gap.
function lineChart(id,labels,series,showAll){
  const el=document.getElementById(id),n=labels.length;
  if(n<2){el.innerHTML='<span style="color:var(--mute)">waiting for data…</span>';return}
  const W=520,H=210,L=46,R=10,T=8,B=22;
  const vals=series.flatMap(s=>s.data).filter(num);
  if(!vals.length){el.innerHTML='<span style="color:var(--mute)">no values yet</span>';return}
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(lo===hi){lo-=1;hi+=1}
  const pad=(hi-lo)*.08;hi+=pad;lo=lo>=0&&lo-pad<0?0:lo-pad;
  const x=i=>L+(W-L-R)*i/(n-1),y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo));
  let g='';
  for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;
    g+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v)+'" y2="'+y(v)+'"/><text x="'+(L-6)+'" y="'+(y(v)+3)+'" text-anchor="end">'+fmt(v)+'</text>'}
  const ticks=showAll?labels.map((_,i)=>i).filter(i=>i%2===0):[0,Math.floor((n-1)/2),n-1];
  ticks.forEach(i=>{g+='<text x="'+x(i)+'" y="'+(H-6)+'" text-anchor="'+(i===0?'start':i===n-1?'end':'middle')+'">'+esc(labels[i])+'</text>'});
  series.forEach(s=>{let d='',pen=false;
    s.data.forEach((v,i)=>{if(!num(v)){pen=false;return}d+=(pen?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1);pen=true});
    g+='<path d="'+d+'" fill="none" stroke="'+s.color+'" stroke-width="1.75" stroke-linejoin="round"'+(s.dash?' stroke-dasharray="5 4"':'')+'/>'});
  const w=(W-L-R)/Math.max(n-1,1);
  labels.forEach((lb,i)=>{const tip=lb+'\\n'+series.map(s=>s.name+': '+(num(s.data[i])?fmt(s.data[i]):'-')).join('\\n');
    g+='<rect x="'+(x(i)-w/2)+'" y="'+T+'" width="'+w+'" height="'+(H-T-B)+'" fill="transparent"><title>'+esc(tip)+'</title></rect>'});
  el.innerHTML='<div class="legend">'+series.map(s=>'<span><i style="border-color:'+s.color+(s.dash?';border-top-style:dashed':'')+'"></i>'+esc(s.name)+'</span>').join('')+'</div>'
    +'<svg viewBox="0 0 '+W+' '+H+'" role="img">'+g+'</svg>';
}
const hhmmss=ts=>new Date(ts).toLocaleTimeString();
function charts(d){
  const t=(d.telemetry||[]).slice().reverse();
  lineChart('ch-temp',t.map(r=>hhmmss(r.ts)),[
    {name:'Nozzle',color:'var(--c2)',data:t.map(r=>r.nozzle&&r.nozzle.act)},
    {name:'Nozzle target',color:'var(--c2)',dash:1,data:t.map(r=>r.nozzle&&r.nozzle.tgt)},
    {name:'Bed',color:'var(--c1)',data:t.map(r=>r.bed&&r.bed.act)},
    {name:'Bed target',color:'var(--c1)',dash:1,data:t.map(r=>r.bed&&r.bed.tgt)}]);
  const v=(d.vibration||[]).slice().reverse();
  const tot=s=>v.map(r=>r.sensors&&r.sensors[s]&&r.sensors[s].grms_total_g);
  lineChart('ch-vib',v.map(r=>hhmmss(r.ts)),[
    {name:'Toolhead',color:'var(--c2)',data:tot('toolhead')},
    {name:'Table',color:'var(--c1)',data:tot('table')}]);
  const last=v[v.length-1];
  ['toolhead','table'].forEach(s=>{
    const sen=last&&last.sensors&&last.sensors[s];
    if(!sen||!sen.bands_grms_g){lineChart('ch-spec-'+s,[],[]);return}
    const labels=(sen.bands_hz||[]).map(b=>b[0]+'-'+b[1]);
    lineChart('ch-spec-'+s,labels,[
      {name:'x',color:'var(--c1)',data:sen.bands_grms_g.x},
      {name:'y',color:'var(--c2)',data:sen.bands_grms_g.y},
      {name:'z',color:'var(--c3)',data:sen.bands_grms_g.z}],true)});
}
async function tick(){
  try{
    const r=await fetch('/api/data');
    if(r.status===401){location.href='/login';return}
    const d=await r.json();
    document.getElementById('sub').textContent=d.device+' · '+d.region+' · updated '+new Date().toLocaleTimeString();
    const st=d.state&&d.state.status;
    document.getElementById('status').innerHTML=st
      ?'<p><span class="pill '+(st.state==="online"?"ok":"bad")+'">'+esc(st.state||"?")+'</span></p>'+kv(st,["pk","sk","state"])
      :'<span style="color:var(--mute)">no status received yet</span>';
    const t=d.telemetry||[];
    document.getElementById('latest').innerHTML=t.length?kv(t[0],["pk","ttl"]):'<span style="color:var(--mute)">no data yet</span>';
    // the presigned URL changes on every call, so only swap the image when the object changes
    const snap=document.getElementById('snap');
    if(!d.snapshot){snap.innerHTML='<span style="color:var(--mute)">no snapshot yet</span>'}
    else{
      if(snap.dataset.key!==d.snapshot.key){
        snap.dataset.key=d.snapshot.key;
        snap.innerHTML='<img alt="snapshot" src="'+d.snapshot.url+'"><div class="sub"></div>'}
      snap.querySelector('.sub').textContent=d.snapshot.at+' · '+Math.round((Date.now()-new Date(d.snapshot.at))/1000)+' s ago'}
    charts(d);
    document.getElementById('tel').innerHTML=table(t.slice(0,20));
    const tg=(r,s)=>r.sensors&&r.sensors[s]&&r.sensors[s].grms_total_g;
    document.getElementById('vib').innerHTML=table((d.vibration||[]).slice(0,10).map(r=>
      ({seq:r.seq,ts:r.ts,'toolhead grms (g)':tg(r,'toolhead'),'table grms (g)':tg(r,'table')})));
    document.getElementById('evt').innerHTML=table(d.events);
    const e=Object.entries(d.errors||{});
    const box=document.getElementById('errs');
    box.style.display=e.length?'block':'none';
    box.textContent=e.map(([k,v])=>k+': '+v).join('\\n');
  }catch(x){document.getElementById('sub').textContent='cannot reach the local server: '+x}
}
tick();setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype, cookies=(), location=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if location:
            self.send_header("Location", location)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, cookies=()):
        self.send(302, b"", "text/plain", cookies, location)

    def cookie(self, name):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return jar[name].value if name in jar else None

    def session(self):
        value = self.cookie("session")
        return read_session(value) if value else None

    # login flow ------------------------------------------------------------------
    def login(self):
        state_value = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": PUBLIC_URL + "/callback",
            "scope": "openid email",
            "state": state_value,
        })
        # the same value must come back in the callback: proves this browser started the login
        cookie = "oauth_state=%s; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax" % state_value
        self.redirect(COGNITO_DOMAIN + "/oauth2/authorize?" + query, [cookie])

    def callback(self, query):
        params = urllib.parse.parse_qs(query)
        code = params.get("code", [""])[0]
        returned = params.get("state", [""])[0]
        expected = self.cookie("oauth_state") or ""
        clear_state = "oauth_state=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        if not code or not expected or not hmac.compare_digest(returned, expected):
            self.send(400, b"login failed, please try again: /login", "text/plain", [clear_state])
            return
        try:
            claims = verify_id_token(exchange_code(code)["id_token"])
        except Exception as exc:
            print("login rejected: %r" % exc, flush=True)
            self.send(400, b"login failed, please try again: /login", "text/plain", [clear_state])
            return
        if "approved" not in claims.get("cognito:groups", []):
            print("not approved: %s" % claims.get("email"), flush=True)
            self.send(403, NOT_APPROVED, "text/html; charset=utf-8", [clear_state])
            return
        session = "session=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (
            make_session(claims.get("email", "?")), SESSION_SECONDS)
        self.redirect("/", [clear_state, session])

    def logout(self):
        clear = "session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        if not AUTH_ENABLED:
            self.redirect("/", [clear])
            return
        query = urllib.parse.urlencode({"client_id": CLIENT_ID, "logout_uri": PUBLIC_URL + "/"})
        self.redirect(COGNITO_DOMAIN + "/logout?" + query, [clear])

    # routes ----------------------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == "/healthz":  # ALB health check: must not depend on AWS calls or login
            self.send(200, b"ok", "text/plain")
            return
        if path == "/logout":
            self.logout()
            return
        if AUTH_ENABLED:
            if path == "/login":
                self.login()
                return
            if path == "/callback":
                self.callback(url.query)
                return
            if not self.session():
                if path == "/api/data":
                    self.send(401, b'{"error": "login required"}', "application/json")
                elif path in ("/", "/index.html"):
                    self.redirect("/login")
                else:
                    self.send_error(404)
                return
        if path == "/api/data":
            self.send(200, json.dumps(collect()).encode(), "application/json")
        elif path in ("/", "/index.html"):
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("Dashboard on http://%s:%d  (device %s, %s)" % (HOST, PORT, DEVICE, REGION), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
