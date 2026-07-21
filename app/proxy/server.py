"""save AI proxy server — webhook receiver + healthcheck.

Used to live inline in start.sh:373-620 as a 246-line heredoc. Extracted
2026-06-03 per the cleanup audit so it can be debugged + reloaded
independently of container boot.

Responsibilities:
  * Receive inbound webhooks from Inkbox (HMAC verified)
  * Inject the [SAVE-TIER:xxx] marker via app.proxy.tier_gate before
    forwarding to the Hermes gateway
  * Sanitize inbound message bodies for prompt-injection patterns
  * Handle OAuth callbacks (for any provider that needs one)
  * Serve /health on the Railway healthcheck path
  * Serve generated share-link files under /public

Entry point: `python3 -m scripts.runtime.proxy` (or direct
`python3 app/proxy/server.py`).
"""
from __future__ import annotations

import http.server
import os
import json
import sys
import urllib.request
import urllib.parse
import subprocess

# V1_SPEC §18.1 — tier gate. Import is best-effort: if the module or psycopg
# is unavailable, the proxy still serves (fail-closed to stranger).
sys.path.insert(0, "/opt/agent")
try:
    from app.proxy import tier_gate as _tier_gate
except Exception as _tg_err:
    sys.stderr.write(f"[proxy] tier_gate import failed: {_tg_err}\n")
    _tier_gate = None
try:
    from app.proxy import sanitize as _sanitize
except Exception as _sn_err:
    sys.stderr.write(f"[proxy] sanitize import failed: {_sn_err}\n")
    _sanitize = None
try:
    from app.proxy import tier_marker as _tier_marker
except Exception as _tm_err:
    sys.stderr.write(f"[proxy] tier_marker import failed: {_tm_err}\n")
    _tier_marker = None
try:
    from app.proxy import signup as _signup
except Exception as _su_err:
    sys.stderr.write(f"[proxy] signup import failed: {_su_err}\n")
    _signup = None

# CORS allowlist for the funnel landing page. Tighten via env once the
# Vercel domain is known; default '*' so first-deploy doesn't 403.
SIGNUP_CORS_ORIGIN = os.environ.get("SIGNUP_CORS_ORIGIN", "*")

INKBOX_PORT = int(os.environ.get("INKBOX_LISTEN_PORT", "8765"))
port = int(os.environ.get("PORT", "8080"))

# In-memory ring buffer of the last 10 inbound phones resolved by the tier
# gate. Used by /health/tier-probe so we can see what number Inkbox is
# actually putting on the wire vs what DREW_PHONE expects. Phones are
# masked to last 4 digits in the response.
_RECENT_PHONES: list = []
# Counters for ALL traffic, even POSTs that fail HMAC / never reach the
# tier gate. Used to discriminate "Inkbox isn't even hitting this proxy"
# from "Inkbox is hitting but tier_gate is failing."
_TRAFFIC_COUNTERS = {
    "post_total": 0, "post_401_missing_sig": 0, "post_401_bad_sig": 0,
    "post_admin_bypass": 0, "post_signed_ok": 0,
    "post_tier_resolved": 0, "post_tier_gate_exception": 0,
    "post_paths_seen": [],
}
def _bump(counter, increment=1):
    try:
        _TRAFFIC_COUNTERS[counter] = _TRAFFIC_COUNTERS.get(counter, 0) + increment
    except Exception:
        pass
def _record_path(path):
    try:
        paths = _TRAFFIC_COUNTERS.setdefault("post_paths_seen", [])
        if path not in paths:
            paths.append(path)
            if len(paths) > 20:
                del paths[:-20]
    except Exception:
        pass
def _record_phone(phone, tier):
    try:
        _RECENT_PHONES.append({"phone": phone, "tier": tier, "ts": __import__("time").time()})
        if len(_RECENT_PHONES) > 10:
            del _RECENT_PHONES[:-10]
    except Exception:
        pass

