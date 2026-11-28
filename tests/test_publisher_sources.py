import time
from types import SimpleNamespace
from unittest.mock import Mock

from web import publisher_sources
from web.cluster_protocol import Exchange, PublisherCoverage, PublisherFinding, Report, WatchList, WatchUser
from web.cluster_store import ClusterStore
from web.cluster_service import ClusterService


def _findings(now):
    return [PublisherFinding(activity=str(123456789012345670 + index), mid="123", name="发布者",
                             published=now - 100 + index)
            for index in range(3)]


def test_only_distinct_recent_activities_enroll_and_continuous_coverage_removes(tmp_path):
    store = ClusterStore(tmp_path)
    now = int(time.time())
    findings = _findings(now)
    store.record_publishers([findings[0]] * 3)
    assert store.publisher_listing(1)["total"] == 0
    store.record_publishers(findings)
    assert store.publisher_listing(1)["items"][0]["mid"] == "123"
    old = now - 1
    end = now + publisher_sources.INACTIVE_SECONDS
    with store.tx() as db:
        assert not store.complete_publisher(db, "123", PublisherCoverage(lower=old, upper=now), now)
        assert not store.complete_publisher(db, "123", PublisherCoverage(lower=now + 1, upper=end - 1), end)
        assert store.complete_publisher(db, "123", PublisherCoverage(lower=end - 1, upper=end + 1), end + 1)
        assert db.execute("SELECT message FROM cluster_events").fetchone()[0].endswith("60 天未发布抽奖活动")
        publisher_sources.record(db, findings, end + 1)
    assert store.publisher_listing(1)["total"] == 0


def test_publishers_follow_organizers_and_only_capable_worker_gets_lease(tmp_path):
    store = ClusterStore(tmp_path)
    host = store.start_host("主机", "http://127.0.0.1:8182")

    def exchange(work=True, **kwargs):
        return store.exchange(Exchange(node=host["node"], local_work="idle", work=work,
                                       video_detection=True, watch_link_workers=True, **kwargs), host["token"])

    exchange(work=False, watch_list=WatchList(revision="a" * 32, users=[WatchUser(mid="456", name="整理者")], complete=True),
             publisher_findings=_findings(int(time.time())))
    store.start_watch()
    organizer = exchange(publisher_findings=[]).task
    assert organizer.kind == "watch" and organizer.key == "456"
    feed = exchange(report=Report(task_id=organizer.task_id, lease=organizer.lease, outcome="done"),
                    publisher_findings=[])
    assert feed.status["publisher_sources_supported"]
    assert feed.task.kind == "publisher" and feed.task.key == "123"
    exchange(report=Report(task_id=feed.task.task_id, lease=feed.task.lease, outcome="done"))
    with store.tx(write=False) as db:
        assert db.execute("SELECT state FROM jobs WHERE kind='publisher'").fetchone()[0] == "done"
    assert store.publisher_listing(1)["items"][0]["checked"] == 0


def test_paused_participation_waits_for_publisher_queue_and_resumes_remaining(tmp_path, monkeypatch):
    from web import task_queue

    service = ClusterService(tmp_path)
    host = service.store.start_host("主机", "http://127.0.0.1:8182")
    key = f"round_participation:{host['id']}"
    service.store.write(key, {"round": 1, "job": 10, "uid": "123"})
    monkeypatch.setattr(service, "_participation_uid", lambda session: "123")
    queued = Mock(return_value=True)
    monkeypatch.setattr(task_queue, "get_task_queue", lambda: SimpleNamespace(publisher_waiting=queued))
    runner = Mock()
    runner.is_running.return_value = False
    runner.resolve_job_status.return_value = SimpleNamespace(
        state="success", result={"round_paused": True, "handled": ["123456789012345678"], "joined": 1},
    )
    runner.try_start.return_value = 11
    assert service._round_participation(host, 1, runner) == ("running", None)
    runner.try_start.assert_not_called()
    queued.return_value = False
    assert service._round_participation(host, 1, runner) == ("running", None)
    assert runner.try_start.call_args.args[1]["_round_handled"] == ["123456789012345678"]
    assert service.store.read(key)["job"] == 11
    service.http.close()
