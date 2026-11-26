
from __future__ import annotations

import secrets
import time
import uuid
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.sources.common import opus_link
from web.cluster_protocol import Activity, Analysis, Exchange, Reference, Register, Report
from web.cluster_service import ClusterService
from web.cluster_store import ClusterError, ClusterStore

KEY = "123456789012345678"
BVID = "BV1234567890"


def _activity(**fields):
    return Activity(dynamic_id=KEY, source_url=opus_link(KEY), lottery_type="互动抽奖",
                    lottery_time=int(time.time()) + 86400, **fields)


def _cluster(path):
    store = ClusterStore(path)
    host = store.start_host("主节点", "http://127.0.0.1:8182")
    member = Register(name="工作节点", node=uuid.uuid4().hex, token=secrets.token_hex(32))
    store.register(member, store.read("invite"))
    return store, host, member.model_dump()


def _exchange(store, node, **values):
    return store.exchange(Exchange(node=node["node"], **values), node["token"])


def _report(task, **values):
    return Report(task_id=task.task_id, lease=task.lease, outcome="done", **values)


def test_time_search_remembers_completed_coverage_and_stops_at_it_after_restart(tmp_path):
    from web.cluster_protocol import KEYWORDS

    store, host, _ = _cluster(tmp_path)
    now = int(time.time())
    first = _exchange(store, host, work=True).task
    original = Reference(kind="video", key=BVID)
    video = _exchange(store, host, work=True, video_detection=True, report=_report(
        first, references=[original], search_dates={original.key: now - 60}, search_exhausted=True,
    )).task
    assert video.kind == "video"
    key = f"search_coverage:{first.keyword}"
    assert store.read(key) is None
    next_search = _exchange(store, host, work=True, report=_report(video)).task
    assert store.read(key) == now - 60

    store = ClusterStore(tmp_path)
    for _ in KEYWORDS[1:]:
        assert next_search.keyword != first.keyword
        next_search = _exchange(store, host, work=True, report=_report(
            next_search, search_dates={}, search_exhausted=True,
        )).task
    assert next_search.keyword == first.keyword
    above = Reference(kind="video", key="BV0000000001")
    equal = Reference(kind="video", key="BV0000000002")
    below = Reference(kind="video", key="BV0000000003")
    older = Reference(kind="video", key="BV0000000004")
    page_two = _exchange(store, host, work=True, report=_report(
        next_search, references=[above, equal, below], more_pages=True,
        search_dates={above.key: now - 10, equal.key: now - 60, below.key: now - 70},
    )).task
    assert page_two.kind == "search" and page_two.page == 2
    collecting = _exchange(store, host, report=_report(
        page_two, references=[older], more_pages=True, search_dates={older.key: now - 80},
    ))
    assert collecting.status["phase"] == "detecting"
    assert collecting.status["counts"]["search"] == {"done": 2}
    assert {row["key"] for row in collecting.status["video_queue"]} == {above.key, equal.key}
    assert store.read(key) == now - 60


def test_update_maintenance_delivers_search_coverage_without_claiming_new_work(tmp_path, monkeypatch):
    from threading import Event

    from src import update_state

    svc = ClusterService(tmp_path)
    session = svc.store.start_host("主节点", "http://127.0.0.1:8182")
    task = _exchange(svc.store, session, work=True).task
    covered = {BVID: int(time.time()) - 60}
    report_key = f"report:{session['node']}"
    report = _report(task, references=[Reference(kind="video", key=BVID)],
                     search_dates=covered, search_exhausted=True)
    svc.store.write(report_key, report.model_dump())
    exchange = Mock(wraps=svc.store.exchange)
    monkeypatch.setattr(svc.store, "exchange", exchange)
    monkeypatch.setattr("web.cluster_service.is_llm_ready", lambda: True)
    stopping = Event()
    stopping.set()
    monkeypatch.setattr(update_state, "maintenance", stopping)
    pool = Mock()
    try:
        feed = svc._sync(session, [], pool)
        sent = exchange.call_args.args[0]
        assert sent.report.search_dates == covered
        assert sent.local_work == "busy"
        assert not sent.work and feed.task is None
        assert svc.store.read(report_key) is None
        assert feed.status["counts"]["video"] == {"pending": 1}
        pool.submit.assert_not_called()
    finally:
        svc.http.close()

