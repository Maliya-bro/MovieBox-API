import base64
from collections import defaultdict
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from curl_cffi import requests as cr
except ImportError:
    cr = None

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API_DESCRIPTION = """
### MovieBox Developer API (Diagnostic & Testing Edition)
Every endpoint returns detailed upstream diagnostics (`upstream_status`, `upstream_headers`, `upstream_body`, and `errors`) if an upstream request fails.
"""

app = FastAPI(
    title="MovieBox Developer API",
    description=API_DESCRIPTION,
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# ==========================================
# GLOBAL EXCEPTION HANDLER (Shows exact crash cause)
# ==========================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "status": "internal_error",
            "path": str(request.url.path),
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc().splitlines()[-5:],
        },
    )

# ==========================================
# SECURITY: RATE LIMITER & IN-MEMORY CACHE
# ==========================================
RATE_LIMIT_STORE: Dict[str, List[float]] = defaultdict(list)
MAX_REQ_PER_MIN = int(os.environ.get("RATE_LIMIT", 40))
RESPONSE_CACHE: Dict[str, Tuple[float, Any]] = {}

def get_cache(key: str) -> Optional[Any]:
    entry = RESPONSE_CACHE.get(key)
    if not entry:
        return None
    expiry, value = entry
    if time.time() > expiry:
        RESPONSE_CACHE.pop(key, None)
        return None
    return value

def set_cache(key: str, value: Any, ttl_sec: int = 600) -> None:
    if len(RESPONSE_CACHE) > 500:
        for k in list(RESPONSE_CACHE.keys())[:100]:
            RESPONSE_CACHE.pop(k, None)
    RESPONSE_CACHE[key] = (time.time() + ttl_sec, value)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    protected_prefixes = ("/api/", "/search", "/detail", "/episodes", "/movies", "/tv-series", "/home")
    if request.url.path.startswith(protected_prefixes):
        forwarded = request.headers.get("x-forwarded-for")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
        now = time.time()
        RATE_LIMIT_STORE[client_ip] = [t for t in RATE_LIMIT_STORE[client_ip] if now - t < 60]
        if len(RATE_LIMIT_STORE[client_ip]) >= MAX_REQ_PER_MIN:
            return JSONResponse(
                status_code=429,
                content={
                    "status": "error",
                    "code": 429,
                    "message": f"Rate limit exceeded ({MAX_REQ_PER_MIN} requests/minute). Please slow down."
                }
            )
        RATE_LIMIT_STORE[client_ip].append(now)
    return await call_next(request)

# ==========================================
# CORE ENGINE & SIGNING
# ==========================================
SECRET_KEY_DEFAULT_B64 = "NzZpUmwwN3MweFNOOWpxbUVXQXQ3OUVCSlp1bElRSXNWNjRGWnIyTw=="
SECRET_KEY_ALT_B64 = "WHFuMm5uTzQxL0w5Mm8xaXVYaFNMSFRiWHZZNFo1Wlo2Mm04bVNMQQ=="
MODERN_USER_AGENT = "com.community.mbox.in/50020130 (Linux; U; Android 14; en_IN; Pixel 8; Build/UD1A.230803.041; Cronet/145.0.7582.0)"
WEB_USER_AGENT = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"
NATIVE_API_BASE = "https://api3.aoneroom.com"
BOOTSTRAP_HOST = "https://apig.inmoviebox.com"
BOOTSTRAP_URL = f"{BOOTSTRAP_HOST}/wefeed-mobile-bff/tab/ranking-list?tabId=0&categoryType=4516404531735022304&page=1&perPage=1"

WEB_DOMAINS = [
    "https://123moviesfree.club",
    "https://movie-box.co",
    "https://movieboxonline.net",
    "https://sflix.film",
    "https://h5-api.aoneroom.com",
    "https://netnaija.film",
    "https://movieboxhd.net",
]

def _b64decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)

