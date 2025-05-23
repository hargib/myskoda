"""MQTT client module for the MySkoda server.

Inspired by https://github.com/YoSmart-Inc/yolink-api/tree/main
"""

import asyncio
import logging
import re
import ssl
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from random import uniform
from typing import Any, cast

import aiomqtt

from .auth.authorization import Authorization
from .const import (
    MQTT_ACCOUNT_EVENT_TOPICS,
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    MQTT_FAST_RETRY,
    MQTT_KEEPALIVE,
    MQTT_MAX_RECONNECT_DELAY,
    MQTT_OPERATION_TOPICS,
    MQTT_RECONNECT_DELAY,
    MQTT_SERVICE_EVENT_TOPICS,
    MQTT_VEHICLE_EVENT_TOPICS,
)
from .event import (
    Event,
    EventAccess,
    EventAccountPrivacy,
    EventAirConditioning,
    EventAuxiliaryHeating,
    EventCharging,
    EventDeparture,
    EventLights,
    EventOdometer,
    EventOperation,
    EventType,
    EventVehicleConnectionStatusUpdate,
    EventVehicleIgnitionStatus,
)
from .models.operation_request import OperationName, OperationRequest, OperationStatus
from .models.service_event import ServiceEvent, ServiceEventWithChargingData
from .models.vehicle_event import VehicleEvent, VehicleEventWithVehicleIgnitionStatusData

_LOGGER = logging.getLogger(__name__)
TOPIC_RE = re.compile("^(.*?)/(.*?)/(.*?)/(.*?)$")
app_uuid = uuid.uuid4()


