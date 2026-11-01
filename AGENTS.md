# AGENTS.md — how to change code in this repo

> This file is the English translation of the repo's process guide. The
> original Indonesian text is kept verbatim in `AGENTS.id.md`. Keep
> `AGENTS.md` in English: the AgentRouter gateway rejects non-English content
> in the first user message (`400 content-blocked`), and this file is injected
> into that message automatically by Claude Code/Codex.

One rule binds the rest: **every commit passes three gates, carries a test
that has been proven capable of failing, and the code it touches is
type-annotated.** The rest of this document spells that out.

The numbers quoted below are a snapshot from 2026-08-24 (`c4f47c5`). Measure
them again; do not trust the numbers — what binds is the commands and their
targets.

---

## 1. Interpreter — pick the wrong one and healthy code looks broken

Three tools, three different locations. This is not a cosmetic detail: the
wrong `pytest` reports **6 collection errors** `ModuleNotFoundError: No module
named 'pyrogram'`, and that is easy to read as "the code is broken" when only
the interpreter is wrong.

| Tool | Run with | Why |
|---|---|---|
| pytest | `.venv/bin/python -m pytest` | Dependencies live in `.venv` (`kurigram` → `pyrogram` module). The `pytest` on PATH (`~/.local/bin`) uses system site-packages, which does not have it. |
| ruff | `ruff check .` | Only on PATH (`~/.local/bin/ruff`), **not** in `.venv`. |
| pyrefly | `.venv/bin/pyrefly check` | Only in `.venv`, **not** on PATH. |

`.venv` was created without system-site-packages, so `.venv/bin/python` is the
only interpreter that sees the full requirements.

**If you are an agent running under the RTK hook:** that hook trims ruff output
(43 raw lines → 28 lines, the last line truncated to `b...`). For the complete
numbers and findings list — which are the basis for the diff in §2 — bypass
the filter:

```bash
rtk proxy 'ruff check . --output-format concise'
```

---

## 2. Three gates

Run all three before committing. Not just one.

```bash
.venv/bin/python -m pytest -q                                # 1. tests
ruff check .                                                 # 2. lint
.venv/bin/pyrefly check --baseline pyrefly-baseline.json      # 3. types
```

| Gate | Target | Snapshot 2026-08-24 |
|---|---|---|
| pytest | 0 failed | 872 passed, 0 failed (~24 s) |
| ruff | 0 findings | **41 findings** — see §5 |
| pyrefly | 0 errors | 0 errors (36 suppressed) |

**About those 41 ruff findings.** All of them are debt that came in *after*
Phase 10 of the refactor finished, and none are recorded in the
`per-file-ignores` ledger (§5) — so they are not sanctioned debt, but
regressions that slipped through. Distribution: `bot/core/handlers.py`
(18× E501), `hosts/vidara.py` (5), `pornhub_scraper.py` (6), plus scattered
I001 and 3 in `tests/`.

The consequence for your work: **do not use the number 41 as permission.**
Compare before and after so your findings do not masquerade as pre-existing
ones:

```bash
ruff check . --output-format concise | sort > /tmp/before.txt   # before touching anything
# ... make your changes ...
ruff check . --output-format concise | sort > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt                            # must be empty, or contain only removed lines
```

Files you touch should ideally leave that list as well. If not, at least do
not add lines to it.

---

## 3. Type hints

`pyrefly` is the type checker; configuration lives in `[tool.pyrefly]`
(`pyproject.toml`), covering `bot` + `web`.

**The rule:** every function/method you add — or whose signature you change —
gets parameter and return annotations.

What you need to know: **ruff will not catch missing hints.** The `ANN` rules
(flake8-annotations) are deliberately disabled because the old code has 2255
ANN findings and enabling them would drown the new ones. That means annotation
completeness is enforced by review, not by the linter. Check your own diff —
and only the lines you touch:

```bash
ruff check <changed-file> --select ANN
```

House style, matching the existing code:

- PEP 604 for unions: `FloodWait | FloodPremiumWait`, `str | None`. Not `Optional[...]`.
- Use `collections.abc` for `Callable`/`Iterable`, not `typing`.
- Add `from __future__ import annotations` **only when needed** for forward
  references or to keep annotations cheap — 23 of 145 files in `bot`/`web` use
  it, so it is not mandatory.