def get_secret_bytes(use_alt_key: bool = False) -> bytes:
    outer = SECRET_KEY_ALT_B64 if use_alt_key else SECRET_KEY_DEFAULT_B64
    inner = _b64decode(outer).decode("utf-8")
    return _b64decode(inner)

def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def generate_device_id() -> str:
    return secrets.token_hex(16)

def generate_x_client_token(timestamp: int) -> str:
    ts_str = str(timestamp)
    reversed_ts = ts_str[::-1].encode("utf-8")
    return f"{ts_str},{md5_hex(reversed_ts)}"

def generate_client_info(device_id: str) -> str:
    return json.dumps(
        {
            "package_name": "com.community.mbox.in",
            "version_name": "4.0.03.0920.03",
            "version_code": 50020130,
            "os": "android",
            "os_version": "14",
            "device_id": device_id,
            "install_store": "official",
            "gaid": "1b2212c1-dadf-43c3-a0c8-bd6ce48ae22d",
            "brand": "Google",
            "model": "Pixel 8",
            "system_language": "en",
            "net": "NETWORK_WIFI",
            "region": "IN",
            "timezone": "Asia/Calcutta",
            "sp_code": "",
        },
        separators=(",", ":"),
    )

def build_canonical_string(method: str, accept: str | None, content_type: str | None, url: str, body: str | None, timestamp: int) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        pairs = []
        for segment in parsed.query.split("&"):
            if not segment:
                continue
            pieces = segment.split("=", 1)
            pairs.append((pieces[0], pieces[1] if len(pieces) > 1 else ""))
        pairs.sort(key=lambda kv: kv[0])
        canonical_query = "&".join(f"{k}={v}" for k, v in pairs)
        canonical_url = f"{path}?{canonical_query}"
    else:
        canonical_url = path

    body_bytes = body.encode("utf-8") if body is not None else None
    if body_bytes:
        body_length = str(len(body_bytes))
        body_hash = md5_hex(body_bytes[:102400])
    else:
        body_length = ""
        body_hash = ""

    return "\n".join(
        [
            method.upper(),
            accept or "",
            content_type or "",
            body_length,
            str(timestamp),
            body_hash,
            canonical_url,
        ]
    )

def generate_x_tr_signature(method: str, accept: str | None, content_type: str | None, url: str, body: str | None, timestamp: int, use_alt_key: bool = False) -> str:
    canonical = build_canonical_string(method, accept, content_type, url, body, timestamp)
    key = get_secret_bytes(use_alt_key)
    digest = hmac.new(key, canonical.encode("utf-8"), hashlib.md5).digest()
    sig_b64 = base64.b64encode(digest).decode("ascii")
    return f"{timestamp}|2|{sig_b64}"

