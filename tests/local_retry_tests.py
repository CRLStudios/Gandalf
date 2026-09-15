"""Standalone tests for bot.py's startup retry hardening.

Run with:  venv/bin/python tests/local_retry_tests.py
No Discord connection needed.

Background: when the FIRST gateway connection fails (e.g. a Discord outage
returning 503 on the websocket handshake), discord.py 2.x crashes with
AttributeError("'NoneType' object has no attribute 'sequence'") instead of
retrying. bot.main() catches that specific failure and retries with backoff.
"""

import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot as botmod  # noqa: E402


class FakeShutdownEvent:
    """Stands in for bot.shutdown_event; wait() 'times out' instantly so the
    backoff sleep costs no test time."""

    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    async def wait(self):
        if self._set:
            return True
        raise asyncio.TimeoutError


def gateway_bug_error():
    exc = AttributeError("'NoneType' object has no attribute 'sequence'")
    exc.__context__ = Exception(
        "503, message='Invalid response status', "
        "url='wss://gateway.discord.gg/?v=10&encoding=json&compress=zlib-stream'"
    )
    return exc


async def test_retries_through_gateway_outage():
    calls = {"n": 0}

    async def fake_start(token, *, reconnect=True):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise gateway_bug_error()

    botmod.shutdown_event = FakeShutdownEvent()
    with mock.patch.object(botmod.bot, "start", fake_start):
        await asyncio.wait_for(botmod.main(), timeout=10)
    assert calls["n"] == 3, f"expected 3 start attempts, got {calls['n']}"


async def test_unrelated_attributeerror_propagates():
    async def fake_start(token, *, reconnect=True):
        raise AttributeError("'Foo' object has no attribute 'bar_typo'")

    botmod.shutdown_event = FakeShutdownEvent()
    with mock.patch.object(botmod.bot, "start", fake_start):
        try:
            await asyncio.wait_for(botmod.main(), timeout=10)
        except AttributeError as e:
            assert "bar_typo" in str(e)
            return
    raise AssertionError("unrelated AttributeError was swallowed")


async def test_shutdown_during_backoff_exits():
    async def fake_start(token, *, reconnect=True):
        raise gateway_bug_error()

    botmod.shutdown_event = FakeShutdownEvent()
    botmod.shutdown_event.set()
    with mock.patch.object(botmod.bot, "start", fake_start):
        await asyncio.wait_for(botmod.main(), timeout=10)


TESTS = [
    test_retries_through_gateway_outage,
    test_unrelated_attributeerror_propagates,
    test_shutdown_during_backoff_exits,
]


async def run_all():
    failed = 0
    for t in TESTS:
        try:
            await t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
