"""Tests for the entry point: env parsing, lifecycle tracking, watchdog, crash capture."""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import bot as bot_module
from bot import MusicBot, env_watchdog_seconds


class Exited(Exception):
    """Raised by the fake _terminate so the watchdog loop actually stops."""


@pytest.fixture
def fresh_bot():
    return MusicBot(dev_guild_id=None, watchdog_seconds=60.0)


@pytest.fixture
def fake_exit(monkeypatch):
    """Replace _terminate and logging.shutdown; returns the recorded exit codes."""
    codes: list[int] = []

    def terminate(code: int) -> None:
        codes.append(code)
        raise Exited

    monkeypatch.setattr(bot_module, "_terminate", terminate)
    monkeypatch.setattr(bot_module.logging, "shutdown", lambda: None)
    return codes


# -- WATCHDOG_DISCONNECT_SECONDS parsing -----------------------------------


def test_env_watchdog_seconds_default_and_valid(monkeypatch):
    monkeypatch.delenv("WATCHDOG_DISCONNECT_SECONDS", raising=False)
    assert env_watchdog_seconds() == 0.0
    monkeypatch.setenv("WATCHDOG_DISCONNECT_SECONDS", "300")
    assert env_watchdog_seconds() == 300.0
    monkeypatch.setenv("WATCHDOG_DISCONNECT_SECONDS", "0")
    assert env_watchdog_seconds() == 0.0


@pytest.mark.parametrize("bad", ["-1", "nope", "inf", "nan"])
def test_env_watchdog_seconds_rejects_bad_values(monkeypatch, bad):
    monkeypatch.setenv("WATCHDOG_DISCONNECT_SECONDS", bad)
    with pytest.raises(ValueError):
        env_watchdog_seconds()


# -- Connection lifecycle tracking ------------------------------------------


async def test_disconnect_records_start_of_outage(fresh_bot):
    assert fresh_bot._disconnected_since is None
    await fresh_bot.on_disconnect()
    assert fresh_bot._disconnected_since is not None


async def test_repeat_disconnects_keep_first_timestamp(fresh_bot):
    await fresh_bot.on_disconnect()
    first = fresh_bot._disconnected_since
    await fresh_bot.on_disconnect()
    assert fresh_bot._disconnected_since == first


async def test_resume_clears_outage(fresh_bot):
    await fresh_bot.on_disconnect()
    await fresh_bot.on_resumed()
    assert fresh_bot._disconnected_since is None


async def test_ready_clears_outage(fresh_bot):
    fresh_bot._connection.user = SimpleNamespace(id=1)
    await fresh_bot.on_disconnect()
    await fresh_bot.on_ready()
    assert fresh_bot._disconnected_since is None


async def test_connect_does_not_clear_outage(fresh_bot):
    # A stalled reconnect loop can reach on_connect without ever becoming
    # functional; only resume/ready count as recovery.
    await fresh_bot.on_disconnect()
    await fresh_bot.on_connect()
    assert fresh_bot._disconnected_since is not None


# -- Watchdog ----------------------------------------------------------------


async def test_watchdog_not_started_when_disabled():
    bot = MusicBot(dev_guild_id=None, watchdog_seconds=0.0)
    bot._start_watchdog()
    assert bot._watchdog_task is None


async def test_watchdog_started_when_enabled(fresh_bot):
    fresh_bot._start_watchdog()
    assert fresh_bot._watchdog_task is not None
    fresh_bot._watchdog_task.cancel()


async def test_watchdog_trips_after_threshold(fresh_bot, fake_exit, monkeypatch):
    monkeypatch.setattr(bot_module, "WATCHDOG_CHECK_INTERVAL_SECONDS", 0.001)
    fresh_bot._disconnected_since = time.monotonic() - 120  # past the 60s threshold
    with pytest.raises(Exited):
        await asyncio.wait_for(fresh_bot._watchdog(), timeout=2)
    assert fake_exit == [70]


async def test_watchdog_tolerates_short_outage(fresh_bot, fake_exit, monkeypatch):
    monkeypatch.setattr(bot_module, "WATCHDOG_CHECK_INTERVAL_SECONDS", 0.001)
    fresh_bot._disconnected_since = time.monotonic()  # just went down
    task = asyncio.get_running_loop().create_task(fresh_bot._watchdog())
    await asyncio.sleep(0.05)
    task.cancel()
    assert fake_exit == []


async def test_watchdog_quiet_while_connected(fresh_bot, fake_exit, monkeypatch):
    monkeypatch.setattr(bot_module, "WATCHDOG_CHECK_INTERVAL_SECONDS", 0.001)
    task = asyncio.get_running_loop().create_task(fresh_bot._watchdog())
    await asyncio.sleep(0.05)
    task.cancel()
    assert fake_exit == []


# -- Crash capture -------------------------------------------------------------


def test_main_logs_fatal_crash_and_exits_nonzero(monkeypatch, caplog):
    monkeypatch.setattr(bot_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(bot_module, "setup_logging", lambda: None)
    monkeypatch.setattr(bot_module.logging, "shutdown", lambda: None)
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    for name in ("DEV_GUILD_ID", "OWNER_ID", "IDLE_TIMEOUT_SECONDS", "WATCHDOG_DISCONNECT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    def explode(self, token, log_handler=None):
        raise RuntimeError("gateway fell over")

    monkeypatch.setattr(MusicBot, "run", explode)
    with caplog.at_level(logging.CRITICAL, logger="bot"), pytest.raises(SystemExit) as excinfo:
        bot_module.main()
    assert excinfo.value.code == 1
    assert any("crashed" in record.message for record in caplog.records)


# -- Logging setup --------------------------------------------------------------


def test_setup_logging_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_module.discord.utils, "setup_logging", lambda root: None)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        bot_module.setup_logging()
        logging.getLogger("bot").warning("hello file")
        assert "hello file" in (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        import faulthandler

        faulthandler.disable()
        if bot_module._faulthandler_file is not None:
            bot_module._faulthandler_file.close()
            bot_module._faulthandler_file = None


def test_setup_logging_degrades_without_writable_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_module.discord.utils, "setup_logging", lambda root: None)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file where the log dir should go")
    monkeypatch.setenv("LOG_DIR", str(blocker))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        bot_module.setup_logging()  # must not raise
        assert list(root.handlers) == before
    finally:
        import faulthandler

        faulthandler.disable()
