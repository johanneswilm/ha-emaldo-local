#!/usr/bin/env python3
"""Fake Emaldo cloud API for module takeover (learn-phase clone, v2).

Serves the module's gm2 HTTP API from behind your TLS reverse proxy
(your hostname + valid certificate), answering
/bmt/e2e-bmt-login/ with OUR e2e credentials and OUR relay address so the
module dials our relay directly (no DNAT needed). Everything else gets
plausible answers; all requests are logged to fake_api.log.

Run: python fake_api.py [port]  (default 8199)
"""
import base64
import http.server
import json
import os
import secrets
import socketserver
import string
import sys
import threading
import time
import urllib.parse
import urllib.request

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

APP_SECRET = b"FpF4Uqiio9k8p9VUSX36UZxy9wLs7ybT"
RELAY_HOST = os.environ.get("EMALDO_RELAY_HOST", "127.0.0.1:1050")
UTIL_HOST = os.environ.get("EMALDO_UTIL_HOST", "127.0.0.1:1051")
PROD_API = os.environ.get("EMALDO_PROD_API", "https://api.emaldo.com")
OFFLINE = os.environ.get("EMALDO_OFFLINE", "") == "1"

# All state files (logs, captured identity, corpus, rotation state) live
# under $EMALDO_HOME (default: this script's directory).
BASE = os.environ.get("EMALDO_HOME", os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(BASE, "fake_api.log")
STATIC_E2E_PATH = os.path.join(BASE, "e2e_static.json")
ALNUM = string.ascii_letters + string.digits

# Fixed per-run fake e2e credentials for the module.
def rand32():
    return "".join(secrets.choice(ALNUM) for _ in range(32))

# The module's registration JWT, captured from its own boot login while
# the vendor still exists (see README capture stage). Replayed verbatim;
# without it the module may refuse to stay on your API.
FAKE_REG_TOKEN = os.environ.get("EMALDO_REG_TOKEN", "")

E2E = {
    "id": "bmt",
    "group_id": rand32() + "@0",
    "end_id": rand32(),
    "end_secret": rand32(),
    "chat_secret": rand32(),
    "host": RELAY_HOST,
    "util_host": RELAY_HOST.replace(":1050", ":1051"),
    "addtime": int(time.time() * 1000) * 10**6,
}
# Roster ends the module will accept as app senders:
APP_END = rand32()
HOME_END = rand32()
print("fake e2e creds:", json.dumps(E2E, indent=1), flush=True)
print("app end:", APP_END, " home end:", HOME_END, flush=True)
json.dump({"e2e": E2E, "app_end": APP_END, "home_end": HOME_END},
          open(os.path.join(BASE, "fake_api_creds.json"), "w"), indent=1)


def rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for c in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(c ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def try_decode(body: bytes):
    txt = body.decode("latin1")
    parts = {}
    for field in txt.split("&"):
        if "=" in field:
            k, v = field.split("=", 1)
            parts[k] = v
        elif field:
            parts["_raw"] = field
    out = {}
    for k, v in parts.items():
        try:
            out[k] = rc4(APP_SECRET, bytes.fromhex(v)).decode("utf-8", "replace")
        except Exception:
            out[k] = v[:400]
    return out


def fake_result(path: str):
    p = path.lower()
    gm = int(time.time() * 1000) * 10**6
    if "e2e-bmt-login" in p:
        r = dict(E2E)
        r["token"] = FAKE_REG_TOKEN
        return r
    if "e2e" in p:
        return dict(E2E)
    if "get-latest-ver-url" in p:
        return {"versions": [], "count": 0, "gmtime": gm}
    if "get_master_switch" in p:
        return {"switch": 1, "gmtime": gm}
    if "get-default-price-percent" in p:
        return {"percent": 0, "gmtime": gm}
    if "iot-online" in p:
        return {"gmtime": gm}
    if "global-config" in p or "config_area" in p:
        return {"config": {}, "gmtime": gm}
    return {"gmtime": gm}


def mirror_to_prod(path: str, body: bytes):
    """Fire-and-forget copy of module metric uploads to the live vendor API
    so the HA cloud integration keeps fresh data while the module talks to
    us. Body is forwarded verbatim (valid under the shared APP_SECRET)."""
    def _go():
        try:
            req = urllib.request.Request(
                PROD_API + path, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            print("mirror fail", path.split("/")[2], repr(exc), flush=True)
    threading.Thread(target=_go, daemon=True).start()


_PROD_CACHE = {}
PROD_LOGIN_TTL = float(os.environ.get("EMALDO_PROD_TTL", "300"))

ACTIVE_E2E_PATH = os.path.join(BASE, "e2e_active.json")
E2E_ROTATE_S = float(os.environ.get("EMALDO_ROTATE_S", "1500"))


def active_e2e():
    """Live app-session chat_secret, rotated on a timer.

    The battery re-rolls its app-facing session at ~1 h: it takes whatever
    chat_secret the vendor login handed it at that moment (and vendor logins
    churn chat_secret), then goes deaf to any older key (observed
    2026-11-17 13:15:23 = exactly 60 min after the app creds fetch at
    12:15:23 -- pinned replay keeps the battery dead). Vendor semantics:
    chat rotates when the APP re-logins; battery learns the new value on its
    next 10 s login. Emulate: rotate on our timer; the battery adopts it
    <=10 s later, app clients re-fetch through the same 21204
    force-refresh dance they used against the vendor's own rotations.
    """
    now = time.time()
    try:
        d = json.load(open(ACTIVE_E2E_PATH))
        if now - d.get("_ts", 0) < E2E_ROTATE_S and d.get("chat_secret"):
            return d
    except Exception:
        pass
    st = static_e2e() or {}
    d = {"chat_secret": rand32(),
         "home_chat_secret": rand32(),
         "group_id": st.get("group_id") or st.get("sender_group_id"),
         "_ts": now}
    with open(ACTIVE_E2E_PATH, "w") as f:
        json.dump(d, f, indent=1)
    os.chmod(ACTIVE_E2E_PATH, 0o600)
    print("e2e chat rotated ->", d["chat_secret"][:8],
          d["group_id"], flush=True)
    return d


def static_e2e():
    """Real e2e identity captured from production (survives bankruptcy)."""
    try:
        return json.load(open(STATIC_E2E_PATH))
    except Exception:
        return None


GM1_CORPUS_PATH = os.path.join(BASE, "gm1_corpus.jsonl")


def _gm1_snappy_try(raw: bytes) -> bytes:
    try:
        import cramjam
        return bytes(cramjam.snappy.decompress_raw(raw))
    except Exception:
        return raw


def _corpus_key(path: str) -> str:
    """Match key for corpus entries. DP stats paths share the /bmt/stats
    prefix and differ only in segment 4 -- collapse there would replay a
    grid day over a schedule request."""
    segs = path.split("/")
    if len(segs) > 3 and segs[2] == "stats":
        return "/".join(segs[:4])
    return "/".join(segs[:3])


def gm1_corpus_lookup(path: str, want_ok: bool = False):
    """Replay the most recently recorded vendor response for a path.

    want_ok: skip vendor error responses (Status != 1) — DP stats paths
    return Status 0 with HTTP 200 since the vendor unbound the BMT, so
    the newest entry may be an error we do not want to replay.
    """
    key = _corpus_key(path)
    last = None
    try:
        with open(GM1_CORPUS_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if _corpus_key(rec["path"]) == key:
                    if want_ok:
                        try:
                            env = json.loads(rec["resp"])
                            if env.get("Status") != 1 or not env.get("Result"):
                                continue
                        except Exception:
                            continue
                    last = rec["resp"]
    except FileNotFoundError:
        return None
    return last.encode() if last else None


APP_DEV = {"id": "<your-bmt-id>", "name": "Power Core 2.0",
           "model": "PC1-BAK15-HS10", "country_code": "SE",
           "delivery_area": "SE1", "addr": ""}


def _gm1_synth(path: str):
    """Local answers for app-API paths whose data we own (vendor-side
    records decay: it unbound the silent BMT on 2026-11-17; e2e-login also
    rotates secrets that our pinned session must not lose).

    client.e2e_login consumes three endpoints:
      /home/e2e-login/        -> {end_id,end_secret,chat_secret,group_id}
      /bmt/e2e-user-login/    -> {"e2es":[{end_id,group_id,chat_secret,
                                    end_secret,host}]}
      /bmt/search-bmt/        -> {"bmts":[{end_id,group_id,...}]}
    """
    p = path.lower()

    def env(result: dict) -> dict:
        return {"Status": 1, "Action": "", "Cmd": "", "ErrorMessage": "",
                "MessageId": "",
                "Result": rc4(APP_SECRET, json.dumps(result).encode()).hex()}

    try:
        creds = json.load(open(os.path.join(BASE, "e2e_new_creds.json")))
    except Exception:
        return None
    st = static_e2e() or {}
    act = active_e2e()
    if "home/e2e-login" in p:
        return env({"end_id": creds.get("home_end_id"),
                    "group_id": creds.get("home_group_id"),
                    "end_secret": creds.get("home_end_secret"),
                    "chat_secret": act["home_chat_secret"],
                    "gmtime": int(time.time() * 1e9)})
    if "e2e-user-login" in p:
        return env({"e2es": [{"end_id": creds.get("sender_end_id"),
                              "group_id": creds.get("sender_group_id"),
                              "chat_secret": act["chat_secret"],
                              "end_secret": creds.get("sender_end_secret"),
                              "host": RELAY_HOST}],
                    "count": 1, "gmtime": int(time.time() * 1e9)})
    if "search-bmt" in p:
        dev = dict(APP_DEV)
        dev["end_id"] = st.get("end_id", creds.get("recipient_end_id"))
        dev["group_id"] = st.get("group_id", creds.get("recipient_group_id"))
        return env({"bmts": [dev], "count": 1,
                    "gmtime": int(time.time() * 1e9)})
    if "list-bmt" in p:
        return env({"bmts": [APP_DEV], "count": 1,
                    "gmtime": int(time.time() * 1e9)})
    return None


def gm1_proxy(path: str, body: bytes) -> bytes:
    """Reverse-proxy the app (gm1) API to the live vendor server, recording
    every exchange to gm1_corpus.jsonl (bankruptcy replay material). The
    e2e-credentials endpoints are always answered LOCALLY (pinned identity,
    OUR relay); list-bmt gets a synthesized device record when the vendor
    no longer holds one (vendor auto-unbinds silent BMTs).
    """
    if any(k in path for k in ("home/e2e-login", "e2e-user-login",
                               "search-bmt")):
        # served locally: vendor e2e-login ROTATES the chat_secret (would
        # break the pinned module identity), search-bmt would hand out the
        # stale vendor-side device end, and after bankruptcy there is no
        # vendor at all
        r = _gm1_synth(path)
        if r:
            return json.dumps(r).encode()
    try:
        dp = any(k in path for k in ("/bmt/stats/", "/home/get-home-fcr"))
        req = urllib.request.Request(
            ("https://dp.emaldo.com" if dp else PROD_API) + path, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        raw = urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print("gm1 proxy fail", path, repr(exc), flush=True)
        r = (_gm1_synth(path) or gm1_corpus_lookup(path, want_ok=True)
             or gm1_corpus_lookup(path))
        if r is None:
            return b'{"Status": 0, "ErrorMessage": "offline"}'
        return r if isinstance(r, bytes) else json.dumps(r).encode()
    try:
        with open(GM1_CORPUS_PATH, "a") as f:
            f.write(json.dumps({"t": time.time(), "path": path,
                                "req": body.decode("latin1"),
                                "resp": raw.decode("latin1")}) + "\n")
    except Exception:
        pass
    # vendor answers DP stats paths with Status 0 (HTTP 200) after
    # unbinding the BMT -- replay our corpus instead when we have it
    if any(k in path for k in ("/bmt/stats/", "get-home-fcr")):
        try:
            if json.loads(raw).get("Status") == 0:
                rp = gm1_corpus_lookup(path, want_ok=True)
                if rp:
                    print("gm1 vendor-dead -> corpus replay:",
                          path.split("/")[2], flush=True)
                    raw = rp
        except Exception:
            pass
    if "list-bmt" in path:
        try:
            env = json.loads(raw)
            pt = _gm1_snappy_try(rc4(APP_SECRET,
                                    bytes.fromhex(env.get("Result", ""))))
            d = json.loads(pt)
            if not d.get("bmts"):
                raw = json.dumps(_gm1_synth(path)).encode()
                print("gm1 list-bmt empty -> synthesized local record",
                      flush=True)
        except Exception:
            pass
    return raw


def prod_login(gm2: dict, path: str):
    """Replay the module's gm2 login against the live production API and
    return the real Rsp with host/util_host rewritten to our relay.

    The module validates the issued identity (group_id / chat_secret / id
    must match the home's roster), so self-issued random identities get
    rejected -- proven by capture of the live server response 2026-11-17.
    Each vendor login rotates end_id/end_secret, so the fetched Rsp is
    cached and re-served for PROD_LOGIN_TTL: the vendor still sees fresh
    logins+metrics (it auto-unbinds silent BMTs -- observed 2026-11-17),
    while our e2e identity stays pinned.
    """
    now = time.time()
    if _PROD_CACHE.get("rsp") and now - _PROD_CACHE["ts"] < PROD_LOGIN_TTL:
        return _bat_serve(_PROD_CACHE["rsp"])
    try:
        inner = gm2.get("json")
        token = gm2.get("token", "")
        iv2 = "".join(secrets.choice(ALNUM) for _ in range(16))

        def enc(s):
            return urllib.parse.quote(base64.b64encode(
                AES.new(APP_SECRET, AES.MODE_CBC, iv2.encode())
                .encrypt(pad(s.encode(), 16))).decode(), safe="")

        data = ("gm=2&iv=" + iv2 + "&json=" + enc(inner) + "&token="
                + enc(token)).encode()
        req = urllib.request.Request(
            PROD_API + path, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if j.get("Status") != 1:
            print("prod login status", j.get("Status"), flush=True)
            return None
        ct = base64.b64decode(j["Result"])
        pt = AES.new(APP_SECRET, AES.MODE_CBC, iv2.encode()).decrypt(ct)
        r = json.loads(pt[:len(pt) - pt[-1]])
        static = static_e2e()
        if static and static.get("end_id"):
            # keep OUR pinned identity; vendor's fresh end_id rotation is
            # irrelevant now that the module never dials the vendor relay
            r["end_id"] = static["end_id"]
            r["end_secret"] = static["end_secret"]
        else:
            json.dump(r, open(STATIC_E2E_PATH, "w"), indent=1)
        _PROD_CACHE["rsp"] = r
        _PROD_CACHE["ts"] = now
        print("prod-login OK, identity:", r.get("id"), r.get("group_id"),
              flush=True)
        return _bat_serve(r)
    except Exception as exc:
        print("prod login fail:", repr(exc), flush=True)
        return None


def _bat_serve(r: dict) -> dict:
    """Battery e2e-bmt-login answer: our relay, our pinned identity, our
    ACTIVE rotating chat_secret, session timer anchored to NOW (vendor's
    stale addtime makes the battery re-roll its app session against a key
    the app clients never learned)."""
    a = active_e2e()
    r = dict(r)
    r["host"] = RELAY_HOST
    r["util_host"] = UTIL_HOST
    r["chat_secret"] = a["chat_secret"]
    r["addtime"] = int(time.time() * 1e9)
    return r


def aes_b64(key: bytes, iv: bytes, plain: bytes) -> str:
    return base64.b64encode(
        AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, 16))).decode()


def wrap_gm2(result: dict, req: dict, variant: int = 0) -> bytes:
    """gm2 response: PLAINTEXT JSON envelope {"Status","ErrorMessage",
    "Result"} where Result = base64 AES-256-CBC(APP_SECRET, IV=request iv)
    of the Rsp JSON.

    Evidence: poster json.Unmarshals body into bhsdk.Result, then hands the
    Result string to policy.OnResponse: gm2 = base64-decode -> AesDecrypt
    (key/iv from policy = request iv). gm1 (user API) = hex -> RC4 -> snappy
    (matches emaldo-py-re crypto.py).
    """
    try:
        iv = req["iv"].encode()
    except Exception:
        iv = "".join(secrets.choice(ALNUM) for _ in range(16)).encode()
    rsp = json.dumps(result, separators=(",", ":")).encode()
    ct = AES.new(APP_SECRET, AES.MODE_CBC, iv).encrypt(pad(rsp, 16))
    v = variant % 4
    if v == 0:
        res = base64.b64encode(ct).decode()
    elif v == 1:
        res = base64.urlsafe_b64encode(ct).decode()
    elif v == 2:
        res = ct.hex()
    else:
        res = base64.b64encode(ct).decode().rstrip("=")
    return json.dumps({"Status": 1, "Action": "", "Cmd": "",
                       "ErrorMessage": "", "MessageId": "",
                       "Result": res}).encode()


def wrap(result: dict, variant: int = 0) -> bytes:
    plain = json.dumps(result)
    if variant == 0:      # RC4 hex (user-API style)
        res = rc4(APP_SECRET, plain.encode()).hex()
        iv = None
    elif variant == 1:    # AES-CBC base64, fixed zero IV
        iv = "0" * 16
        res = base64.b64encode(AES.new(APP_SECRET, AES.MODE_CBC, iv.encode()).encrypt(pad(plain.encode(), 16))).decode()
    elif variant == 2:    # AES-CBC base64, random IV echoed in response
        iv = "".join(secrets.choice(ALNUM) for _ in range(16))
        res = base64.b64encode(AES.new(APP_SECRET, AES.MODE_CBC, iv.encode()).encrypt(pad(plain.encode(), 16))).decode()
    elif variant == 3:    # plaintext JSON dict
        return json.dumps({"Status": 1, "Action": "", "Cmd": "",
                           "ErrorMessage": "", "MessageId": "", "Result": result}).encode()
    elif variant == 4:    # RC4 hex, snappy-compressed first
        try:
            import snappy
            res = rc4(APP_SECRET, snappy.compress(plain.encode())).hex()
        except Exception:
            res = rc4(APP_SECRET, plain.encode()).hex()
        iv = None
    elif variant == 5:    # full gm2-style envelope: {"gm":"2","iv":..,"json":..}
        iv = "".join(secrets.choice(ALNUM) for _ in range(16))
        res = base64.b64encode(AES.new(APP_SECRET, AES.MODE_CBC, iv.encode()).encrypt(pad(plain.encode(), 16))).decode()
        return json.dumps({"gm": "2", "iv": iv, "json": res}).encode()
    body = {"Status": 1, "Action": "", "Cmd": "", "ErrorMessage": "",
            "MessageId": "", "Result": res}
    if iv:
        body["iv"] = iv
    return json.dumps(body).encode()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    logins = 0
    last_login = 0.0

    def log_message(self, fmt, *args):
        pass

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        dec = try_decode(body) if body else None
        gm2 = None
        if dec and dec.get("gm") == "2" and "iv" in dec:
            gm2 = dec
            try:  # decrypt the "json"/"token" fields for observability
                iv = dec["iv"].encode()
                gm2 = dict(dec)
                for fld in ("json", "token"):
                    if fld in dec:
                        ct = base64.b64decode(
                            urllib.parse.unquote(dec[fld]))
                        gm2[fld] = AES.new(APP_SECRET, AES.MODE_CBC, iv) \
                            .decrypt(ct).rstrip(b"\x00\x01\x02\x03\x04"
                                                b"\x05\x06\x07\x08") \
                            .decode("utf-8", "replace")[:600]
            except Exception:
                pass
        rec = {"t": time.strftime("%H:%M:%S"), "method": method,
               "path": self.path, "body_len": len(body),
               "decoded": gm2 or dec,
               "peer": self.client_address[0]}
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
        key = self.path.split("/")[2] if len(self.path.split("/")) > 2 else self.path
        if "metrics/" in self.path or "submit-sync-24h" in self.path:
            mirror_to_prod(self.path, body)
        variant = 0
        if "e2e-bmt-login" in key and gm2 is not None:
            now = time.time()
            if H.last_login:
                print("login interval: %.0f s" % (now - H.last_login), flush=True)
            H.last_login = now
            H.logins += 1
            if H.logins % 10 == 1:
                print(time.strftime("%H:%M:%S"), "login#", H.logins, flush=True)
            result = None if OFFLINE else prod_login(gm2, self.path)
            if result is None:
                result = static_e2e()
                if result is not None:
                    result["host"] = RELAY_HOST
                    result["util_host"] = UTIL_HOST
                    result["chat_secret"] = active_e2e()["chat_secret"]
                    result["addtime"] = int(now * 1e9)
                    print("static-e2e replay", flush=True)
                else:
                    result = dict(E2E)
                    result["token"] = FAKE_REG_TOKEN
            resp = wrap_gm2(result, gm2, 0)
        elif gm2 is not None:
            resp = wrap_gm2(fake_result(self.path), gm2, variant)
        elif dec and dec.get("gm") == "1":
            resp = gm1_proxy(self.path, body)
        else:
            resp = wrap(fake_result(self.path), variant)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8199
    Server(("0.0.0.0", port), H).serve_forever()
