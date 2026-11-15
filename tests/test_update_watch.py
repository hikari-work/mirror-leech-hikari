"""An update that arrives while the bot is running.

``update.py`` only runs at process start, so a commit pushed to the tracked
branch is invisible to a running bot. The watcher closes that gap in two
steps, and both are pinned here:

* the remote head is compared with the local one, and a difference -- and
  only a difference -- marks an update as waiting;
* once one is waiting, a restart happens only after ``task_dict`` has stayed
  empty for the quiet window, and not a moment sooner.

Nothing turns new tasks away in the meantime: they are admitted as usual and
simply postpone the restart, which is what the last tests check.
"""

from __future__ import annotations

import asyncio

import pytest

import bot.helper.util.update_watch as uw
from bot.core.config_manager import Config

LOCAL = "a" * 40
REMOTE = "b" * 40


@pytest.fixture(autouse=True)
def watch(monkeypatch):
    """A pending-free watcher with a repo configured, over an empty ledger."""
    monkeypatch.setattr(uw, "_pending_commit", "")
    monkeypatch.setattr(uw, "_pending_branch", "")
    monkeypatch.setattr(uw, "_restart_started", False)
    monkeypatch.setattr(uw, "_idle_since", None)
    monkeypatch.setattr(uw, "IDLE_GRACE", 0)
    monkeypatch.setattr(uw, "_reported_error", "")
    monkeypatch.setattr(uw, "restart_hook", None)
    monkeypatch.setattr(uw, "task_dict", {})
    monkeypatch.setattr(Config, "UPSTREAM_REPO", "https://example.test/repo")
    monkeypatch.setattr(Config, "UPSTREAM_BRANCH", "main")
    return uw


def _git(monkeypatch, *, local=LOCAL, remote=REMOTE, local_code=0, remote_code=0):
    """Script the two git calls and return what was asked of them."""
    calls = []

    async def fake(cmd, shell=False):
        calls.append(cmd)
        if cmd[1] == "rev-parse":
            return local, "fatal" if local_code else "", local_code
        if remote_code:
            return "", "cannot reach", remote_code
        return f"{remote}\trefs/heads/main", "", 0

    monkeypatch.setattr(uw, "cmd_exec", fake)
    return calls


def _failures():
    """An async restart hook that records its calls instead of exiting."""

    called = []

    async def hook():
        called.append(True)

    return hook, called


async def _no_sleep(_seconds):
    """Stand-in for the quiet-window sleep in a test with a fake clock."""


def _pending(monkeypatch):
    """Put the watcher in the state a detected update leaves it in.

    Detection has its own tests above; these ones are about what the rest of
    the bot does with the result, so they start from the result.
    """
    monkeypatch.setattr(uw, "_pending_commit", REMOTE)
    monkeypatch.setattr(uw, "_pending_branch", "main")


# ── detection ───────────────────────────────────────────────────────


async def test_without_an_upstream_branch_nothing_is_looked_up(monkeypatch):
    monkeypatch.setattr(Config, "UPSTREAM_REPO", "")
    calls = _git(monkeypatch)

    assert await uw.check_once() is None
    assert calls == []


async def test_a_different_remote_head_marks_the_update_pending(monkeypatch):
    _git(monkeypatch)

    assert await uw.check_once() == REMOTE


async def test_the_same_head_is_not_an_update(monkeypatch):
    _git(monkeypatch, remote=LOCAL)

    assert await uw.check_once() is None


async def test_an_unreachable_remote_is_not_an_update(monkeypatch):
    _git(monkeypatch, remote_code=128)

    assert await uw.check_once() is None


async def test_a_checkout_without_a_commit_is_not_an_update(monkeypatch):
    _git(monkeypatch, local="", local_code=128)

    assert await uw.check_once() is None


async def test_the_lookup_stops_once_a_commit_is_pending(monkeypatch):
    calls = _git(monkeypatch)
    await uw.check_once()
    asked = len(calls)

    assert await uw.check_once() == REMOTE
    assert len(calls) == asked


