import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from ray._private.telemetry.open_telemetry_metric_recorder import (
    OpenTelemetryMetricRecorder,
)
from ray.core.generated import (
    events_base_event_pb2,
)
from ray.core.generated.events_base_event_pb2 import RayEvent


@dataclass
class _ConsumerState:
    # Index of the next event to be consumed by this consumer
    cursor_index: int
    # Name of the consumer used for metric naming
    consumer_name: str
    # Metric name for tracking evicted events for this consumer
    evicted_events_metric_name: str
    # Event to signal that there are new events to consume
    has_new_events_to_consume: asyncio.Event


class MultiConsumerEventBuffer:
    """A buffer which allows adding one event at a time and consuming events in batches.
    Supports multiple consumers, each with their own cursor index. Tracks the number of events evicted for each consumer.

    Buffer is not thread-safe but is asyncio-friendly. All operations must be called from within the same event loop.

    Arguments:
        max_size: Maximum number of events to store in the buffer.
        max_batch_size: Maximum number of events to return in a batch when calling wait_for_batch.
        common_metric_tags: Tags to add to all metrics.
    """

    def __init__(
        self,
        max_size: int,
        max_batch_size: int,
        common_metric_tags: Optional[Dict[str, str]] = None,
    ):
        self._buffer = deque(maxlen=max_size)
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._consumers: Dict[str, _ConsumerState] = {}

        self._max_batch_size = max_batch_size

        self._common_metrics_tags = common_metric_tags or {}
        self._metric_recorder = OpenTelemetryMetricRecorder()

    async def add_event(self, event: events_base_event_pb2.RayEvent) -> None:
        """Add an event to the buffer.

        If the buffer is full, the oldest event is dropped.
        """
        async with self._lock:
            dropped_event = None
            if len(self._buffer) >= self._max_size:
                dropped_event = self._buffer.popleft()
            self._buffer.append(event)

            for _, consumer_state in self._consumers.items():
                # Signal the consumer that there are new events to consume
                consumer_state.has_new_events_to_consume.set()

                if dropped_event is None:
                    continue

                # Update consumer cursor index and evicted events metric if an event was dropped
                if consumer_state.cursor_index == 0:
                    # The dropped event was the next event this consumer would have consumed, publish eviction metric
                    self._metric_recorder.set_metric_value(
                        consumer_state.evicted_events_metric_name,
                        {
                            **self._common_metrics_tags,
                            "event_type": RayEvent.EventType.Name(
                                dropped_event.event_type
                            ),
                        },
                        1,
                    )
                else:
                    # The dropped event was already consumed by the consumer, so we need to adjust the cursor
                    consumer_state.cursor_index -= 1

    async def wait_for_batch(
        self, consumer_id: str, timeout_seconds: float = 1.0
    ) -> List[events_base_event_pb2.RayEvent]:
        """Wait for batch respecting self.max_batch_size and timeout_seconds.

        Returns a batch of up to self.max_batch_size items. Waits for up to
        timeout_seconds after receiving the first event that will be in
        the next batch. After the timeout, returns as many items as are ready.

        Always returns a batch with at least one item - will block
        indefinitely until an item comes in.

        Arguments:
            consumer_id: id of the consumer consuming the batch
            timeout_seconds: maximum time to wait for a batch

        Returns:
            A list of up to max_batch_size events ready for consumption.
            The list always contains at least one event.
        """
        max_batch = self._max_batch_size
        async with self._lock:
            consumer_state = self._consumers.get(consumer_id)
            if consumer_state is None:
                raise KeyError(f"unknown consumer '{consumer_id}'")
            has_events_to_consume = consumer_state.has_new_events_to_consume

        # Phase 1: read the first event, wait indefinitely until there is at least one event to consume
        # Wait inside a loop to deal with spurious wakeups.
        while True:
            # Wait outside the lock to avoid deadlocks
            await has_events_to_consume.wait()
            async with self._lock:
                if consumer_state.cursor_index < len(self._buffer):
                    # Add the first event to the batch
                    event = self._buffer[consumer_state.cursor_index]
                    consumer_state.cursor_index += 1
                    batch = [event]
                    break

                # There are no new events to consume, clear the event and wait for it to be set again
                has_events_to_consume.clear()

        # Phase 2: add items to the batch up to timeout or until full
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while len(batch) < max_batch:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            async with self._lock:
                # Drain whatever is available
                while len(batch) < max_batch and consumer_state.cursor_index < len(
                    self._buffer
                ):
                    batch.append(self._buffer[consumer_state.cursor_index])
                    consumer_state.cursor_index += 1

                if len(batch) >= max_batch:
                    break

                # There is still room in the batch, but no new events to consume, clear the event and wait for it to be set again
                has_events_to_consume.clear()
            try:
                await asyncio.wait_for(has_events_to_consume.wait(), remaining)
            except asyncio.TimeoutError:
                # Timeout, return the current batch
                break

        return batch

    async def register_consumer(self, consumer_name: str) -> str:
        """Register a new consumer with a name.

        Arguments:
            consumer_name: A unique name for the consumer.

        Returns:
            Id of the consumer, used to identify the consumer in other methods.
        """
        async with self._lock:
            consumer_id = str(uuid.uuid4())
            _metric_prefix = "event_aggregator_agent"
            evicted_events_metric_name = (
                f"{_metric_prefix}_{consumer_name}_queue_dropped_events_total"
            )

            # Register the counter metric
            self._metric_recorder.register_counter_metric(
                evicted_events_metric_name,
                "Total number of events dropped because the publish/buffer queue was full.",
            )

            self._consumers[consumer_id] = _ConsumerState(
                cursor_index=0,
                consumer_name=consumer_name,
                evicted_events_metric_name=evicted_events_metric_name,
                has_new_events_to_consume=asyncio.Event(),
            )
            return consumer_id

    async def size(self) -> int:
        """Get total number of events in the buffer. Does not take consumer cursors into account."""
        async with self._lock:
            return len(self._buffer)
