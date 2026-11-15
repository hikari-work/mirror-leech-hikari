"""Apply a new commit on the tracked branch without losing running work.

The bot's code is replaced by ``update.py``, which ``start.sh`` runs once per
process. A commit pushed to ``UPSTREAM_REPO``/``UPSTREAM_BRANCH`` while the bot
is up therefore stays invisible until someone sends ``/restart``. This module
closes that gap: it compares the branch's remote head with the local ``HEAD``
and waits for a quiet moment to restart. Nothing is refused in the meantime --
the bot keeps taking tasks, and the restart is pushed back until the ledger has
been empty for ``IDLE_GRACE`` seconds. The restarted process runs ``update.py``
again, which resets the checkout to the branch and starts the new code.

Only the difference is watched, never the commit message or its author: the two
hashes are the whole question. A failed lookup is not an update either.
"""

from asyncio import sleep
from collections.abc import Awaitable, Callable
from time import monotonic

from ... import LOGGER, bot_loop, task_dict, task_dict_lock
from ...core.config_manager import Config
from .bot_utils import cmd_exec

CHECK_INTERVAL = 60
"""Seconds between two lookups of the tracked branch."""

IDLE_GRACE = 60
"""Seconds ``task_dict`` must stay empty before the restart is allowed.

A busy bot empties its ledger between one task and the next, so emptiness on
its own is not the moment a restart costs nobody a transfer. A task that
arrives inside this window restarts the clock.
"""

RestartHook = Callable[[], Awaitable[None]]


restart_hook: RestartHook | None = None
"""What ``restart_if_idle`` calls once the bot may go down.

``None`` means the real restart (``modules.restart.perform_restart``). It is a
module-level slot so a test can substitute one that records the call instead of
replacing the process.
"""

_pending_commit = ""
_pending_branch = ""
_restart_started = False
_idle_since: float | None = None
"""When the ledger was last seen empty, or None while a task is running."""
_reported_error = ""
"""The last failed lookup already logged, so a dead remote is said once."""


def _branch() -> str:
    """The branch ``update.py`` resets to, with the same default it uses."""
    return str(Config.UPSTREAM_BRANCH or "master").strip() or "master"


def _sha(text: str) -> str | None:
    """The commit hash in *text*, or None when git answered something else."""
    candidate = text.strip().split()[0] if text.strip() else ""
    if len(candidate) != 40:
        return None
    if not all(char in "0123456789abcdef" for char in candidate):
        return None
    return candidate


async def _local_commit() -> str | None:
    """``HEAD`` of the checkout this process is running, or None without one.

    ``update.py`` rebuilds the repository at every start, so in a deployment
    this is the commit the branch had when the process came up.
    """
    out, err, code = await cmd_exec(["git", "rev-parse", "HEAD"])
    if code != 0:
        LOGGER.debug(f"Update check: no local commit to compare ({err})")
        return None
    return _sha(out)


async def _remote_commit(repo: str, branch: str) -> str | None:
    """The branch's head on *repo*, or None when it cannot be read.

    ``ls-remote`` rather than ``fetch``: this only asks a question, and it
    leaves the local refs untouched -- which matters because a failed fetch
    part-way through would otherwise rewrite the state the next comparison
    reads.
    """
    global _reported_error

    out, err, code = await cmd_exec(
        ["git", "ls-remote", repo, f"refs/heads/{branch}"]
    )
    if code != 0:
        _report_failure(f"Update check could not reach {repo} for {branch}: {err}")
        return None
    _reported_error = ""
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return _sha(sha)
    LOGGER.debug(f"Update check: {repo} has no branch {branch!r}")
    return None


def _report_failure(message: str) -> None:
    """Log a failed lookup once, not once per check interval.

    ``watch_updates`` asks again every minute; a remote that stays unreachable
    would otherwise write the same warning sixty times an hour until someone
    reads the log and fixes it.
    """
    global _reported_error

    if message != _reported_error:
        LOGGER.warning(message)
        _reported_error = message