# Inbound dedup for BlueBubbles. BB retries delivery when our response is
# slow, so the same message guid can arrive 2-3 times; without this every
# retry would generate another reply. Track recently-seen guids in a bounded,
# thread-safe ring (the proxy runs on ThreadingHTTPServer). In-memory by
# design: a guid only needs to be remembered for the few seconds BB retries
# within, so losing the set on restart is harmless.
import threading as _threading
_BB_SEEN_LOCK = _threading.Lock()
_BB_SEEN_IDS: list = []      # ring buffer, oldest first
_BB_SEEN_SET: set = set()    # O(1) membership mirror of _BB_SEEN_IDS
_BB_SEEN_MAX = 512
def _bb_already_seen(msg_id) -> bool:
    """True if this BlueBubbles message guid was already processed.

    Empty/unknown ids are never treated as seen (always processed) so a
    missing guid can't accidentally swallow every inbound message.
    """
    msg_id = str(msg_id or "")
    if not msg_id:
        return False
    with _BB_SEEN_LOCK:
        if msg_id in _BB_SEEN_SET:
            return True
        _BB_SEEN_IDS.append(msg_id)
        _BB_SEEN_SET.add(msg_id)
        if len(_BB_SEEN_IDS) > _BB_SEEN_MAX:
            _old = _BB_SEEN_IDS.pop(0)
            _BB_SEEN_SET.discard(_old)
        return False

# Build-time commit sha — exposed on /health so external probes (and the
# bot itself) can verify which commit is actually live. Railway injects
# RAILWAY_GIT_COMMIT_SHA into every deploy automatically. Falls back to
# reading .git/HEAD locally if running outside Railway.
def _resolve_git_sha() -> str:
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
    if sha:
        return sha[:12]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/opt/agent",
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
        return out[:12] if out else "unknown"
    except Exception:
        return "unknown"

GIT_SHA = _resolve_git_sha()

def inkbox_health_ok():
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{INKBOX_PORT}/health",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False

