import asyncio
import datetime
import errno
import json
import logging
from typing import Any, Dict, Optional, Set, TYPE_CHECKING

import lifter.models
import websockets

from pocketsmith_web.util.event_stream import EventStream

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from pocketsmith_web import PocketsmithWebClient


class PusherEvent(lifter.models.Model):
    @classmethod
    def from_json(cls, raw_event: str) -> 'PusherEvent':
        event = json.loads(raw_event)
        return cls.from_dict(event)

    @classmethod
    def from_dict(cls, event: Dict[str, Any]) -> 'PusherEvent':
        event_name = event['event']
        channel = event.get('channel')

        data = event.get('data', {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass

        return cls(event=event_name, data=data, channel=channel)

    @classmethod
    def ConnectionEstablished(cls):
        return cls.event == 'pusher:connection_established'

    @classmethod
    def Pong(cls):
        return cls.event == 'pusher:pong'

    @classmethod
    def SubscriptionSucceeded(cls, channel: str):
        return (cls.event == 'pusher_internal:subscription_succeeded') & (cls.channel == channel)

    @classmethod
    def MfaChanged(cls, channel: str):
        return (cls.event == 'yodsmith-mfa-changed') & (cls.channel == channel)


class PocketsmithPusher:
    URL_FMT = 'wss://ws.pusherapp.com/app/{client_id}?protocol=7&client=js&version=2.2.4&flash=false'

    pwc: 'PocketsmithWebClient'
    client_id: str
    loop: asyncio.AbstractEventLoop
    stale_timeout: Optional[float]
    quiet_period: Optional[float]

    ws_url: str
    ws: Optional[websockets.WebSocketClientProtocol]

    socket_id: Optional[str]
    activity_timeout: Optional[int]

    _last_recv: Optional[datetime.datetime]
    _last_send: Optional[datetime.datetime]

    _stale_timeout_task: Optional[asyncio.Task]
    _heartbeat_task: Optional[asyncio.Task]
    _recv_event_loop_task: Optional[asyncio.Task]
    events: EventStream[PusherEvent]

    _subscribed_channels = Set[str]

    class StaleSocket(TimeoutError):
        """Raised when the websocket may no longer be receiving fresh infp

        After a period of times, authed Pusher sockets seem to no longer receive
        MFA events.
        """

    def __init__(self,
                 pwc: 'PocketsmithWebClient',
                 client_id: str,
                 loop: asyncio.AbstractEventLoop = None,
                 stale_timeout: float = 3600,
                 quiet_period: float = 60):
        self.pwc = pwc
        self.client_id = client_id
        self.loop = loop or asyncio.get_event_loop()
        self.stale_timeout = stale_timeout
        self.quiet_period = quiet_period

        self.ws_url = self.URL_FMT.format(client_id=client_id)
        self.ws = None

        self.socket_id = None
        self.activity_timeout = None

        self._last_recv = None
        self._last_send = None

        self._stale_timeout_task = None
        self._heartbeat_task = None
        self._recv_event_loop_task = None

        self.events = EventStream()
        self._subscribed_channels = set()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self._stale_timeout_task:
            self._stale_timeout_task.cancel()
            self._stale_timeout_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._recv_event_loop_task:
            self._recv_event_loop_task.cancel()
            self._recv_event_loop_task = None

        if self.ws:
            await self.ws.close()
            self.ws = None

    async def connect(self):
        logger.debug(f'Attempting to connect to Pusher')
        self.ws = await websockets.connect(self.ws_url)
        logger.info(f'Connected to Pusher')

        self._recv_event_loop_task = self.loop.create_task(self._recv_event_loop())

        logger.debug(f'Awaiting Pusher connection_established event')
        event = await self.events.expect(PusherEvent.ConnectionEstablished())
        logger.debug(f'Received Pusher connection_established event')

        self.socket_id = event.data['socket_id']
        self.activity_timeout = event.data['activity_timeout']

        logger.debug(f'Beginning heartbeat task '
                     f'(pings every {self.activity_timeout} seconds)')
        self._heartbeat_task = self.loop.create_task(self._heartbeat_loop())

        if self.stale_timeout is not None:
            logger.debug(f'Beginning staleness monitoring task '
                         f'(not stale before {self.stale_timeout} seconds;'
                         f' considered stale if quiet for an additional {self.quiet_period or 0} seconds)')
            self._stale_timeout_task = self.loop.create_task(self._staleness_monitor())

    async def _heartbeat_loop(self):
        while True:
            await self.send_event('pusher:ping')
            await self.events.expect(PusherEvent.Pong())

            await asyncio.sleep(self.activity_timeout)

    async def _staleness_monitor(self):
        await asyncio.sleep(self.stale_timeout)
        logger.debug(f'Staleness timeout of {self.stale_timeout} seconds reached.')

        await self.wait_until_quiet()
        raise self.StaleSocket

    async def wait_until_quiet(self):
        """Waits until no message has been sent/received within the quiet period"""
        if self.quiet_period is None:
            return

        while True:
            now = datetime.datetime.utcnow()
            last_activity = max(self._last_recv, self._last_send)

            last_activity_elapsed = now - last_activity
            last_activity_elapsed_seconds = last_activity_elapsed.total_seconds()

            if last_activity_elapsed_seconds < self.quiet_period:
                wait_seconds = self.quiet_period - last_activity_elapsed_seconds
                logger.debug(f'Activity detected {last_activity_elapsed_seconds} seconds ago. '
                             f'This is less than the required quiet period of {self.quiet_period}. '
                             f'Waiting {wait_seconds:.2f} seconds until serenity achieved.')
                await asyncio.sleep(wait_seconds + 1)
                continue

            break

    async def subscribe(self, channel: str):
        if channel not in self._subscribed_channels:
            self._subscribed_channels.add(channel)
            await self._perform_subscription(channel)

    async def _perform_subscription(self, channel: str):
        auth = await self.pwc.pusher_auth(self.socket_id, channel)
        data = {
            'auth': f'{auth}',
            'channel': channel,
        }
        await self.send_event('pusher:subscribe', data)
        await self.events.expect(PusherEvent.SubscriptionSucceeded(channel))

    async def _resubscribe(self):
        """Resubscribe to all channels previously subscribed to

        This is used to recreate subscriptions after re-authenticating.
        See recv_forever
        """
        subscriptions = [
            self._perform_subscription(channel)
            for channel in self._subscribed_channels
        ]
        await asyncio.gather(*subscriptions)

    async def _recv_event_loop(self):
        while True:
            await self.recv_event()

    async def recv_until_quiet(self, *, raise_on_connection_closed: bool = False):
        """Receive messages until the socket goes stale and serenity is achieved

        This can be useful if one wishes to perform some tasks with the Pusher
        websocket, and exit once published messages subside — e.g. after
        submitting a sync of institutions, Yodlee performs the actual syncing,
        sending messages of its refreshes; once it stops sending messages, one
        can consider the sync "finished"
        """
        try:
            await asyncio.gather(
                self._recv_event_loop_task,
                self._stale_timeout_task,
            )
        except self.StaleSocket:
            return
        except websockets.ConnectionClosedError as e:
            logger.warning(f'Pusher websocket closed unexpectedly: {e}')

            if raise_on_connection_closed:
                raise
            else:
                return

    async def recv_forever(self):
        """Receive messages forever, re-authing transparently once the socket is stale
        """
        while True:
            await self.recv_until_quiet()
            logger.info(f'Socket went stale and serenity achieved. '
                        f'Reconnecting and re-authenticating ...')

            await self.reconnect()

    async def reconnect(self, max_retries: int = 10):
        assert max_retries >= 1

        await self.close()

        backoff = 1
        for attempt in range(max_retries):
            try:
                await self.connect()
                break
            except OSError as e:
                if e.errno == errno.EBUSY:
                    logger.warning(
                        f'Encountered EBUSY when reconnecting ({e}). '
                        f'Waiting {backoff} seconds before trying again. '
                        f'{max_retries - attempt + 1} attempts left.')

                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise
        else:
            raise RuntimeError(f'Unable to reconnect after {max_retries} attempts.')

        await self._resubscribe()

    async def recv_event(self) -> PusherEvent:
        """Read an event from the websocket, and return its info

        :return:
            A tuple of (event_name, data, channel), where event_name is the event
            type as returned by Pusher; data is a dictionary of event-specific
            info; and channel is an optional name of the channel the event is
            being returned from. For events like "pusher:pong", there is no
            channel name.
        """
        raw_event = await self.ws.recv()
        logger.debug(f'PUSHER RECV: {raw_event}')
        event = PusherEvent.from_json(raw_event)

        if event.event != 'pong':
            self._last_recv = datetime.datetime.utcnow()

        await self.events.emit(event)
        return event

    async def send_event(self, event_name: str, data: Dict[str, Any] = None, channel: str = None):
        """Send an event to the websocket
        """
        event = {
            'event': event_name,
            'data': data or {},
        }
        if channel is not None:
            event['channel'] = channel

        raw_event = json.dumps(event)
        logger.debug(f'PUSHER SEND: {raw_event}')
        await self.ws.send(raw_event)

        if event_name != 'ping':
            self._last_send = datetime.datetime.utcnow()