@pytest.mark.parametrize("interruption", ["target", "worker_without_dates", "video_failed"])
def test_incomplete_cluster_search_keeps_previous_time_boundary(tmp_path, monkeypatch, interruption):
    from web import cluster_store

    store, host, _ = _cluster(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(cluster_store, "TARGET", 1)
    search = _exchange(store, host, work=True).task
    key = f"search_coverage:{search.keyword}"
    old_cutoff = now - 500
    store.write(key, old_cutoff)
    report = _report(search, references=[Reference(kind="video", key=BVID)],
                     more_pages=interruption == "target", search_exhausted=interruption != "target",
                     search_dates=None if interruption == "worker_without_dates" else {BVID: now - 50})
    video = _exchange(store, host, work=True, video_detection=True, report=report).task
    if interruption == "video_failed":
        for delay in (30, 60, 120, 0):
            _exchange(store, host, report=Report(
                task_id=video.task_id, lease=video.lease, outcome="retry", reason="network",
            ))
            if delay:
                now += delay
                video = _exchange(store, host, work=True, video_detection=True).task
    else:
        _exchange(store, host, report=_report(video))
    assert store.read(key) == old_cutoff


def test_round_deduplicates_video_and_fans_out_activity(tmp_path):
    store, host, member = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    assert _exchange(store, member, work=True).task is None
    ref = Reference(kind="video", key=BVID)
    first_result = _report(first, references=[ref], more_pages=True)
    second = _exchange(store, host, work=True, report=first_result).task
    assert (second.keyword, second.order, second.page) == ("抽奖", "pubdate", 2)
    _exchange(store, host, report=_report(second, references=[ref]))
    _exchange(store, host, report=first_result)
    assert store.status()["counts"]["video"] == {"pending": 1}

    result = _exchange(store, member, activities=[_activity(prize="奖品")])
    assert len(result.changes) == 1
    assert _exchange(store, host).changes == result.changes
    assert _exchange(store, member, after=result.changes[-1].seq).changes == []


def test_old_peer_skips_undated_entries_and_new_peer_can_replay_them(tmp_path, monkeypatch):
    from web.cluster_protocol import JoinRequest
    from web import cluster_service

    store, host, peer = _cluster(tmp_path / "host")
    pending = _activity(prize="时间待定").model_copy(update={"lottery_time": None})
    known_key = "223456789012345678"
    known = _activity(prize="时间明确").model_copy(update={"dynamic_id": known_key, "source_url": opus_link(known_key)})
    _exchange(store, host, activities=[pending, known], unknown_activity_time=True)
    legacy = _exchange(store, peer)
    assert legacy.status["active"] == 2
    assert [change.activity for change in legacy.changes] == [known]

    svc = ClusterService(tmp_path / "member")
    received = {}
    monkeypatch.setattr(cluster_service, "is_llm_ready", lambda: False)
    monkeypatch.setattr(cluster_service, "_import",
                        lambda rows, _: received.update((row.activity.dynamic_id, row.activity) for row in rows))

    def remote(_session, path, token, payload):
        if path == "/join":
            return store.register(Register.model_validate(payload), token)
        return store.exchange(Exchange.model_validate(payload), token).model_dump()

    monkeypatch.setattr(svc, "_remote", remote)
    svc.join(JoinRequest(name="升级成员", address="http://127.0.0.1:8182", invite=store.read("invite")))
    session = svc.store.read("session")
    node = session["node"]
    svc.store.write(f"cursor:{node}", legacy.changes[-1].seq)
    svc._sync(session, [], Mock())
    assert received == {}
    svc._sync(session, [], Mock())
    assert received == {pending.dynamic_id: pending, known.dynamic_id: known}
    assert svc.store.read(f"full_cursor:{node}") == legacy.changes[-1].seq
    closed = pending.model_copy(update={"closed": True})
    assert _exchange(store, host, activities=[closed], unknown_activity_time=True).status["active"] == 1
    svc.http.close()


def test_exports_inferred_time_as_unknown_and_closes_previously_shared_item(monkeypatch):
    from web import cluster_service

    record = {
        "dynamic_id": KEY, "lottery_type": "转发抽奖", "draw_status": "active",
        "lottery_time": int(time.time()) + 180 * 86400, "conditions": {"lottery_time_inferred": True},
    }
    monkeypatch.setattr(cluster_service, "load_activities", lambda: [record])
    now = int(time.time())
    assert cluster_service._export(now)[0].lottery_time is None
    record["draw_status"] = "ended"
    assert cluster_service._export(now) == []
    assert cluster_service._export(now, {KEY})[0].closed


def test_expired_lease_cannot_publish_after_reassignment(tmp_path):
    store, host, member = _cluster(tmp_path)
    _exchange(store, host)
    with store.tx() as db:
        db.execute("DELETE FROM jobs")
        store._queue(db, 1, "video", BVID)
    task = _exchange(store, host, work=True, video_detection=True).task
    with store.tx() as db:
        db.execute("UPDATE jobs SET until=0 WHERE id=?", (task.task_id,))
    replacement = _exchange(store, member, work=True, video_detection=True).task
    assert replacement.task_id == task.task_id
    assert replacement.lease != task.lease
    stale = _exchange(store, host, report=_report(task, activities=[_activity(prize="过期结果")]))
    assert stale.changes == []
    result = _exchange(store, member, report=_report(replacement, activities=[_activity(prize="有效结果")]))
    assert result.changes[-1].activity.prize == "有效结果"


def test_model_reservation_is_reused_across_nodes(tmp_path):
    store, host, member = _cluster(tmp_path)
    search = _exchange(store, host, work=True).task
    refs = [Reference(kind="video", key=key) for key in (BVID, "BV0000000001")]
    first = _exchange(store, host, work=True, video_detection=True,
                      report=_report(search, references=refs)).task
    second = _exchange(store, member, work=True, video_detection=True).task
    key = secrets.token_hex(32)

    def request(node, task, fields=None):
        return store.analysis(Analysis(node=node["node"], task_id=task.task_id,
                                       lease=task.lease, key=key, fields=fields), node["token"])

    assert request(host, first) == {"fields": None}
    assert request(member, second) == {"busy": True}
    fields = {"model_assessment": "entry", "rules_summary": "入口"}
    assert request(host, first, fields) == {"fields": fields}
    assert request(member, second) == {"fields": fields}


def test_host_api_login_auth_and_protocol(isolated_home, monkeypatch):
    from web import cluster_routes
    from web.app import app

    monkeypatch.setattr(cluster_routes, "DATA_DIR", isolated_home / "data")
    logged_in = False
    monkeypatch.setattr(cluster_routes, "get_account_profile", lambda: {"logged_in": logged_in})
    monkeypatch.setattr(cluster_routes, "has_login_cookie", lambda: logged_in)
    with TestClient(app) as api:
        headers = {"X-Binggo-Dashboard": "1"}
        data = {"name": "主节点", "address": "http://127.0.0.1:8182"}
        assert api.post("/api/cluster/host", json=data, headers=headers).status_code == 401
        logged_in = True
        assert api.post("/api/cluster/host", json=data).status_code == 403
        assert api.post("/api/cluster/host", json=data, headers=headers).json()["role"] == "host"
        invite = api.post("/api/cluster/invite", headers=headers).json()["invite"]
        base = "/api/cluster/peer/v1"
        other = Register(name="成员", node=uuid.uuid4().hex, token=secrets.token_hex(32))
        assert api.post(base + "/join", json=other.model_dump()).status_code == 401
        joined = api.post(base + "/join", headers={"Authorization": f"Bearer {invite}"}, json=other.model_dump())
        assert joined.json()["protocol"] == 1
        body = Exchange(node=other.node, after=0, activities=[_activity(prize="同步")]).model_dump()
        creds = {"Authorization": f"Bearer {other.token}"}
        result = api.post(base + "/exchange", headers=creds, json=body)
        assert result.json()["changes"][0]["activity"]["prize"] == "同步"
        assert api.get("/api/cluster/detected-activities", headers=headers).json()["total"] == 0
        assert api.get("/api/cluster/detected-activities").status_code == 403
        assert api.post(base + "/detected-activities", json={"node": other.node}).status_code == 401
        assert api.post(base + "/detected-activities", headers=creds, json={"node": other.node}).json()["total"] == 0
        assert api.post(base + "/exchange", headers=creds, content=b"x" * (256 * 1024 + 1)).status_code == 413
        assert api.post(base + "/leave/" + other.node, headers=creds).status_code == 200
        assert api.post(base + "/exchange", headers=creds, json=body).status_code == 401


def test_dashboard_search_totals_survive_pruning(tmp_path):
    store, host, member = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    second = _exchange(store, host, work=True, report=_report(first, more_pages=True,
                       references=[Reference(kind="video", key=BVID)])).task
    _exchange(store, host, report=Report(task_id=second.task_id, lease=second.lease, outcome="retry"))
    status = store.status()
    assert status["counts"]["search"]["done"] == 1
    assert status["search_pages_total"] == 1
    for _ in range(4):
        with store.tx() as db:
            store._next_round(db, int(time.time()))
    next_task = _exchange(store, member, work=True).task
    updated = _exchange(store, member,
                        report=_report(next_task, references=[Reference(kind="video", key=BVID)])).status
    assert updated["search_pages_total"] == 2
    assert updated["search_history_start"] == 1
    assert updated["counts"]["search"]["done"] == 1
    assert ClusterStore(tmp_path).status()["search_pages_total"] == 2


def test_member_reports_disconnect_and_reconnects(tmp_path, monkeypatch):
    from web.cluster_protocol import JoinRequest
    from web import cluster_service

    host, host_config, _ = _cluster(tmp_path / "host")
    svc = ClusterService(tmp_path / "member")
    loaded = []
    monkeypatch.setattr(cluster_service, "is_llm_ready", lambda: False)
    monkeypatch.setattr(cluster_service, "_import", lambda changes, cluster_id: loaded.extend(changes))

    def remote(_session, path, token, payload):
        if path == "/join":
            return host.register(Register.model_validate(payload), token)
        return host.exchange(Exchange.model_validate(payload), token).model_dump()

    monkeypatch.setattr(svc, "_remote", remote)
    svc.join(JoinRequest(name="另一台", address="http://127.0.0.1:8182", invite=host.read("invite")))
    session = svc.store.read("session")
    svc._sync(session, [_activity(prize="双向")], None)
    assert loaded[0].activity.prize == "双向"
    assert _exchange(host, host_config).changes == loaded
    pool = Mock()
    feed = svc._sync(session, [], pool)
    assert feed.task.kind == "search"
    pool.submit.assert_called_once_with(svc._execute, session, feed.task, two_stage=True, watch_batch=True)
    assert [row.activity.dynamic_id for row in loaded] == [KEY, KEY]  # Upgrade cursor replays the already-imported row once.
    with pytest.raises(ClusterError):
        host.exchange(Exchange(node=session["node"], after=0), "bad token")
    svc.http.close()


def test_round_targets_1000_distinct_videos_and_starts_again(tmp_path):
    from web.cluster_protocol import TARGET

    store, host, member = _cluster(tmp_path)
    search = _exchange(store, host, work=True).task
    assert _exchange(store, member, work=True).task is None
    with store.tx() as db:
        for number in range(TARGET - 1):
            store._queue(db, 1, "video", f"BV{number:010d}")
    refs = [Reference(kind="video", key="BV0000000000"),
            Reference(kind="video", key="BV9999999998"),
            Reference(kind="video", key="BV9999999999")]
    feed = _exchange(store, host, report=_report(search, references=refs, more_pages=True))
    assert feed.task is None
    assert feed.status["phase"] == "detecting"
    assert feed.status["counts"]["video"] == {"pending": TARGET}
    assert feed.status["counts"]["search"] == {"done": 1, "skipped": 1}
    assert _exchange(store, member, work=True).task is None
    assert _exchange(store, host, work=True, video_detection=True).task.kind == "video"
    with store.tx() as db:
        db.execute("UPDATE jobs SET state='done' WHERE kind='video'")
    assert _exchange(store, host, work=True).task is None
    next_round = _exchange(store, member, work=True)
    assert next_round.status["round"] == 2 and next_round.task.page == 1
    assert next_round.status["next_round"] == 0
    assert next_round.task.keyword == next_round.status["round_keyword"] == "福利"


def test_new_keyword_rounds_follow_legacy_round_and_wrap_after_exhaustion(tmp_path):
    from web.cluster_protocol import KEYWORDS

    store, host, _ = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    with store.tx() as db:
        db.execute("DELETE FROM settings WHERE key IN ('round_keyword','round_search')")
        store._queue(db, 1, "search", "pubdate:福利:1", keyword="福利", order="pubdate", page=1)
    legacy = store.status()
    assert legacy["round_keyword"] is None and legacy["upcoming_keywords"] == KEYWORDS
    assert {item["keyword"] for item in legacy["search_tasks"]} == {"抽奖", "福利"}

    feed = _exchange(store, host, work=True, report=_report(first))
    while feed.status["round"] == 1:
        feed = _exchange(store, host, work=True, report=_report(feed.task))
    for keyword in (*KEYWORDS, KEYWORDS[0]):
        assert feed.task.keyword == feed.status["round_keyword"] == keyword
        assert (feed.task.order, feed.task.page) == ("pubdate", 1)
        assert {item["keyword"] for item in feed.status["search_tasks"]} == {keyword}
        store = ClusterStore(tmp_path)
        feed = _exchange(store, host, work=True, report=_report(feed.task))


def test_search_turns_follow_registration_order_and_resume_on_disconnect(tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr("web.cluster_store.time.time", lambda: now)
    store, host, member = _cluster(tmp_path)
    extra = Register(name="第三节点", node=uuid.uuid4().hex, token=secrets.token_hex(32))
    store.register(extra, store.read("invite"))
    third = extra.model_dump()
    first = _exchange(store, host, work=True).task
    assert _exchange(store, member, work=True).task is None
    assert _exchange(store, third, work=True).task is None
    assert _exchange(store, host, work=True, report=_report(first)).task is None
    second = _exchange(store, member, work=True).task
    assert (second.keyword, second.order, second.page) == ("福利", "pubdate", 1)
    assert _exchange(store, member, work=True, report=_report(second)).task is None
    offline = Register(name="离线节点", node=uuid.uuid4().hex, token=secrets.token_hex(32))
    store.register(offline, store.read("invite"))
    third_turn = _exchange(store, third, work=True).task
    assert (third_turn.keyword, third_turn.order, third_turn.page) == ("奖品", "pubdate", 1)
    scan = _exchange(store, third, work=True, report=_report(third_turn, more_pages=True,
                     references=[Reference(kind="video", key=BVID)])).task
    assert scan.page == 2
    store = ClusterStore(tmp_path)
    assert _exchange(store, host, work=True).task is None
    now += 121
    resumed = _exchange(store, host, work=True).task
    assert (resumed.task_id, resumed.page) == (scan.task_id, 2)
    assert resumed.lease != scan.lease
    assert _exchange(store, third, work=True, report=_report(scan)).task is None
    finished = _exchange(store, host, report=_report(resumed))
    assert finished.status["counts"]["search"] == {"done": 2}
    assert finished.status["phase"] == "detecting"


def test_history_merges_joined_nodes_and_reopens_search_if_queue_shrinks(tmp_path, monkeypatch):
    monkeypatch.setattr("web.cluster_store.TARGET", 2)
    store, host, member = _cluster(tmp_path)
    old, shared, unique, new = [f"BV{number:010d}" for number in range(4)]
    with store.tx() as db:
        db.execute("INSERT INTO checked_videos VALUES(?,1)", (old,))
        store._next_round(db, int(time.time()))
        db.execute("DELETE FROM settings WHERE key='round_search'")
        store._queue(db, 1, "search", "totalrank:抽奖:1", keyword="抽奖", order="totalrank", page=1)
    task = _exchange(store, host, work=True, video_history={"ids": [shared], "complete": True}).task
    active = _exchange(store, host, work=True, report=_report(task, more_pages=True,
        references=[Reference(kind="video", key=key) for key in (old, shared, unique)])).task
    assert active is not None
    member_task = _exchange(store, member, work=True).task
    found = _exchange(store, member, report=_report(member_task, more_pages=True,
        references=[Reference(kind="video", key=new)])).status
    assert found["counts"]["video"] == {"pending": 2}
    assert sum(query["found"] for query in found["search_tasks"]) == 2
    pending = _exchange(store, member, work=True, video_history={"ids": [shared, unique, shared]})
    assert pending.task is None and pending.status["history_syncing"] == 1
    retried = _exchange(store, member, work=True, video_history={"ids": [shared, unique], "complete": True})
    assert retried.task.kind == "search"
    assert retried.status["counts"]["video"] == {"pending": 1, "skipped": 1}
    assert sum(query["found"] for query in retried.status["search_tasks"]) == 1
    assert retried.status["known_videos"] == 3
    assert {node["id"]: node["history_count"] for node in retried.status["nodes"]} == {
        host["node"]: 1, member["node"]: 2,
    }
    store.leave(member["node"], member["token"])
    store = ClusterStore(tmp_path)
    assert store.status()["known_videos"] == 3


def test_round_import_uses_node_order_and_waits_for_applied_catalog_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr("web.cluster_store.TARGET", 1)
    store, host, member = _cluster(tmp_path)
    history = {"ids": [], "complete": True}
    _exchange(store, member, video_history=history)
    search = _exchange(store, host, work=True, video_history=history).task
    scan = _exchange(store, host, work=True, video_detection=True,
                     report=_report(search, references=[Reference(kind="video", key=BVID)])).task
    first = _exchange(store, host, work=True, report=_report(scan, activities=[_activity(prize="测试活动")]))
    assert first.task is None
    assert first.status["phase"] == "importing"
    assert first.status["round_import"] == {"round": 1, "node": host["node"], "pending": 2, "done": 0, "skipped": 0}
    assert _exchange(store, member, work=True, import_round=1).changes == []
    assert _exchange(store, host, import_round=1).status["round_import"]["node"] == host["node"]
    seq = first.changes[-1].seq
    assert _exchange(store, host, after=seq, import_round=1).status["round_import"]["node"] == member["node"]
    member_import = _exchange(store, member, import_round=1)
    assert member_import.changes[-1].seq == seq
    next_round = _exchange(store, member, after=seq, work=True, import_round=1)
    assert next_round.task.kind == "search" and next_round.status["round"] == 2
    assert next_round.status["known_videos"] == 1
    host_work = next(node for node in next_round.status["nodes"] if node["host"])
    assert (host_work["videos_checked"], host_work["videos_total"], host_work["videos_matched"]) == (0, 1, 1)


def test_cluster_watch_merges_by_mid_and_prioritizes_assignment_for_selected_node(tmp_path):
    from web.cluster_protocol import WatchAssignment

    store, host, member = _cluster(tmp_path)
    revision = uuid.uuid4().hex
    users = [{"mid": "31", "name": "同一 UP 主"}]
    _exchange(store, host, video_detection=True, local_work="idle", watch_list={
        "revision": revision, "users": users, "complete": True,
    })
    _exchange(store, member, video_detection=True, local_work="participating", watch_list={
        "revision": revision, "users": users + [{"mid": "49", "name": "另一位"}], "complete": True,
    })
    result = store.tracked_users(1, node=member["node"], token=member["token"])
    assert result["total"] == 2
    shared = next(user for user in result["items"] if user["mid"] == "31")
    assert set(shared["sources"]) == {host["name"], member["name"]}
    assignment = WatchAssignment(mid="31", node=member["node"])
    store.assign_watch(assignment)
    store.assign_watch(assignment)
    assert _exchange(store, host, work=True, video_detection=True, local_work="idle").task.kind == "search"
    assigned = _exchange(store, member, work=True, video_detection=True, local_work="idle")
    assert (assigned.task.kind, assigned.task.key) == ("watch", "31")
    displayed = next(node for node in assigned.status["nodes"] if node["id"] == member["node"])
    assert displayed["kind"] == "watch" and displayed["task_label"] == "追踪检查「同一 UP 主」"
    report = _exchange(store, member, local_work="idle", report=_report(assigned.task, activities=[_activity(prize="追踪检出")]))
    assert report.status["conversion"] == {"checked": 0, "matched": 0}
    assert report.changes[0].activity.prize == "追踪检出"
    assert next(user for user in store.tracked_users(1)["items"] if user["mid"] == "31")["hits"] == 1

    _exchange(store, member, video_detection=True, local_work="idle", watch_list={
        "revision": uuid.uuid4().hex, "users": [], "complete": True,
    })
    result = ClusterStore(tmp_path).tracked_users(1)
    assert result["total"] == 1 and result["items"][0]["sources"] == [host["name"]]


def test_watch_batch_distributes_every_uid_once_and_shares_partial_findings(tmp_path):
    from web.cluster_protocol import WatchBatch, WatchFindings

    store, host, member = _cluster(tmp_path)
    capable = {"watch_batch_capable": True, "video_detection": True, "local_work": "idle"}
    _exchange(store, host, **capable, watch_list={
        "revision": uuid.uuid4().hex, "complete": True,
        "users": [{"mid": "31", "name": "甲"}, {"mid": "32", "name": "乙"}],
    })
    _exchange(store, member, **capable, watch_list={
        "revision": uuid.uuid4().hex, "complete": True,
        "users": [{"mid": "31", "name": "甲"}, {"mid": "33", "name": "丙"}],
    })
    assignment = WatchBatch(nodes=[host["node"], member["node"]])
    store.assign_watch_batch(assignment)
    first = _exchange(store, host, work=True, **capable).task
    second = _exchange(store, member, work=True, **capable).task
    assert [(job.kind, job.key) for job in (first, second)] == [("watch", "31"), ("watch", "32")]
    finding = WatchFindings(node=host["node"], task_id=first.task_id, lease=first.lease,
                            activities=[_activity(prize="合集活动")])
    store.publish_watch_findings(finding, host["token"])
    store.publish_watch_findings(finding, host["token"])
    assert _exchange(store, member, **capable).changes[0].activity.dynamic_id == KEY
    store.assign_watch_batch(WatchBatch(nodes=[host["node"]]))
    other = _exchange(store, member, work=True, **capable, report=_report(second)).task
    assert other is None or other.kind != "watch"
    final = _exchange(store, host, work=True, **capable, report=_report(first)).task
    assert (final.kind, final.key) == ("watch", "33")
    status = _exchange(store, host, **capable, report=_report(final)).status
    assert status["watch_batch"]["counts"] == {"done": 3}
    assert next(item["hits"] for item in store.tracked_users(1)["items"] if item["mid"] == "31") == 1
    with pytest.raises(ClusterError):
        store.publish_watch_findings(finding, host["token"])


def test_legacy_watch_node_can_check_dynamics_and_modern_node_finishes_collection(tmp_path):
    from web.cluster_protocol import WatchBatch

    store, host, legacy = _cluster(tmp_path)
    ready = {"video_detection": True, "local_work": "idle"}
    _exchange(store, host, **ready, watch_batch_capable=True, watch_list={
        "revision": uuid.uuid4().hex, "complete": True, "users": [{"mid": "885439", "name": "合集作者"}],
    })
    store.assign_watch_batch(WatchBatch(nodes=[legacy["node"]]))
    task = _exchange(store, legacy, work=True, **ready).task
    assert (task.kind, task.key) == ("watch", "885439")
    found = _exchange(store, legacy, **ready, report=_report(task, activities=[_activity(prize="动态活动")]))
    assert found.status["watch_batch"]["counts"] == {"pending": 1}
    assert found.status["watch_batch"]["collection_pending"] == 1
    assert found.changes[0].activity.dynamic_id == KEY
    follow_up = _exchange(store, host, work=True, **ready, watch_batch_capable=True).task
    assert (follow_up.kind, follow_up.task_id) == ("watch", task.task_id)
    assert follow_up.lease != task.lease
    result = _exchange(store, host, **ready, watch_batch_capable=True, report=_report(follow_up))
    assert result.status["watch_batch"]["counts"] == {"done": 1}
    assert store.tracked_users(1)["items"][0]["hits"] == 1


@pytest.mark.parametrize("final_reference_fails", [False, True])
def test_older_nodes_share_collection_links_while_active_discovery_is_paused(tmp_path, monkeypatch, final_reference_fails):
    from web.cluster_protocol import WatchFindings, WorkAssignments

    store, host, first = _cluster(tmp_path)
    second = Register(name="另一台旧节点", node=uuid.uuid4().hex, token=secrets.token_hex(32))
    store.register(second, store.read("invite"))
    second = second.model_dump()
    ready = {"video_detection": True, "local_work": "idle"}
    modern = {**ready, "watch_batch_capable": True, "watch_link_workers": True}
    _exchange(store, host, **modern, watch_list={
        "revision": uuid.uuid4().hex, "complete": True, "users": [{"mid": "885439", "name": "合集作者"}],
    })
    store.set_work_assignments(WorkAssignments(collection_nodes=[], watch_nodes=[
        host["node"], first["node"], second["node"],
    ]))
    store.set_collection_paused(True)
    store.start_watch()
    dynamics = _exchange(store, first, work=True, **ready).task
    assert dynamics.kind == "watch"
    previously_found = _activity(prize="已找到")
    _exchange(store, first, **ready, report=_report(dynamics, activities=[previously_found]))
    preparation = _exchange(store, host, work=True, **modern)
    assert preparation.task.task_id == dynamics.task_id
    assert preparation.status["watch_collection_mode"] == "collection_only"
    dynamic = Reference(kind="dynamic", key=str(int(KEY) + 1))
    short = Reference(kind="short", key="link1")
    upload = WatchFindings(node=host["node"], task_id=preparation.task.task_id, lease=preparation.task.lease, references=[
        dynamic, short, Reference(kind="dynamic", key=KEY),
    ])
    store.publish_watch_findings(upload, host["token"])
    store.publish_watch_findings(upload, host["token"])
    assert _exchange(store, first, work=True, **ready).task is None
    _exchange(store, host, **modern, report=_report(preparation.task))
    first_lease = _exchange(store, first, work=True, **ready)
    second_lease = _exchange(store, second, work=True, **ready)
    assert (first_lease.task.kind, first_lease.task.key) == ("dynamic", dynamic.key)
    assert (second_lease.task.kind, second_lease.task.key) == ("short", short.key)
    assert second_lease.status["watch_batch"]["references"] == {"leased": 2}
    assert sum(node["kind"] == "watch" for node in second_lease.status["nodes"]) == 2
    new_activity = previously_found.model_copy(update={"dynamic_id": dynamic.key, "source_url": opus_link(dynamic.key)})
    _exchange(store, first, **ready, report=_report(first_lease.task, activities=[new_activity]))
    next_link = Reference(kind="dynamic", key=str(int(KEY) + 2))
    next_task = _exchange(store, second, work=True, **ready,
                          report=_report(second_lease.task, references=[dynamic, next_link])).task
    assert (next_task.key, next_task.depth) == (next_link.key, 2)
    if final_reference_fails:
        now = int(time.time())
        monkeypatch.setattr(time, "time", lambda: now)
        for retry_delay in (30, 60, 120, 0):
            complete = _exchange(store, second, **ready, report=Report(
                task_id=next_task.task_id, lease=next_task.lease, outcome="retry", reason="dynamic_detail",
            ))
            if retry_delay:
                assert store.tracked_users(1)["items"][0]["failed_refs"] == 0
                now += retry_delay
                next_task = _exchange(store, second, work=True, **ready).task
        assert complete.status["watch_batch"]["counts"] == {"failed": 1}
        assert complete.status["watch_batch"]["references"] == {"done": 2, "failed": 1}
    else:
        complete = _exchange(store, second, **ready, report=_report(next_task))
        assert complete.status["watch_batch"]["counts"] == {"done": 1}
        assert complete.status["watch_batch"]["references"] == {"done": 3}
    result = store.tracked_users(1)["items"][0]
    assert result["failed_refs"] == int(final_reference_fails)
    assert result["hits"] == 2


@pytest.mark.parametrize("unreadable", [False, True])
def test_watch_shares_reference_results_and_keeps_pending_links_across_batches(tmp_path, monkeypatch, unreadable):
    from web.cluster_protocol import CollectionCoverage, WatchFindings
    from web.cluster_store import WATCH_FAILED_TTL

    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    store, host, member = _cluster(tmp_path)
    ready = {"video_detection": True, "local_work": "idle", "watch_batch_capable": True, "watch_link_workers": True}
    _exchange(store, host, **ready, watch_list={
        "revision": uuid.uuid4().hex, "complete": True,
        "users": [{"mid": "885439", "name": "甲"}, {"mid": "126038161", "name": "乙"}],
    })
    store.set_collection_paused(True)
    ref = Reference(kind="dynamic", key=KEY)
    published = now - 100

    def prepare(refs):
        store.start_watch()
        leases = [_exchange(store, node, work=True, **ready) for node in (host, member)]
        for node, feed in zip((host, member), leases):
            task = feed.task
            assert task.kind == "watch"
            if refs:
                store.publish_watch_findings(WatchFindings(
                    node=node["node"], task_id=task.task_id, lease=task.lease, references=refs,
                ), node["token"])
            _exchange(store, node, **ready, report=_report(
                task, watch_through=now - 20,
                watch_collection=CollectionCoverage(container_id=task.key, published_at=published),
            ))
        return leases

    prepare([ref])
    task = _exchange(store, host, work=True, **ready).task
    assert task.kind == "dynamic"
    assert _exchange(store, member, work=True, **ready).task is None
    if unreadable:
        for delay in (30, 60, 120, 0):
            _exchange(store, host, **ready, report=Report(
                task_id=task.task_id, lease=task.lease, outcome="retry", reason="dynamic_detail",
            ))
            if delay:
                assert _exchange(store, member, work=True, **ready).task is None
                now += delay
                task = _exchange(store, host, work=True, **ready).task
    else:
        _exchange(store, host, **ready, report=_report(task, activities=[_activity(prize="合集活动")]))
    assert _exchange(store, member, work=True, **ready).task is None
    _exchange(store, host, **ready)
    rows = store.tracked_users(1)["items"]
    assert [(row["hits"], row["failed_refs"]) for row in rows] == ([(0, 1)] * 2 if unreadable else [(1, 0)] * 2)

    store = ClusterStore(tmp_path)
    leases = prepare([] if unreadable else [ref])
    for feed in leases:
        coverage = feed.status["watch_coverage"]
        assert coverage["dynamics"] == published + 80
        assert ("collection" in coverage) == (not unreadable)
    assert _exchange(store, member, work=True, **ready).task is None
    _exchange(store, host, **ready)
    assert [row["failed_refs"] for row in store.tracked_users(1)["items"]] == ([1, 1] if unreadable else [0, 0])
    if unreadable:
        now += WATCH_FAILED_TTL
        prepare([])
        assert _exchange(store, host, work=True, **ready).task.key == KEY


def test_collection_assignment_controls_search_and_detection_and_preserves_leases(tmp_path):
    from web.cluster_protocol import WorkAssignments

    store, host, member = _cluster(tmp_path)
    store.set_work_assignments(WorkAssignments(collection_nodes=[host["node"]], watch_nodes=[]))
    assert _exchange(store, member, work=True, video_detection=True).task is None
    search = _exchange(store, host, work=True).task
    store.set_work_assignments(WorkAssignments(collection_nodes=[member["node"]], watch_nodes=[]))
    assert _exchange(store, member, work=True, video_detection=True).task is None
    other_video = "BV0000000032"
    _exchange(store, host, report=_report(search, more_pages=True, references=[Reference(kind="video", key=BVID)]))
    page = _exchange(store, member, work=True).task
    assert (page.kind, page.page, page.order) == ("search", 2, "pubdate")
    _exchange(store, member, report=_report(page, references=[Reference(kind="video", key=other_video)]))
    assert _exchange(store, host, work=True, video_detection=True).task is None
    assigned = _exchange(store, member, work=True, video_detection=True).task
    assert (assigned.kind, assigned.key) == ("video", BVID)
    store.set_work_assignments(WorkAssignments(collection_nodes=[], watch_nodes=[]))
    _exchange(store, member, report=_report(assigned))
    assert _exchange(store, member, work=True, video_detection=True).task is None
    assert store.status()["counts"]["video"] == {"done": 1, "pending": 1}
    assert ClusterStore(tmp_path).status()["work_assignments"]["collection_nodes"] == []


def test_collection_pause_leaves_tracking_available_and_resumes_pending_search(tmp_path):
    from web.cluster_protocol import WorkAssignments

    store, host, member = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    store.set_work_assignments(WorkAssignments(collection_nodes=[host["node"]], watch_nodes=[member["node"]]))
    store.set_collection_paused(True)
    _exchange(store, host, report=_report(first, more_pages=True, references=[Reference(kind="video", key=BVID)]))
    ready = {"video_detection": True, "local_work": "idle", "watch_batch_capable": True}
    _exchange(store, member, **ready, watch_list={
        "revision": uuid.uuid4().hex, "complete": True, "users": [{"mid": "31", "name": "作者"}],
    })
    store.start_watch()
    tracking = _exchange(store, member, work=True, **ready).task
    assert tracking.kind == "watch"
    assert _exchange(store, host, work=True).task is None
    store.set_work_assignments(WorkAssignments(collection_nodes=[host["node"]], watch_nodes=[]))
    _exchange(store, member, report=_report(tracking), **ready)
    assert store.status()["watch_batch"]["counts"] == {"done": 1}
    store.set_collection_paused(False)
    assert _exchange(store, host, work=True).task.page == 2


def test_legacy_pause_migrates_to_collection_pause(tmp_path):
    store, _, _ = _cluster(tmp_path)
    with store.tx() as db:
        db.execute("DELETE FROM settings WHERE key='collection_paused'")
        store.put(db, "paused", True)
    updated = ClusterStore(tmp_path)
    assert updated.status()["collection_paused"]
    assert not updated.status()["paused"]
    updated.set_paused(True)
    assert ClusterStore(tmp_path).status()["paused"]


def test_rename_only_changes_the_authenticated_node_name(tmp_path):
    from web.cluster_protocol import RenameNode

    store, host, member = _cluster(tmp_path)
    first = _exchange(store, host, work=True).task
    with pytest.raises(ClusterError):
        store.rename_node(RenameNode(node=member["node"], name="冒名节点"), host["token"])
    store.rename_node(RenameNode(node=member["node"], name=" 新工作节点 "), member["token"])
    store.rename_node(RenameNode(node=host["node"], name="新主节点"), host["token"])
    assert store.read("session") == {**host, "name": "新主节点"}
    feed = _exchange(store, host, report=_report(first, references=[Reference(kind="video", key=BVID)]))
    assert feed.status["counts"]["search"] == {"done": 1}
    assert {row["id"]: row["name"] for row in feed.status["nodes"]} == {
        host["node"]: "新主节点", member["node"]: "新工作节点",
    }


def test_dispatch_falls_back_only_when_selected_search_nodes_are_offline(tmp_path, monkeypatch):
    from web.cluster_protocol import DispatchSettings

    now = int(time.time())
    monkeypatch.setattr("web.cluster_store.time.time", lambda: now)
    store, host, member = _cluster(tmp_path)
    store.set_dispatch(DispatchSettings(search_nodes=[member["node"]], fallback=True))
    assert _exchange(store, host, work=True).task is None
    now += 31
    first = _exchange(store, host, work=True).task
    assert first.kind == "search"
    assert _exchange(store, member, work=True).task is None
    assert _exchange(store, host, work=True, report=_report(first, more_pages=True,
                     references=[Reference(kind="video", key=BVID)])).task is None
    assert _exchange(store, member, work=True).task.page == 2


def test_round_import_waits_for_enabled_nodes_and_worker_starts_only_once(tmp_path, monkeypatch):
    from web import cluster_service

    svc = ClusterService(tmp_path)
    host = svc.store.start_host("主节点", "http://127.0.0.1:8182")
    history = {"ids": [], "complete": True}
    first = _exchange(svc.store, host, work=True, video_history=history).task
    _exchange(svc.store, host, report=_report(first))
    assert svc.store.status()["phase"] == "importing"
    runner = Mock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 17
    runner.resolve_job_status.return_value = Mock(state="success", result={"failed": 0})
    monkeypatch.setattr(cluster_service, "get_login_uid", lambda: 42)
    monkeypatch.setattr(cluster_service, "is_llm_ready", lambda: True)
    assert svc._round_participation(host, 1, runner) == "disabled"
    svc.set_round_participation(True)
    assert svc._round_participation(host, 1, runner) == "ready"
    runner.try_start.assert_not_called()
    grant = _exchange(svc.store, host, import_round=1, round_participation="ready")
    assert grant.status["round_import"]["participating"]
    svc.status_remote, svc.last_sync = grant.status, int(time.time())
    assert svc._round_participation(host, 1, runner) == "running"
    assert svc._round_participation(host, 1, runner) == "done"
    assert svc._round_participation(host, 1, runner) == "done"
    runner.try_start.assert_called_once()
    finished = _exchange(svc.store, host, import_round=1, round_participation="done")
    assert finished.status["round"] == 2
    monkeypatch.setattr(cluster_service, "get_login_uid", lambda: 43)
    assert svc._round_participation(host, 2, runner) == "disabled"
    svc.http.close()


def test_watch_worker_checks_recent_original_and_forwarded_dynamics(monkeypatch):
    from web import cluster_work

    now = int(time.time())
    seen = []
    def item(key, at, *, original=None, text=""):
        row = {"id_str": key, "type": "DYNAMIC_TYPE_WORD", "text": text,
               "modules": {"module_author": {"pub_ts": at}}}
        if original:
            row.update(type="DYNAMIC_TYPE_FORWARD", orig={"id_str": original})
        return row
    direct, forwarded, old = KEY, str(int(KEY) + 1), str(int(KEY) + 2)
    records = [item(direct, now, text="新品抽奖"), item(str(int(KEY) + 3), now, original=forwarded),
               item(old, now - 2 * 86400, text="过往抽奖")]
    monkeypatch.setattr(cluster_work, "read_data", lambda *_a, **_kw: {"items": records})
    monkeypatch.setattr(cluster_work.d, "_extract_from_detail_item", lambda row: row.get("text", ""))
    def verify(_client, key, _now):
        seen.append(key)
        return [], [_activity(prize=key)]
    monkeypatch.setattr(cluster_work, "_dynamic", verify)
    activities = []
    cluster_work._check_watched_author(object(), "31", activities)
    assert set(seen) == {direct, forwarded}
    assert {entry.prize for entry in activities} == {direct, forwarded}


@pytest.mark.parametrize("reaches_boundary", [True, False])
def test_watch_feed_stops_at_complete_per_creator_boundary(monkeypatch, reaches_boundary):
    from web import cluster_work

    now, cutoff = 500_000, 200_000
    monkeypatch.setattr(time, "time", lambda: now)

    def forward(digit, published):
        return {"type": "DYNAMIC_TYPE_FORWARD", "orig": {"id_str": digit * 18},
                "modules": {"module_author": {"pub_ts": published}}}

    first = {"items": [forward("1", now - 10), forward("2", cutoff), forward("3", cutoff - 1)],
             "has_more": True, "offset": "next"}
    second = {"items": [forward("4", cutoff - 2)], "has_more": True, "offset": "unused"}
    fetch = Mock(side_effect=[first, second] if reaches_boundary else [{**first, "offset": ""}])
    verify = Mock(return_value=([], []))
    monkeypatch.setattr(cluster_work, "read_data", fetch)
    monkeypatch.setattr(cluster_work, "_dynamic", verify)

    covered = cluster_work._check_watched_author(object(), "31", [], covered_through=cutoff)
    assert {call.args[1] for call in verify.call_args_list} == {"1" * 18, "2" * 18}
    assert covered == (now - 10 if reaches_boundary else None)
    assert fetch.call_count == (2 if reaches_boundary else 1)


def test_watched_video_collection_skips_covered_detail_but_reads_new_id_at_boundary(monkeypatch, isolated_home):
    from contextlib import nullcontext

    from src.sources import ds1_xiaozhuli

    client = Mock()
    monkeypatch.setattr(ds1_xiaozhuli, "BilibiliClient", Mock(return_value=nullcontext(client)))
    covered = {"container_id": BVID, "published_at": 100}
    client.get_latest_video.return_value = {"bvid": BVID, "created": 100}
    assert ds1_xiaozhuli.check_update(force=True, covered=covered) is None
    client.get_latest_video.return_value = {"bvid": "BV0000000000", "created": 99}
    assert ds1_xiaozhuli.check_update(force=True, covered=covered) is None
    client.get_video_detail.assert_not_called()
    client.get_latest_video.return_value["created"] = 100
    client.get_video_detail.return_value = {"desc": "", "pubdate": 100}
    assert ds1_xiaozhuli.check_update(force=True, covered=covered).container_id == "BV0000000000"
    client.get_video_detail.assert_called_once_with("BV0000000000")


def test_worker_search_and_linked_activity_without_account_actions(tmp_path, monkeypatch):
    from web import cluster_work
    from web.cluster_protocol import Task

    now = int(time.time())
    link = opus_link(KEY)
    endpoints = {
        cluster_work.d.SEARCH_URL: {"result": [{"bvid": BVID}, {"bvid": BVID}], "numPages": 2},
        cluster_work.d.VIDEO_URL: {"title": "节目福利", "desc": "详情见评论区", "owner": {"mid": 123}, "aid": 42},
        cluster_work.d.REPLIES_URL: {"top": {"upper": {"mid": 123, "member": {"mid": "123"},
                                                     "content": {"message": "抽奖入口：" + link}}}},
        cluster_work.d.DYNAMIC_DETAIL_URL: {"item": {"id_str": KEY, "modules": {}}},
        cluster_work.d.LOTTERY_NOTICE_URL: {"lottery_id": "9", "status": 0, "participated": 1,
                                           "lottery_time": now + 3600, "first_prize_cmt": "奖品"},
    }
    monkeypatch.setattr(cluster_work.WorkClient, "read", lambda self, url, *args, **kwargs:
                        {"code": 0, "data": endpoints[url]})

    def run(kind, key, **params):
        task = Task(task_id=1, lease=uuid.uuid4().hex, kind=kind, key=key, **params)
        return cluster_work.execute(task, tmp_path / "cache.json", lambda _: None, lambda _, fields: fields)

    search = run("search", "pubdate:抽奖:1", keyword="抽奖")
    assert [row.key for row in search.references] == [BVID] and search.more_pages
    scanned = run("video", BVID)
    assert not scanned.references
    assert scanned.activities[0].prize == "奖品"
    assert scanned.activities[0].lottery_time == now + 3600
    assert scanned.model_calls == 0
    endpoints[cluster_work.d.LOTTERY_NOTICE_URL] = {}
    endpoints[cluster_work.d.OPUS_DETAIL_URL] = {"item": None}
    endpoints[cluster_work.d.DYNAMIC_DETAIL_URL] = {"item": {
        "id_str": KEY, "type": "DYNAMIC_TYPE_AV", "visible": True,
        "modules": {"module_dynamic": {"major": {"type": "MAJOR_TYPE_ARCHIVE"}}},
    }}
    archive = run("video", BVID)
    assert archive.outcome == "done" and not archive.activities
    endpoints[cluster_work.d.DYNAMIC_DETAIL_URL] = {"item": None}
    missing = run("video", BVID)
    assert missing.outcome == "retry" and missing.reason == "dynamic_detail"


def test_conversion_tracks_completed_videos_and_catalog_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setattr("web.cluster_store.TARGET", 2)
    store, host, member = _cluster(tmp_path)
    _exchange(store, host, video_history={"ids": [], "complete": True})
    key = str(int(KEY) + 1)
    uploaded = Activity(dynamic_id=key, source_url=opus_link(key), lottery_type="互动抽奖",
                        lottery_time=int(time.time()) + 86400, prize="节点上传")
    search = _exchange(store, host, work=True, activities=[uploaded]).task
    refs = [Reference(kind="video", key=code) for code in (BVID, "BV0000000001")]
    first = _exchange(store, host, work=True, video_detection=True, report=_report(search, references=refs)).task
    second = _exchange(store, member, work=True, video_detection=True).task
    valid = _activity(prize="集群检出")
    _exchange(store, host, report=Report(task_id=first.task_id, lease=first.lease, outcome="retry",
                                        reason="dynamic_body", activities=[valid]))
    assert store.status()["conversion"] == {"matched": 0, "checked": 0}
    assert store.detected_activities(1)["items"][0]["dynamic_id"] == KEY
    result = _exchange(store, member, report=_report(second, activities=[valid])).status
    assert result["conversion"] == {"matched": 1, "checked": 1}
    assert result["local_conversion"] == result["conversion"]
    with store.tx() as db:
        db.execute("UPDATE jobs SET due=0 WHERE id=?", (first.task_id,))
    retried = _exchange(store, host, work=True, video_detection=True).task
    _exchange(store, host, report=_report(retried, activities=[valid]))
    assert store.status()["conversion"] == {"matched": 2, "checked": 2}
    assert store.status()["local_conversion"] == {"matched": 1, "checked": 1}
    assert store.detected_activities(1)["total"] == 1

    with store.tx() as db:
        store.put(db, "video_results_backfilled", False)
        db.execute("DELETE FROM cluster_events WHERE message LIKE '第 1 轮开始%'")
        db.execute("DELETE FROM cluster_video_results")
    reopened = ClusterStore(tmp_path)
    assert reopened.status()["conversion"] == {"matched": 2, "checked": 2}


def test_upgrade_requeues_video_with_unfinished_link_and_keeps_catalog(tmp_path):
    store, host, _ = _cluster(tmp_path)
    _exchange(store, host, activities=[_activity(prize="既有活动")])
    with store.tx() as db:
        store.put(db, "two_stage", False)
        store.put(db, "phase", "running")
        db.execute("UPDATE jobs SET state='done'")
        store._queue(db, 1, "video", BVID)
        store._queue(db, 1, "short", "abbr")
        store._queue(db, 1, "dynamic", KEY)
        db.execute("UPDATE jobs SET state='done' WHERE kind IN ('video','short')")
        db.execute("INSERT INTO sightings SELECT round,id,'short','abbr' FROM jobs WHERE kind='video'")
        db.execute("INSERT INTO sightings SELECT round,id,'dynamic',? FROM jobs WHERE kind='short'", (KEY,))
    upgraded = ClusterStore(tmp_path)
    assert upgraded.read("session")["id"] == host["id"]
    first = _exchange(upgraded, host, work=True, video_detection=True)
    assert first.status["phase"] == "detecting"  # Below target with no search left.
    assert first.task.kind == "video" and first.task.key == BVID
    assert set(first.status["counts"]) == {"search", "video"}
    assert first.changes[0].activity.prize == "既有活动"
    verified = _activity(prize="核验更新")
    completed = _exchange(upgraded, host, report=_report(first.task, activities=[verified]))
    assert completed.changes[-1].activity == verified


def test_shared_cluster_log_records_work_retry_and_result(tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr("web.cluster_store.time.time", lambda: now)
    store, host, member = _cluster(tmp_path)
    task = _exchange(store, host, work=True).task
    assert task is not None
    retry = Report(task_id=task.task_id, lease=task.lease, outcome="retry", reason="model")
    _exchange(store, host, report=retry)
    now += 30
    member_job = _exchange(store, member, work=True).task
    report = _report(member_job, references=[Reference(kind="video", key=BVID)])
    view_from_member = _exchange(store, member, report=report).status["events"]
    assert view_from_member == store.status()["events"]
    assert any(event["node"] == host["node"] and event["level"] == "warning"
               and "模型未返回有效结果" in event["message"] for event in view_from_member)
    assert any(event["node"] == member["node"] and "去重后收录 1 个" in event["message"]
               for event in view_from_member)
    assert any(event["node"] is None and "第 1 轮开始" in event["message"] for event in view_from_member)
    assert host["token"] not in str(view_from_member) and member["token"] not in str(view_from_member)
    _exchange(store, member, report=report)
    assert store.status()["events"] == view_from_member


def test_existing_cluster_adds_event_table_and_keeps_state(tmp_path):
    store, host, member = _cluster(tmp_path)
    _exchange(store, host, activities=[_activity(prize="原有活动")])
    with store.tx() as db:
        db.execute("DROP TABLE cluster_events")
    updated = ClusterStore(tmp_path)
    assert updated.read("session")["id"] == host["id"]
    assert _exchange(updated, member).changes[0].activity.prize == "原有活动"
    updated.set_paused(True)
    assert _exchange(updated, member).status["events"][0]["message"] == "暂停领取新任务"


def test_active_search_only_counts_this_nodes_cluster_work(tmp_path):
    store, host, member = _cluster(tmp_path)
    host_task = _exchange(store, host, work=True).task
    video = Reference(kind="video", key=BVID)
    assert "第 1 页" in next(row for row in store.status()["nodes"] if row["host"])["task_label"]
    first = _exchange(store, host, report=_report(host_task, references=[video]))
    second = _exchange(store, member)
    assert first.status["local_work"] == {"pages": 1, "found": 1, "videos": 0, "activities": 0}
    assert second.status["local_work"] == {"pages": 0, "found": 0, "videos": 0, "activities": 0}
    assert all(event["node"] == member["node"] for event in second.status["local_events"])
    _exchange(store, host, report=_report(host_task, references=[video]))
    assert store.status()["local_work"] == first.status["local_work"]
    with store.tx() as db:
        db.execute("UPDATE jobs SET state='skipped' WHERE kind='search' AND state='pending'")
    scan = _exchange(store, member, work=True, video_detection=True).task
    assert scan.kind == "video"
    recent = _exchange(store, member, report=_report(scan)).status
    assert next(node for node in recent["nodes"] if node["id"] == member["node"])["videos_total"] == 1
    assert store.status()["local_work"]["videos"] == 0
    assert any(BVID in event["message"] for event in recent["local_events"])
    store.write("next_round", 1)
    assert _exchange(store, member).status["local_work"]["videos"] == 0
def test_collection_watch_uses_existing_source_checks_and_publishes_each_match(monkeypatch):
    from src.watch_users import WatchUser
    from web import cluster_service, cluster_work
    from web.actions import DS_HANDLER_BY_ID

    check = Mock(return_value=Mock(
        activity_links=[f"https://t.bilibili.com/{KEY}"] * 2, published_at=10, cv_id=None, container_id=BVID,
    ))
    monkeypatch.setitem(DS_HANDLER_BY_ID, "DS-1", (check, None))
    findings = [_activity(prize="合集活动")]
    follow = Mock(return_value=([], findings))
    monkeypatch.setattr(cluster_work, "_follow_reference", follow)
    publish = Mock()
    cluster_work._check_collection(object(), "885439", publish)
    check.assert_called_once_with(force=True)
    follow.assert_called_once()
    publish.assert_called_once_with(findings)
    monkeypatch.setattr(cluster_service, "list_watch_users", lambda **_: [WatchUser(mid=885439, name="手动设置的名字")])
    merged = cluster_service._tracked_creators(include_collections=True)
    assert len(merged) == 7
    assert next(user.name for user in merged if user.mid == "885439") == "手动设置的名字"