def _create_ssl_context() -> ssl.SSLContext:
    """Create a SSL context for the MQTT connection."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS)
    context.load_default_certs()
    return context


_SSL_CONTEXT = _create_ssl_context()

background_tasks = set()


class OperationListener:
    """Used to track callbacks to execute for a given OperationName."""

    operation_name: OperationName
    future: asyncio.Future[OperationRequest]

    def __init__(
        self, operation_name: OperationName, future: asyncio.Future[OperationRequest]
    ) -> None:
        self.operation_name = operation_name
        self.future = future


class OperationFailedError(Exception):
    def __init__(self, operation: OperationRequest) -> None:
        op = operation.operation
        error = operation.error_code
        trace = operation.trace_id
        super().__init__(f"Operation {op} with trace {trace} failed: {error}")


class MySkodaMqttClient:
    user_id: str | None
    vehicle_vins: list[str]
    _callbacks: list[Callable[[Event], Coroutine[Any, Any, None]]]
    _operation_listeners: list[OperationListener]

    def __init__(
        self,
        authorization: Authorization,
        hostname: str = MQTT_BROKER_HOST,
        port: int = MQTT_BROKER_PORT,
        enable_ssl: bool = True,
    ) -> None:
        self.authorization = authorization
        self.hostname = hostname
        self.port = port
        self.vehicle_vins = []
        self.enable_ssl = enable_ssl
        self._callbacks = []
        self._operation_listeners = []
        self._listener_task = None
        self._running = False
        self._subscribed = asyncio.Event()
        self._reconnect_delay = MQTT_RECONNECT_DELAY

    async def connect(self, user_id: str, vehicle_vins: list[str]) -> None:
        """Connect to the MQTT broker and listen for messages for the given user_id and VINs."""
        _LOGGER.info("Connecting to MQTT with %s/%s", user_id, vehicle_vins)
        self.user_id = user_id
        self.vehicle_vins = vehicle_vins
        self._listener_task = asyncio.create_task(self._connect_and_listen())
        await self._subscribed.wait()

    async def disconnect(self) -> None:
        """Cancel listener task and set self_running to False, causing the listen loop to end."""
        if self._listener_task is None:
            return
        self._listener_task.cancel()
        self._listener_task = None
        self._running = False

    def subscribe(self, callback: Callable[[Event], Coroutine[Any, Any, None]]) -> None:
        """Listen for events emitted by MySkoda's MQTT broker."""
        self._callbacks.append(callback)

    def wait_for_operation(self, operation_name: OperationName) -> asyncio.Future[OperationRequest]:
        """Wait until the next operation of the specified type completes."""
        _LOGGER.debug("Waiting for operation %s complete.", operation_name)
        future: asyncio.Future[OperationRequest] = asyncio.get_event_loop().create_future()

        self._operation_listeners.append(OperationListener(operation_name, future))

        return future

    async def _connect_and_listen(self) -> None:
        """Connect to the MQTT broker and listen for messages for the given user_id and VINs.

        Reconnect loop based on https://github.com/empicano/aiomqtt/blob/main/docs/reconnection.md.

        Recreate the aiomqtt.Client on every try to get the latest authorization token.

        Passing in pre-created SSLContext (vs 'tls_params=aiomqtt.TLSParameters()') to avoid a
        blocking call in paho.mqtt.client. See https://github.com/w1ll1am23/pyeconet/pull/43.
        """
        _LOGGER.debug("Starting _connect_and_listen")
        self._running = True
        retry_count = 0  # Track the number of retries
        self._reconnect_delay = MQTT_RECONNECT_DELAY  # Initial delay for backoff
        while self._running:
            try:
                # client_id = Id + session_uuid4 + # + random_uuid4
                client_id = "Id" + str(app_uuid) + "#" + str(uuid.uuid4())
                async with aiomqtt.Client(
                    hostname=self.hostname,
                    port=self.port,
                    username="android-app",  # Explicit username from working payload
                    identifier=client_id,
                    password=await self.authorization.get_access_token(),
                    logger=_LOGGER,
                    tls_context=_SSL_CONTEXT if self.enable_ssl else None,
                    keepalive=MQTT_KEEPALIVE,
                    clean_session=True,
                ) as client:
                    _LOGGER.info("Connected to MQTT")
                    _LOGGER.debug("using MQTT client %s", client)
                    for vin in self.vehicle_vins:
                        for topic in MQTT_OPERATION_TOPICS:
                            await client.subscribe(
                                f"{self.user_id}/{vin}/operation-request/{topic}"
                            )
                        for topic in MQTT_SERVICE_EVENT_TOPICS:
                            await client.subscribe(f"{self.user_id}/{vin}/service-event/{topic}")
                        for topic in MQTT_ACCOUNT_EVENT_TOPICS:
                            await client.subscribe(f"{self.user_id}/{vin}/account-event/{topic}")
                        for topic in MQTT_VEHICLE_EVENT_TOPICS:
                            await client.subscribe(f"{self.user_id}/{vin}/vehicle-event/{topic}")

                    self._subscribed.set()
                    self._reconnect_delay = MQTT_RECONNECT_DELAY
                    retry_count = 0  # Reset retry count on successful connection
                    async for message in client.messages:
                        self._on_message(message)
            except aiomqtt.MqttError as exc:
                retry_count += 1
                _LOGGER.info(
                    "Connection lost (%s); reconnecting in %ss", exc, self._reconnect_delay
                )
                await asyncio.sleep(self._reconnect_delay)
                if (
                    retry_count > MQTT_FAST_RETRY
                    and self._reconnect_delay < MQTT_MAX_RECONNECT_DELAY
                ):  # first x retries are not exponential
                    self._reconnect_delay *= 2
                    self._reconnect_delay += uniform(0, 1)  # noqa: S311
                    self._reconnect_delay = min(self._reconnect_delay, MQTT_MAX_RECONNECT_DELAY)
                    _LOGGER.debug("Increased reconnect backoff to %s", self._reconnect_delay)

    def _on_message(self, msg: aiomqtt.Message) -> None:
        """Deserialize received MQTT message and emit Event to subscribed callbacks."""
        # Extract the topic, user id and vin from the topic's name.
        # Internally, the topic will always look like this:
        # `/{user_id}/{vin}/path/to/topic`
        topic_match = TOPIC_RE.match(str(msg.topic))
        if not topic_match:
            _LOGGER.warning("Unexpected MQTT topic encountered: %s", topic_match)
            return

        # Cast the data from binary string, ignoring empty messages.
        data = cast("str", msg.payload)
        if len(data) == 0:
            return

        self._parse_topic(topic_match, data)

    @staticmethod
    def _get_charging_event(data: str) -> ServiceEvent:
        try:
            event = ServiceEventWithChargingData.from_json(data)
        except ValueError:
            event = ServiceEvent.from_json(data)
        return event

    @staticmethod
    def _get_vehicle_ignition_status_changed_event(data: str) -> VehicleEvent:
        try:
            event = VehicleEventWithVehicleIgnitionStatusData.from_json(data)
        except ValueError:
            event = VehicleEvent.from_json(data)
        return event

    def _parse_topic(self, topic_match: re.Match[str], data: str) -> None:  # noqa: C901
        """Parse the topic and extract relevant parts."""
        [user_id, vin, event_type, topic] = topic_match.groups()
        event_type = EventType(event_type)

        _LOGGER.debug("Message (%s) received for %s on topic %s: %s", event_type, vin, topic, data)

        # Messages will contain payload as JSON.
        try:
            if event_type == EventType.OPERATION:
                self._emit(
                    EventOperation(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        operation=OperationRequest.from_json(data),
                    )
                )
            elif event_type == EventType.ACCOUNT_EVENT:
                self._emit(
                    EventAccountPrivacy(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "air-conditioning":
                self._emit(
                    EventAirConditioning(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=ServiceEvent.from_json(data),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "auxiliary-heating":
                self._emit(
                    EventAuxiliaryHeating(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=ServiceEvent.from_json(data),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "charging":
                self._emit(
                    EventCharging(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=self._get_charging_event(data),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "departure":
                self._emit(
                    EventDeparture(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=ServiceEvent.from_json(data),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "vehicle-status/access":
                self._emit(
                    EventAccess(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=ServiceEvent.from_json(data),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "vehicle-status/lights":
                self._emit(
                    EventLights(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=ServiceEvent.from_json(data),
                    )
                )
            elif event_type == EventType.SERVICE_EVENT and topic == "vehicle-status/odometer":
                self._emit(
                    EventOdometer(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=ServiceEvent.from_json(data),
                    )
                )
            elif event_type == EventType.VEHICLE_EVENT and topic == "vehicle-ignition-status":
                self._emit(
                    EventVehicleIgnitionStatus(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=self._get_vehicle_ignition_status_changed_event(data),
                    )
                )
            elif (
                event_type == EventType.VEHICLE_EVENT
                and topic == "vehicle-connection-status-update"
            ):
                self._emit(
                    EventVehicleConnectionStatusUpdate(
                        vin=vin,
                        user_id=user_id,
                        timestamp=datetime.now(tz=UTC),
                        event=VehicleEvent.from_json(data),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Exception parsing MQTT event: %s", exc)

    def _emit(self, event: Event) -> None:
        for callback in self._callbacks:
            result = callback(event)
            if result is not None:
                task = asyncio.create_task(result)
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)

        self._handle_operation(event)

    def _handle_operation(self, event: Event) -> None:
        if event.type != EventType.OPERATION:
            return

        if event.operation.status == OperationStatus.IN_PROGRESS:
            _LOGGER.debug(
                "An operation '%s' is now in progress. Trace id: %s",
                event.operation.operation,
                event.operation.trace_id,
            )
            return

        _LOGGER.debug(
            "Operation '%s' for trace id '%s' completed.",
            event.operation.operation,
            event.operation.trace_id,
        )
        self._handle_operation_completed(event.operation)

    def _handle_operation_completed(self, operation: OperationRequest) -> None:
        listeners = self._operation_listeners
        self._operation_listeners = []
        for listener in listeners:
            if listener.operation_name != operation.operation:
                self._operation_listeners.append(listener)
                continue

            if operation.status == OperationStatus.ERROR:
                _LOGGER.error(
                    "Resolving listener for operation '%s' with error '%s'.",
                    operation.operation,
                    operation.error_code,
                )
                listener.future.set_exception(OperationFailedError(operation))
            else:
                if operation.status == OperationStatus.COMPLETED_WARNING:
                    _LOGGER.warning("Operation '%s' completed with warnings.", operation.operation)

                _LOGGER.debug("Resolving listener for operation '%s'.", operation.operation)
                listener.future.set_result(operation)
