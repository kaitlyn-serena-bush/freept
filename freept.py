#!/usr/bin/env python3
"""FreePT: route LLM requests to free-tier providers until their quotas run out.

Python 3.8+, standard library only (no pip). API keys are read from Windows
Credential Manager entries named "FreePT:<VAR>" (saved by keys.ps1), or from
environment variables of the same name (GROQ_API_KEY, CEREBRAS_API_KEY, ...).
Keys are only ever sent to the provider they belong to.

Usage:
  python freept.py status [--offline]   check every key, the model picked per provider, today's usage
  python freept.py usage [--offline]    usage meter: what's left per provider and when it resets
  python freept.py ask [--route=R] "prompt"   send one prompt, print the answer (stdin if no prompt)
  python freept.py serve [port]         OpenAI-compatible server on 127.0.0.1 (default 4000)

Server: GET / is a live usage page, GET /usage is the same as JSON.
POST /v1/chat/completions with model "freept" (best available),
"freept/fast", or "<provider>" / "<provider>/<model>" to pin one.
HTTP 429 with error type "freept_all_spent" means every provider is used up.

The local ledger (ledger.sqlite next to this file, or $FREEPT_DB) records calls,
cooldowns and rate-limit headers. It never stores keys.
"""
import json, os, re, sqlite3, sys, threading, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FREEPT_DB") or os.path.join(HERE, "ledger.sqlite")
UA = "FreePT/1.0"

# ---------------------------------------------------------------- keys
def read_key(var):
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            class CREDENTIAL(ctypes.Structure):
                _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
            adv = ctypes.WinDLL("advapi32", use_last_error=True)
            adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
            adv.CredReadW.restype = wintypes.BOOL
            adv.CredFree.argtypes = [ctypes.c_void_p]
            p = ctypes.POINTER(CREDENTIAL)()
            if adv.CredReadW("FreePT:" + var, 1, 0, ctypes.byref(p)):
                try:
                    c = p.contents
                    raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
                    val = raw.decode("utf-16-le").strip()
                    if val:
                        return val
                finally:
                    adv.CredFree(p)
        except Exception:
            pass
    return os.environ.get(var, "").strip() or None

