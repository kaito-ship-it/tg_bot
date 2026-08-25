from datetime import datetime, timedelta, timezone

from app.processing_queue import ProcessingQueue


def test_queue_claims_out_of_order_messages_in_telegram_order(tmp_path) -> None:
    queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    queue.enqueue([105])
    queue.enqueue([101])
    queue.enqueue([103])

    first = queue.claim_next_ready()
    assert first is not None
    assert first.message_ids == [101]
    queue.mark_completed(first.job_id)

    second = queue.claim_next_ready()
    assert second is not None
    assert second.message_ids == [103]


def test_default_settle_window_collects_a_short_burst_before_claiming(tmp_path) -> None:
    queue = ProcessingQueue(tmp_path / "queue")
    queue.enqueue([110])

    assert queue.claim_next_ready() is None
    ready = queue.claim_next_ready(
        now=datetime.now(timezone.utc) + timedelta(seconds=1)
    )
    assert ready is not None
    assert ready.message_ids == [110]


def test_queue_survives_new_instance_and_recovers_processing_job(tmp_path) -> None:
    queue_dir = tmp_path / "queue"
    first_queue = ProcessingQueue(queue_dir, settle_seconds=0)
    original = first_queue.enqueue([200, 201], grouped_id=999)
    claimed = first_queue.claim_next_ready()
    assert claimed is not None
    assert claimed.job_id == original.job_id
    assert claimed.status == "processing"

    restarted_queue = ProcessingQueue(queue_dir, settle_seconds=0)
    assert restarted_queue.recover_interrupted() == 1
    recovered = restarted_queue.claim_next_ready()

    assert recovered is not None
    assert recovered.message_ids == [200, 201]
    assert recovered.draft_id == original.draft_id
    assert recovered.attempts == 1


def test_queue_retries_then_marks_job_failed(tmp_path) -> None:
    queue = ProcessingQueue(
        tmp_path / "queue", max_attempts=3, settle_seconds=0
    )
    job = queue.enqueue([300])

    for expected_attempt in range(1, 4):
        claimed = queue.claim_next_ready(
            now=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        assert claimed is not None
        assert claimed.attempts == expected_attempt
        updated = queue.mark_attempt_failed(job.job_id, "temporary error")
        assert updated is not None

    assert updated.status == "failed"
    assert updated.next_attempt_at is None
    assert queue.stats()["failed"] == 1


def test_enqueue_is_idempotent_and_preserves_draft_id(tmp_path) -> None:
    queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)

    first = queue.enqueue([401])
    second = queue.enqueue([401])

    assert second.job_id == first.job_id
    assert second.draft_id == first.draft_id
    assert queue.stats()["pending"] == 1


def test_album_parts_are_merged_before_processing(tmp_path) -> None:
    queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    queue.enqueue([501], grouped_id=777)
    merged = queue.enqueue([502, 503], grouped_id=777)

    assert merged.message_ids == [501, 502, 503]
    assert queue.stats()["pending"] == 1


def test_retry_delay_does_not_block_a_later_news_item(tmp_path) -> None:
    queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    first = queue.enqueue([601])
    queue.enqueue([602])

    claimed = queue.claim_next_ready()
    assert claimed is not None
    assert claimed.job_id == first.job_id
    queue.mark_attempt_failed(first.job_id, "temporary error")

    next_job = queue.claim_next_ready()
    assert next_job is not None
    assert next_job.message_ids == [602]