def build_auth_headers(method: str, url: str, body: str | None = None, token: str | None = None, device_id: str | None = None, use_alt_key: bool = False, content_type: str = "application/json", accept: str = "application/json") -> Dict[str, str]:
    ts = int(time.time() * 1000)
    dev_id = device_id or generate_device_id()
    headers = {
        "user-agent": MODERN_USER_AGENT,
        "accept": accept,
        "content-type": content_type,
        "connection": "keep-alive",
        "x-client-token": generate_x_client_token(ts),
        "x-tr-signature": generate_x_tr_signature(method, accept, content_type, url, body, ts, use_alt_key),
        "x-client-info": generate_client_info(dev_id),
        "x-client-status": "0",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers

def extract_policy_resource(sign_cookie: str | None) -> str | None:
    if not sign_cookie:
        return None
    m_edge = re.search(r"Edge-Cache-Cookie=urlprefix=([^:;\s]+)", sign_cookie)
    if m_edge:
        try:
            decoded = _b64decode(m_edge.group(1)).decode("utf-8").rstrip("/")
            return decoded + "/index.mpd"
        except Exception:
            pass
    m_cf = re.search(r"CloudFront-Policy=([^;]+)", sign_cookie)
    if m_cf:
        raw = m_cf.group(1)
        for t in ("cf", "std"):
            try:
                b = raw.replace("-", "+").replace("_", "=") if t == "cf" else raw
                b += "=" * (-len(b) % 4)
                data = json.loads(base64.b64decode(b).decode("utf-8"))
                for s in data.get("Statement", []):
                    res = s.get("Resource")
                    if res:
                        return res.rstrip("*").rstrip("/") + "/index.mpd"
            except Exception:
                pass
    return None

class MovieBoxEngine:
    _instance = None

    def __init__(self):
        self.device_id = generate_device_id()
        self.bearer_token: Optional[str] = None
        if cr:
            self.session = cr.Session(impersonate="chrome120")
        else:
            self.session = httpx.Client(timeout=25, follow_redirects=True)

    @classmethod
    def get_instance(cls) -> "MovieBoxEngine":
        if cls._instance is None:
            cls._instance = MovieBoxEngine()
        return cls._instance

    def bootstrap_token(self, force: bool = False) -> str:
        if self.bearer_token and not force:
            return self.bearer_token
        headers = build_auth_headers("GET", BOOTSTRAP_URL, device_id=self.device_id)
        try:
            resp = self.session.get(BOOTSTRAP_URL, headers=headers, timeout=20)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "bootstrap_token",
                    "error": "Upstream connection exception",
                    "target_url": BOOTSTRAP_URL,
                    "tls_client": "curl_cffi" if cr else "httpx",
                    "exception": str(e),
                },
            )

        x_user = resp.headers.get("x-user")
        if not x_user:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "bootstrap_token",
                    "error": "Upstream authentication failed (missing x-user header)",
                    "target_url": BOOTSTRAP_URL,
                    "tls_client": "curl_cffi" if cr else "httpx",
                    "upstream_status": resp.status_code,
                    "upstream_headers": dict(resp.headers),
                    "upstream_body": resp.text[:600],
                },
            )

        try:
            token_data = json.loads(x_user)
            token = token_data.get("token")
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "bootstrap_token",
                    "error": "Failed to parse x-user header JSON",
                    "raw_x_user": x_user[:300],
                    "exception": str(e),
                },
            )

        if not token:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "bootstrap_token",
                    "error": "Upstream returned empty session token",
                    "x_user_data": token_data,
                },
            )
        self.bearer_token = token
        return token

    def api_request(self, method: str, path_or_url: str, body_dict: Optional[Dict[str, Any]] = None, use_token: bool = True, retry_auth: bool = True) -> Dict[str, Any]:
        full_url = path_or_url if path_or_url.startswith(("http://", "https://")) else f"{NATIVE_API_BASE}{path_or_url}"
        if use_token and not self.bearer_token:
            self.bootstrap_token()
        body_str = json.dumps(body_dict, separators=(",", ":")) if body_dict is not None else None
        headers = build_auth_headers(
            method=method,
            url=full_url,
            body=body_str,
            token=self.bearer_token if use_token else None,
            device_id=self.device_id,
        )
        data_bytes = body_str.encode("utf-8") if body_str is not None else None
        try:
            if method.upper() == "POST":
                resp = self.session.post(full_url, headers=headers, data=data_bytes, timeout=25)
            else:
                resp = self.session.get(full_url, headers=headers, timeout=25)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "api_request",
                    "error": "Upstream API connection failed",
                    "url": full_url,
                    "exception": str(e),
                },
            )

        if resp.status_code in (401, 441) and retry_auth:
            self.bootstrap_token(force=True)
            return self.api_request(method, path_or_url, body_dict, use_token, retry_auth=False)

        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "api_request",
                    "error": f"Upstream server returned HTTP {resp.status_code}",
                    "url": full_url,
                    "upstream_status": resp.status_code,
                    "upstream_headers": dict(resp.headers),
                    "upstream_body": resp.text[:600],
                },
            )

        try:
            parsed = resp.json()
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "api_request",
                    "error": "Invalid JSON returned by upstream server",
                    "url": full_url,
                    "upstream_body": resp.text[:600],
                    "exception": str(e),
                },
            )

        if parsed.get("code") not in (0, 200, None):
            raise HTTPException(
                status_code=502,
                detail={
                    "step": "api_request",
                    "error": "Upstream API returned logical error code",
                    "url": full_url,
                    "upstream_json": parsed,
                },
            )
        return parsed

    def search(self, query: str, page: int = 1, per_page: int = 20) -> List[Dict[str, Any]]:
        payload = {"keyword": query, "page": page, "perPage": per_page}
        data = self.api_request("POST", "/wefeed-mobile-bff/subject-api/search", body_dict=payload).get("data", {})
        raw_items = data.get("items", []) or data.get("list", [])
        results = []
        for item in raw_items:
            sub = item.get("subject", item)
            results.append({
                "subject_id": str(sub.get("subjectId")),
                "title": sub.get("title"),
                "type": "Movie" if sub.get("subjectType") == 1 else "Series",
                "subject_type": sub.get("subjectType"),
                "release_date": sub.get("releaseDate"),
                "imdb_rating": sub.get("imdbRatingValue"),
                "genre": sub.get("genre"),
                "cover": sub.get("cover", {}).get("url") if isinstance(sub.get("cover"), dict) else None,
                "detail_path": sub.get("detailPath"),
                "language": sub.get("language")
            })
        return results

    def get_details(self, subject_id: str) -> Dict[str, Any]:
        data = self.api_request("GET", f"/wefeed-mobile-bff/subject-api/get?subjectId={subject_id}").get("data", {})
        if not data:
            raise HTTPException(status_code=404, detail={"error": "Title not found", "subject_id": subject_id})
        detail_url_str = data.get("detailUrl", "")
        detail_path = detail_url_str.split("/")[-1] if "/" in detail_url_str else detail_url_str
        sub_cover = data.get("cover")
        return {
            "subject_id": str(data.get("subjectId")),
            "title": data.get("title"),
            "subject_type": data.get("subjectType"),
            "type": "Movie" if data.get("subjectType") == 1 else "Series",
            "description": data.get("description"),
            "release_date": data.get("releaseDate"),
            "duration": data.get("duration"),
            "imdb_rating": data.get("imdbRatingValue"),
            "genre": data.get("genre"),
            "country": data.get("countryName"),
            "language": data.get("language"),
            "cover": sub_cover.get("url") if isinstance(sub_cover, dict) else None,
            "detail_path": detail_path,
            "dubs": [
                {
                    "subject_id": str(d.get("subjectId")),
                    "lang_name": d.get("lanName"),
                    "language": d.get("language"),
                }
                for d in data.get("dubs", [])
                if isinstance(d, dict)
            ]
        }

    def get_episodes(self, subject_id: str) -> List[Dict[str, Any]]:
        res = self.api_request("GET", f"/wefeed-mobile-bff/subject-api/season-info?subjectId={subject_id}")
        seasons = res.get("data", {}).get("seasons", [])
        return [
            {
                "season": s.get("se", 1),
                "episode_count": s.get("maxEp", 1),
                "resolutions": s.get("resolutions", [])
            }
            for s in seasons
        ]

    def get_streams(self, subject_id: str, season: int = 0, episode: int = 0, detail_path: Optional[str] = None) -> Dict[str, Any]:
        domain_logs = []
        if not detail_path:
            details = self.get_details(subject_id)
            detail_path = details.get("detail_path", "")

        token = self.bootstrap_token()
        streams: List[Dict[str, Any]] = []
        seen_urls = set()

        for domain in WEB_DOMAINS:
            play_url = (
                f"{domain}/wefeed-h5api-bff/subject/play?"
                f"subjectId={subject_id}&se={season}&ep={episode}&detailPath={detail_path}&"
                f"streamSignType=1&supportCodecs%5Bhevc%5D=1&supportCodecs%5Bh264%5D=1"
            )
            referer = f"{domain}/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe=&detailEp=&lang=en"
            headers = {
                "User-Agent": WEB_USER_AGENT,
                "Referer": referer,
                "Accept": "application/json",
                "x-client-info": '{"timezone":"Asia/Calcutta"}',
                "x-request-lang": "en",
                "x-vip-restrict": "0",
                "x-no-high-risk-restrict": "0",
                "x-source": "moviebox",
                "Authorization": f"Bearer {token}",
            }
            try:
                resp = self.session.get(play_url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    domain_logs.append({"domain": domain, "status": resp.status_code, "body": resp.text[:200]})
                    continue
                json_body = resp.json()
                data = json_body.get("data", {})
                raw_streams = data.get("streams", [])
                dash_items = data.get("dash", [])

                if not raw_streams and not dash_items:
                    domain_logs.append({"domain": domain, "status": 200, "reason": "Empty streams array", "upstream_json": json_body})
                    continue

                for st in raw_streams:
                    url = st.get("url")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    res_str = str(st.get("resolutions", "unknown"))
                    size_bytes = int(st.get("size") or 0)
                    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                    quality_val = 0
                    for q in (2160, 1440, 1080, 720, 480, 360, 240):
                        if str(q) in res_str:
                            quality_val = q
                            break
                    streams.append({
                        "quality": f"{quality_val}p" if quality_val else res_str,
                        "quality_value": quality_val,
                        "format": st.get("format", "MP4"),
                        "size_mb": round(size_mb, 1),
                        "codec": st.get("codecName"),
                        "duration_sec": st.get("duration"),
                        "url": url,
                        "headers": {
                            "User-Agent": "Mozilla/5.0",
                            "Referer": f"{domain}/",
                            "Origin": domain,
                        }
                    })

                for d in dash_items:
                    url = d.get("url")
                    cookie = d.get("signCookie")
                    extracted_mpd = extract_policy_resource(cookie) if cookie else None
                    final_url = extracted_mpd or url
                    if final_url and final_url not in seen_urls:
                        seen_urls.add(final_url)
                        res_tag = str(d.get("resolutions", "adaptive"))
                        streams.append({
                            "quality": "1080p (Adaptive)" if "1080" in res_tag else "Adaptive",
                            "quality_value": 1080 if "1080" in res_tag else 999,
                            "format": "DASH",
                            "size_mb": 0,
                            "codec": "hevc/h264",
                            "duration_sec": None,
                            "url": final_url,
                            "cookie": cookie,
                            "headers": {
                                "User-Agent": "Mozilla/5.0",
                                "Referer": f"{domain}/",
                                "Origin": domain,
                            }
                        })
                break
            except Exception as e:
                domain_logs.append({"domain": domain, "exception": str(e)})
                continue

        if not streams:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "No streams resolved from any Web CDN domain",
                    "subject_id": subject_id,
                    "season": season,
                    "episode": episode,
                    "domain_attempts": domain_logs,
                },
            )

        streams.sort(key=lambda s: s["quality_value"], reverse=True)
        return {"streams": streams}

    def get_subtitles(self, subject_id: str, stream_id: Optional[str] = None, season: int = 0, episode: int = 0) -> List[Dict[str, Any]]:
        subs: List[Dict[str, Any]] = []
        seen_urls = set()
        errors = []

        if not stream_id:
            native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
            raw_streams = self.api_request("GET", native_url).get("data", {}).get("streams", [])
            if raw_streams and raw_streams[0].get("id"):
                stream_id = str(raw_streams[0]["id"])

        def _add_sub(item: Dict[str, Any]):
            u = item.get("url")
            if u and u not in seen_urls:
                seen_urls.add(u)
                subs.append({
                    "id": str(item.get("id", "")),
                    "language": item.get("lanName") or item.get("language") or "English",
                    "code": item.get("lan", ""),
                    "url": u,
                    "size": item.get("size")
                })

        if stream_id:
            try:
                url = f"/wefeed-mobile-bff/subject-api/get-stream-captions?subjectId={subject_id}&streamId={stream_id}"
                data = self.api_request("GET", url).get("data", {})
                captions = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
                for s in captions:
                    if isinstance(s, dict):
                        _add_sub(s)
            except Exception as e:
                errors.append({"source": "get-stream-captions", "detail": getattr(e, "detail", str(e))})

        try:
            res_id = stream_id or "0"
            url = f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={res_id}&episode={episode}"
            data = self.api_request("GET", url).get("data", {})
            ext = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
            for s in ext:
                if isinstance(s, dict):
                    _add_sub(s)
        except Exception as e:
            errors.append({"source": "get-ext-captions", "detail": getattr(e, "detail", str(e))})

        if not subs and errors:
            raise HTTPException(status_code=502, detail={"error": "Failed to fetch subtitles", "errors": errors})

        return subs