async def check_once() -> str | None:
    """Look for a commit newer than the running one, and remember it.

    Returns the pending commit's hash, or None when the checkout is current (or
    when that cannot be determined). Once a commit is pending the lookup stops:
    it is the update to apply, and asking again every minute would only repeat
    a network round trip whose answer cannot change the decision.
    """
    global _pending_commit, _pending_branch

    if _pending_commit:
        return _pending_commit

    repo = str(Config.UPSTREAM_REPO or "").strip()
    if not repo:
        return None

    branch = _branch()
    remote = await _remote_commit(repo, branch)
    if remote is None:
        return None
    local = await _local_commit()
    if local is None or local == remote:
        return None

    _pending_commit = remote
    _pending_branch = branch
    LOGGER.info(
        f"Update pending: {branch}@{remote[:7]} differs from the running"
        f" {local[:7]}; the bot will restart once no task has run for"
        f" {IDLE_GRACE} seconds"
    )
    return remote


async def _default_restart() -> None:
    """The restart ``/restart`` performs, imported late to keep this light."""
    from ...modules.restart import perform_restart

    await perform_restart()


async def restart_if_idle() -> bool:
    """Restart once an update is pending and the ledger has been quiet.

    ``task_dict`` is the whole ledger: downloading, uploading and queued tasks
    are all in it, so an empty dict means a restart costs nobody a transfer.
    A seeding task stays in it by design -- it is still active, and the restart
    waits for it like it waits for any other task.

    Empty right now is not enough, because a bot that keeps taking work empties
    between one task and the next and a task just handed to an engine may not
    have reached the ledger yet. The ledger therefore has to stay empty for
    ``IDLE_GRACE`` seconds. The first look starts that clock; the callers look
    again when it has run out, and any task seen in between clears it.

    Returns whether a restart was started. The flag makes the several callers
    -- the periodic pass and the completion hooks of finishing tasks -- agree
    on a single restart.
    """
    global _restart_started, _idle_since

    if _restart_started or not _pending_commit:
        return False
    async with task_dict_lock:
        active = bool(task_dict)
    if active:
        _idle_since = None
        return False

    now = monotonic()
    if _idle_since is None:
        _idle_since = now
    if now - _idle_since < IDLE_GRACE:
        return False

    _restart_started = True
    LOGGER.info(
        f"Restarting for {_pending_branch}@{_pending_commit[:7]}:"
        " no task has been active"
    )
    try:
        await (restart_hook or _default_restart)()
    except Exception:
        # The process is still here, so the next pass may try again.
        _restart_started = False
        raise
    return True


async def _restart_when_quiet() -> None:
    """Wait out the quiet window opened by a task that just finished."""
    await restart_if_idle()
    if _restart_started or _idle_since is None:
        return
    await sleep(IDLE_GRACE)
    await restart_if_idle()


def schedule_restart_if_idle() -> None:
    """Check for idleness now, from a synchronous caller.

    A task that has just left ``task_dict`` is the earliest moment worth
    looking; waiting for the next periodic pass would delay the restart by up
    to ``CHECK_INTERVAL`` seconds for no reason. The check is skipped entirely
    when no update is pending, so the common completion path pays one truth
    test and creates nothing. While the ledger is empty this also sleeps out
    the quiet window, so the restart lands the moment it has run out rather
    than on the next periodic pass.
    """
    global _idle_since

    if _pending_commit and not _restart_started:
        # The task left the ledger this instant, so the quiet window starts
        # here rather than at whenever the ledger was last seen empty.
        _idle_since = None
        bot_loop.create_task(_restart_when_quiet())


async def watch_updates() -> None:
    """Poll the branch, then restart when the last task is gone.

    Errors are logged and the loop carries on: a restart is an optimisation,
    and a transient failure of one lookup must not take down the watcher. A
    restart that returns without replacing the process ends the loop instead
    of retrying every minute -- ``perform_restart`` only returns at all when
    something is wrong with it, and hammering it would not help.
    """
    while True:
        try:
            await check_once()
            await restart_if_idle()
        except Exception as error:
            LOGGER.error(f"Update watch failed: {error}")
        if _restart_started:
            return
        await sleep(CHECK_INTERVAL)