- If many call sites read `self` attributes set in another file, write the
  contract once in an inert annotation-only class. Precedent:
  `bot/helper/task/_host.py` (41 attributes, no `__init__`, no values — the
  composition changes nothing at runtime).
- Root-cause a cause that feeds many findings at once, rather than annotating
  each call site. Precedent: `flood_seconds()` in
  `bot/helper/telegram/flood.py`: one function replaced five
  `sleep(f.value * slack)` calls computing on a possibly-`str` type.

**What is not allowed:**

- `# type: ignore` to bypass a gate. Find the root cause. If the finding really
  is a third-party bug, then baseline it, with a comment explaining why — the
  5 existing entries are all one pyrogram bug (`thumb: Union[str,
  BinaryIO] = None` in its own signature).
- `pyrefly check --update-baseline` to silence new findings. That command is
  only for when debt is actually paid off (entries disappear) or deliberately
  accepted after thought.

Baseline was chosen over disabling code errors per file precisely because of
the difference: disabling `bad-assignment` for one module also swallows the
*next* `bad-assignment` in that module; a baseline records findings one by
one, so new ones are still reported even if their type and file have appeared
before.

---

## 4. Tests

**Every behavior change brings a test, and that test has been proven to fail
without the change.** The proof is written in the commit body — not claimed,
but reported. A test that has never been seen to fail guarantees nothing.

Location and shape:

- `tests/test_<subject>.py`. `asyncio_mode = auto`, so `async def test_...`
  needs no marker. The only recognized marker is `slow`, and
  `--strict-markers` is on — a misspelled marker errors rather than being
  silently ignored.
- Importing the `bot` module pulls in the whole chain (lxml, cloudscraper,
  pyrogram), and `bot/__init__.py` has import side effects
  (`uvloop.install()`, creates an event loop, opens `log.txt`). To test a leaf
  module, **stub the package** instead of importing the chain — precedent: the
  `bunkr` and `vidara` fixtures in `tests/conftest.py` load one host file via
  `spec_from_file_location` with `_common`/`registry` faked.
- `DOWNLOAD_DIR = "/app/downloads/"` is hardcoded; tests that touch paths need
  monkeypatching.

**For a refactor that promises unchanged behavior**, two tests are not
enough — this repo uses two layers:

1. **Differential harness** in `tools/archive/` — loads the pre-refactor
   module version directly from git alongside the working-tree version, runs
   both through the same scenarios, then compares **calls** (which method,
   what arguments, what order), **logs**, **state**, and **results** (return
   value or exception). Precedent: `tools/archive/_phase11b_diff.py`, 103
   scenarios.
2. **Mutation check** — a harness reporting "identical" means nothing until it
   is proven able to tell a difference. The mutation script breaks the new
   code one small change at a time and requires the harness to fail on every
   mutation. Precedent: `tools/archive/_phase11b_mutants.py`, 62 mutations,
   62 caught. **A mutant that survives is not a pass** — it is either a hole
   in the harness or a line with no effect, and which one must be determined,
   not assumed.

`tests/` and `tools/` are outside pyrefly's scope (164 findings, they would
drown findings from code that ships), but **still** inside ruff's scope.

---

## 5. Lint ledger — shrinks, never grows

`[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` is the list of debt
that existed before the refactor, withdrawn phase by phase. The rule is
one-way: **delete a line when the file is cleaned up; adding a new line is a
regression.** If you touch a file that has an entry, clear the remaining
findings and remove its line.

To make sure a directory really has no debt — not merely that its debt is
hidden:

```bash
ruff check bot/helper/upload/ --config 'lint.per-file-ignores = {}'
```

`upload/` is now clean with that command. If someone later adds its line back,
that is a regression, not an adjustment.

Effective thresholds: `line-length = 88`, `target-version = "py314"`,
`max-complexity = 10`. `.venv` and `qBittorrent` are excluded.

To judge when a ledger line may be removed (and to see whether your change
adds complexity):

```bash
.venv/bin/python tools/complexity_report.py --summary
.venv/bin/python tools/complexity_report.py --diff tools/baseline.txt
```

