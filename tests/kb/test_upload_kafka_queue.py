"""KB 上传队列 Kafka 后端：限深、位点提交、死信、worker 分派。不连接真实 Kafka。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.kb import kafka_queue, kb_job
from app.worker import runner


def _payload(task_id: str = "t1") -> dict:
    return {"kind": "kb_upload", "task_id": task_id}


class _Producer:
    def __init__(self, *, leftover: int = 0, err: object | None = None) -> None:
        self.leftover = leftover
        self.err = err
        self.sent: list[tuple] = []

    def produce(self, topic: str, key: bytes | None = None, value: bytes | None = None, callback=None):
        self.sent.append((topic, key, value))
        if callback is not None:
            callback(self.err, None)

    def flush(self, _timeout: float) -> int:
        return self.leftover


class QueueBackendTests(unittest.TestCase):
    def test_kafka_backend_is_accepted(self):
        with mock.patch.object(kb_job.settings, "KB_UPLOAD_QUEUE_BACKEND", "kafka", create=True):
            self.assertEqual(kb_job.queue_backend(), "kafka")


class KafkaEnqueueTests(unittest.TestCase):
    def test_enqueue_produces_after_reserving_depth(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kb_job.cache, "incr_if_below", return_value=1
        ) as inc, mock.patch.object(kafka_queue, "produce_json", return_value=True) as prod, mock.patch.object(
            kb_job.cache, "decr_floor"
        ) as dec:
            self.assertTrue(kb_job.enqueue_task("t1"))
        inc.assert_called_once()
        prod.assert_called_once_with(_payload())
        dec.assert_not_called()

    def test_enqueue_releases_depth_when_produce_fails(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kb_job.cache, "incr_if_below", return_value=1
        ), mock.patch.object(kafka_queue, "produce_json", return_value=False), mock.patch.object(
            kb_job.cache, "decr_floor"
        ) as dec:
            self.assertFalse(kb_job.enqueue_task("t1"))
        dec.assert_called_once()

    def test_enqueue_does_not_produce_when_depth_full_or_redis_down(self):
        for reserved in (0, None):
            with self.subTest(reserved=reserved), mock.patch.object(
                kb_job, "queue_backend", return_value="kafka"
            ), mock.patch.object(kb_job.cache, "incr_if_below", return_value=reserved), mock.patch.object(
                kafka_queue, "produce_json"
            ) as prod, mock.patch.object(kb_job.cache, "decr_floor") as dec:
                self.assertFalse(kb_job.enqueue_task("t1"))
            prod.assert_not_called()
            dec.assert_not_called()


class KafkaAckDepthTests(unittest.TestCase):
    def test_ack_decrements_only_after_commit(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kafka_queue, "commit", return_value=True
        ), mock.patch.object(kb_job.cache, "decr_floor") as dec:
            kb_job.ack_task("msg")
        dec.assert_called_once()

    def test_ack_keeps_depth_when_commit_fails(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kafka_queue, "commit", return_value=False
        ), mock.patch.object(kb_job.cache, "decr_floor") as dec:
            kb_job.ack_task("msg")
        dec.assert_not_called()

    def test_depth_reads_redis_counter(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kb_job.cache, "get_int", return_value=4
        ) as get:
            self.assertEqual(kb_job.queue_depth(), 4)
        get.assert_called_once_with(kb_job._KAFKA_DEPTH_KEY)

    def test_dequeue_uses_kafka_poll(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kafka_queue, "poll_one", return_value=("msg", _payload())
        ) as poll:
            self.assertEqual(kb_job.dequeue_task(consumer="c1"), ("msg", _payload()))
        poll.assert_called_once()
        self.assertEqual(poll.call_args.args[0], "c1")


class KafkaDeliveryTests(unittest.TestCase):
    def _meta(self, **extra: object) -> dict:
        base = {"task_id": "t1", "user_id": 7, "status": "queued", "deliveries": 0}
        base.update(extra)
        return base

    def test_first_delivery_is_recorded_and_runs(self):
        with mock.patch.object(kb_job.cache, "get_json", return_value=self._meta()), mock.patch.object(
            kb_job, "_update_meta"
        ) as upd, mock.patch.object(kafka_queue, "produce_json") as prod:
            self.assertTrue(kb_job.accept_kafka_delivery("t1"))
        self.assertEqual(upd.call_args.kwargs["deliveries"], 1)
        prod.assert_not_called()

    def test_over_max_dead_letters_and_skips_run(self):
        meta = self._meta(status="running", deliveries=3)
        with mock.patch.object(kb_job.settings, "KB_UPLOAD_MAX_DELIVERIES", 3), mock.patch.object(
            kb_job.cache, "get_json", return_value=meta
        ), mock.patch.object(kb_job, "_update_meta") as upd, mock.patch.object(
            kb_job, "_remove_user_active"
        ) as rm, mock.patch.object(kafka_queue, "produce_json", return_value=True) as prod, mock.patch.object(
            kafka_queue, "dead_topic", return_value="kb-upload-dead"
        ):
            self.assertFalse(kb_job.accept_kafka_delivery("t1"))
        self.assertEqual(upd.call_args.kwargs["status"], "failed")
        self.assertEqual(upd.call_args.kwargs["deliveries"], 4)
        body = prod.call_args.args[0]
        self.assertEqual(body["task_id"], "t1")
        self.assertEqual(body["deliveries"], 4)
        self.assertEqual(prod.call_args.kwargs["topic_name"], "kb-upload-dead")
        rm.assert_called_once_with(7, "t1")

    def test_terminal_meta_skips_without_dead_letter(self):
        with mock.patch.object(
            kb_job.cache, "get_json", return_value=self._meta(status="completed")
        ), mock.patch.object(kb_job, "_update_meta") as upd, mock.patch.object(
            kafka_queue, "produce_json"
        ) as prod:
            self.assertFalse(kb_job.accept_kafka_delivery("t1"))
        upd.assert_not_called()
        prod.assert_not_called()


class KafkaClientTests(unittest.TestCase):
    def tearDown(self) -> None:
        kafka_queue._topics_ready = False
        kafka_queue._producer = None
        kafka_queue._local.consumer = None

    def test_poll_interval_covers_task_timeout(self):
        with mock.patch.object(kafka_queue.settings, "KB_UPLOAD_TASK_TIMEOUT_SECONDS", 900):
            self.assertEqual(kafka_queue.max_poll_interval_ms(), 1_200_000)
        with mock.patch.object(kafka_queue.settings, "KB_UPLOAD_TASK_TIMEOUT_SECONDS", 2000):
            self.assertEqual(kafka_queue.max_poll_interval_ms(), 2_180_000)

    def test_produce_json_flushes_and_reports_failure(self):
        ok = _Producer()
        bad = _Producer(err=RuntimeError("broker down"))
        with mock.patch.object(kafka_queue, "ensure_topics", return_value=True), mock.patch.object(
            kafka_queue, "_get_producer", return_value=ok
        ):
            self.assertTrue(kafka_queue.produce_json(_payload(), topic_name="kb-upload-jobs"))
        self.assertEqual(ok.sent[0][0], "kb-upload-jobs")
        with mock.patch.object(kafka_queue, "ensure_topics", return_value=True), mock.patch.object(
            kafka_queue, "_get_producer", return_value=bad
        ):
            self.assertFalse(kafka_queue.produce_json(_payload()))

    def test_commit_without_consumer_is_false(self):
        self.assertFalse(kafka_queue.commit(object()))

    def test_ensure_topics_accepts_already_exists(self):
        class KafkaError:
            TOPIC_ALREADY_EXISTS = 36

        class KafkaException(Exception):
            def __init__(self) -> None:
                err = mock.Mock()
                err.code.return_value = 36
                super().__init__(err)

        class Fut:
            def __init__(self, exc: BaseException | None = None) -> None:
                self.exc = exc

            def result(self, timeout: float | None = None) -> None:
                if self.exc is not None:
                    raise self.exc

        admin = mock.Mock()
        admin.create_topics.return_value = {
            "kb-upload-jobs": Fut(KafkaException()),
            "kb-upload-dead": Fut(),
        }
        admin_cls = mock.Mock(return_value=admin)
        with mock.patch.object(
            kafka_queue,
            "_libs",
            return_value=(mock.Mock(), KafkaError, KafkaException, admin_cls, mock.Mock()),
        ):
            self.assertTrue(kafka_queue.ensure_topics())
        self.assertTrue(kafka_queue._topics_ready)


class WorkerDispatchTests(unittest.TestCase):
    def test_kafka_reclaim_is_noop(self):
        with mock.patch.object(kb_job, "queue_backend", return_value="kafka"), mock.patch.object(
            kb_job, "reclaim_stale_tasks"
        ) as stream, mock.patch.object(kb_job, "recover_stale_processing") as listed:
            self.assertEqual(runner._reclaim_once("c1"), 0)
        stream.assert_not_called()
        listed.assert_not_called()

    def test_dead_letter_skips_pipeline_but_still_acks(self):
        entry = ("msg", _payload())
        with mock.patch.object(kb_job, "dequeue_task", return_value=entry), mock.patch.object(
            kb_job, "queue_backend", return_value="kafka"
        ), mock.patch.object(kb_job, "accept_kafka_delivery", return_value=False), mock.patch.object(
            kb_job, "run_upload_task_from_source"
        ) as run, mock.patch.object(kb_job, "ack_task") as ack:
            self.assertTrue(runner._run_one("worker-1"))
        run.assert_not_called()
        ack.assert_called_once_with("msg")

    def test_stream_run_does_not_consult_kafka_delivery(self):
        entry = ("7-0", _payload())
        with mock.patch.object(kb_job, "dequeue_task", return_value=entry), mock.patch.object(
            kb_job, "queue_backend", return_value="stream"
        ), mock.patch.object(kb_job, "accept_kafka_delivery") as accept, mock.patch.object(
            kb_job, "run_upload_task_from_source"
        ) as run, mock.patch.object(kb_job, "ack_task"):
            self.assertTrue(runner._run_one("worker-1"))
        accept.assert_not_called()
        run.assert_called_once_with("t1")
