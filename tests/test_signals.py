import asyncio
from threading import Thread

from agent_hub.signals import EVENT_KEY, Signals, context_key, task_key


def test_keys_are_distinct_per_subject() -> None:
    assert context_key("c1") != context_key("c2")
    assert task_key("t1") != context_key("t1")
    assert EVENT_KEY not in {context_key("c1"), task_key("t1")}


async def test_notify_wakes_a_subscriber() -> None:
    signals = Signals()

    with signals.subscribe("k") as woken:
        assert signals.waiting("k") == 1
        signals.notify("k")
        await asyncio.wait_for(woken.wait(), 1)

    assert signals.waiting("k") == 0


async def test_notification_before_the_wait_is_not_lost() -> None:
    signals = Signals()

    with signals.subscribe("k") as woken:
        # Subscribing first is what makes the read-then-wait sequence safe.
        signals.notify("k")
        await asyncio.wait_for(woken.wait(), 1)


async def test_notify_without_subscribers_is_a_no_op() -> None:
    signals = Signals()

    signals.notify("nobody")

    assert signals.waiting("nobody") == 0


async def test_notify_from_another_thread_reaches_the_owning_loop() -> None:
    signals = Signals()

    with signals.subscribe("k") as woken:
        writer = Thread(target=signals.notify, args=("k",))
        writer.start()
        await asyncio.wait_for(woken.wait(), 1)
        writer.join()
