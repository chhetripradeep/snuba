import logging
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from typing import Any, Optional, Sequence

import click
from arroyo import Topic
from arroyo.backends.kafka import KafkaConsumer, KafkaProducer
from arroyo.processing import StreamProcessor
from arroyo.processing.strategies.batching import BatchProcessingStrategyFactory
from arroyo.synchronized import SynchronizedConsumer

from snuba import environment, settings
from snuba.datasets.factory import DATASET_NAMES, enforce_table_writer, get_dataset
from snuba.environment import setup_logging, setup_sentry
from snuba.redis import redis_client
from snuba.subscriptions.codecs import SubscriptionTaskResultEncoder
from snuba.subscriptions.consumer import TickConsumer
from snuba.subscriptions.data import PartitionId
from snuba.subscriptions.scheduler import SubscriptionScheduler
from snuba.subscriptions.store import RedisSubscriptionDataStore
from snuba.subscriptions.worker import SubscriptionWorker
from snuba.utils.metrics.wrapper import MetricsWrapper
from snuba.utils.streams.configuration_builder import (
    build_kafka_consumer_configuration,
    build_kafka_producer_configuration,
)
from snuba.utils.streams.encoding import ProducerEncodingWrapper
from snuba.utils.streams.metrics_adapter import StreamMetricsAdapter

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--dataset",
    "dataset_name",
    default="events",
    type=click.Choice(DATASET_NAMES),
    help="The dataset to target",
)
@click.option("--topic")
@click.option("--partitions", type=int)
@click.option("--commit-log-topic")
@click.option(
    "--commit-log-group",
    "commit_log_groups",
    multiple=True,
    default=["snuba-consumers"],
)
@click.option(
    "--consumer-group",
    default="snuba-subscriptions",
    help="Consumer group use for consuming the dataset source topic.",
)
@click.option(
    "--auto-offset-reset",
    default="error",
    type=click.Choice(["error", "earliest", "latest"]),
    help="Kafka consumer auto offset reset.",
)
@click.option(
    "--bootstrap-server",
    "bootstrap_servers",
    multiple=True,
    help="Kafka bootstrap server(s) to use.",
)
@click.option(
    "--max-batch-size",
    default=settings.DEFAULT_MAX_BATCH_SIZE,
    type=int,
    help="Max number of messages to batch in memory before writing to Kafka.",
)
@click.option(
    "--max-batch-time-ms",
    default=settings.DEFAULT_MAX_BATCH_TIME_MS,
    type=int,
    help="Max length of time to buffer messages in memory before writing to Kafka.",
)
@click.option(
    "--max-query-workers",
    type=int,
    help="Maximum number of worker threads to use for concurrent query execution",
)
@click.option("--schedule-ttl", type=int, default=60 * 5)
@click.option("--result-topic")
@click.option("--log-level", help="Logging level to use.")
@click.option("--delay-seconds", type=int)
def subscriptions(
    *,
    dataset_name: str,
    topic: Optional[str],
    partitions: Optional[int],
    commit_log_topic: Optional[str],
    commit_log_groups: Sequence[str],
    consumer_group: str,
    auto_offset_reset: str,
    bootstrap_servers: Sequence[str],
    max_batch_size: int,
    max_batch_time_ms: int,
    max_query_workers: Optional[int],
    schedule_ttl: int,
    result_topic: Optional[str],
    log_level: Optional[str],
    delay_seconds: Optional[int],
) -> None:
    """Evaluates subscribed queries for a dataset."""

    setup_logging(log_level)
    setup_sentry()

    dataset = get_dataset(dataset_name)

    storage = dataset.get_default_entity().get_writable_storage()
    assert (
        storage is not None
    ), f"Dataset {dataset_name} does not have a writable storage by default."

    loader = enforce_table_writer(dataset).get_stream_loader()
    commit_log_topic_spec = loader.get_commit_log_topic_spec()
    assert commit_log_topic_spec is not None

    result_topic_spec = loader.get_subscription_result_topic_spec()
    assert result_topic_spec is not None

    metrics = MetricsWrapper(
        environment.metrics,
        "subscriptions",
        tags={"group": consumer_group, "dataset": dataset_name},
    )

    consumer = TickConsumer(
        SynchronizedConsumer(
            KafkaConsumer(
                build_kafka_consumer_configuration(
                    loader.get_default_topic_spec().topic,
                    consumer_group,
                    auto_offset_reset=auto_offset_reset,
                    bootstrap_servers=bootstrap_servers,
                ),
            ),
            KafkaConsumer(
                build_kafka_consumer_configuration(
                    commit_log_topic_spec.topic,
                    f"subscriptions-commit-log-{uuid.uuid1().hex}",
                    auto_offset_reset="earliest",
                    bootstrap_servers=bootstrap_servers,
                ),
            ),
            (
                Topic(commit_log_topic)
                if commit_log_topic is not None
                else Topic(commit_log_topic_spec.topic_name)
            ),
            set(commit_log_groups),
        ),
        time_shift=(
            timedelta(seconds=delay_seconds * -1) if delay_seconds is not None else None
        ),
    )

    producer = ProducerEncodingWrapper(
        KafkaProducer(
            build_kafka_producer_configuration(
                loader.get_default_topic_spec().topic,
                bootstrap_servers=bootstrap_servers,
                override_params={
                    "partitioner": "consistent",
                    "message.max.bytes": 50000000,  # 50MB, default is 1MB
                },
            )
        ),
        SubscriptionTaskResultEncoder(),
    )

    executor = ThreadPoolExecutor(max_workers=max_query_workers)
    logger.debug(
        "Starting %r with %s workers...", executor, getattr(executor, "_max_workers", 0)
    )
    metrics.gauge("executor.workers", getattr(executor, "_max_workers", 0))

    with closing(consumer), executor, closing(producer):
        from arroyo import configure_metrics

        configure_metrics(StreamMetricsAdapter(metrics))
        batching_consumer = StreamProcessor(
            consumer,
            (
                Topic(topic)
                if topic is not None
                else Topic(loader.get_default_topic_spec().topic_name)
            ),
            BatchProcessingStrategyFactory(
                SubscriptionWorker(
                    dataset,
                    executor,
                    {
                        index: SubscriptionScheduler(
                            RedisSubscriptionDataStore(
                                redis_client, dataset, PartitionId(index)
                            ),
                            PartitionId(index),
                            cache_ttl=timedelta(seconds=schedule_ttl),
                            metrics=metrics,
                        )
                        for index in range(
                            partitions
                            if partitions is not None
                            else loader.get_default_topic_spec().partitions_number
                        )
                    },
                    producer,
                    Topic(result_topic)
                    if result_topic is not None
                    else Topic(result_topic_spec.topic_name),
                    metrics,
                ),
                max_batch_size,
                max_batch_time_ms,
            ),
        )

        def handler(signum: int, frame: Optional[Any]) -> None:
            batching_consumer.signal_shutdown()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

        batching_consumer.run()