engine = MovieBoxEngine.get_instance()

# ==========================================
# INTERACTIVE DEVELOPER PORTAL UI
# ==========================================
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def developer_portal():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MovieBox Developer API — Docs & Diagnostic Playground</title>
    <style>
        :root {
            --bg: #09090b; --surface: #111114; --surface-2: #18181c; --border: #27272a;
            --text: #fafafa; --dim: #a1a1aa; --blue: #3b82f6; --green: #10b981;
            --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; line-height: 1.6; padding: 28px 18px 60px; }
        .container { max-width: 980px; margin: 0 auto; display: flex; flex-direction: column; gap: 24px; }
        header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 18px; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 12px; }
        .btn { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }
        .btn.primary { background: var(--text); color: var(--bg); border: none; }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
        tr:last-child td { border-bottom: none; }
        code { font-family: var(--mono); background: #000; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div>
            <h1 style="font-size: 20px;">MovieBox Developer API <span style="font-size: 12px; color: var(--green);">v3.1.0 (Full Diagnostics)</span></h1>
        </div>
        <div style="display: flex; gap: 8px;">
            <a href="/health" target="_blank" class="btn">Health & TLS Check</a>
            <a href="/docs" target="_blank" class="btn primary">Swagger UI ↗</a>
        </div>
    </header>
    <div class="card" style="padding: 0; overflow-x: auto;">
        <table>
            <thead><tr><th>Method</th><th>Endpoint</th><th>Description</th><th style="text-align: right;">Test</th></tr></thead>
            <tbody>
                <tr><td><code>GET</code></td><td><code>/health</code></td><td>Verify server status & <code>curl_cffi</code> TLS client</td><td style="text-align: right;"><a href="/health" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/search?q=Avatar</code></td><td>Search titles (shows exact upstream error if blocked)</td><td style="text-align: right;"><a href="/search?q=Avatar" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/detail/8906247916759695608</code></td><td>Metadata & audio dubs</td><td style="text-align: right;"><a href="/detail/8906247916759695608" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/episodes/6207982430134357800</code></td><td>TV seasons & episode counts</td><td style="text-align: right;"><a href="/episodes/6207982430134357800" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/api/stream/8906247916759695608</code></td><td>Stream links + per-domain diagnostic logs</td><td style="text-align: right;"><a href="/api/stream/8906247916759695608" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/api/stream/8906247916759695608/captions</code></td><td>Subtitles (.srt) + error trace</td><td style="text-align: right;"><a href="/api/stream/8906247916759695608/captions" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/movies?page=1</code></td><td>Paginated movie catalog</td><td style="text-align: right;"><a href="/movies?page=1" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/tv-series?page=1</code></td><td>Paginated TV series catalog</td><td style="text-align: right;"><a href="/tv-series?page=1" target="_blank" class="btn">Open</a></td></tr>
                <tr><td><code>GET</code></td><td><code>/home</code></td><td>Homepage trending & banners</td><td style="text-align: right;"><a href="/home" target="_blank" class="btn">Open</a></td></tr>
            </tbody>
        </table>
    </div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html_content)

