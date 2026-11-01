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
