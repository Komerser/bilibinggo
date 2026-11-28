
"""Read-only search and verification jobs. No account actions are performed here."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from pathlib import Path

import httpx

from src import lottery_discovery as d
from src.bilibili_client import BilibiliRequestError
from src.discovery_coverage import ordered_page_times, publication_time, search_page_exhausted
from src.llm_settings import get_llm_config
from src.lottery_discovery_broad import has_direct_video_entry, has_video_clue, search_author_lotteries
from src.sources.common import opus_link
from src.watch_feed import DEFAULT_MAX_PAGES, SPACE_FEED_URL, extract_feed_pub_ts, extract_forward_orig_id
from web.cluster_protocol import Activity, CollectionCoverage, PublisherCoverage, PublisherFinding, Reference, Report, Task

MAX_VIDEO_REFERENCES = 16
MAX_REFERENCE_DEPTH = 4
WATCH_LOOKBACK = 24 * 3600


class RetryCheck(Exception):
    def __init__(self, message, reason="platform"):
        super().__init__(message)
        self.reason = reason


class WorkClient(d.DiscoveryClient):
    def __init__(self, cache_path, get_analysis, put_analysis):
        super().__init__(cache_path, budget=None)
        self.get_analysis = get_analysis
        self.put_analysis = put_analysis
        self.publisher_findings = {}

    def extract(self, text, *, source="关联动态", followers=None, context=""):
        config = get_llm_config()
        fingerprint = json.dumps([d.PROMPT, source, followers, context[:240], text,
                                  config.base_url if config else "", config.model_name if config else ""],
                                 ensure_ascii=False)
        key = hashlib.sha256(fingerprint.encode()).hexdigest()
        cached = self.get_analysis(key)
        if cached is not None:
            return cached
        fields = super().extract(text, source=source, followers=followers, context=context)
        if not fields.get("model_assessment"):
            raise RetryCheck("模型未返回有效判定", reason="model")
        return self.put_analysis(key, fields)


def read_data(client, url, params, *, referer, wbi=False, ttl=600):
    payload = client.read(url, params, referer=referer, ttl=ttl, wbi=wbi)
    if d.api_code(payload) != 0 or not isinstance(payload.get("data"), dict):
        raise RetryCheck("平台未返回可读取结果", reason="source_response")
    return payload["data"]


def references(text):
    refs = []
    for kind, regex in (("dynamic", d.DYNAMIC_RE), ("article", d.ARTICLE_RE), ("short", d.SHORT_LINK_RE)):
        for key in dict.fromkeys(regex.findall(text)):
            if kind != "dynamic" or len(key) in (18, 19):
                refs.append(Reference(kind=kind, key=key))
    return refs[:120]


def _author_ref(uid):
    return [Reference(kind="author", key=uid)] if uid.isascii() and uid.isdigit() and not uid.startswith("0") and len(uid) <= 20 else []


def _search(client, task):
    data = read_data(client, d.SEARCH_URL, {"search_type": "video", "keyword": task.keyword, "page": task.page,
                                           "page_size": 36, "order": task.order},
                     referer="https://search.bilibili.com/", wbi=True, ttl=1800)
    videos = {}
    for row in data.get("result") or []:
        if isinstance(row, dict) and d.BV_RE.fullmatch(key := str(row.get("bvid") or "")):
            videos.setdefault(key, publication_time(row.get("pubdate")))
    refs = [Reference(kind="video", key=key) for key in list(videos)[:36]]
    pages = data.get("numPages")
    result = {"references": refs, "more_pages": bool(refs) and (type(pages) is not int or task.page < pages)}
    if task.order == "pubdate" and len(videos) <= 36:
        result["search_dates"] = {key: date for key, date in videos.items() if date is not None}
        result["search_exhausted"] = search_page_exhausted(data, task.page)
    return result


def _video(client, key):
    url = f"https://www.bilibili.com/video/{key}"
    data = read_data(client, d.VIDEO_URL, {"bvid": key}, referer=url)
    texts = [str(data.get("title") or ""), str(data.get("desc") or "")]
    uid = str((data.get("owner") or {}).get("mid") or "")
    if uid and data.get("aid") and not has_direct_video_entry("\n".join(texts)):
        replies = read_data(client, d.REPLIES_URL, {"type": 1, "oid": data["aid"], "mode": 3},
                            referer=url, wbi=True)
        texts.extend(d._owner_comments(replies, uid))
    text = "\n".join(texts)
    links = references(text)
    if not links and _author_ref(uid) and has_video_clue(text):
        if d._home_participation_hint(text) or client.extract(
            text[:8000], source="视频标题、简介及作者评论"
        ).get("model_assessment") in {"lottery", "entry", "collection"}:
            links = _author_ref(uid)
    return links


def _dynamic(client, key, now):
    linked = []
    unreadable = []

    def on_links(text):
        linked.extend(ref for ref in references(text) if ref.key != key)

    def on_skip(status, message):
        if status == "pending":
            unreadable.append("dynamic_detail" if "详情" in message else "dynamic_body")

    row = d.inspect_dynamic(client, key, now=now, on_links=on_links,
                            on_skip=on_skip, skip_video_cards=True)
    if not row and unreadable:
        raise RetryCheck("动态暂时无法核验", reason=unreadable[0])
    if not row:
        return linked, []
    notice = row.get("official_notice") or {}
    status = row["status"]
    # "joined" only describes the worker's account. Reassess the public evidence.
    if status == "joined":
        public_notice = {k: v for k, v in notice.items() if k != "participated"}
        status = d.assess_candidate(row["evidence"], now=now, notice=public_notice, require_deadline=True)["status"]
    draw = row.get("draw_time")
    uid, published = row.get("publisher_uid", ""), row.get("published_at")
    if (status in {"open", "closed"} and not row.get("is_forward")
            and (notice or (row.get("evidence") or {}).get("prize") and
                 (row.get("evidence") or {}).get("rules") and (draw or row.get("deadline"))) and published
            and _author_ref(uid) and hasattr(client, "publisher_findings")):
        client.publisher_findings[key] = PublisherFinding(
            activity=key, mid=uid, name=str(row.get("publisher") or uid)[:64], published=published,
        )
    if status not in {"open", "closed"} or not draw:
        return linked, []
    activity = Activity(dynamic_id=key, source_url=opus_link(key),
                        lottery_type="互动抽奖" if notice else "转发抽奖",
                        lottery_time=draw, deadline=row.get("deadline"),
                        prize=str(row.get("prize") or "")[:300], closed=status == "closed")
    return linked, [activity]


def _follow_reference(client, ref, now):
    if ref.kind == "dynamic":
        return _dynamic(client, ref.key, now)
    if ref.kind == "article":
        url = f"https://www.bilibili.com/read/cv{ref.key}"
        data = read_data(client, d.ARTICLE_URL, {"id": int(ref.key)}, referer=url)
        return references("\n".join(d.extract_opus_links_from_article(data))), []
    if ref.kind == "short":
        return references(client.resolve_short_link(f"https://b23.tv/{ref.key}") or ""), []
    rows, _metadata = search_author_lotteries(client, ref.key, limit=4)
    return references("\n".join(opus_link(str(row.get("id_str") or "")) for row in rows)), []


def _detect_video(client, key, activities):
    pending = deque((ref, 1) for ref in _video(client, key))
    seen = set()
    retry = None
    while pending and len(seen) < MAX_VIDEO_REFERENCES:
        ref, depth = pending.popleft()
        identity = ref.kind, ref.key
        if identity in seen:
            continue
        seen.add(identity)
        try:
            links, verified = _follow_reference(client, ref, int(time.time()))
        except (RetryCheck, httpx.HTTPError, BilibiliRequestError) as exc:
            if retry is None:
                retry = exc
            continue
        activities.extend(verified)
        if depth < MAX_REFERENCE_DEPTH:
            pending.extend((link, depth + 1) for link in links)
    if retry is not None:
        raise retry


def _check_watched_author(client, mid, activities, *, publish=None, covered_through=0, publisher=False):
    now = int(time.time())
    lower = covered_through or now - WATCH_LOOKBACK
    offset = ""
    seen_offsets, candidates = set(), set()
    newest = oldest = None
    dated = True
    ordered = True
    complete = False
    for _ in range(DEFAULT_MAX_PAGES):
        data = read_data(client, SPACE_FEED_URL,
                         {"host_mid": int(mid), "offset": offset, "type": "all", "platform": "web",
                          "timezone_offset": -480, "web_location": "333.1365"},
                         referer=f"https://space.bilibili.com/{mid}/dynamic", wbi=True, ttl=90)
        items = data.get("items")
        if not isinstance(items, list):
            if data.get("has_more") is False and items is None:
                items = []
            else:
                break
        timestamps = [extract_feed_pub_ts(item) if isinstance(item, dict) else None for item in items]
        if any(stamp is None or stamp > now for stamp in timestamps):
            dated = False
        elif timestamps:
            newest = max(newest or 0, *timestamps)
        page_times = ordered_page_times(timestamps, now, oldest)
        if items and page_times is None:
            ordered = False
        elif page_times is not None:
            _, oldest = page_times
        for item, published in zip(items, timestamps):
            if not isinstance(item, dict):
                continue
            if published and not lower <= published <= now:
                continue
            original = extract_forward_orig_id(item)
            if publisher and (original or item.get("type") == "DYNAMIC_TYPE_FORWARD"):
                continue
            if original and len(original) in (18, 19) and original.isascii() and original.isdecimal():
                candidates.add(original)
            # The official lottery marker can be present even when the body is short.
            text = d._extract_from_detail_item(d._normalize_dynamic_item(item))
            key = str(item.get("id_str") or "")
            if (len(key) in (18, 19) and key.isascii() and key.isdecimal()
                    and (d.LOTTERY_RE.search(text) or "RICH_TEXT_NODE_TYPE_LOTTERY" in json.dumps(item))):
                candidates.add(key)
        offset = str(data.get("offset") or "")
        if data.get("has_more") is False or (ordered and page_times is not None and page_times[0] < lower):
            complete = True
            break
        if not data.get("has_more") or not offset or offset in seen_offsets:
            break
        seen_offsets.add(offset)
    retry = None
    keys = sorted(candidates, key=int, reverse=True)
    if not publish and len(keys) > MAX_VIDEO_REFERENCES:
        complete = False
        keys = keys[:MAX_VIDEO_REFERENCES]
    for key in keys:
        try:
            _, matches = _dynamic(client, key, now)
        except (RetryCheck, httpx.HTTPError, BilibiliRequestError) as exc:
            retry = retry or exc
            continue
        if publisher:
            finding = client.publisher_findings.get(key)
            if not finding or finding.mid != mid:
                continue
        if publish and matches:
            publish(matches)
        else:
            activities.extend(matches)
    if retry is not None:
        raise retry
    if publisher and dated and complete:
        return PublisherCoverage(lower=lower, upper=now) if lower < now else None
    if dated and complete and newest is not None and newest >= lower:
        return newest


def _collection_references(mid, covered=None):
    from web.actions import DS_HANDLER_BY_ID
    from web.activity_service import collection_source_for_mid

    source = collection_source_for_mid(mid)
    if source is None:
        return [], None
    check, _ = DS_HANDLER_BY_ID[source]
    result = check(force=True, covered=covered) if covered else check(force=True)
    if result is None:
        return [], None
    stamp = publication_time(result.published_at)
    container_id = result.cv_id or result.container_id
    coverage = CollectionCoverage(container_id=container_id, published_at=stamp) if stamp and container_id else None
    return [ref for link in dict.fromkeys(result.activity_links) for ref in references(link)], coverage


def _check_collection(client, mid, publish, covered=None):
    refs, coverage = _collection_references(mid, covered)
    pending = deque((ref, 1) for ref in refs)
    seen = set()
    retry = None
    while pending:
        ref, depth = pending.popleft()
        if (ref.kind, ref.key) in seen:
            continue
        seen.add((ref.kind, ref.key))
        try:
            links, matches = _follow_reference(client, ref, int(time.time()))
            if matches:
                publish(matches)
        except (RetryCheck, httpx.HTTPError, BilibiliRequestError) as exc:
            retry = retry or exc
            continue
        if depth < MAX_REFERENCE_DEPTH:
            pending.extend((link, depth + 1) for link in links)
    if retry is not None:
        raise retry
    return coverage


def execute(task: Task, cache_path: Path, get_analysis, put_analysis, *, two_stage=True, publish=None,
            collection_mode=None, publish_references=None, watch_coverage=None, record_publishers=None) -> Report:
    result = {"task_id": task.task_id, "lease": task.lease, "outcome": "done"}
    client = WorkClient(cache_path, get_analysis, put_analysis)
    try:
        if task.kind == "search":
            result.update(_search(client, task))
        elif task.kind == "publisher":
            result["activities"] = activities = []
            result["publisher_coverage"] = _check_watched_author(
                client, task.key, activities, covered_through=(watch_coverage or {}).get("dynamics", 0), publisher=True,
            )
        elif task.kind == "watch":
            result["activities"] = activities = []
            watch_coverage = watch_coverage or {}
            covered_through = watch_coverage.get("dynamics", 0)
            covered_collection = watch_coverage.get("collection")
            if collection_mode:
                refs, result["watch_collection"] = _collection_references(task.key, covered_collection)
                for start in range(0, len(refs), 120):
                    publish_references(refs[start:start + 120])
            elif publish:
                try:
                    result["watch_collection"] = _check_collection(client, task.key, publish, covered_collection)
                except (RetryCheck, httpx.HTTPError, RuntimeError):
                    _check_watched_author(client, task.key, activities, publish=publish, covered_through=covered_through)
                    raise
            if collection_mode != "collection_only":
                result["watch_through"] = _check_watched_author(
                    client, task.key, activities, publish=publish, covered_through=covered_through,
                )
        elif task.kind == "video" and two_stage:
            activities = []
            result["activities"] = activities
            _detect_video(client, task.key, activities)
        elif task.kind == "video":  # Legacy v1 host verifies the returned links separately.
            result["references"] = _video(client, task.key)
        else:
            ref = Reference(kind=task.kind, key=task.key)
            result["references"], result["activities"] = _follow_reference(client, ref, int(time.time()))
    except d.DiscoveryStopped:
        result.update(outcome="cooldown", cooldown=min(86400, max(900, math.ceil(
            client.cache.get("paused_until", 0) - time.time()))))
    except RetryCheck as exc:
        result.update(outcome="retry", reason=exc.reason)
    except d.DiscoveryModelError:
        result.update(outcome="retry", reason="model")
    except (httpx.HTTPError, BilibiliRequestError):
        result.update(outcome="retry", reason="network")
    except (RuntimeError, ValueError):
        result.update(outcome="retry", reason="worker")
    finally:
        result["model_calls"] = client.llm_requests
        client.close()
        client.save()
    if record_publishers and client.publisher_findings:
        record_publishers(client.publisher_findings.values())
    if task.kind == "publisher":
        result["publisher_latest"] = max(
            (item for item in client.publisher_findings.values() if item.mid == task.key),
            key=lambda item: item.published, default=None,
        )
    return Report(**result)
