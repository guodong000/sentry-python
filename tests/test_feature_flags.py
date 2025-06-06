import concurrent.futures as cf
import sys
import copy
import threading

import pytest

import sentry_sdk
from sentry_sdk.feature_flags import add_feature_flag, FlagBuffer


def test_featureflags_integration(sentry_init, capture_events, uninstall_integration):
    sentry_init()

    add_feature_flag("hello", False)
    add_feature_flag("world", True)
    add_feature_flag("other", False)

    events = capture_events()
    sentry_sdk.capture_exception(Exception("something wrong!"))

    assert len(events) == 1
    assert events[0]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
            {"flag": "world", "result": True},
            {"flag": "other", "result": False},
        ]
    }


def test_featureflags_integration_threaded(
    sentry_init, capture_events, uninstall_integration
):
    sentry_init()
    events = capture_events()

    # Capture an eval before we split isolation scopes.
    add_feature_flag("hello", False)

    def task(flag_key):
        # Creates a new isolation scope for the thread.
        # This means the evaluations in each task are captured separately.
        with sentry_sdk.isolation_scope():
            add_feature_flag(flag_key, False)
            # use a tag to identify to identify events later on
            sentry_sdk.set_tag("task_id", flag_key)
            sentry_sdk.capture_exception(Exception("something wrong!"))

    # Run tasks in separate threads
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        pool.map(task, ["world", "other"])

    # Capture error in original scope
    sentry_sdk.set_tag("task_id", "0")
    sentry_sdk.capture_exception(Exception("something wrong!"))

    assert len(events) == 3
    events.sort(key=lambda e: e["tags"]["task_id"])

    assert events[0]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
        ]
    }
    assert events[1]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
            {"flag": "other", "result": False},
        ]
    }
    assert events[2]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
            {"flag": "world", "result": False},
        ]
    }


@pytest.mark.skipif(sys.version_info < (3, 7), reason="requires python3.7 or higher")
def test_featureflags_integration_asyncio(
    sentry_init, capture_events, uninstall_integration
):
    asyncio = pytest.importorskip("asyncio")

    sentry_init()
    events = capture_events()

    # Capture an eval before we split isolation scopes.
    add_feature_flag("hello", False)

    async def task(flag_key):
        # Creates a new isolation scope for the thread.
        # This means the evaluations in each task are captured separately.
        with sentry_sdk.isolation_scope():
            add_feature_flag(flag_key, False)
            # use a tag to identify to identify events later on
            sentry_sdk.set_tag("task_id", flag_key)
            sentry_sdk.capture_exception(Exception("something wrong!"))

    async def runner():
        return asyncio.gather(task("world"), task("other"))

    asyncio.run(runner())

    # Capture error in original scope
    sentry_sdk.set_tag("task_id", "0")
    sentry_sdk.capture_exception(Exception("something wrong!"))

    assert len(events) == 3
    events.sort(key=lambda e: e["tags"]["task_id"])

    assert events[0]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
        ]
    }
    assert events[1]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
            {"flag": "other", "result": False},
        ]
    }
    assert events[2]["contexts"]["flags"] == {
        "values": [
            {"flag": "hello", "result": False},
            {"flag": "world", "result": False},
        ]
    }


def test_flag_tracking():
    """Assert the ring buffer works."""
    buffer = FlagBuffer(capacity=3)
    buffer.set("a", True)
    flags = buffer.get()
    assert len(flags) == 1
    assert flags == [{"flag": "a", "result": True}]

    buffer.set("b", True)
    flags = buffer.get()
    assert len(flags) == 2
    assert flags == [{"flag": "a", "result": True}, {"flag": "b", "result": True}]

    buffer.set("c", True)
    flags = buffer.get()
    assert len(flags) == 3
    assert flags == [
        {"flag": "a", "result": True},
        {"flag": "b", "result": True},
        {"flag": "c", "result": True},
    ]

    buffer.set("d", False)
    flags = buffer.get()
    assert len(flags) == 3
    assert flags == [
        {"flag": "b", "result": True},
        {"flag": "c", "result": True},
        {"flag": "d", "result": False},
    ]

    buffer.set("e", False)
    buffer.set("f", False)
    flags = buffer.get()
    assert len(flags) == 3
    assert flags == [
        {"flag": "d", "result": False},
        {"flag": "e", "result": False},
        {"flag": "f", "result": False},
    ]

    # Test updates
    buffer.set("e", True)
    buffer.set("e", False)
    buffer.set("e", True)
    flags = buffer.get()
    assert flags == [
        {"flag": "d", "result": False},
        {"flag": "f", "result": False},
        {"flag": "e", "result": True},
    ]

    buffer.set("d", True)
    flags = buffer.get()
    assert flags == [
        {"flag": "f", "result": False},
        {"flag": "e", "result": True},
        {"flag": "d", "result": True},
    ]


def test_flag_buffer_concurrent_access():
    buffer = FlagBuffer(capacity=100)
    error_occurred = False

    def writer():
        for i in range(1_000_000):
            buffer.set(f"key_{i}", True)

    def reader():
        nonlocal error_occurred

        try:
            for _ in range(1000):
                copy.deepcopy(buffer)
        except RuntimeError:
            error_occurred = True

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)

    writer_thread.start()
    reader_thread.start()

    writer_thread.join(timeout=5)
    reader_thread.join(timeout=5)

    # This should always be false. If this ever fails we know we have concurrent access to a
    # shared resource. When deepcopying we should have exclusive access to the underlying
    # memory.
    assert error_occurred is False
