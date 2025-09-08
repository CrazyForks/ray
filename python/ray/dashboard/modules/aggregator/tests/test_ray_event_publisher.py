import uuid
import pytest
import sys
import asyncio
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

from ray._common.test_utils import async_wait_for_condition
from ray.dashboard.modules.aggregator.publisher.ray_event_publisher import (
    RayEventPublisher,
    NoopPublisher,
)
from ray.dashboard.modules.aggregator.publisher.async_publisher_client import (
    PublishStats,
    PublisherClientInterface,
)
from ray.dashboard.modules.aggregator.multi_consumer_event_buffer import (
    MultiConsumerEventBuffer,
)
from ray.core.generated import events_base_event_pb2
from typing import Optional
from google.protobuf.timestamp_pb2 import Timestamp


class MockPublisherClient(PublisherClientInterface):
    """Test implementation of PublisherClientInterface."""

    def __init__(
        self,
        publish_result: Optional[PublishStats] = None,
        batch_size: int = 1,
        side_effect=None,
    ):
        self.publish_result = publish_result or PublishStats(True, 1, 0)
        self.batch_size = batch_size
        self.publish_calls = []
        self._side_effect = side_effect

    async def publish(self, batch) -> PublishStats:
        self.publish_calls.append(batch)
        if self._side_effect is not None:
            if asyncio.iscoroutinefunction(self._side_effect):
                return await self._side_effect(batch)
            return self._side_effect(batch)
        return self.publish_result

    def count_num_events_in_batch(self, batch) -> int:
        return self.batch_size

    async def close(self) -> None:
        pass


@pytest.fixture
def base_kwargs():
    """Common kwargs for publisher initialization."""
    return {
        "name": "test",
        "max_retries": 2,
        "initial_backoff": 0,
        "max_backoff": 0,
        "jitter_ratio": 0,
        "enable_publisher_stats": True,
    }


class TestRayEventPublisher:
    """Test the main RayEventsPublisher functionality."""

    @pytest.mark.asyncio
    async def test_publish_with_retries_failure_then_success(self, base_kwargs):
        """Test publish that fails then succeeds."""
        call_count = {"count": 0}

        # fail the first publish call but succeed on retry
        def side_effect(batch):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return PublishStats(False, 0, 0)
            return PublishStats(True, 1, 0)

        client = MockPublisherClient(side_effect=side_effect)
        event_buffer = MultiConsumerEventBuffer(max_size=10, max_batch_size=10)
        publisher = RayEventPublisher(
            name=base_kwargs["name"] + str(uuid.uuid4()),
            publish_client=client,
            event_buffer=event_buffer,
            max_retries=base_kwargs["max_retries"],
            initial_backoff=base_kwargs["initial_backoff"],
            max_backoff=base_kwargs["max_backoff"],
            jitter_ratio=base_kwargs["jitter_ratio"],
        )

        task = asyncio.create_task(publisher.run_forever())
        try:
            # ensure consumer is registered
            await publisher.wait_until_running(2.0)
            # Enqueue one event into buffer
            e = events_base_event_pb2.RayEvent(
                event_id=b"1",
                source_type=events_base_event_pb2.RayEvent.SourceType.CORE_WORKER,
                event_type=events_base_event_pb2.RayEvent.EventType.TASK_DEFINITION_EVENT,
                timestamp=Timestamp(seconds=123, nanos=0),
                severity=events_base_event_pb2.RayEvent.Severity.INFO,
                message="hello",
            )
            await event_buffer.add_event(e)

            # wait for two publish attempts (failure then success)
            await async_wait_for_condition(lambda: len(client.publish_calls) == 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_publish_with_retries_max_retries_exceeded(self, base_kwargs):
        """Test publish that fails all retries and records failed events."""
        client = MockPublisherClient(publish_result=PublishStats(False, 0, 0))
        event_buffer = MultiConsumerEventBuffer(max_size=10, max_batch_size=10)
        publisher = RayEventPublisher(
            name=base_kwargs["name"] + str(uuid.uuid4()),
            publish_client=client,
            event_buffer=event_buffer,
            max_retries=2,  # override to finite retries
            initial_backoff=0,
            max_backoff=0,
            jitter_ratio=0,
        )

        task = asyncio.create_task(publisher.run_forever())
        try:
            # ensure consumer is registered
            await publisher.wait_until_running(2.0)
            e = events_base_event_pb2.RayEvent(
                event_id=b"1",
                source_type=events_base_event_pb2.RayEvent.SourceType.CORE_WORKER,
                event_type=events_base_event_pb2.RayEvent.EventType.TASK_DEFINITION_EVENT,
                timestamp=Timestamp(seconds=123, nanos=0),
                severity=events_base_event_pb2.RayEvent.Severity.INFO,
                message="hello",
            )
            await event_buffer.add_event(e)

            # wait for publish attempts (initial + 2 retries)
            await async_wait_for_condition(lambda: len(client.publish_calls) == 3)
            assert len(client.publish_calls) == 3
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestNoopPublisher:
    """Test no-op publisher implementation."""

    @pytest.mark.asyncio
    async def test_all_methods_noop(self):
        """Test that run_forever can be cancelled and metrics return expected values."""
        publisher = NoopPublisher()

        # Start and cancel run_forever
        task = asyncio.create_task(publisher.run_forever())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
