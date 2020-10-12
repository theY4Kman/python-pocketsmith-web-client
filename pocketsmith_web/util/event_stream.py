import asyncio
from typing import (
    AsyncIterable,
    Awaitable,
    Callable,
    Generic,
    Set,
    TypeVar,
    Union,
)

from aiostream import stream
from lifter.backends.python import QueryImpl
from lifter.query import BaseQueryNode

EventType = TypeVar('EventType')
EventStreamType = TypeVar('EventStreamType')


def build_predicate(*matchers: Union[BaseQueryNode, Callable[[EventType], bool]],
                    ) -> Callable[[EventType], bool]:
    predicates = []
    for matcher in matchers:
        if isinstance(matcher, BaseQueryNode):
            predicates.append(QueryImpl(matcher))
        elif callable(matcher):
            predicates.append(matcher)
        else:
            raise TypeError(f'Expected lifter query node or callable. Found: {matcher!r}')

    predicate = lambda event: all(predicate(event) for predicate in predicates)
    return predicate


def event_filter(source: AsyncIterable[EventType],
                 *matchers: Union[BaseQueryNode, Callable[[EventType], bool]],
                 ) -> AsyncIterable[EventType]:
    predicate = build_predicate(*matchers)
    return stream.filter(source, predicate)


class BaseEventStream(Generic[EventType], AsyncIterable[EventType], Awaitable[EventType]):
    def __init__(self, factory: Callable[[], AsyncIterable[EventType]]):
        self._factory = factory

    def __aiter__(self):
        return self._factory().__aiter__()

    def __await__(self):
        return self.expect().__await__()

    def filter(
        self,
        *matchers: Union[BaseQueryNode, Callable[[EventType], bool]]
    ) -> AsyncIterable[EventType]:
        return BaseEventStream(lambda: event_filter(self, *matchers))

    async def expect(
        self,
        *matchers: Union[BaseQueryNode, Callable[[EventType], bool]],
    ) -> EventType:
        aiter = self.filter(*matchers) if matchers else self
        return await stream.take(aiter, 1)


class EventStream(Generic[EventType], BaseEventStream[EventType]):
    def __init__(self):
        self._queues: Set[asyncio.Queue] = set()
        super().__init__(self._listen)

    async def emit(self, event: EventType, *events: EventType):
        """Publish event(s) to the stream, notifying all listeners"""
        await self._emit(event)

        for event in events:
            await asyncio.sleep(0)
            await self._emit(event)

    async def _emit(self, event: EventType):
        for queue in tuple(self._queues):
            await queue.put(event)

    async def _listen(self) -> AsyncIterable[EventType]:
        queue = asyncio.Queue()
        self._queues.add(queue)

        try:
            while True:
                yield await queue.get()
                queue.task_done()
        finally:
            self._queues.remove(queue)