# ==========================================
# PUBLIC REST ENDPOINTS (WITH FULL ERROR REPORTING)
# ==========================================
@app.get("/health", tags=["System"], summary="Check API Server Health & TLS Client")
def health_check():
    return {
        "status": "ok",
        "version": "3.1.0",
        "tls_client": "curl_cffi (chrome120)" if cr is not None else "httpx (fallback - no TLS impersonation)",
        "cached_entries": len(RESPONSE_CACHE),
        "rate_limit_per_min": MAX_REQ_PER_MIN
    }

@app.get("/search", tags=["Search & Discovery"], summary="Search Movies & TV Series")
def search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50)
):
    cache_key = f"search:{q.lower()}:{page}:{per_page}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    res = {"query": q, "page": page, "results": engine.search(q, page=page, per_page=per_page)}
    set_cache(cache_key, res, ttl_sec=600)
    return res

@app.get("/search/suggest", tags=["Search & Discovery"], summary="Live Search Autocomplete")
def search_suggest(q: str = Query(..., min_length=1)):
    cache_key = f"suggest:{q.lower()}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    url = f"{API_BASE}/subject/search-suggest"
    headers = {"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}
    try:
        resp = engine.session.post(url, headers=headers, json={"keyword": q, "perPage": 10}, timeout=15)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": "Upstream connection failed", "url": url, "exception": str(e)})

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"Upstream returned HTTP {resp.status_code}",
                "url": url,
                "upstream_headers": dict(resp.headers),
                "upstream_body": resp.text[:600],
            },
        )
    raw = resp.json().get("data", {}).get("items", [])
    suggestions = [
        {
            "title": (sub := item.get("subject") or {}).get("title") or item.get("word") or item.get("title"),
            "slug": sub.get("detailPath") or item.get("detailPath"),
            "subject_id": str(sub.get("subjectId") or item.get("subjectId"))
        }
        for item in raw
    ]
    res = {"suggestions": suggestions}
    set_cache(cache_key, res, ttl_sec=600)
    return res