CX is computed with the same approach as ruff's C901, so the numbers are
comparable. Note that the §5 targets in `REFACTOR_PLAN.md` are not all met
(`cx_max` is still 69, target ≤ 15) — do not add, and reduce if over.

---

## 6. One change, end to end

1. **Read first.** `REFACTOR_PLAN.md` records why many things are shaped the
   way they are, including deliberate asymmetries. Fighting one of those
   without knowing it is deliberate usually becomes a bug.
2. **Record the ruff baseline** (§2) before touching anything.
3. **One logical change per commit.** Extraction and behavior fixes are not
   combined — if a refactor changes behavior, the behavior change becomes its
   own `fix:`/`feat:` commit (precedent: four `eval()` calls in Phase 7).
4. **Write the test, watch it fail, then fix.**
5. **Annotate anything you write** (§3).
6. **Run the three gates** (§2). Nothing is skipped because it is "only one
   line".
7. **Remove the ledger line** if the file you touched is now clean (§5).
8. **Commit** (§7).

Done means: 0 failing tests, no new ruff findings, pyrefly 0 errors, new code
annotated, and the new test proven able to fail. If part of it cannot be
finished, say which part and why — do not stay silent.

---

## 7. Commit

Summary: `<type>: <lowercase imperative>` — `feat`, `fix`, `refactor`,
`perf`, `docs`. The body explains **why**, not a repeat of the diff: what
problem existed before, what changed behaviorally, and the verification
results (test count, harness scenarios, mutations caught, results in a real
environment if any). End with:

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

**Language.** This is not a preference, it is a pattern already consistent in
the repo:

| Where | Language |
|---|---|
| Commit messages | English |
| Docstrings & comments in `bot/`, `web/`, `tests/` | English |
| `AGENTS.md` (this file, auto-loaded at session start) | English |
| Process docs (`AGENTS.id.md`, `REFACTOR_PLAN.md`), `pyproject.toml` comments, `tools/` docstrings | Indonesian |

Commit and push only when asked.

---

## 8. Two remotes — one fork, one personal repo

This checkout is wired to three remotes. They are not interchangeable:

| Remote | Repository | Role |
|---|---|---|
| `upstream` | `anasty17/mirror-leech-telegram-bot` | Read-only source of truth. Never push here. |
| `origin` | `hikari-work/mirror-leech-telegram-bot` | GitHub fork. `master` is kept **byte-identical** to `upstream/master` so a PR branch cut from it contains only that PR's commits. |
| `personal` | `hikari-work/mirror-leech-hikari` | The diverged tree — every local feature commit lives here. `master` tracks this remote. |

Why the split: `master` on the fork once carried 161 feature commits on top
of upstream. A PR opened from such a branch shows all 161, not the one change
you meant to send. The fork's `master` was force-pushed back to
`upstream/master`; the 162-commit tree is preserved on `personal/master`.

Working rules:

- **Local `master` tracks `personal/master`.** Run the gates (§2) here and
  push feature work with `git push personal master`.
- **Contributing upstream:** cut the branch from `upstream/master`, never from
  local `master`, then push the branch to the fork.

  ```bash
  git fetch upstream
  git checkout -b fix-a-upstream-bug upstream/master
  # ... commit ...
  git push origin fix-a-upstream-bug
  gh pr create --repo anasty17/mirror-leech-telegram-bot --head hikari-work:fix-a-upstream-bug
  ```

- **Pulling upstream fixes** into the personal tree:

  ```bash
  git fetch upstream
  git merge upstream/master        # on local master, then push personal
  ```

- **Never** push local feature commits to `origin/master`. The fork's `master`
  must equal `upstream/master`; verify with:

  ```bash
  git fetch upstream && git diff --stat upstream/master...origin/master   # must be empty
  ```

### Two worktrees, one `.git`

Both trees share a single object store, so nothing is cloned twice:

| Directory | Branch | For |
|---|---|---|
| `~/mirror-leech-telegram-bot` | `master` → `personal/master` | personal work, and the deployment |
| `~/mirror-leech-contrib` | `contrib` → `upstream/master` | upstream contributions only |

The directory names do **not** say which remote they belong to -- the personal
tree is the one named `mirror-leech-telegram-bot`, while the fork it was named
after is only a remote-tracking ref there. Check `git branch -vv`, not `pwd`.

