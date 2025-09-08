import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
import logging
from ray._private.gcs_utils import create_gcs_channel
from ray.dashboard.modules.aggregator.multi_consumer_event_buffer import (
    MultiConsumerEventBuffer,
)
from ray.dashboard.modules.aggregator.publisher.async_publisher_client import (
    AsyncHttpPublisherClient,
    AsyncGCSPublisherClient,
)
from ray.dashboard.modules.aggregator.publisher.ray_event_publisher import (
    NoopPublisher,
    RayEventPublisher,
)
from ray.dashboard.modules.aggregator.task_metadata_buffer import TaskMetadataBuffer

from ray.util.metrics import Counter

import ray
from ray._private import ray_constants
import ray.dashboard.utils as dashboard_utils
import ray.dashboard.consts as dashboard_consts
from ray.core.generated import (
    events_event_aggregator_service_pb2,
    events_event_aggregator_service_pb2_grpc,
    events_base_event_pb2,
    gcs_service_pb2_grpc,
)

logger = logging.getLogger(__name__)

# Environment variables for the aggregator agent
env_var_prefix = "RAY_DASHBOARD_AGGREGATOR_AGENT"
# Max number of threads for the thread pool executor handling CPU intensive tasks
THREAD_POOL_EXECUTOR_MAX_WORKERS = ray_constants.env_integer(
    f"{env_var_prefix}_THREAD_POOL_EXECUTOR_MAX_WORKERS", 1
)
# Interval to check the main thread liveness
CHECK_MAIN_THREAD_LIVENESS_INTERVAL_SECONDS = ray_constants.env_float(
    f"{env_var_prefix}_CHECK_MAIN_THREAD_LIVENESS_INTERVAL_SECONDS", 0.1
)
# Maximum size of the event buffer in the aggregator agent
MAX_EVENT_BUFFER_SIZE = ray_constants.env_integer(
    f"{env_var_prefix}_MAX_EVENT_BUFFER_SIZE", 1000000
)
# Maximum number of events to send in a single batch to the destination
MAX_EVENT_SEND_BATCH_SIZE = ray_constants.env_integer(
    f"{env_var_prefix}_MAX_EVENT_SEND_BATCH_SIZE", 10000
)
# Address of the external service to send events with format of "http://<ip>:<port>"
EVENTS_EXPORT_ADDR = os.environ.get(f"{env_var_prefix}_EVENTS_EXPORT_ADDR", "")
# Event filtering configurations
# Comma-separated list of event types that are allowed to be exposed to external services
# Valid values: TASK_DEFINITION_EVENT, TASK_EXECUTION_EVENT, ACTOR_TASK_DEFINITION_EVENT, ACTOR_TASK_EXECUTION_EVENT
# The list of all supported event types can be found in src/ray/protobuf/events_base_event.proto (EventType enum)
# By default TASK_PROFILE_EVENT is not exposed to external services
DEFAULT_EXPOSABLE_EVENT_TYPES = (
    "TASK_DEFINITION_EVENT,TASK_EXECUTION_EVENT,"
    "ACTOR_TASK_DEFINITION_EVENT,ACTOR_TASK_EXECUTION_EVENT,"
    "DRIVER_JOB_DEFINITION_EVENT,DRIVER_JOB_EXECUTION_EVENT"
)
EXPOSABLE_EVENT_TYPES = os.environ.get(
    f"{env_var_prefix}_EXPOSABLE_EVENT_TYPES", DEFAULT_EXPOSABLE_EVENT_TYPES
)
# flag to enable publishing events to the external HTTP service
PUBLISH_EVENTS_TO_EXTERNAL_HTTP_SVC = ray_constants.env_bool(
    f"{env_var_prefix}_PUBLISH_EVENTS_TO_EXTERNAL_HTTP_SVC", True
)
# flag to enable publishing events to GCS
PUBLISH_EVENTS_TO_GCS = ray_constants.env_bool(
    f"{env_var_prefix}_PUBLISH_EVENTS_TO_GCS", False
)