@app.get("/detail/{subject_id}", tags=["Metadata & Episodes"], summary="Get Full Metadata & Audio Dubs")
def get_detail(subject_id: str = Path(...)):
    cache_key = f"detail:{subject_id}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    res = engine.get_details(subject_id)
    set_cache(cache_key, res, ttl_sec=900)
    return res

@app.get("/episodes/{subject_id}", tags=["Metadata & Episodes"], summary="Get TV Series Seasons & Episodes")
def get_episodes(subject_id: str = Path(...)):
    cache_key = f"episodes:{subject_id}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    res = {"subject_id": subject_id, "seasons": engine.get_episodes(subject_id)}
    set_cache(cache_key, res, ttl_sec=900)
    return res

@app.get("/api/stream/{subject_id}", tags=["Streams & Subtitles"], summary="Resolve Direct MP4 & DASH Stream Links")
def get_streams(
    subject_id: str = Path(...),
    se: int = Query(0, ge=0),
    ep: int = Query(0, ge=0),
    detail_path: Optional[str] = Query(None)
):
    cache_key = f"stream:{subject_id}:{se}:{ep}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    result = engine.get_streams(subject_id, season=se, episode=ep, detail_path=detail_path)
    streams = result["streams"]
    res = {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(streams),
        "streams": streams
    }
    set_cache(cache_key, res, ttl_sec=300)
    return res