def proxy_to_inkbox(method, path, headers, body):
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{INKBOX_PORT}{path}",
            data=body,
            headers=dict(headers),
            method=method,
        )
        with urllib.request.urlopen(req, timeout=int(os.environ.get("SAVE_PROXY_TIMEOUT", "135"))) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except Exception as e:
        return 502, {}, json.dumps({"error": str(e)}).encode()

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            ok = inkbox_health_ok()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok" if ok else "degraded",
                "service": "inkbox-agent",
                "inkbox": "up" if ok else "down",
                "git_sha": GIT_SHA,
            }).encode())
        elif parsed.path == "/health/tier-probe":
            # Diagnostic for "bot calling Drew a stranger" — surfaces:
            #   - DREW_PHONE env presence + last 4 digits (admin gate input)
            #   - last 10 inbound phones the tier_gate has seen + the tier
            #     they resolved to (admin gate output)
            #   - whether the public tier_gate.lookup_tier returns admin
            #     for DREW_PHONE itself (sanity check the resolver)
            # H-7 fix: requires SAVE_ADMIN_TOKEN (was unauthenticated and
            # leaked DREW_PHONE presence + last 4 digits to anyone who
            # could reach the proxy). Compare with hmac.compare_digest
            # (timing-safe) while we're here (also addresses H-6).
            import hmac as _hmc
            import os as _os
            _tok_env = _os.environ.get("SAVE_ADMIN_TOKEN", "")
            _tok_req = self.headers.get("X-Save-Admin-Token", "")
            if not _tok_env or not _hmc.compare_digest(_tok_req, _tok_env):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"admin token required"}')
                return
            drew_env = (_os.environ.get("DREW_PHONE") or "").strip()
            def _mask(p):
                if not p or not isinstance(p, str):
                    return None
                return p[-4:] if len(p) >= 4 else p
            tier_for_drew = None
            if _tier_gate is not None and drew_env:
                try:
                    tier_for_drew = _tier_gate.lookup_tier(drew_env)
                except Exception as _e:
                    tier_for_drew = f"lookup_failed:{_e}"
            payload = {
                "drew_phone_set": bool(drew_env),
                "drew_phone_last4": _mask(drew_env),
                "drew_phone_format_ok": (
                    drew_env.startswith("+") and drew_env[1:].isdigit()
                    if drew_env else False
                ),
                "tier_for_drew_phone": tier_for_drew,
                "recent_inbound_phones": [
                    {"phone_last4": _mask(r["phone"]),
                     "phone_starts_with_plus": bool(r["phone"] and r["phone"].startswith("+")),
                     "tier_resolved": r["tier"],
                     "ts": r["ts"]}
                    for r in _RECENT_PHONES[-10:]
                ],
                "memory_md_path_candidates_present": {
                    "/opt/data/MEMORY.md": _os.path.isfile("/opt/data/MEMORY.md"),
                    "$MEMORY_MD_PATH": bool(_os.environ.get("MEMORY_MD_PATH")) and _os.path.isfile(_os.environ.get("MEMORY_MD_PATH", "")),
                },
                "inkbox_identity_set": bool(_os.environ.get("INKBOX_IDENTITY")),
                "traffic_counters": dict(_TRAFFIC_COUNTERS),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, default=str).encode())
        elif parsed.path == "/health/recall-probe":
            # Diagnostic for "bot doesn't remember anything" — surfaces:
            #   - recall plugin db.is_available() result
            # Admin-gated: requires X-Save-Admin-Token matching SAVE_ADMIN_TOKEN.
            import hmac as _hmc
            import os as _os
            admin_token = _os.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if not admin_token or not _hmc.compare_digest(req_token, admin_token):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "admin token required"}).encode())
                return
            payload = {"db_url_set": bool(
                _os.environ.get("DATABASE_URL") or _os.environ.get("DATABASE_PUBLIC_URL")
            )}
            try:
                sys.path.insert(0, "/opt/agent")
                from app.plugins.recall import db as _recall_db
                payload["db_is_available"] = _recall_db.is_available()
            except Exception as _e:
                payload["db_is_available_error"] = str(_e)
            # Hermes plugin-manager introspection: proxy runs in a separate
            # Python process from the Hermes gateway, so its in-process
            # PluginManager singleton is empty. Instead, simulate what
            # Hermes sees by calling discover_and_load(force=True) here —
            # same disk manifests, same enable list. Surfaces import
            # errors that would otherwise be silently swallowed.
            try:
                from hermes_cli.plugins import get_plugin_manager
                _mgr = get_plugin_manager()
                _mgr.discover_and_load(force=True)
                _plug = getattr(_mgr, "_plugins", {}) or {}
                payload["hermes_plugins_loaded"] = sorted(_plug.keys())
                def _state(name):
                    p = _plug.get(name)
                    if p is None:
                        return "NOT_DISCOVERED"
                    return {
                        "enabled": getattr(p, "enabled", None),
                        "error": getattr(p, "error", None),
                        "hooks": list(getattr(p, "hooks_registered", []) or []),
                        "tools": list(getattr(p, "tools_registered", []) or []),
                    }
                payload["hermes_recall_state"] = _state("recall")
                payload["hermes_tier_injector_state"] = _state("save_tier_injector")
                _hooks = getattr(_mgr, "_hooks", {}) or {}
                payload["hermes_hooks_registered"] = {
                    h: len(cbs) for h, cbs in _hooks.items() if cbs
                }
            except Exception as _e:
                payload["hermes_introspect_error"] = f"{type(_e).__name__}: {_e}"
            try:
                import psycopg
                url = _os.environ.get("DATABASE_URL") or _os.environ.get("DATABASE_PUBLIC_URL")
                with psycopg.connect(url, connect_timeout=5) as _c:
                    with _c.cursor() as _cur:
                        for tbl in ("recall_facts", "conversation_history", "memories"):
                            try:
                                _cur.execute(
                                    "SELECT to_regclass(%s)", (f"public.{tbl}",)
                                )
                                exists = _cur.fetchone()[0] is not None
                                count = None
                                if exists:
                                    _cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                                    count = _cur.fetchone()[0]
                                payload[f"table_{tbl}"] = {"exists": exists, "rows": count}
                            except Exception as _te:
                                payload[f"table_{tbl}"] = {"error": str(_te)}
                        try:
                            _cur.execute(
                                "SELECT direction, LEFT(message, 80), tier, created_at "
                                "FROM conversation_history "
                                "ORDER BY created_at DESC LIMIT 6"
                            )
                            payload["recent_messages"] = [
                                {"direction": r[0], "snippet": r[1], "tier": r[2], "ts": str(r[3])}
                                for r in _cur.fetchall()
                            ]
                        except Exception as _re:
                            payload["recent_messages_error"] = str(_re)
            except Exception as _e:
                payload["psycopg_error"] = str(_e)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, default=str).encode())
        elif parsed.path == "/health/capabilities":
            # Debug: surface the runtime tool inventory (admin-gated).
            import hmac as _hmc
            import os as _os
            admin_token = _os.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if not admin_token or not _hmc.compare_digest(req_token, admin_token):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "admin token required"}).encode())
                return
            query = urllib.parse.parse_qs(parsed.query)
            run_probes = (query.get("probe") or ["0"])[0].lower() in {"1", "true", "yes"}
            try:
                from app import capabilities as _capabilities

                payload = _capabilities.inventory(run_probes=run_probes)
            except Exception as _e:
                payload = {"ok": False, "error": f"{type(_e).__name__}: {_e}"}
            self.send_response(200 if payload.get("ok") else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, default=str).encode())
        elif parsed.path == "/health/detailed":
            # Admin-gated: this endpoint surfaces active_task + watchdog state,
            # which leaks operational posture to anyone who can reach the proxy.
            import hmac as _hmc
            import os as _os
            admin_token = _os.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if not admin_token or not _hmc.compare_digest(req_token, admin_token):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "admin token required"}).encode())
                return
            import pathlib as _p
            hermes_home = _os.environ.get("HERMES_HOME", "/opt/data")
            active_task_path = _p.Path(hermes_home) / "active_task.json"
            watchdog_state_path = _p.Path(hermes_home) / "watchdog_state.json"
            watchdog_log = _p.Path(hermes_home) / "logs" / "watchdog.log"
            def _read_json(p):
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    return None
            payload = {
                "service": "demo-agent",
                "git_sha": GIT_SHA,
                "inkbox": "up" if inkbox_health_ok() else "down",
                "active_task": _read_json(active_task_path),
                "watchdog_state": _read_json(watchdog_state_path),
                "watchdog_log_present": watchdog_log.exists(),
                "alerter_channels": {
                    "bluebubbles": bool(
                        _os.environ.get("DREW_PHONE")
                        and _os.environ.get("BLUEBUBBLES_SERVER_URL")
                        and _os.environ.get("BLUEBUBBLES_PASSWORD")
                    ),
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, default=str).encode())
        elif parsed.path == "/health/sms-cap":
            # Admin-gated: 10DLC rate-limit visibility. Surfaces recent 429
            # events from $HERMES_HOME/logs/delivery_failures.jsonl so the
            # operator can see when Inkbox's 100-recipients/24h cap is hit
            # without grepping Railway logs. Pairs with the watchdog 429
            # monitor in scripts/runtime/watchdog.py — that one alerts on the
            # same threshold, this one is the read-only dashboard surface.
            import hmac as _hmc
            import os as _os
            from datetime import datetime as _dt, timezone as _tz
            admin_token = _os.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if not admin_token or not _hmc.compare_digest(req_token, admin_token):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "admin token required"}).encode())
                return
            import pathlib as _p2
            hermes_home = _os.environ.get("HERMES_HOME", "/opt/data")
            log_path = _p2.Path(hermes_home) / "logs" / "delivery_failures.jsonl"
            payload: dict = {
                "log_present": log_path.exists(),
                "rate_limited": False,
                "recent_429s_1h": 0,
                "recent_429s_24h": 0,
                "last_429_at": None,
                "first_429_in_24h_at": None,
                "earliest_429_in_24h_at": None,
                "distinct_to_phones_24h": [],
            }
            if log_path.exists():
                now_ts = _dt.now(_tz.utc).timestamp()
                cutoff_1h = now_ts - 3600
                cutoff_24h = now_ts - 86400
                distinct_phones: set = set()
                try:
                    with log_path.open(encoding="utf-8") as _fh:
                        for _line in _fh:
                            try:
                                _e = json.loads(_line)
                            except json.JSONDecodeError:
                                continue
                            _is_429 = (
                                _e.get("status_code") == 429
                                or _e.get("kind") == "10dlc_429"
                                or "rate_limit" in str(_e.get("error_code", "")).lower()
                                or "rate_limit" in str(_e.get("category", "")).lower()
                                or "rate_limit" in str(_e.get("error", "")).lower()
                            )
                            if not _is_429:
                                continue
                            _ts_raw = _e.get("ts")
                            if not _ts_raw:
                                continue
                            try:
                                _entry_ts = _dt.fromisoformat(
                                    str(_ts_raw).strip().replace("Z", "+00:00")
                                ).timestamp()
                            except Exception:
                                continue
                            if _entry_ts < cutoff_24h:
                                continue
                            payload["recent_429s_24h"] += 1
                            if _entry_ts >= cutoff_1h:
                                payload["recent_429s_1h"] += 1
                            if payload["last_429_at"] is None or _ts_raw > payload["last_429_at"]:
                                payload["last_429_at"] = _ts_raw
                            if payload["earliest_429_in_24h_at"] is None or _ts_raw < payload["earliest_429_in_24h_at"]:
                                payload["earliest_429_in_24h_at"] = _ts_raw
                            _phone = _e.get("to_phone") or _e.get("chat_id")
                            if _phone:
                                distinct_phones.add(str(_phone))
                        payload["distinct_to_phones_24h"] = sorted(distinct_phones)
                        payload["rate_limited"] = payload["recent_429s_24h"] > 0
                except Exception as _e:
                    payload["read_error"] = f"{type(_e).__name__}: {_e}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, default=str).encode())
        elif parsed.path == "/last-reply":
            import os as _os2, pathlib as _p3, hmac as _hmc2
            admin_token = _os2.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if admin_token and not _hmc2.compare_digest(req_token, admin_token):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "admin token required"}).encode())
                return
            buf = _p3.Path(_os2.environ.get("HERMES_HOME", "/opt/data")) / "test-reply-buffer.jsonl"
            entries: list = []
            if buf.exists():
                for _ln in buf.read_text(encoding="utf-8").splitlines():
                    try:
                        entries.append(json.loads(_ln))
                    except Exception:
                        pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"replies": entries, "count": len(entries)}).encode())
        elif parsed.path == "/admin/hermes-logs":
            import os as _osl, pathlib as _pl, hmac as _hmcl
            admin_token = _osl.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if admin_token and not _hmcl.compare_digest(req_token, admin_token):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "admin token required"}).encode())
                return
            q = urllib.parse.parse_qs(parsed.query)
            lines_n = int((q.get("n") or ["200"])[0])
            log_file = (q.get("file") or ["agent.log"])[0].replace("/", "").replace("..", "")
            hermes_home = _osl.environ.get("HERMES_HOME", "/opt/data")
            log_path = _pl.Path(hermes_home) / "logs" / log_file
            content = ""
            if log_path.exists():
                all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                content = "\n".join(all_lines[-lines_n:])
            # also list what's in logs dir
            logs_dir = _pl.Path(hermes_home) / "logs"
            log_files = [str(f.name) + f" ({f.stat().st_size}B)" for f in logs_dir.iterdir()] if logs_dir.exists() else []
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"log_files": log_files, "reading": log_file, "tail": content, "lines": lines_n}).encode())
        elif parsed.path == "/admin/plugin-debug":
            import os as _osd, pathlib as _pld, hmac as _hmcd, importlib.util as _ilu
            admin_token = _osd.environ.get("SAVE_ADMIN_TOKEN", "")
            req_token = self.headers.get("X-Save-Admin-Token", "")
            if admin_token and not _hmcd.compare_digest(req_token, admin_token):
                self.send_response(401); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"error":"admin token required"}).encode()); return
            hermes_home = _osd.environ.get("HERMES_HOME", "/opt/data")
            plugins_dir = _pld.Path(hermes_home) / "plugins"
            result = {"hermes_home": hermes_home, "plugins_dir_exists": plugins_dir.exists(), "plugins": []}
            if plugins_dir.exists():
                for p in sorted(plugins_dir.iterdir()):
                    info = {"name": p.name, "is_symlink": p.is_symlink(), "target": str(p.resolve()) if p.is_symlink() else None,
                            "init_exists": (p / "__init__.py").exists() if p.is_dir() else False}
                    result["plugins"].append(info)
            # List HERMES_HOME root
            hh = _pld.Path(hermes_home)
            result["hermes_home_files"] = sorted([f.name + ("/" if f.is_dir() else "") for f in hh.iterdir()]) if hh.exists() else []
            # Check for plugin registry files
            for regfile in ["plugins.json", "plugin-registry.json", ".plugins", "config.json", "hermes.json", "user-plugins.json"]:
                rfp = hh / regfile
                if rfp.exists():
                    try:
                        result[f"registry_{regfile}"] = rfp.read_text(encoding="utf-8")[:500]
                    except Exception:
                        result[f"registry_{regfile}"] = "unreadable"
            # Try to import reply_capture
            rc_path = plugins_dir / "reply_capture" / "__init__.py"
            if rc_path.exists():
                try:
                    _spec = _ilu.spec_from_file_location("reply_capture_test", str(rc_path))
                    _mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
                    result["import_ok"] = True
                    result["has_register"] = hasattr(_mod, "register")
                    result["has_on_transform"] = hasattr(_mod, "_on_transform")
                except Exception as e:
                    result["import_ok"] = False
                    result["import_error"] = str(e)
            else:
                result["rc_path_missing"] = str(rc_path)
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps(result, indent=2).encode())
        elif parsed.path == "/oauth/email/callback":
            query = urllib.parse.parse_qs(parsed.query)
            error = (query.get("error") or [""])[0]
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if error:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Email was not connected</h1><p>You can close this and try again from iMessage.</p>")
                return
            if not code or not state:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Missing email connection code</h1><p>Go back to iMessage and request a new link.</p>")
                return
            script = "/opt/agent/scripts/runtime/email_tool.py"
            result = subprocess.run(
                ["python3", script, "callback", "--code", code, "--state", state],
                capture_output=True,
                text=True,
                timeout=45,
            )
            try:
                payload = json.loads(result.stdout or "{}")
            except Exception:
                payload = {"ok": False, "error": "invalid_callback_output"}
            if result.returncode == 0 and payload.get("ok"):
                email = payload.get("email") or "your email"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    f"<h1>Email connected</h1><p>{email} is connected. You can close this and go back to iMessage.</p>".encode()
                )
            else:
                sys.stderr.write(f"[email] callback failed: {result.stderr or result.stdout}\n")
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Email connection failed</h1><p>Go back to iMessage and request a new link.</p>")
        elif parsed.path.startswith("/shares/"):
            filename = os.path.basename(parsed.path)
            shares_dir = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "public", "shares")
            safe_path = os.path.join(shares_dir, filename)
            if filename and os.path.exists(safe_path) and os.path.abspath(safe_path).startswith(os.path.abspath(shares_dir)):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                with open(safe_path, "rb") as fh:
                    self.wfile.write(fh.read())
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Not Found\n")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Agent is running.\n")
    def do_OPTIONS(self):
        # CORS preflight for the funnel signup endpoint (browser → /api/signup).
        # Other endpoints don't need CORS — they're either internal webhooks
        # (HMAC-signed, no browser origin) or admin-token gated GETs.
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/api/signup":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", SIGNUP_CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-Signup-Token",
            )
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def _client_ip(self) -> str:
        # Vercel and Cloudflare set X-Forwarded-For; trust the first hop.
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        try:
            return self.client_address[0]
        except Exception:
            return "unknown"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _bump("post_total")
        _record_path(self.path)

        # ── Funnel signup endpoint ─────────────────────────────────
        # Public POST from the Vercel landing page. Auth via shared
        # signup token header (NOT the admin token — different blast
        # radius). Inserts into phones as tier='user' and fires the
        # outbound BlueBubbles welcome.
        parsed_signup = urllib.parse.urlparse(self.path)
        if parsed_signup.path == "/api/signup":
            if _signup is None:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", SIGNUP_CORS_ORIGIN)
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"signup module unavailable"}')
                return
            status, resp_payload = _signup.handle_signup(
                body_bytes=body,
                header_token=self.headers.get("X-Signup-Token", ""),
                client_ip=self._client_ip(),
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", SIGNUP_CORS_ORIGIN)
            self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(json.dumps(resp_payload).encode())
            return

        # ── BlueBubbles inbound webhook ────────────────────────────
        # BlueBubbles posts new-message events directly here. We
        # translate to an Inkbox text_message envelope so the existing
        # tier_gate + sanitize + tier_marker pipeline below handles it
        # unchanged. Auth is the BlueBubbles password as a ?password=
        # query param (BlueBubbles webhook API has no header support).
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/bluebubbles/webhook":
            qs = urllib.parse.parse_qs(parsed_url.query)
            supplied_pw = (qs.get("password") or [""])[0]
            expected_pw = os.environ.get("BLUEBUBBLES_PASSWORD", "")
            if not expected_pw:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"bluebubbles not configured"}')
                return
            import hmac as _hmc_bb
            if not _hmc_bb.compare_digest(supplied_pw, expected_pw):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                sys.stderr.write("[proxy] 401 — invalid BlueBubbles webhook password\n")
                return
            try:
                payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
            except Exception as _e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"bad json: {_e}"}).encode())
                return
            try:
                sys.path.insert(0, "/opt/agent")
                from app.plugins.bluebubbles import translate_webhook_to_inkbox_event
                envelope = translate_webhook_to_inkbox_event(payload)
            except Exception as _e:
                sys.stderr.write(f"[proxy] bluebubbles translate failed: {_e}\n")
                envelope = None
            if envelope is None:
                # Not a user message we care about — ack so BB doesn't retry
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ignored"}')
                return
            # Dedup: BlueBubbles retries delivery on slow responses. Skip a
            # message guid we've already processed so the user never gets a
            # double reply.
            try:
                _bb_msg_id = str(
                    envelope.get("data", {}).get("text_message", {}).get("id") or ""
                )
            except Exception:
                _bb_msg_id = ""
            if _bb_already_seen(_bb_msg_id):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"already_processed"}')
                return
            # Re-emit as a synthetic Inkbox-shaped body. Fall through into
            # the existing tier_gate / sanitize / forward pipeline by
            # replacing `body` and `self.path` to mimic an Inkbox POST.
            # Path is `/webhook` — Hermes' Inkbox platform plugin's
            # DEFAULT_WEBHOOK_PATH (gateway/platforms/inkbox.py:116). A
            # previous bug here used `/inkbox/webhook` which 404'd silently,
            # making every BB-translated webhook die after tier resolution.
            body = json.dumps(envelope).encode("utf-8")
            self.path = "/webhook"
            sys.stderr.write(
                f"[proxy] bluebubbles → inkbox-shaped forward "
                f"(len={len(body)} from={envelope.get('data', {}).get('from', '')[-4:]})\n"
            )
            # No HMAC required for translated payload — internal hand-off.
            require_sig = False
        else:
            require_sig = os.environ.get("INKBOX_REQUIRE_SIGNATURE", "true").lower() == "true"
        admin_token = os.environ.get("SAVE_ADMIN_TOKEN", "")
        # Hybrid auth: admin token bypass OR Inkbox HMAC
        if require_sig:
            import hmac as _hmac_post
            request_admin = self.headers.get("X-Save-Admin-Token", "")
            if admin_token and _hmac_post.compare_digest(request_admin, admin_token):
                pass  # admin bypass — let through
            else:
                # Inkbox HMAC verification
                signing_key = os.environ.get("INKBOX_SIGNING_KEY", "")
                signature = self.headers.get("X-Inkbox-Signature", "")
                if signing_key and signature:
                    import hmac
                    import hashlib
                    expected = hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(signature.split("=")[-1], expected):
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "invalid signature"}).encode())
                        sys.stderr.write("[proxy] 401 — invalid Inkbox signature\n")
                        return
                elif signing_key and not signature:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "missing signature"}).encode())
                    sys.stderr.write("[proxy] 401 — missing X-Inkbox-Signature\n")
                    return
        # ── Tier gate (V1_SPEC §18.1) ───────────────────────────────
        # Resolve sender phone → tier. Inject X-Save-Tier header BEFORE
        # forwarding to Hermes. Fails closed to 'stranger' on any error.
        forward_headers = dict(self.headers)
        _tier = "stranger"
        if _tier_gate is not None:
            try:
                _phone, _tier = _tier_gate.resolve(body)
                forward_headers.update(_tier_gate.header_for(_tier))
                sys.stderr.write(f"[proxy] tier={_tier} phone={_phone or 'unknown'}\n")
                _record_phone(_phone, _tier)
                _bump("post_tier_resolved")
            except Exception as _e:
                sys.stderr.write(f"[proxy] tier_gate failure: {_e} — defaulting to stranger\n")
                forward_headers["X-Save-Tier"] = "stranger"
                _tier = "stranger"
                _bump("post_tier_gate_exception")
        else:
            forward_headers["X-Save-Tier"] = "stranger"

        # ── Stranger sanitization + tier marker injection ───────────
        # V1_SPEC §18.4 sanitization (stranger only) + §18.1 tier marker
        # (all tiers). The marker is the authoritative tier signal the
        # LLM reads via SKILL.md — Hermes does not surface HTTP headers.
        forward_body = body
        try:
            _parsed = json.loads(body.decode("utf-8", errors="replace"))
            _changed = False
            _data = _parsed.get("data", {}) if isinstance(_parsed, dict) else {}
            _tm_msg = _data.get("text_message") if isinstance(_data, dict) else None
            _text_owner = None
            if isinstance(_tm_msg, dict) and isinstance(_tm_msg.get("text"), str):
                _text_owner = _tm_msg
            elif isinstance(_data, dict) and isinstance(_data.get("text"), str):
                _text_owner = _data
            if _text_owner is not None:
                _raw_text = _text_owner["text"]
                # Stranger only: strip injection patterns + size cap.
                if _tier == "stranger" and _sanitize is not None:
                    _clean, _modified = _sanitize.sanitize(_raw_text)
                    if _modified:
                        _raw_text = _clean
                        sys.stderr.write("[proxy] sanitize: stranger input modified\n")
                # All tiers: prepend authoritative tier marker.
                if _tier_marker is not None:
                    _raw_text = _tier_marker.prepend(_raw_text, _tier)
                if _raw_text != _text_owner["text"]:
                    _text_owner["text"] = _raw_text
                    _changed = True
            if _changed:
                forward_body = json.dumps(_parsed).encode("utf-8")
                forward_headers["Content-Length"] = str(len(forward_body))
        except Exception as _e:
            sys.stderr.write(f"[proxy] text rewrite skipped: {_e}\n")

        # ── Sign the trusted internal forward ──────────────────────────
        # Hermes's _handle_webhook calls inkbox.verify_webhook() over the
        # forwarded body before processing it.  The algorithm (from
        # inkbox/signing_keys.py) is:
        #
        #   key    = INKBOX_SIGNING_KEY.removeprefix("whsec_")
        #   msg    = f"{request_id}.{timestamp}.".encode() + payload
        #   sig    = "sha256=" + hmac_sha256(key, msg).hexdigest()
        #
        # The proxy is the trusted signer: it signs the *final* forward_body
        # (which may differ from the original body after tier-marker injection)
        # using its own request-id and timestamp so the signature stays valid.
        #
        # SECURITY INVARIANT: this block is reached only AFTER external auth
        # has already passed (BlueBubbles password check at line 597, or
        # Inkbox HMAC / admin-token check in the require_sig block above).
        # Signing never occurs for unauthenticated external requests.
        _inkbox_signing_key = os.environ.get("INKBOX_SIGNING_KEY", "")
        if _inkbox_signing_key:
            import hmac as _fwd_hmac
            import hashlib as _fwd_hashlib
            import uuid as _fwd_uuid
            import time as _fwd_time
            _fwd_key = _inkbox_signing_key
            if _fwd_key.startswith("whsec_"):
                _fwd_key = _fwd_key[len("whsec_"):]
            _fwd_request_id = str(_fwd_uuid.uuid4())
            _fwd_timestamp = str(int(_fwd_time.time()))
            _fwd_msg = (
                f"{_fwd_request_id}.{_fwd_timestamp}.".encode()
                + forward_body
            )
            _fwd_sig = "sha256=" + _fwd_hmac.new(
                _fwd_key.encode(), _fwd_msg, _fwd_hashlib.sha256
            ).hexdigest()
            forward_headers["X-Inkbox-Signature"] = _fwd_sig
            forward_headers["X-Inkbox-Request-Id"] = _fwd_request_id
            forward_headers["X-Inkbox-Timestamp"] = _fwd_timestamp
            forward_headers["Content-Length"] = str(len(forward_body))

        status, resp_headers, resp_body = proxy_to_inkbox("POST", self.path, forward_headers, forward_body)
        self.send_response(status)
        self.send_header("Content-Type", resp_headers.get("Content-Type", "application/json"))
        self.end_headers()
        self.wfile.write(resp_body)
    def log_message(self, format, *args):
        sys.stderr.write("[proxy] %s\n" % (format % args))

server = http.server.ThreadingHTTPServer(("0.0.0.0", port), ProxyHandler)
sys.stderr.write(f"[proxy] Listening on 0.0.0.0:{port} -> Inkbox on {INKBOX_PORT}\n")
server.serve_forever()