class AggregatorAgent(
    dashboard_utils.DashboardAgentModule,
    events_event_aggregator_service_pb2_grpc.EventAggregatorServiceServicer,
):
    """
    AggregatorAgent is a dashboard agent module that collects events sent with
    gRPC from other components, buffers them, and periodically sends them to GCS and
    an external service with HTTP POST requests for further processing or storage
    """

    def __init__(self, dashboard_agent) -> None:
        super().__init__(dashboard_agent)
        self._ip = dashboard_agent.ip
        self._pid = os.getpid()

        # common prometheus labels for aggregator-owned metrics
        self._common_tags = {
            "ip": self._ip,
            "pid": str(self._pid),
            "Version": ray.__version__,
            "Component": "event_aggregator_agent",
            "SessionName": self.session_name,
        }

        self._event_buffer = MultiConsumerEventBuffer(
            max_size=MAX_EVENT_BUFFER_SIZE,
            max_batch_size=MAX_EVENT_SEND_BATCH_SIZE,
            common_metric_tags=self._common_tags,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=THREAD_POOL_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="event_aggregator_agent_executor",
        )

        # Task metadata buffer accumulates dropped task attempts for GCS publishing
        self._task_metadata_buffer = TaskMetadataBuffer()

        self._lock = asyncio.Lock()
        self._events_export_addr = (
            dashboard_agent.events_export_addr or EVENTS_EXPORT_ADDR
        )

        self._exposable_event_types = {
            event_type.strip()
            for event_type in EXPOSABLE_EVENT_TYPES.split(",")
            if event_type.strip()
        }

        self._event_processing_enabled = False
        if PUBLISH_EVENTS_TO_EXTERNAL_HTTP_SVC and self._events_export_addr:
            logger.info(
                f"Publishing events to external HTTP service is enabled. events_export_addr: {self._events_export_addr}"
            )
            self._event_processing_enabled = True
            self._http_endpoint_publisher = RayEventPublisher(
                name="http_publisher",
                publish_client=AsyncHttpPublisherClient(
                    endpoint=self._events_export_addr,
                    executor=self._executor,
                    events_filter_fn=self._can_expose_event,
                ),
                event_buffer=self._event_buffer,
                common_metric_tags=self._common_tags,
            )
        else:
            logger.info(
                f"Event HTTP target is not enabled or publishing events to external HTTP service is disabled. Skipping sending events to external HTTP service. events_export_addr: {self._events_export_addr}"
            )
            self._http_endpoint_publisher = NoopPublisher()

        if PUBLISH_EVENTS_TO_GCS:
            logger.info("Publishing events to GCS is enabled")
            self._event_processing_enabled = True
            self._async_gcs_channel = create_gcs_channel(self.gcs_address, aio=True)
            self._async_gcs_event_stub = (
                gcs_service_pb2_grpc.RayEventExportGcsServiceStub(
                    self._async_gcs_channel
                )
            )
            self._gcs_publisher = RayEventsPublisher(
                name="gcs_publisher",
                publish_client=AsyncGCSPublisherClient(
                    gcs_stub=self._async_gcs_event_stub
                ),
                event_buffer=self._event_buffer,
                task_metadata_buffer=self._task_metadata_buffer,
            )
        else:
            logger.info("Publishing events to GCS is disabled")
            self._gcs_publisher = NoopPublisher()
 
        # Metrics
        _metrics_prefix = "event_aggregator_agent"
        self._events_received = Counter(
            f"{_metrics_prefix}_events_received_total",
            "Total number of events received via AddEvents gRPC.",
            tag_keys=tuple(dashboard_consts.COMPONENT_METRICS_TAG_KEYS),
        )
        self._events_received.set_default_tags(self._common_tags)
        self._events_failed_to_add_to_aggregator = Counter(
            f"{_metrics_prefix}_events_buffer_add_failures_total",
            "Total number of events that failed to be added to the event buffer.",
            tag_keys=tuple(dashboard_consts.COMPONENT_METRICS_TAG_KEYS),
        )
        self._events_failed_to_add_to_aggregator.set_default_tags(self._common_tags)

    async def AddEvents(self, request, context) -> None:
        """
        gRPC handler for adding events to the event aggregator. Receives events from the
        request and adds them to the event buffer.
        """
        if not self._event_processing_enabled:
            return events_event_aggregator_service_pb2.AddEventsReply()

        events_data = request.events_data
        await self._task_metadata_buffer.merge(events_data.task_events_metadata)
        for event in events_data.events:
            self._events_received.inc(value=1)
            try:
                await self._event_buffer.add_event(event)
            except Exception as e:
                logger.error(
                    f"Failed to add event with id={event.event_id.decode()} to buffer. "
                    "Error: %s",
                    e,
                )
                self._events_failed_to_add_to_aggregator.inc(value=1)

        return events_event_aggregator_service_pb2.AddEventsReply()

    def _can_expose_event(self, event) -> bool:
        """
        Check if an event should be allowed to be sent to external services.
        """
        return (
            events_base_event_pb2.RayEvent.EventType.Name(event.event_type)
            in self._exposable_event_types
        )

    async def run(self, server) -> None:
        if server:
            events_event_aggregator_service_pb2_grpc.add_EventAggregatorServiceServicer_to_server(
                self, server
            )

        await asyncio.gather(
            self._http_endpoint_publisher.run_forever(),
            self._gcs_publisher.run_forever(),
        )

        self._executor.shutdown()
        self._async_gcs_channel.close()

    @staticmethod
    def is_minimal_module() -> bool:
        return False