# ---------------------------------------------------------------- providers
# rank: lower = tried first for "freept" (quality); fast_rank for "freept/fast".
# rpd: requests/day we allow ourselves (a little under the free limit, or a guess
#      where the provider doesn't publish one; a 429 always wins over the guess).
# reset_utc_hour: hour (UTC) when the provider's day rolls over.
# prefer: model id substrings in order; the first one the provider lists is used.
PROVIDERS = [
    dict(name="gemini", var="GEMINI_API_KEY", base="https://generativelanguage.googleapis.com/v1beta/openai",
         prefer=["gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.8-flash", "gemini-2.5-flash"],
         rank=1, fast_rank=5, rpd=100, rpm=5, reset_utc_hour=8),
    dict(name="gemini-flash", var="GEMINI_API_KEY", base="https://generativelanguage.googleapis.com/v1beta/openai",
         prefer=["gemma-4-31b", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
         rank=4, fast_rank=3, rpd=250, rpm=10, reset_utc_hour=8),
    dict(name="cerebras", var="CEREBRAS_API_KEY", base="https://api.cerebras.ai/v1",
         prefer=["gpt-oss-120b", "qwen"], rank=2, fast_rank=1, rpd=2300, rpm=5),  # free: 2400/day, 150/hour, 5/min
    dict(name="groq", var="GROQ_API_KEY", base="https://api.groq.com/openai/v1",
         prefer=["openai/gpt-oss-120b", "gpt-oss-120b", "qwen", "gpt-oss-20b"],
         rank=3, fast_rank=2, rpd=950, rpm=28),
    dict(name="sambanova", var="SAMBANOVA_API_KEY", base="https://api.sambanova.ai/v1",
         prefer=["DeepSeek", "gpt-oss-120b", "Llama-3.3-70B"],
         rank=5, fast_rank=6, rpd=19, rpm=15),
    dict(name="nvidia", var="NVIDIA_API_KEY", base="https://integrate.api.nvidia.com/v1",
         prefer=["kimi-k3", "nemotron-3-ultra", "mistral-large-4", "glm-5", "qwen3-coder", "deepseek"],
         rank=6, fast_rank=7, rpd=500, rpm=35),
    dict(name="openrouter", var="OPENROUTER_API_KEY", base="https://openrouter.ai/api/v1",
         prefer=["nemotron-3-ultra", "gemma-4-31b", "ernie", ":free"], free_only=True,
         rank=7, fast_rank=8, rpd=48, rpm=18),
    dict(name="cloudflare", var="CLOUDFLARE_API_TOKEN", base="https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
         fixed_models=["@cf/openai/gpt-oss-120b", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"],
         rank=8, fast_rank=4, rpd=60, rpm=60),
    dict(name="cohere", var="COHERE_API_KEY", base="https://api.cohere.ai/compatibility/v1",
         fixed_models=["command-a-03-2025", "command-r-plus"], rank=9, fast_rank=9, rpd=60, rpm=15, rpmonth=1000),
    dict(name="mistral", var="MISTRAL_API_KEY", base="https://api.mistral.ai/v1",
         prefer=["codestral", "mistral-large", "mistral-medium"], rank=5, fast_rank=6, rpd=500, rpm=50),
    dict(name="ollama", var=None, base="http://127.0.0.1:11434/v1",
         prefer=["qwen", "coder", "llama", ""], rank=99, fast_rank=99, rpd=10**9, rpm=10**6),
]

def provider(name):
    for p in PROVIDERS:
        if p["name"] == name:
            return p
    return None

# ---------------------------------------------------------------- ledger
_db_lock = threading.Lock()

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("create table if not exists calls (ts real, provider text, model text, status int,"
                " prompt_tokens int, completion_tokens int, seconds real, note text)")
    con.execute("create table if not exists cooldown (provider text primary key, until real, reason text)")
    con.execute("create table if not exists limits (provider text primary key, ts real, json text)")
    con.execute("create table if not exists badmodel (provider text, model text, reason text, primary key (provider, model))")
    return con

def day_start(p, now=None):
    now = now or time.time()
    h = p.get("reset_utc_hour", 0)
    dt = datetime.fromtimestamp(now, timezone.utc)
    start = dt.replace(hour=h, minute=0, second=0, microsecond=0)
    if start > dt:
        start -= timedelta(days=1)
    return start.timestamp()

def used_today(con, p):
    return con.execute("select count(*) from calls where provider=? and ts>=? and status!=429",
                       (p["name"], day_start(p))).fetchone()[0]

def month_start(now=None):
    dt = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

def used_month(con, p):
    return con.execute("select count(*) from calls where provider=? and ts>=? and status!=429",
                       (p["name"], month_start())).fetchone()[0]

def tokens_today(con, p):
    r = con.execute("select coalesce(sum(prompt_tokens),0), coalesce(sum(completion_tokens),0) from calls"
                    " where provider=? and ts>=?", (p["name"], day_start(p))).fetchone()
    return r[0] + r[1]

def save_limits(con, p, hdr):
    lim = {k.lower(): v for k, v in (hdr or {}).items() if k.lower().startswith(("x-ratelimit", "ratelimit"))}
    if lim:
        con.execute("insert or replace into limits values (?,?,?)", (p["name"], time.time(), json.dumps(lim)))

def used_last_minute(con, p):
    return con.execute("select count(*) from calls where provider=? and ts>=?",
                       (p["name"], time.time() - 60)).fetchone()[0]

def cooled_until(con, p):
    r = con.execute("select until from cooldown where provider=?", (p["name"],)).fetchone()
    return r[0] if r and r[0] > time.time() else 0

def set_cooldown(con, p, until, reason):
    con.execute("insert or replace into cooldown values (?,?,?)", (p["name"], until, reason))

def log_call(con, p, model, status, usage, secs, note=""):
    usage = usage or {}
    con.execute("insert into calls values (?,?,?,?,?,?,?,?)",
                (time.time(), p["name"], model, status, usage.get("prompt_tokens"),
                 usage.get("completion_tokens"), secs, note[:300]))

# ---------------------------------------------------------------- http
def base_url(p):
    b = p["base"]
    if "{CLOUDFLARE_ACCOUNT_ID}" in b:
        acct = read_key("CLOUDFLARE_ACCOUNT_ID")
        if not acct:
            return None
        b = b.replace("{CLOUDFLARE_ACCOUNT_ID}", acct)
    return b.rstrip("/")

def http_json(url, key, body=None, timeout=120):
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = {"error": {"message": txt[:500]}}
        return e.code, j, dict(e.headers or {})

_model_cache = {}

def pick_model(p, key):
    if p["name"] in _model_cache:
        return _model_cache[p["name"]]
    if p.get("fixed_models"):
        con = db()
        bad = {r[0] for r in con.execute("select model from badmodel where provider=?", (p["name"],))}
        con.close()
        left = [x for x in p["fixed_models"] if x not in bad]
        if not left:
            raise RuntimeError("all fixed models marked bad")
        m = left[0]
    else:
        b = base_url(p)
        st, j, _ = http_json(b + "/models", key, timeout=30)
        if st != 200:
            raise RuntimeError("models list failed: HTTP %s %s" % (st, err_text(j)))
        ids = [m.get("id", "") for m in j.get("data", []) if isinstance(m, dict)]
        if p.get("free_only"):
            ids = [i for i in ids if i.endswith(":free")]
        con = db()
        bad = {r[0] for r in con.execute("select model from badmodel where provider=?", (p["name"],))}
        con.close()
        ids = [i for i in ids if i not in bad and i.replace("models/", "", 1) not in bad]
        ids_l = [i.lower() for i in ids]
        m = None
        for want in p.get("prefer", []):
            for i, il in zip(ids, ids_l):
                if want.lower() in il and "embed" not in il and "whisper" not in il and "guard" not in il:
                    m = i
                    break
            if m:
                break
        if not m:
            raise RuntimeError("no preferred model among %d listed" % len(ids))
        if m.startswith("models/"):
            m = m[len("models/"):]
    _model_cache[p["name"]] = m
    return m

def err_text(j):
    e = j.get("error") if isinstance(j, dict) else None
    if isinstance(e, dict):
        return str(e.get("message", e))[:300]
    if isinstance(j, list) and j:
        return err_text(j[0])
    return str(e or j)[:300]

# ---------------------------------------------------------------- routing
class AllSpent(Exception):
    pass

def candidates(route):
    if route in ("freept", "", None, "auto"):
        order = sorted(PROVIDERS, key=lambda p: p["rank"])
    elif route == "freept/fast":
        order = sorted(PROVIDERS, key=lambda p: p["fast_rank"])
    else:
        name, _, model = route.partition("/")
        p = provider(name)
        if not p:
            raise ValueError("unknown provider or route: %s" % route)
        return [(p, model or None)]
    return [(p, None) for p in order]

def available(con, p):
    """Return (ok, reason)."""
    if p["var"] and not read_key(p["var"]):
        return False, "no key"
    if p["name"] == "ollama" and not ollama_up():
        return False, "not running"
    cu = cooled_until(con, p)
    if cu:
        return False, "cooling down until %s" % time.strftime("%H:%M" if cu - time.time() < 20 * 3600 else "%a %d %b %H:%M",
                                                              time.localtime(cu))
    if used_today(con, p) >= p["rpd"]:
        return False, "daily budget used"
    if p.get("rpmonth") and used_month(con, p) >= p["rpmonth"]:
        return False, "monthly budget used"
    return True, ""

_ollama = {"t": 0, "up": False}
def ollama_up():
    if time.time() - _ollama["t"] > 60:
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1).read()
            _ollama["up"] = True
        except Exception:
            _ollama["up"] = False
        _ollama["t"] = time.time()
    return _ollama["up"]

THOUGHT = re.compile(r"^\s*<(thought|think)>.*?</\1>", re.S)

def chat(body, route="freept", verbose=False):
    """Send an OpenAI-style chat body. Returns (response_json, provider_name, model)."""
    tried = []
    queue = list(candidates(route))
    retried = set()
    while queue:
        p, pinned_model = queue.pop(0)
        with _db_lock:
            con = db()
            ok, why = available(con, p)
            if ok and used_last_minute(con, p) >= p["rpm"]:
                ok, why = False, "per-minute limit"
            con.close()
        if not ok:
            tried.append("%s: %s" % (p["name"], why))
            continue
        key = read_key(p["var"]) if p["var"] else None
        b = base_url(p)
        if not b:
            tried.append("%s: missing account id" % p["name"]); continue
        try:
            model = pinned_model or pick_model(p, key)
        except Exception as e:
            tried.append("%s: %s" % (p["name"], e)); continue
        req = dict(body); req["model"] = model; req.pop("stream", None)
        t0 = time.time()
        try:
            st, j, hdr = http_json(b + "/chat/completions", key, req)
        except Exception as e:
            st, j, hdr = 0, {"error": {"message": str(e)}}, {}
        secs = time.time() - t0
        with _db_lock:
            con = db()
            log_call(con, p, model, st, j.get("usage") if isinstance(j, dict) else None, secs,
                     "" if st == 200 else err_text(j))
            save_limits(con, p, hdr)
            if st == 429:
                ra = hdr.get("Retry-After") or hdr.get("retry-after")
                try:
                    wait = float(ra)
                except (TypeError, ValueError):
                    wait = 65
                msg = err_text(j).lower()
                if "per minute" in msg or "per-minute" in msg or "minute" in msg and "day" not in msg:
                    until = time.time() + max(wait, 20)   # e.g. Cerebras "Tokens per minute limit exceeded" (param: quota)
                elif wait > 120 or any(w in msg for w in ("per day", "daily", "quota", "exhausted", "credits")):
                    until = day_start(p) + 86400 + 60
                else:
                    until = time.time() + max(wait, 20)
                set_cooldown(con, p, until, err_text(j))
            elif st == 402:
                set_cooldown(con, p, time.time() + 7 * 86400, "needs payment: " + err_text(j))
            elif st in (401, 403):
                set_cooldown(con, p, time.time() + 3600, "auth: " + err_text(j))
            elif st in (400, 404) and not pinned_model and "model" in err_text(j).lower() and any(
                    w in err_text(j).lower() for w in ("not found", "no longer available", "does not exist",
                                                       "unknown model", "invalid model", "not supported")):
                con.execute("insert or replace into badmodel values (?,?,?)", (p["name"], model, err_text(j)))
                _model_cache.pop(p["name"], None)
                if p["name"] not in retried:
                    retried.add(p["name"]); queue.insert(0, (p, None))
            elif st == 0 or st >= 500:
                set_cooldown(con, p, time.time() + (600 if st == 0 else 90), "unavailable: " + err_text(j))
            con.commit(); con.close()
        if st == 200 and isinstance(j, dict) and j.get("choices"):
            for ch in j["choices"]:  # some models (Gemma) put their reasoning in the answer text
                msg = ch.get("message") or {}
                if isinstance(msg.get("content"), str):
                    msg["content"] = THOUGHT.sub("", msg["content"]).lstrip()
            j.setdefault("freept", {})["provider"] = p["name"]
            return j, p["name"], model
        tried.append("%s/%s: HTTP %s %s" % (p["name"], model, st, err_text(j)[:120]))
        if verbose:
            print("  ..", tried[-1], file=sys.stderr)
    raise AllSpent("no provider could answer:\n  " + "\n  ".join(tried))

# ---------------------------------------------------------------- commands
def cmd_status(live=True):
    con = db()
    print("%-13s %-6s %-9s %-34s %s" % ("provider", "key", "today", "model", "state"))
    for p in sorted(PROVIDERS, key=lambda p: p["rank"]):
        has = bool(read_key(p["var"])) if p["var"] else ollama_up()
        used = used_today(con, p)
        ok, why = available(con, p)
        model, state = "", why or "ready"
        if has and live:
            try:
                _model_cache.pop(p["name"], None)
                model = pick_model(p, read_key(p["var"]) if p["var"] else None)
            except Exception as e:
                state = "FAILED: %s" % e
        lim = "-" if p["name"] == "ollama" else "%d/%d" % (used, p["rpd"])
        print("%-13s %-6s %-9s %-34s %s" % (p["name"], "yes" if has else "-", lim, model[:34], state))
    con.close()

def _fmt_in(secs):
    secs = max(0, int(secs))
    h, m = secs // 3600, (secs % 3600) // 60
    return ("%dd %dh" % (h // 24, h % 24)) if h >= 48 else ("%dh %02dm" % (h, m))

def openrouter_live():
    k = read_key("OPENROUTER_API_KEY")
    if not k:
        return None
    try:
        st, j, _ = http_json("https://openrouter.ai/api/v1/key", k, timeout=15)
        return j.get("data") if st == 200 else None
    except Exception:
        return None

def usage_rows(live=True):
    now = time.time()
    con = db()
    rows = []
    for p in sorted(PROVIDERS, key=lambda p: p["rank"]):
        has = bool(read_key(p["var"])) if p["var"] else ollama_up()
        used = used_today(con, p)
        reset = day_start(p) + 86400
        r = dict(provider=p["name"], key=has, used_today=used, budget_today=p["rpd"],
                 left_today=max(0, p["rpd"] - used), tokens_today=tokens_today(con, p),
                 reset_at=time.strftime("%a %H:%M", time.localtime(reset)), reset_in=_fmt_in(reset - now),
                 model=_model_cache.get(p["name"], ""), state="ready" if has else "no key", note="")
        if p.get("rpmonth"):
            um = used_month(con, p)
            nxt = datetime.fromtimestamp(month_start(), timezone.utc)
            nxt = (nxt.replace(year=nxt.year + 1, month=1) if nxt.month == 12 else nxt.replace(month=nxt.month + 1)).timestamp()
            r.update(used_month=um, budget_month=p["rpmonth"], left_month=max(0, p["rpmonth"] - um),
                     month_reset_in=_fmt_in(nxt - now))
        if p["name"] == "ollama":
            r.update(budget_today=None, left_today=None, reset_at="", reset_in="never runs out")
        if has:
            ok, why = available(con, p)
            if not ok:
                r["state"] = why
                c = con.execute("select reason from cooldown where provider=?", (p["name"],)).fetchone()
                if c and cooled_until(con, p):
                    r["note"] = (c[0] or "")[:140]
        lim = con.execute("select ts, json from limits where provider=?", (p["name"],)).fetchone()
        if lim:
            d = json.loads(lim[1])
            r["live"] = {k.replace("x-ratelimit-", ""): v for k, v in d.items()
                         if "remaining" in k or "limit-requests" in k or "limit-tokens" in k}
            r["live_age"] = _fmt_in(now - lim[0]) + " ago"
        rows.append(r)
    con.close()
    if live:
        d = openrouter_live()
        if d:
            for r in rows:
                if r["provider"] == "openrouter":
                    r["live"] = {k: d.get(k) for k in ("usage", "limit", "limit_remaining", "is_free_tier") if k in d}
                    r["live_age"] = "now"
    return rows

def cmd_usage(live=True):
    rows = usage_rows(live)
    print("%-12s %-11s %-13s %-9s %-16s %s" % ("provider", "left today", "used/budget", "tokens", "resets", "state"))
    for r in rows:
        if not r["key"]:
            print("%-12s %-11s %-13s %-9s %-16s %s" % (r["provider"], "-", "-", "-", "-", "no key")); continue
        left = "unlimited" if r["left_today"] is None else str(r["left_today"])
        ub = "-" if r["budget_today"] is None else "%d/%d" % (r["used_today"], r["budget_today"])
        print("%-12s %-11s %-13s %-9s %-16s %s" % (r["provider"], left, ub, r["tokens_today"],
              ("in " + r["reset_in"]) if r["reset_at"] else r["reset_in"], r["state"]))
        if "left_month" in r:
            print("%12s month: %d of %d left, resets in %s" % ("", r["left_month"], r["budget_month"], r["month_reset_in"]))
        if r.get("live"):
            print("%12s provider says (%s): %s" % ("", r.get("live_age", ""),
                  ", ".join("%s=%s" % kv for kv in r["live"].items())))
        if r["note"]:
            print("%12s why: %s" % ("", r["note"]))

USAGE_HTML = """<!doctype html><html><head><meta charset=utf-8><title>FreePT usage</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#1b1b1f;--mut:#6b6b76;--line:#e4e4ea;--ok:#1f8a4c;--warn:#b26a00;--bad:#c23a3a;--bar:#e9e9ef}
@media (prefers-color-scheme:dark){:root{--bg:#141417;--fg:#ececf1;--mut:#9a9aa6;--line:#2a2a31;--ok:#4cc27e;--warn:#e0a040;--bad:#ef6b6b;--bar:#26262c}}
body{background:var(--bg);color:var(--fg);font:14px system-ui,Segoe UI,sans-serif;margin:24px;max-width:1000px}
h1{font-size:20px;margin:0 0 4px}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%;margin-top:16px}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:12px;text-transform:uppercase}
.bar{height:8px;background:var(--bar);border-radius:4px;overflow:hidden;width:160px;margin-top:4px}.bar i{display:block;height:100%;background:var(--ok)}
.ready{color:var(--ok)}.cool{color:var(--warn)}.off{color:var(--mut)}.small{font-size:12px;color:var(--mut)}
</style></head><body>
<h1>FreePT usage</h1><div class=mut id=sub>loading...</div>
<table><thead><tr><th>Provider</th><th>Left today</th><th>Tokens today</th><th>Resets</th><th>State</th></tr></thead><tbody id=rows></tbody></table>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){
 try{const r=await (await fetch('/usage')).json();let h='';
 for(const x of r.rows){
  if(!x.key){h+=`<tr class=off><td>${esc(x.provider)}</td><td colspan=4>no key</td></tr>`;continue}
  let left=x.left_today==null?'unlimited':`${x.left_today} of ${x.budget_today}`;
  let pct=x.budget_today?Math.round(100*x.left_today/x.budget_today):100;
  let extra='';
  if(x.left_month!=null)extra+=`<div class=small>month: ${x.left_month} of ${x.budget_month} left, resets in ${esc(x.month_reset_in)}</div>`;
  if(x.live)extra+=`<div class=small>provider says (${esc(x.live_age)}): ${esc(Object.entries(x.live).map(([k,v])=>k+'='+v).join(', '))}</div>`;
  let cls=x.state=='ready'?'ready':'cool';
  h+=`<tr><td><b>${esc(x.provider)}</b><div class=small>${esc(x.model)}</div></td>
   <td>${esc(left)}<div class=bar><i style="width:${pct}%"></i></div>${extra}</td>
   <td>${x.tokens_today.toLocaleString()}</td>
   <td>${x.reset_at?esc(x.reset_at)+'<div class=small>in '+esc(x.reset_in)+'</div>':esc(x.reset_in)}</td>
   <td class=${cls}>${esc(x.state)}${x.note?'<div class=small>'+esc(x.note)+'</div>':''}</td></tr>`}
 document.getElementById('rows').innerHTML=h;
 document.getElementById('sub').textContent='Updated '+new Date().toLocaleTimeString()+' · refreshes every 30 s · budgets are daily caps set in FreePT, "provider says" is what the provider reported on its last answer';
 }catch(e){document.getElementById('sub').textContent='Could not reach FreePT: '+e}}
load();setInterval(load,30000);
</script></body></html>"""

def cmd_ask(prompt, route="freept"):
    j, prov, model = chat({"messages": [{"role": "user", "content": prompt}]}, route, verbose=True)
    print("[%s / %s]" % (prov, model), file=sys.stderr)
    print(j["choices"][0]["message"].get("content", ""))

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/index.html"):
            data = USAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/usage":
            return self._send(200, {"rows": usage_rows(live=True)})
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            names = ["freept", "freept/fast"] + [p["name"] for p in PROVIDERS]
            return self._send(200, {"object": "list", "data": [{"id": n, "object": "model"} for n in names]})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            route = body.pop("model", "freept")
            j, _, _ = chat(body, route)
            self._send(200, j)
        except AllSpent as e:
            self._send(429, {"error": {"message": str(e), "type": "freept_all_spent"}})
        except Exception as e:
            self._send(400, {"error": {"message": str(e)}})

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

def cmd_serve(port=4000):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("FreePT listening on http://127.0.0.1:%d/v1  (model: freept, freept/fast, or a provider name)" % port)
    print("Usage meter: http://127.0.0.1:%d/" % port)
    srv.serve_forever()

def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__); return 0
    c = argv[0]
    if c == "status":
        cmd_status(live="--offline" not in argv)
    elif c == "usage":
        cmd_usage(live="--offline" not in argv)
    elif c == "ask":
        route = "freept"
        args = argv[1:]
        if args and args[0].startswith("--route="):
            route = args[0].split("=", 1)[1]; args = args[1:]
        prompt = " ".join(args) if args else sys.stdin.read()
        try:
            cmd_ask(prompt, route)
        except AllSpent as e:
            print(e, file=sys.stderr); return 2
    elif c == "serve":
        cmd_serve(int(argv[1]) if len(argv) > 1 else 4000)
    else:
        print(__doc__); return 1
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