@app.get("/api/stream/{subject_id}/captions", tags=["Streams & Subtitles"], summary="Get Multi-Language Subtitles (.srt)")
def get_captions(
    subject_id: str = Path(...),
    se: int = Query(0, ge=0),
    ep: int = Query(0, ge=0),
    stream_id: Optional[str] = Query(None)
):
    cache_key = f"captions:{subject_id}:{se}:{ep}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    subs = engine.get_subtitles(subject_id, stream_id=stream_id, season=se, episode=ep)
    res = {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(subs),
        "captions": subs
    }
    if subs:
        set_cache(cache_key, res, ttl_sec=600)
    return res

@app.get("/movies", tags=["Search & Discovery"], summary="Browse Recommended Movies")
def get_movies(
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=50),
    sort: str = Query("RECOMMEND")
):
    cache_key = f"movies:{page}:{per_page}:{sort}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    url = f"{API_BASE}/subject/filter"
    payload = {"tabId": 2, "filter": {"sort": sort, "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}, "page": page, "perPage": per_page}
    try:
        resp = engine.session.post(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, json=payload, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": "Upstream connection failed", "url": url, "exception": str(e)})

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"Upstream returned HTTP {resp.status_code}",
                "url": url,
                "upstream_headers": dict(resp.headers),
                "upstream_body": resp.text[:600],
            },
        )
    data = resp.json().get("data", {})
    raw = data.get("items") or data.get("subjects") or []
    res = {
        "page": page,
        "per_page": per_page,
        "total": data.get("pager", {}).get("totalCount") or len(raw),
        "items": [
            {
                "title": s.get("title"),
                "subject_id": str(s.get("subjectId")),
                "detail_path": s.get("detailPath"),
                "cover": s.get("cover", {}).get("url") if isinstance(s.get("cover"), dict) else None,
                "rating": s.get("imdbRatingValue"),
                "year": s.get("releaseDate", "")[:4] if s.get("releaseDate") else None
            }
            for s in raw
        ]
    }
    set_cache(cache_key, res, ttl_sec=600)
    return res

@app.get("/tv-series", tags=["Search & Discovery"], summary="Browse Recommended TV Series")
def get_tv_series(
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=50),
    sort: str = Query("RECOMMEND")
):
    cache_key = f"tv:{page}:{per_page}:{sort}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    url = f"{API_BASE}/subject/filter"
    payload = {"tabId": 5, "filter": {"sort": sort, "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}, "page": page, "perPage": per_page}
    try:
        resp = engine.session.post(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, json=payload, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": "Upstream connection failed", "url": url, "exception": str(e)})

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"Upstream returned HTTP {resp.status_code}",
                "url": url,
                "upstream_headers": dict(resp.headers),
                "upstream_body": resp.text[:600],
            },
        )
    data = resp.json().get("data", {})
    raw = data.get("items") or data.get("subjects") or []
    res = {
        "page": page,
        "per_page": per_page,
        "total": data.get("pager", {}).get("totalCount") or len(raw),
        "items": [
            {
                "title": s.get("title"),
                "subject_id": str(s.get("subjectId")),
                "detail_path": s.get("detailPath"),
                "cover": s.get("cover", {}).get("url") if isinstance(s.get("cover"), dict) else None,
                "rating": s.get("imdbRatingValue"),
                "year": s.get("releaseDate", "")[:4] if s.get("releaseDate") else None
            }
            for s in raw
        ]
    }
    set_cache(cache_key, res, ttl_sec=600)
    return res

@app.get("/home", tags=["Search & Discovery"], summary="Get Homepage Banners & Trending Feeds")
def get_home():
    cached = get_cache("home")
    if cached:
        return cached
    url = f"{API_BASE}/home?host=moviebox.ph"
    try:
        resp = engine.session.get(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": "Upstream connection failed", "url": url, "exception": str(e)})

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"Upstream returned HTTP {resp.status_code}",
                "url": url,
                "upstream_headers": dict(resp.headers),
                "upstream_body": resp.text[:600],
            },
        )
    res = resp.json()
    set_cache("home", res, ttl_sec=600)
    return res

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port)
