import asyncio
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
from fastapi.responses import HTMLResponse, JSONResponse, Response

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
### APEX API — Movies, Anime & Batch TV Series Stream + Sinhala Subtitle Engine


- **Movies & Anime Movies:** `GET /api/stream/{subject_id}`
- **TV & Anime Series Step 1:** `GET /epi/{subject_id}` (Get seasons & episode counts)
- **TV & Anime Series Step 2:** `GET /dl/{subject_id}?se=1` (Get all episodes in Season 1 with qualities, sizes & EN/SI subtitles)
"""

app = FastAPI(
    title="APEX API — MovieBox Stream & Subtitle Engine",
    description=API_DESCRIPTION,
    version="3.7.1",
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
# GLOBAL EXCEPTION HANDLER
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
# SECURITY: RATE LIMITER & 10-MIN TTL CACHE
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
    protected_prefixes = ("/api/", "/epi/", "/dl/", "/search", "/detail", "/movies", "/tv-series", "/home")
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
# CONFIGURATION & SIGNING ENGINE
# ==========================================
SECRET_KEY_DEFAULT_B64 = "NzZpUmwwN3MweFNOOWpxbUVXQXQ3OUVCSlp1bElRSXNWNjRGWnIyTw=="
SECRET_KEY_ALT_B64 = "WHFuMm5uTzQxL0w5Mm8xaXVYaFNMSFRiWHZZNFo1Wlo2Mm04bVNMQQ=="
MODERN_USER_AGENT = "com.community.mbox.in/50020130 (Linux; U; Android 14; en_US; Pixel 8; Build/UD1A.230803.041; Cronet/145.0.7582.0)"
WEB_USER_AGENT = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"
NATIVE_API_BASE = "https://api3.aoneroom.com"
BOOTSTRAP_HOST = "https://apig.inmoviebox.com"
BOOTSTRAP_URL = f"{BOOTSTRAP_HOST}/wefeed-mobile-bff/tab/ranking-list?tabId=0&categoryType=4516404531735022304&page=1&perPage=1"

WEB_DOMAINS = [
    "https://moviebox.ph",
    "https://h5-api.aoneroom.com",
    "https://123moviesfree.club",
    "https://movie-box.co",
    "https://movieboxonline.net",
    "https://sflix.film",
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
            "region": "US",
            "timezone": "America/New_York",
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

def format_human_size(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return "Adaptive"
    mb = size_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"

# ==========================================
# SINHALA SUBTITLE TRANSLATOR & WATERMARK
# ==========================================
async def _translate_chunk(client: httpx.AsyncClient, lines: List[str]) -> List[str]:
    joined = "\n|||\n".join(lines)
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": "en",
        "tl": "si",
        "dt": "t",
        "q": joined,
    }
    try:
        resp = await client.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            translated_full = "".join(seg[0] for seg in data[0] if seg and seg[0])
            parts = [p.strip() for p in translated_full.split("|||")]
            if len(parts) == len(lines):
                return parts
    except Exception:
        pass
    return lines

async def build_subtitles_from_english(en_srt_url: str) -> Tuple[str, str]:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(en_srt_url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Failed to download English SRT (HTTP {resp.status_code})")
        raw_srt = resp.text.lstrip("\ufeff")

    # Strip WEBVTT header if upstream subtitle is VTT format
    cleaned_raw = re.sub(r"^WEBVTT[^\r\n]*\r?\n+", "", raw_srt.strip(), flags=re.IGNORECASE)
    blocks = re.split(r"\r?\n\r?\n", cleaned_raw.strip())
    parsed_blocks = []
    texts_to_translate = []

    for blk in blocks:
        lines = [l.rstrip("\r") for l in blk.split("\n")]
        if len(lines) >= 3 and "-->" in lines[1]:
            idx_line = lines[0].strip()
            time_line = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", lines[1].strip())
            body_text = " ".join(lines[2:]).strip()
            clean_body = re.sub(r"<[^>]+>|\{\\[^}]+\}", "", body_text).strip()
            parsed_blocks.append((idx_line, time_line, clean_body))
            texts_to_translate.append(clean_body)
        elif len(lines) >= 2 and "-->" in lines[0]:
            time_line = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", lines[0].strip())
            body_text = " ".join(lines[1:]).strip()
            clean_body = re.sub(r"<[^>]+>|\{\\[^}]+\}", "", body_text).strip()
            parsed_blocks.append((str(len(parsed_blocks) + 1), time_line, clean_body))
            texts_to_translate.append(clean_body)

    chunk_size = 35
    chunks = [texts_to_translate[i:i + chunk_size] for i in range(0, len(texts_to_translate), chunk_size)]
    translated_texts: List[str] = []

    async with httpx.AsyncClient(timeout=20) as t_client:
        sem = asyncio.Semaphore(5)
        async def bounded_translate(c):
            async with sem:
                return await _translate_chunk(t_client, c)

        results = await asyncio.gather(*(bounded_translate(c) for c in chunks))
        for res_list in results:
            translated_texts.extend(res_list)

    watermark_block = (
        "1\n"
        "00:00:01,000 --> 00:00:03,000\n"
        '<font color="#FFFF00"><b>APEX API TRANSLATE - POWERED BY MALIYA-MD</b></font>'
    )
    out_blocks = [watermark_block]

    for i, (_, time_line, _) in enumerate(parsed_blocks):
        si_text = translated_texts[i] if i < len(translated_texts) else ""
        if not si_text:
            continue
        if i == 0 and time_line.startswith("00:00:00"):
            parts = time_line.split("-->")
            if len(parts) == 2:
                time_line = f"00:00:03,100 --> {parts[1].strip()}"
        out_blocks.append(f"{i + 2}\n{time_line}\n{si_text}")

    sinhala_srt = "\n\n".join(out_blocks) + "\n"
    return raw_srt, sinhala_srt

# ==========================================
# CORE MOVIEBOX ENGINE (MOVIES, SERIES & ANIME)
# ==========================================
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

    @staticmethod
    def _is_dummy_stream(url: str, size_bytes: int) -> bool:
        if not url:
            return True
        # MovieBox decoy/placeholder clips (~0.8 MB) are hosted under /other/
        if "/other/" in url:
            return True
        if 0 < size_bytes < 5 * 1024 * 1024:
            return True
        return False

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
        detail_path = (
            data.get("detailPath")
            or (detail_url_str.split("/")[-1].split("?")[0] if "/" in detail_url_str else detail_url_str)
            or ""
        )
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
        if not seasons:
            try:
                details = self.get_details(subject_id)
                for dub in details.get("dubs", []):
                    dub_id = dub.get("subject_id")
                    if dub_id and dub_id != subject_id:
                        dub_res = self.api_request("GET", f"/wefeed-mobile-bff/subject-api/season-info?subjectId={dub_id}")
                        seasons = dub_res.get("data", {}).get("seasons", [])
                        if seasons:
                            break
            except Exception:
                pass
        return [
            {
                "season": s.get("se", 1),
                "episode_count": s.get("maxEp", 1),
                "resolutions": s.get("resolutions", [])
            }
            for s in seasons
        ]

    def _fetch_streams_single_target(self, target_id: str, season: int, episode: int, detail_path: str, domain_logs: list) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        streams: List[Dict[str, Any]] = []
        seen_urls = set()
        primary_stream_id: Optional[str] = None

        # 1. Try Web H5 Download & Play Endpoints (Best for Anime MP4s)
        for domain in WEB_DOMAINS[:5]:
            if season > 0 or episode > 0:
                referer = f"{domain}/series/{detail_path}?id={target_id}&type=/series/detail&detailSe={season}&detailEp={episode}&lang=en"
            else:
                referer = f"{domain}/movies/{detail_path}?id={target_id}&type=/movie/detail&lang=en"

            h5_headers = {
                "User-Agent": WEB_USER_AGENT,
                "Referer": referer,
                "Origin": domain,
                "Accept": "application/json",
                "x-client-info": '{"timezone":"America/New_York"}',
                "x-request-lang": "en",
                "x-source": "moviebox",
            }
            candidate_urls = [
                f"{domain}/wefeed-h5api-bff/subject/download?subjectId={target_id}&se={season}&ep={episode}",
                f"{domain}/wefeed-h5api-bff/subject/play?subjectId={target_id}&se={season}&ep={episode}&detailPath={detail_path}",
            ]
            for play_url in candidate_urls:
                try:
                    resp = self.session.get(play_url, headers=h5_headers, timeout=12)
                    if resp.status_code != 200:
                        domain_logs.append({"domain": domain, "target_id": target_id, "status": resp.status_code})
                        continue
                    data = resp.json().get("data", {})
                    raw_streams = data.get("streams") or data.get("downloads") or []
                    dash_items = data.get("dash", [])

                    for st in raw_streams:
                        url = st.get("url")
                        size_bytes = int(st.get("size") or 0)
                        if self._is_dummy_stream(url, size_bytes) or url in seen_urls:
                            continue
                        if not primary_stream_id and st.get("id"):
                            primary_stream_id = str(st["id"])
                        seen_urls.add(url)
                        res_str = str(st.get("resolutions") or st.get("resolution") or "unknown")
                        size_mb = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0
                        quality_val = next((q for q in (2160, 1440, 1080, 720, 480, 360, 240) if str(q) in res_str), 0)
                        streams.append({
                            "quality": f"{quality_val}p" if quality_val else res_str,
                            "quality_value": quality_val,
                            "format": st.get("format", "MP4"),
                            "size": format_human_size(size_bytes),
                            "size_mb": size_mb,
                            "codec": st.get("codecName"),
                            "duration_sec": st.get("duration"),
                            "dl_link": url,
                            "headers": {
                                "User-Agent": WEB_USER_AGENT,
                                "Referer": f"{domain}/",
                                "Origin": domain,
                            }
                        })

                    for d in dash_items:
                        url = d.get("url")
                        cookie = d.get("signCookie")
                        final_url = (extract_policy_resource(cookie) if cookie else None) or url
                        if final_url and not self._is_dummy_stream(final_url, 0) and final_url not in seen_urls:
                            if not primary_stream_id and d.get("id"):
                                primary_stream_id = str(d["id"])
                            seen_urls.add(final_url)
                            res_tag = str(d.get("resolutions", "adaptive"))
                            streams.append({
                                "quality": "1080p (Adaptive)" if "1080" in res_tag else "Adaptive",
                                "quality_value": 1080 if "1080" in res_tag else 999,
                                "format": "DASH",
                                "size": "Adaptive",
                                "size_mb": 0,
                                "codec": "hevc/h264",
                                "duration_sec": None,
                                "dl_link": final_url,
                                "cookie": cookie,
                                "headers": {
                                    "User-Agent": WEB_USER_AGENT,
                                    "Referer": f"{domain}/",
                                    "Origin": domain,
                                }
                            })
                    if streams:
                        return streams, primary_stream_id
                except Exception as e:
                    domain_logs.append({"domain": domain, "target_id": target_id, "exception": str(e)})
                    continue

        # 2. Fallback to Native Android Mobile API (Filtering <5MB dummy streams)
        try:
            native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={target_id}&se={season}&ep={episode}"
            native_data = self.api_request("GET", native_url).get("data", {})
            for st in native_data.get("streams", []):
                url = st.get("url")
                size_bytes = int(st.get("size") or 0)
                if self._is_dummy_stream(url, size_bytes) or url in seen_urls:
                    domain_logs.append({"source": "native_dummy_filtered", "target_id": target_id, "size": size_bytes})
                    continue
                if not primary_stream_id and st.get("id"):
                    primary_stream_id = str(st["id"])
                seen_urls.add(url)
                res_str = str(st.get("resolutions", "unknown"))
                size_mb = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0
                quality_val = next((q for q in (2160, 1440, 1080, 720, 480, 360, 240) if str(q) in res_str), 0)
                streams.append({
                    "quality": f"{quality_val}p" if quality_val else res_str,
                    "quality_value": quality_val,
                    "format": st.get("format", "MP4"),
                    "size": format_human_size(size_bytes),
                    "size_mb": size_mb,
                    "codec": st.get("codecName"),
                    "duration_sec": st.get("duration"),
                    "dl_link": url,
                    "headers": {
                        "User-Agent": MODERN_USER_AGENT,
                        "Referer": "https://moviebox.ph/",
                    }
                })

            for h in native_data.get("hls", []) + native_data.get("dash", []):
                url = h.get("url")
                cookie = h.get("signCookie")
                final_url = (extract_policy_resource(cookie) if cookie else None) or url
                if final_url and not self._is_dummy_stream(final_url, 0) and final_url not in seen_urls:
                    if not primary_stream_id and h.get("id"):
                        primary_stream_id = str(h["id"])
                    seen_urls.add(final_url)
                    streams.append({
                        "quality": "1080p (Adaptive)",
                        "quality_value": 1080,
                        "format": "HLS/DASH",
                        "size": "Adaptive",
                        "size_mb": 0,
                        "codec": "hevc/h264",
                        "duration_sec": None,
                        "dl_link": final_url,
                        "cookie": cookie,
                        "headers": {
                            "User-Agent": MODERN_USER_AGENT,
                            "Referer": "https://moviebox.ph/",
                        }
                    })
        except Exception as e:
            domain_logs.append({"source": "native_play_info", "target_id": target_id, "exception": str(e)})

        return streams, primary_stream_id

    def get_streams(self, subject_id: str, season: int = 0, episode: int = 0, detail_path: Optional[str] = None, raise_if_empty: bool = True) -> Dict[str, Any]:
        domain_logs = []
        title = None
        dubs = []
        try:
            details = self.get_details(subject_id)
            if not detail_path:
                detail_path = details.get("detail_path", "")
            title = details.get("title")
            dubs = details.get("dubs", [])
        except Exception:
            detail_path = detail_path or ""

        resolved_subject_id = subject_id
        resolved_se = season
        resolved_ep = episode

        # Attempt 1: Direct subject_id with requested se/ep
        streams, stream_id = self._fetch_streams_single_target(subject_id, season, episode, detail_path, domain_logs)

        # Attempt 2: If Anime movie/OVA is indexed as S01E01 instead of S00E00
        if not streams and season == 0 and episode == 0:
            streams, stream_id = self._fetch_streams_single_target(subject_id, 1, 1, detail_path, domain_logs)
            if streams:
                resolved_se, resolved_ep = 1, 1

        # Attempt 3: Check Anime Dubs (Japanese Original / English Dub subject_ids)
        if not streams and dubs:
            sorted_dubs = sorted(
                dubs,
                key=lambda d: 0 if "japan" in (d.get("lang_name") or "").lower() or "ja" in (d.get("language") or "").lower()
                else (1 if "english" in (d.get("lang_name") or "").lower() or "en" in (d.get("language") or "").lower() else 2)
            )
            for dub in sorted_dubs:
                dub_id = dub.get("subject_id")
                if not dub_id or dub_id == subject_id:
                    continue
                streams, stream_id = self._fetch_streams_single_target(dub_id, season, episode, detail_path, domain_logs)
                if not streams and season == 0 and episode == 0:
                    streams, stream_id = self._fetch_streams_single_target(dub_id, 1, 1, detail_path, domain_logs)
                    if streams:
                        resolved_se, resolved_ep = 1, 1
                if streams:
                    resolved_subject_id = dub_id
                    break

        if not streams and raise_if_empty:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "No streams resolved from Web or Native endpoints",
                    "subject_id": subject_id,
                    "season": season,
                    "episode": episode,
                    "domain_attempts": domain_logs,
                },
            )

        streams.sort(key=lambda s: s["quality_value"], reverse=True)
        return {
            "title": title,
            "streams": streams,
            "resolved_subject_id": resolved_subject_id,
            "stream_id": stream_id,
            "resolved_se": resolved_se,
            "resolved_ep": resolved_ep,
        }

    def get_english_subtitle_url(self, subject_id: str, season: int = 0, episode: int = 0, stream_id: Optional[str] = None) -> Optional[str]:
        subs: List[Dict[str, Any]] = []
        seen_urls = set()

        def _add_sub(item: Dict[str, Any]):
            u = item.get("url")
            if u and u not in seen_urls:
                seen_urls.add(u)
                subs.append({
                    "language": (item.get("lanName") or item.get("language") or "").lower(),
                    "code": (item.get("lan") or "").lower(),
                    "url": u,
                })

        try:
            native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
            p_data = self.api_request("GET", native_url).get("data", {})
            raw_streams = p_data.get("streams", [])
            if not stream_id and raw_streams and raw_streams[0].get("id"):
                stream_id = str(raw_streams[0]["id"])
            for s in p_data.get("subtitles", []) or p_data.get("captions", []):
                if isinstance(s, dict):
                    _add_sub(s)
        except Exception:
            pass

        # 1. Native Stream Captions
        if stream_id:
            try:
                url = f"/wefeed-mobile-bff/subject-api/get-stream-captions?subjectId={subject_id}&streamId={stream_id}"
                data = self.api_request("GET", url).get("data", {})
                captions = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
                for s in captions:
                    if isinstance(s, dict):
                        _add_sub(s)
            except Exception:
                pass

        # 2. Native External Captions (with season & episode)
        try:
            res_id = stream_id or "0"
            url = f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={res_id}&season={season}&episode={episode}"
            data = self.api_request("GET", url).get("data", {})
            ext = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
            for s in ext:
                if isinstance(s, dict):
                    _add_sub(s)
        except Exception:
            pass

        # 3. Web H5 Caption API (Used by Anime streams)
        if not subs and stream_id:
            try:
                token = self.bootstrap_token()
                h5_cap_url = f"{API_BASE}/subject/caption?format=MP4&id={stream_id}&subjectId={subject_id}"
                resp = self.session.get(
                    h5_cap_url,
                    headers={
                        "User-Agent": WEB_USER_AGENT,
                        "Referer": "https://moviebox.ph/",
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=12,
                )
                if resp.status_code == 200:
                    cap_data = resp.json().get("data", {})
                    captions = cap_data if isinstance(cap_data, list) else cap_data.get("captions") or cap_data.get("extCaptions") or []
                    for s in captions:
                        if isinstance(s, dict):
                            _add_sub(s)
            except Exception:
                pass

        for s in subs:
            if s["code"].startswith("en") or "english" in s["language"]:
                return s["url"]
        return subs[0]["url"] if subs else None

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
    <title>APEX API — MovieBox Movies, Anime & TV Series Engine</title>
    <style>
        :root {
            --bg: #09090b; --surface: #111114; --surface-2: #18181c; --border: #27272a;
            --text: #fafafa; --dim: #a1a1aa; --blue: #3b82f6;
            --green: #10b981; --green-soft: rgba(16, 185, 129, 0.14);
            --yellow: #facc15; --yellow-soft: rgba(250, 204, 21, 0.14);
            --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; line-height: 1.6; padding: 28px 18px 60px; }
        .container { max-width: 1020px; margin: 0 auto; display: flex; flex-direction: column; gap: 26px; }
        header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 18px; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 12px; }
        .title-row { display: flex; align-items: center; gap: 10px; }
        .title-row h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.4px; }
        .badge { font-family: var(--mono); font-size: 11px; padding: 3px 8px; border-radius: 5px; background: var(--surface-2); border: 1px solid var(--border); color: var(--dim); }
        .badge.live { background: var(--green-soft); border-color: rgba(16,185,129,0.3); color: var(--green); }
        .badge.si { background: var(--yellow-soft); border-color: rgba(250,204,21,0.3); color: var(--yellow); }
        .nav-btns { display: flex; gap: 8px; }
        .btn { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; transition: 0.15s; }
        .btn:hover { background: #222228; border-color: #3f3f46; }
        .btn.primary { background: var(--text); color: var(--bg); border: none; }
        h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.7px; color: var(--dim); margin-bottom: 10px; }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
        code { font-family: var(--mono); background: #000; padding: 2px 5px; border-radius: 4px; font-size: 12px; color: #e4e4e7; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
        tr:last-child td { border-bottom: none; }
        th { font-size: 11px; text-transform: uppercase; color: var(--dim); }
        .method { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--green); background: var(--green-soft); padding: 2px 6px; border-radius: 4px; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div class="title-row">
            <h1>APEX API</h1>
            <span class="badge">v3.7.1</span>
            <span class="badge live">● Movies + Anime + Batch TV Series</span>
            <span class="badge si">● EN + Sinhala SRT (10m Cache)</span>
        </div>
        <div class="nav-btns">
            <a href="/health" target="_blank" class="btn">Health Status</a>
            <a href="/docs" target="_blank" class="btn primary">Swagger UI ↗</a>
        </div>
    </header>

    <div>
        <h2>Endpoints Overview</h2>
        <div class="card" style="padding: 0; overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th style="width: 75px;">Method</th>
                        <th>Endpoint</th>
                        <th>Description</th>
                        <th style="width: 95px; text-align: right;">Sample</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/search?q=Breaking+Bad</code></td>
                        <td style="color: var(--dim);">Search Movies, Anime & TV Series to get <code>subject_id</code></td>
                        <td style="text-align: right;"><a href="/search?q=Breaking+Bad" target="_blank" class="btn">Open</a></td>
                    </tr>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/api/stream/8906247916759695608</code></td>
                        <td style="color: var(--dim);"><strong>[Movie / Anime]</strong> DL links + qualities + sizes + EN & Sinhala SRT links</td>
                        <td style="text-align: right;"><a href="/api/stream/8906247916759695608" target="_blank" class="btn">Open</a></td>
                    </tr>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/epi/6207982430134357800</code></td>
                        <td style="color: var(--dim);"><strong>[Series / Anime Step 1]</strong> Get Seasons & Episode counts</td>
                        <td style="text-align: right;"><a href="/epi/6207982430134357800" target="_blank" class="btn">Open</a></td>
                    </tr>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/dl/6207982430134357800?se=1</code></td>
                        <td style="color: var(--dim);"><strong>[Series / Anime Step 2]</strong> Batch all episodes in Season 1 (DL links + EN/SI SRT)</td>
                        <td style="text-align: right;"><a href="/dl/6207982430134357800?se=1" target="_blank" class="btn">Open</a></td>
                    </tr>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/detail/8906247916759695608</code></td>
                        <td style="color: var(--dim);">Full metadata, synopsis, rating & dubs</td>
                        <td style="text-align: right;"><a href="/detail/8906247916759695608" target="_blank" class="btn">Open</a></td>
                    </tr>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/movies?page=1</code></td>
                        <td style="color: var(--dim);">Browse recommended movies</td>
                        <td style="text-align: right;"><a href="/movies?page=1" target="_blank" class="btn">Open</a></td>
                    </tr>
                    <tr>
                        <td><span class="method">GET</span></td>
                        <td><code>/tv-series?page=1</code></td>
                        <td style="color: var(--dim);">Browse recommended TV series</td>
                        <td style="text-align: right;"><a href="/tv-series?page=1" target="_blank" class="btn">Open</a></td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html_content)

# ==========================================
# PUBLIC REST ENDPOINTS
# ==========================================
@app.get("/health", tags=["System"], summary="Check APEX API Health")
def health_check():
    return {
        "status": "ok",
        "api": "APEX API",
        "version": "3.7.1",
        "tls_client": "curl_cffi (chrome120)" if cr is not None else "httpx (fallback)",
        "cached_entries": len(RESPONSE_CACHE),
        "rate_limit_per_min": MAX_REQ_PER_MIN
    }

@app.get("/search", tags=["Search & Discovery"], summary="Search Movies, Anime & TV Series")
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

@app.get("/detail/{subject_id}", tags=["Metadata"], summary="Get Full Metadata & Audio Dubs")
def get_detail(subject_id: str = Path(...)):
    cache_key = f"detail:{subject_id}"
    cached = get_cache(cache_key)
    if cached:
        return cached
    res = engine.get_details(subject_id)
    set_cache(cache_key, res, ttl_sec=900)
    return res

# ==========================================
# TV & ANIME SERIES STEP 1: /epi/{subject_id}
# ==========================================
@app.get("/epi/{subject_id}", tags=["TV Series"], summary="Get Seasons & Episode Counts Only")
def get_series_seasons(request: Request, subject_id: str = Path(...)):
    cache_key = f"epi:{subject_id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    details = engine.get_details(subject_id)
    seasons_raw = engine.get_episodes(subject_id)
    base_url = str(request.base_url).rstrip("/")

    seasons_list = [
        {
            "season": s["season"],
            "episodes_count": s["episode_count"],
            "dl_endpoint": f"{base_url}/dl/{subject_id}?se={s['season']}"
        }
        for s in seasons_raw
    ]

    res = {
        "subject_id": subject_id,
        "title": details.get("title"),
        "total_seasons": len(seasons_list),
        "seasons": seasons_list
    }
    set_cache(cache_key, res, ttl_sec=900)
    return res

# ==========================================
# TV & ANIME SERIES STEP 2: /dl/{subject_id}?se=1
# ==========================================
@app.get("/dl/{subject_id}", tags=["TV Series"], summary="Batch Get All Episodes DL Links & Subtitles for a Season")
async def get_season_batch_downloads(
    request: Request,
    subject_id: str = Path(...),
    se: int = Query(1, ge=1, description="Season number (e.g. 1)")
):
    cache_key = f"dl_season:{subject_id}:se{se}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    details = engine.get_details(subject_id)
    detail_path = details.get("detail_path", "")
    series_title = details.get("title")

    seasons_raw = engine.get_episodes(subject_id)
    target_season = next((s for s in seasons_raw if s["season"] == se), None)
    if not target_season:
        raise HTTPException(status_code=404, detail={"error": f"Season {se} not found", "available_seasons": [s["season"] for s in seasons_raw]})

    total_eps = target_season["episode_count"]
    base_url = str(request.base_url).rstrip("/")
    sem = asyncio.Semaphore(4)

    async def fetch_single_episode(ep_num: int) -> Dict[str, Any]:
        async with sem:
            stream_res = await asyncio.to_thread(
                engine.get_streams,
                subject_id,
                se,
                ep_num,
                detail_path,
                False
            )
            streams = stream_res.get("streams", [])
            resolved_sid = stream_res.get("resolved_subject_id", subject_id)
            stream_id = stream_res.get("stream_id")

            ep_entry: Dict[str, Any] = {
                "episode": f"{ep_num:02d}",
            }

            mp4_streams = [s for s in streams if s["format"] == "MP4"]
            mp4_streams.sort(key=lambda x: x["quality_value"])

            for s in mp4_streams:
                ep_entry[f"{s['quality']} download"] = {
                    "size": s["size"],
                    "link": s["dl_link"],
                    "headers": s["headers"]
                }

            if not mp4_streams:
                for s in streams:
                    ep_entry[f"{s['quality']} download"] = {
                        "size": s["size"],
                        "link": s["dl_link"],
                        "headers": s["headers"]
                    }

            en_url = await asyncio.to_thread(engine.get_english_subtitle_url, resolved_sid, se, ep_num, stream_id)
            if en_url:
                sid_param = f"&stream_id={stream_id}" if stream_id else ""
                ep_entry["sinhala subtitle"] = f"{base_url}/sub/{resolved_sid}/si.srt?se={se}&ep={ep_num}{sid_param}"
                ep_entry["english subtitle"] = f"{base_url}/sub/{resolved_sid}/en.srt?se={se}&ep={ep_num}{sid_param}"
            else:
                ep_entry["sinhala subtitle"] = None
                ep_entry["english subtitle"] = None

            return ep_entry

    episodes_data = await asyncio.gather(*(fetch_single_episode(ep) for ep in range(1, total_eps + 1)))

    res = {
        "series": series_title,
        "season": se,
        "total_episodes": total_eps,
        "episodes": episodes_data
    }
    set_cache(cache_key, res, ttl_sec=600)
    return res

# ==========================================
# MOVIES & ANIME (OR SINGLE EPISODE): /api/stream/{subject_id}
# ==========================================
@app.get("/api/stream/{subject_id}", tags=["Movies & Single Stream"], summary="Get Streams, Sizes, DL Links & Direct EN/SI Subtitles")
async def get_streams(
    request: Request,
    subject_id: str = Path(...),
    se: int = Query(0, ge=0),
    ep: int = Query(0, ge=0),
    detail_path: Optional[str] = Query(None)
):
    cache_key = f"stream_full:{subject_id}:{se}:{ep}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    result = engine.get_streams(subject_id, season=se, episode=ep, detail_path=detail_path)
    streams = result["streams"]
    resolved_sid = result.get("resolved_subject_id", subject_id)
    stream_id = result.get("stream_id")
    actual_se = result.get("resolved_se", se)
    actual_ep = result.get("resolved_ep", ep)

    downloads = [
        {
            "quality": s["quality"],
            "format": s["format"],
            "size": s["size"],
            "dl_link": s["dl_link"],
            "headers": s["headers"],
        }
        for s in streams
    ]

    subtitles_obj = None
    en_url = engine.get_english_subtitle_url(resolved_sid, season=actual_se, episode=actual_ep, stream_id=stream_id)
    if en_url:
        try:
            en_srt, si_srt = await build_subtitles_from_english(en_url)
            set_cache(f"srt_file:en:{resolved_sid}:{actual_se}:{actual_ep}", en_srt, ttl_sec=600)
            set_cache(f"srt_file:si:{resolved_sid}:{actual_se}:{actual_ep}", si_srt, ttl_sec=600)

            base_url = str(request.base_url).rstrip("/")
            sid_param = f"&stream_id={stream_id}" if stream_id else ""
            subtitles_obj = {
                "english": f"{base_url}/sub/{resolved_sid}/en.srt?se={actual_se}&ep={actual_ep}{sid_param}",
                "sinhala": f"{base_url}/sub/{resolved_sid}/si.srt?se={actual_se}&ep={actual_ep}{sid_param}",
            }
        except Exception:
            subtitles_obj = None

    res = {
        "subject_id": subject_id,
        "resolved_subject_id": resolved_sid,
        "title": result.get("title"),
        "season": actual_se,
        "episode": actual_ep,
        "count": len(streams),
        "streams": streams,
        "downloads": downloads,
        "subtitles": subtitles_obj
    }
    set_cache(cache_key, res, ttl_sec=600)
    return res

# ==========================================
# CACHED SUBTITLE FILE ENDPOINT (10-MIN TTL)
# ==========================================
@app.get("/sub/{subject_id}/{lang}.srt", include_in_schema=False)
async def download_cached_subtitle(
    subject_id: str,
    lang: str,
    se: int = Query(0, ge=0),
    ep: int = Query(0, ge=0),
    stream_id: Optional[str] = Query(None)
):
    if lang not in ("en", "si"):
        raise HTTPException(status_code=404, detail="Invalid subtitle language. Use en.srt or si.srt")

    srt_key = f"srt_file:{lang}:{subject_id}:{se}:{ep}"
    cached_srt = get_cache(srt_key)

    if not cached_srt:
        en_url = engine.get_english_subtitle_url(subject_id, season=se, episode=ep, stream_id=stream_id)
        if not en_url:
            raise HTTPException(status_code=404, detail="No subtitle available for this title")
        en_srt, si_srt = await build_subtitles_from_english(en_url)
        set_cache(f"srt_file:en:{subject_id}:{se}:{ep}", en_srt, ttl_sec=600)
        set_cache(f"srt_file:si:{subject_id}:{se}:{ep}", si_srt, ttl_sec=600)
        cached_srt = en_srt if lang == "en" else si_srt

    filename = f"{subject_id}_S{se:02d}E{ep:02d}_{lang}.srt" if (se or ep) else f"{subject_id}_{lang}.srt"
    return Response(
        content=cached_srt.encode("utf-8"),
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

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
