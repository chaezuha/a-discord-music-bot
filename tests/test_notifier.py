"""Tests for the owner breakage notifier."""

from __future__ import annotations

import pytest

from musicbot.notifier import BreakageNotifier

from .conftest import FakeBot


@pytest.fixture
def fake_now():
    return [1000.0]


@pytest.fixture
def notifier(fake_now):
    return BreakageNotifier(owner_id=42, threshold=3, cooldown=100.0, clock=lambda: fake_now[0])


async def test_no_dm_below_threshold(notifier):
    bot = FakeBot()
    await notifier.record_failure(bot)
    await notifier.record_failure(bot)
    assert bot.owner.dms == []


async def test_dm_at_threshold(notifier):
    bot = FakeBot()
    for _ in range(3):
        await notifier.record_failure(bot)
    assert len(bot.owner.dms) == 1
    assert "yt-dlp" in bot.owner.dms[0]


async def test_success_resets_counter(notifier):
    bot = FakeBot()
    await notifier.record_failure(bot)
    await notifier.record_failure(bot)
    notifier.record_success()
    await notifier.record_failure(bot)
    await notifier.record_failure(bot)
    assert bot.owner.dms == []


async def test_cooldown_limits_dms(notifier, fake_now):
    bot = FakeBot()
    for _ in range(6):
        await notifier.record_failure(bot)
    assert len(bot.owner.dms) == 1

    fake_now[0] += 101.0
    await notifier.record_failure(bot)
    assert len(bot.owner.dms) == 2


async def test_no_owner_never_dms(fake_now):
    notifier = BreakageNotifier(owner_id=None, threshold=1, clock=lambda: fake_now[0])
    bot = FakeBot()
    for _ in range(5):
        await notifier.record_failure(bot)
    assert bot.owner.dms == []
