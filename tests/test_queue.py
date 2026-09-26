from kb.queue import FAILED, FAILED_PERMANENT, Queue


def test_enqueue_and_dedup(queue: Queue):
    r1 = queue.enqueue("https://youtu.be/dQw4w9WgXcQ", "telegram", hint_tags=["#AI"])
    assert r1.status == "queued"
    item = queue.get(r1.item_id)
    assert item.source_type == "youtube" and item.hint_tags == ["ai"] and item.status == "queued"

    r2 = queue.enqueue("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5", "telegram")
    assert (r2.status, r2.item_id) == ("duplicate", r1.item_id)
    assert queue.get(r1.item_id).note is None  # same origin: nothing appended


def test_duplicate_from_other_origin_appends_note(queue: Queue):
    r1 = queue.enqueue("https://x.com/a/status/1", "x_bookmark")
    queue.enqueue("https://twitter.com/b/status/1", "telegram", hint_tags=["sd"], note="great thread")
    note = queue.get(r1.item_id).note
    assert "also captured via telegram" in note and "#sd" in note and "great thread" in note


def test_invalid_url(queue: Queue):
    assert queue.enqueue("nonsense", "cli").status == "invalid"


def test_failure_resume_and_permanent(queue: Queue):
    iid = queue.enqueue("https://example.com/a", "cli").item_id
    queue.advance(iid, "fetched")
    assert queue.fail(iid, "timeout", permanent=False, reason="fetch_error", max_attempts=3) == FAILED
    item = queue.get(iid)
    assert (item.status, item.resume_from, item.attempts) == (FAILED, "fetched", 1)

    [pending] = queue.pending(None, 3)
    resumed = queue.resume(pending)
    assert resumed.status == "fetched"

    queue.fail(iid, "timeout", permanent=False, reason=None, max_attempts=3)
    assert queue.fail(iid, "timeout", permanent=False, reason=None, max_attempts=3) == FAILED_PERMANENT
    assert queue.get(iid).resume_from == "fetched"
    assert queue.pending(None, 3) == []


def test_permanent_failure_skips_retries(queue: Queue):
    iid = queue.enqueue("https://example.com/a", "cli").item_id
    assert queue.fail(iid, "404", permanent=True, reason="http_404", max_attempts=3) == FAILED_PERMANENT
    assert queue.retry(iid) == 1
    item = queue.get(iid)
    assert item.status == FAILED and item.attempts == 0
    assert queue.resume(item).status == "queued"


def test_pending_orders_fresh_work_before_retries_and_limits(queue: Queue):
    a = queue.enqueue("https://example.com/a", "cli").item_id
    b = queue.enqueue("https://example.com/b", "cli").item_id
    c = queue.enqueue("https://example.com/c", "cli").item_id
    queue.fail(a, "x", permanent=False, reason=None, max_attempts=3)
    queue.advance(c, "done")
    assert [i.id for i in queue.pending(None, 3)] == [b, a]
    assert [i.id for i in queue.pending(1, 3)] == [b]


def test_payload_and_state_round_trip(queue: Queue):
    iid = queue.enqueue("https://example.com/a", "cli").item_id
    queue.save_payload(iid, {"title": "ñ", "n": 1})
    assert queue.load_payload(iid) == {"title": "ñ", "n": 1}
    queue.drop_payload(iid)
    assert queue.load_payload(iid) is None
    queue.set_state("k", "v")
    assert queue.get_state("k") == "v"
    queue.set_state("k", None)
    assert queue.get_state("k", "d") == "d"


def test_schema_survives_reopen(settings):
    q = Queue(settings.db_path)
    q.enqueue("https://example.com/a", "cli")
    q.close()
    q2 = Queue(settings.db_path)
    assert q2.counts()["queued"] == 1
    q2.close()
