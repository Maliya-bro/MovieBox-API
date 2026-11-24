def _parse_stream_items(self, raw_streams: List[Dict[str, Any]], referer_domain: str, seen_urls: set) -> List[Dict[str, Any]]:
        parsed = []
        for st in raw_streams:
            cookie = st.get("signCookie")
            # Avoid 21s anti-piracy dummy mp4 if signCookie has real DASH/HLS base URL
            extracted_mpd = extract_policy_resource(cookie) if cookie else None
            url = extracted_mpd or st.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            res_str = str(st.get("resolutions") or st.get("resolution") or "unknown")
            size_bytes = int(st.get("size") or 0)
            size_mb = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0
            quality_val = 0
            for q in (2160, 1440, 1080, 720, 480, 360, 240):
                if str(q) in res_str:
                    quality_val = q
                    break

            q_label = f"{quality_val}p" if quality_val else res_str
            fmt = "DASH" if extracted_mpd else st.get("format", "MP4")
            entry = {
                "quality": q_label,
                "quality_value": quality_val,
                "format": fmt,
                "size": format_human_size(size_bytes),
                "size_mb": size_mb,
                "codec": st.get("codecName"),
                "duration_sec": st.get("duration"),
                "dl_link": url,
                "headers": {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": f"{referer_domain}/",
                    "Origin": referer_domain,
                },
            }
            if cookie:
                entry["cookie"] = cookie
            parsed.append(entry)
        return parsed

    def get_streams(self, subject_id: str, season: int = 0, episode: int = 0, detail_path: Optional[str] = None, raise_if_empty: bool = True) -> Dict[str, Any]:
        domain_logs = []
        title = None
        dubs: List[Dict[str, Any]] = []
        if detail_path is None:
            try:
                details = self.get_details(subject_id)
                detail_path = details.get("detail_path", "")
                title = details.get("title")
                dubs = details.get("dubs", [])
            except Exception:
                detail_path = ""

        token = self.bootstrap_token()
        streams: List[Dict[str, Any]] = []
        seen_urls = set()

        # 1) Try Web BFF domains
        for domain in WEB_DOMAINS:
            play_url = (
                f"{domain}/wefeed-h5api-bff/subject/play?"
                f"subjectId={subject_id}&se={season}&ep={episode}&detailPath={detail_path}&"
                f"streamSignType=1&supportCodecs%5Bhevc%5D=1&supportCodecs%5Bh264%5D=1"
            )
            referer = f"{domain}/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={season or ''}&detailEp={episode or ''}&lang=en"
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
                    domain_logs.append({"domain": domain, "status": resp.status_code})
                    continue
                json_body = resp.json()
                data = json_body.get("data", {})
                raw_streams = data.get("streams", [])
                dash_items = data.get("dash", [])

                if not raw_streams and not dash_items:
                    domain_logs.append({"domain": domain, "status": 200, "reason": "Empty streams array"})
                    continue

                streams.extend(self._parse_stream_items(raw_streams, domain, seen_urls))
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
                            "size": "Adaptive",
                            "size_mb": 0,
                            "codec": "hevc/h264",
                            "duration_sec": None,
                            "dl_link": final_url,
                            "cookie": cookie,
                            "headers": {"User-Agent": "Mozilla/5.0", "Referer": f"{domain}/", "Origin": domain},
                        })
                if streams:
                    break
            except Exception as e:
                domain_logs.append({"domain": domain, "exception": str(e)})
                continue

        # 2) Fallback: Native API (play-info & resource/v2) — fixes Anime & restricted titles
        if not streams:
            native_endpoints = [
                f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}",
                f"/wefeed-mobile-bff/subject-api/resource/v2?subjectId={subject_id}&se={season}&epFrom={episode}&epTo={episode}&page=1&perPage=20&isVip=true&pagerMode=2",
            ]
            for ep_path in native_endpoints:
                try:
                    n_data = self.api_request("GET", ep_path).get("data", {})
                    n_streams = n_data.get("streams") or n_data.get("streamList") or n_data.get("list") or []
                    if n_streams:
                        streams.extend(self._parse_stream_items(n_streams, NATIVE_API_BASE, seen_urls))
                except Exception as e:
                    domain_logs.append({"native_endpoint": ep_path, "exception": str(e)})
                if streams:
                    break

        # 3) Fallback: Check dubs subject_ids (common for Anime subbed/dubbed entries)
        if not streams and dubs:
            for d in dubs:
                dub_id = d.get("subject_id")
                if dub_id and dub_id != str(subject_id):
                    try:
                        dub_res = self.get_streams(dub_id, season=season, episode=episode, detail_path=detail_path, raise_if_empty=False)
                        if dub_res.get("streams"):
                            streams.extend(dub_res["streams"])
                            break
                    except Exception:
                        continue

        if not streams and raise_if_empty:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "No streams resolved from Web CDN or Native API",
                    "subject_id": subject_id,
                    "season": season,
                    "episode": episode,
                    "domain_attempts": domain_logs,
                },
            )

        streams.sort(key=lambda s: s["quality_value"], reverse=True)
        return {"title": title, "streams": streams}

    def get_english_subtitle_url(self, subject_id: str, season: int = 0, episode: int = 0, _check_dubs: bool = True) -> Optional[str]:
        subs: List[Dict[str, Any]] = []
        seen_urls = set()
        stream_id = None

        def _add_sub(item: Dict[str, Any]):
            u = item.get("url")
            if u and u not in seen_urls:
                seen_urls.add(u)
                subs.append({
                    "language": (item.get("lanName") or item.get("language") or "").lower(),
                    "code": (item.get("lan") or item.get("languageCode") or "").lower(),
                    "url": u,
                })

        # 1) Check Native play-info (streams + bundled captions/subTitleList)
        try:
            native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
            p_data = self.api_request("GET", native_url).get("data", {})
            raw_streams = p_data.get("streams") or p_data.get("streamList") or []
            if raw_streams and raw_streams[0].get("id"):
                stream_id = str(raw_streams[0]["id"])
            for cap in (p_data.get("captions") or p_data.get("subTitleList") or []):
                if isinstance(cap, dict):
                    _add_sub(cap)
        except Exception:
            pass

        # 2) Check Web BFF captions directly (h5-api.aoneroom.com)
        if not subs:
            try:
                token = self.bootstrap_token()
                web_play = f"{API_BASE}/subject/play?subjectId={subject_id}&se={season}&ep={episode}"
                w_resp = self.session.get(
                    web_play,
                    headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/", "Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if w_resp.status_code == 200:
                    w_data = w_resp.json().get("data", {})
                    for cap in (w_data.get("captions") or []):
                        if isinstance(cap, dict):
                            _add_sub(cap)
            except Exception:
                pass

        # 3) Check get-stream-captions & get-ext-captions
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

        try:
            res_id = stream_id or "0"
            url = f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={res_id}&episode={episode}"
            data = self.api_request("GET", url).get("data", {})
            ext = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
            for s in ext:
                if isinstance(s, dict):
                    _add_sub(s)
        except Exception:
            pass

        for s in subs:
            if s["code"].startswith("en") or "english" in s["language"]:
                return s["url"]
        if subs:
            return subs[0]["url"]

        # 4) Fallback: Check dubs subject_ids for Anime subtitles
        if _check_dubs:
            try:
                for d in self.get_details(subject_id).get("dubs", []):
                    dub_id = d.get("subject_id")
                    if dub_id and dub_id != str(subject_id):
                        found = self.get_english_subtitle_url(dub_id, season=season, episode=episode, _check_dubs=False)
                        if found:
                            return found
            except Exception:
                pass

        return None