`contrib` is a long-lived branch parked on `upstream/master`, not a per-PR
branch. Start a PR by branching off it inside the worktree:

```bash
cd ~/mirror-leech-contrib
git fetch upstream && git merge --ff-only upstream/master   # on contrib
git switch -c feat/some-fix                                 # now branch from here
# ... commit ...
git push origin feat/some-fix
gh pr create --repo anasty17/mirror-leech-telegram-bot --head hikari-work:feat/some-fix
git switch contrib && git branch -D feat/some-fix           # after the merge
```

The worktree has no `.venv`, `config.py` or `.env`: it is for commits, not for
running the bot. Run the gates (§2) from the personal tree.

`git worktree list` shows both; `git worktree remove ~/mirror-leech-contrib`
drops the second one without touching the personal checkout.

### A deployment must point at `personal`, never at the fork

`update.py` rewrites the running checkout on every boot: it deletes `.git`,
`git add .`s the whole tree into one throwaway commit, then
`git reset --hard origin/{UPSTREAM_BRANCH}`. Two things follow, and both have
already cost a deployment.

**1. `UPSTREAM_REPO` decides which code the next restart installs.** Point it
at the fork and a restart installs plain upstream -- the 162 feature commits
are gone from the checkout, while `config.py`, the sessions and the downloads
survive, so the damage is silent until a command that no longer exists is
typed. A deployment's `UPSTREAM_REPO` must be the personal remote:

```
UPSTREAM_REPO   = 'https://github.com/hikari-work/mirror-leech-hikari.git'
UPSTREAM_BRANCH = 'master'
```

It is not only read from `config.py`. `update.py` also reads
`settings_config.data` from PostgreSQL for the running `bot_id` and lets that
override the file, and the bot itself re-reads `UPSTREAM_REPO` from the same
row at runtime. **Every `bot_id` in that table is a loaded gun**: starting any
of them re-runs `update.py` against *its* stored repo, into the *shared*
`/app`. Change one row and the running bot keeps its code; miss one and the
next bot to boot replaces the code under all of them. Audit the whole table,
not the row of the bot you happen to be looking at.

**2. `git add .` commits untracked files, and `reset --hard` deletes them.**
Anything the ignore file does not cover is committed into the throwaway commit
and then wiped. In practice that is the qBittorrent state:
`qBittorrent/data/nova3/**` (search plugins), `qBittorrent/data/GeoDB/*.mmdb`,
`qBittorrent/config/{categories,watched_folders}.json`, `rss/`, and the
edited `qBittorrent/config/qBittorrent.conf` -- which carries
`WebUI\Password_PBKDF2` and the chosen listen ports.

Some of that regenerates (the plugins are reinstalled from
`Config.SEARCH_PLUGINS`, GeoDB is re-downloaded), but the WebUI password and
the port choices only live in that file. Back it up before any update that
touches the checkout:

```bash
docker cp <container>:/app/qBittorrent/config/qBittorrent.conf ./qbit-backup.conf
```

**Updating a deployment safely.** A restart is only free when nothing is
running: `aria2.tellActive` empty, `curl /api/v2/torrents/info` on the
qBittorrent WebUI empty, and no `ffmpeg`/`7z`/`split` child. Then either let
`/restart` run `update.py` after backing the files above up, or do the reset by
hand and keep the local edits:

```bash
# inside the container, before the reset
tar czf /tmp/qb.tgz \
  qBittorrent/config/qBittorrent.conf qBittorrent/config/qBittorrent-data.conf \
  qBittorrent/config/categories.json qBittorrent/config/watched_folders.json \
  qBittorrent/config/rss qBittorrent/data/GeoDB qBittorrent/data/nova3
git stash push -u -- qBittorrent/config/qBittorrent.conf aria-nox.sh

git merge --ff-only origin/master   # or let update.py do this on next boot
tar xzf /tmp/qb.tgz                 # restore what the merge removed
```

Note that `update.py` is not optional for a *boot* -- `start.sh` always runs
it -- so the safe path is to make its inputs correct rather than to avoid it.

Do not put hostnames, paths or credentials of a live deployment in this file:
the repository is public. The invariants above are the part that transfers.