async def test_the_pending_state_names_the_branch_and_the_commit(monkeypatch):
    _git(monkeypatch)
    await uw.check_once()

    assert uw._pending_branch == "main"
    assert uw._pending_commit == REMOTE


# ── restarting ──────────────────────────────────────────────────────


async def test_a_current_checkout_never_restarts(monkeypatch):
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is False
    assert called == []


async def test_an_active_task_keeps_the_restart_waiting(monkeypatch):
    _pending(monkeypatch)
    uw.task_dict[1] = object()
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is False
    assert called == []


async def test_the_restart_happens_as_soon_as_no_task_is_left(monkeypatch):
    _pending(monkeypatch)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is True
    assert called == [True]
    # the second caller -- the periodic pass, or another finishing task --
    # must not start a second restart
    assert await uw.restart_if_idle() is False
    assert called == [True]


async def test_a_failed_restart_is_left_to_be_tried_again(monkeypatch):
    _pending(monkeypatch)

    async def hook():
        raise RuntimeError("the restart did not take")

    monkeypatch.setattr(uw, "restart_hook", hook)

    with pytest.raises(RuntimeError):
        await uw.restart_if_idle()
    assert uw._restart_started is False


async def test_finishing_the_last_task_schedules_the_idle_check(monkeypatch):
    _pending(monkeypatch)
    monkeypatch.setattr(uw, "bot_loop", asyncio.get_running_loop())
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    uw.schedule_restart_if_idle()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert called == [True]


async def test_nothing_is_scheduled_without_a_pending_update(monkeypatch):
    calls = []
    monkeypatch.setattr(
        uw,
        "bot_loop",
        type(
            "Loop",
            (),
            {"create_task": lambda self, coro: calls.append(coro)},
        )(),
    )

    uw.schedule_restart_if_idle()

    assert calls == []


async def test_the_loop_ends_after_handing_over_to_the_restart(monkeypatch):
    _pending(monkeypatch)
    monkeypatch.setattr(uw, "CHECK_INTERVAL", 0)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    await asyncio.wait_for(uw.watch_updates(), 1)

    assert called == [True]


async def test_a_task_left_in_the_ledger_blanks_the_quiet_window(monkeypatch):
    """A task that starts after the first empty look restarts the clock.

    Without it, a ledger that was empty a minute ago and is empty again now
    would restart even though a task ran and finished in between.
    """
    clock = [0.0]
    monkeypatch.setattr(uw, "IDLE_GRACE", 30)
    monkeypatch.setattr(uw, "monotonic", lambda: clock[0])
    _pending(monkeypatch)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is False
    uw.task_dict[1] = object()
    clock[0] = 5
    assert await uw.restart_if_idle() is False
    del uw.task_dict[1]
    clock[0] = 20
    assert await uw.restart_if_idle() is False
    clock[0] = 49
    assert await uw.restart_if_idle() is False
    clock[0] = 50
    assert await uw.restart_if_idle() is True
    assert called == [True]


async def test_a_finished_task_reopens_the_quiet_window(monkeypatch):
    """The window starts at the completion, not the last time it looked empty."""
    clock = [0.0]
    monkeypatch.setattr(uw, "IDLE_GRACE", 30)
    monkeypatch.setattr(uw, "monotonic", lambda: clock[0])
    monkeypatch.setattr(uw, "bot_loop", asyncio.get_running_loop())
    monkeypatch.setattr(uw, "sleep", _no_sleep)
    _pending(monkeypatch)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is False
    clock[0] = 5
    uw.schedule_restart_if_idle()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    clock[0] = 34
    assert await uw.restart_if_idle() is False
    clock[0] = 35
    assert await uw.restart_if_idle() is True
    assert called == [True]


async def test_a_new_task_is_admitted_while_the_update_waits(monkeypatch):
    """Nothing refuses work: a task arriving mid-wait only postpones the restart."""
    _pending(monkeypatch)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    uw.task_dict[1] = object()

    assert await uw.restart_if_idle() is False
    assert called == []
    assert uw._pending_commit == REMOTE
