"""知识库上传的 Kafka 传输层。

只搬运 ``{kind, task_id}``。进度、取消、准入和处理锁仍在 Redis。
客户端懒加载：inline / stream 模式不连接 broker，也无需安装包已导入。
每个 worker 线程持有自己的 Consumer（confluent-kafka 消费者不能跨线程）。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from loguru import logger

from app.settings import settings

_producer: Any = None
_producer_guard = threading.Lock()
_produce_lock = threading.Lock()
_topics_ready = False
_topics_lock = threading.Lock()
_local = threading.local()

# 单条任务最长 900s；处理期间不能 poll，间隔必须盖过超时，否则组重平衡。
_MIN_POLL_INTERVAL_MS = 1_200_000


def bootstrap_servers() -> str:
    return str(getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "") or "127.0.0.1:9094").strip()


def topic() -> str:
    return str(getattr(settings, "KAFKA_UPLOAD_TOPIC", "") or "kb-upload-jobs").strip()


def dead_topic() -> str:
    return str(getattr(settings, "KAFKA_UPLOAD_DEAD_TOPIC", "") or "kb-upload-dead").strip()


def consumer_group() -> str:
    return str(getattr(settings, "KB_UPLOAD_CONSUMER_GROUP", "") or "kb_upload_workers").strip()


def partitions() -> int:
    return max(1, int(getattr(settings, "KAFKA_UPLOAD_PARTITIONS", 16) or 16))


def max_poll_interval_ms() -> int:
    """处理中不 poll；间隔须大于单任务超时，避免 Kafka 把活着的消费者踢出组。"""
    timeout = max(1, int(getattr(settings, "KB_UPLOAD_TASK_TIMEOUT_SECONDS", 900) or 900))
    return max(_MIN_POLL_INTERVAL_MS, (timeout + 180) * 1000)


def _libs() -> tuple[Any, Any, Any, Any, Any] | None:
    try:
        import confluent_kafka
        from confluent_kafka import KafkaError, KafkaException
        from confluent_kafka.admin import AdminClient, NewTopic
    except ImportError:
        logger.error("未安装 confluent-kafka，无法使用 Kafka 上传队列")
        return None
    return confluent_kafka, KafkaError, KafkaException, AdminClient, NewTopic


def _already_exists(exc: BaseException, kafka_error: Any, kafka_exception: Any) -> bool:
    if isinstance(exc, kafka_exception) and exc.args:
        err = exc.args[0]
        code = err.code() if hasattr(err, "code") else None
        if code == kafka_error.TOPIC_ALREADY_EXISTS:
            return True
    return "TOPIC_ALREADY_EXISTS" in str(exc)


def ensure_topics() -> bool:
    """幂等创建上传主题与死信主题。broker 不可用时返回 False，下次再试。"""
    global _topics_ready
    if _topics_ready:
        return True
    with _topics_lock:
        if _topics_ready:
            return True
        libs = _libs()
        if libs is None:
            return False
        _ck, kafka_error, kafka_exception, admin_client, new_topic = libs
        try:
            admin = admin_client({"bootstrap.servers": bootstrap_servers()})
            specs = [
                new_topic(
                    topic(),
                    num_partitions=partitions(),
                    replication_factor=1,
                    config={"retention.ms": str(7 * 24 * 3600 * 1000), "min.insync.replicas": "1"},
                ),
                new_topic(
                    dead_topic(),
                    num_partitions=1,
                    replication_factor=1,
                    config={"retention.ms": str(7 * 24 * 3600 * 1000), "min.insync.replicas": "1"},
                ),
            ]
            futures = admin.create_topics(specs, request_timeout=15)
            for name, fut in futures.items():
                try:
                    fut.result(timeout=20)
                    logger.info("Kafka 主题已就绪 topic={}", name)
                except Exception as e:  # noqa: BLE001
                    if _already_exists(e, kafka_error, kafka_exception):
                        continue
                    logger.warning("Kafka 创建主题失败 topic={}: {}", name, e)
                    return False
        except Exception as e:  # noqa: BLE001
            logger.warning("Kafka 主题初始化失败: {}", e)
            return False
        _topics_ready = True
        return True


def _get_producer() -> Any | None:
    global _producer
    if _producer is not None:
        return _producer
    with _producer_guard:
        if _producer is not None:
            return _producer
        libs = _libs()
        if libs is None:
            return None
        ck = libs[0]
        _producer = ck.Producer(
            {
                "bootstrap.servers": bootstrap_servers(),
                "acks": "all",
                "enable.idempotence": True,
                "client.id": "kb-upload-producer",
                "message.timeout.ms": 10000,
                "socket.timeout.ms": 10000,
            }
        )
        return _producer


def produce_json(payload: dict[str, Any], *, topic_name: str | None = None) -> bool:
    """同步写入一条 JSON。失败返回 False（不抛），由调用方回滚深度计数。"""
    if not ensure_topics():
        return False
    producer = _get_producer()
    if producer is None:
        return False
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    key = str(payload.get("task_id") or "").encode("utf-8")
    dest = topic_name or topic()
    errors: list[Any] = []

    def _on_delivery(err: Any, _msg: Any) -> None:
        if err is not None:
            errors.append(err)

    try:
        with _produce_lock:
            producer.produce(dest, key=key, value=body, callback=_on_delivery)
            leftover = producer.flush(10)
        if leftover or errors:
            logger.warning(
                "Kafka 入队未确认 topic={} leftover={} err={}", dest, leftover, errors[:1]
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Kafka 入队失败 topic={}: {}", dest, e)
        return False


def _consumer(name: str) -> Any | None:
    current = getattr(_local, "consumer", None)
    if current is not None:
        return current
    libs = _libs()
    if libs is None:
        return None
    ck = libs[0]
    consumer = ck.Consumer(
        {
            "bootstrap.servers": bootstrap_servers(),
            "group.id": consumer_group(),
            "client.id": name or "kb-worker",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": max_poll_interval_ms(),
            "session.timeout.ms": 45000,
            "socket.timeout.ms": 10000,
        }
    )
    consumer.subscribe([topic()])
    _local.consumer = consumer
    return consumer


def poll_one(consumer_name: str, block_ms: int) -> tuple[Any, dict[str, Any]] | None:
    """阻塞拉取一条。返回 (位点令牌, payload)；超时或错误返回 None。"""
    if not ensure_topics():
        return None
    consumer = _consumer(consumer_name)
    if consumer is None:
        return None
    libs = _libs()
    if libs is None:
        return None
    kafka_error = libs[1]
    try:
        msg = consumer.poll(max(0.001, int(block_ms) / 1000.0))
    except Exception as e:  # noqa: BLE001
        logger.warning("Kafka 出队失败: {}", e)
        return None
    if msg is None:
        return None
    err = msg.error()
    if err is not None:
        code = err.code() if hasattr(err, "code") else None
        if code == kafka_error._PARTITION_EOF:
            return None
        logger.warning("Kafka 出队错误: {}", err)
        return None
    raw = msg.value()
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (TypeError, ValueError, AttributeError):
        logger.warning("Kafka 消息不是 JSON，将确认丢弃")
        return msg, {}
    if not isinstance(payload, dict):
        return msg, {}
    return msg, payload


def commit(token: Any) -> bool:
    """处理结束后提交该消息位点。失败返回 False，调用方不得减少深度计数。"""
    consumer = getattr(_local, "consumer", None)
    if consumer is None or token is None:
        logger.warning("Kafka 提交位点失败：当前线程没有消费者或令牌为空")
        return False
    try:
        consumer.commit(message=token, asynchronous=False)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Kafka 提交位点失败: {}", e)
        return False


def close_consumer() -> None:
    """线程退出时关闭本线程消费者，便于消费组尽快重平衡。"""
    consumer = getattr(_local, "consumer", None)
    if consumer is None:
        return
    try:
        consumer.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("Kafka 消费者关闭失败: {}", e)
    finally:
        _local.consumer = None
