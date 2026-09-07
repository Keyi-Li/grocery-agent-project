---
name: bottom-up-agent-build
description: Use this skill for any work on the grocery tracker agent project (or any project with a docs/*-plan.md build plan organized into numbered Stages). Governs HOW to build, not what to build — one stage at a time, bottom-up, with mandatory pytest checkpoints and a teaching pause after every stage. Trigger this whenever the user asks to start, continue, resume, or work on "the agent," "the plan," "the next stage," or references stage numbers, even if they don't name the skill directly.
---

# Bottom-Up Agent Build (teaching mode)

This skill governs the *process* of building the project described in
`docs/grocery-agent-plan.md` (or whichever `docs/*-plan.md` is present).
The plan file is the source of truth for *what* to build (classes,
functions, data model, tool definitions). This skill is the source of
truth for *how* to build it: one stage at a time, from the bottom of
the dependency stack up, never skipping ahead, with a real teaching
pause after every stage.

**The user's stated goal is to learn the standard workflow of building
an AI agent, not just to get working code.** Optimize for
understanding at each step over speed. Do not rush past explanations
even if the code itself is simple.

## Before starting any work

1. Read `docs/*-plan.md` in full (there should be exactly one). This
   defines the Stages (numbered, bottom-up: domain models → core logic
   → persistence → tool functions → LLM parsing → API → external
   integration) and, within each stage, what classes/functions belong
   there.
2. Read `PROGRESS.md` in the project root if it exists. It records
   which stage was last completed and confirmed. If it doesn't exist
   yet, create it (template at the bottom of this file) and treat the
   project as starting from Stage 1.
3. Never start work on a stage without checking `PROGRESS.md` first —
   this is what makes the workflow resumable across separate Claude
   Code sessions.

## The per-stage loop (repeat for every stage, no exceptions)

### 1. Announce the stage
Before writing any code, state in plain language: which stage this is,
what it depends on from prior stages (or "nothing — this is the
foundation" for Stage 1), and what will exist by the end of it. Keep
this to a short paragraph, not a wall of text.

### 2. Implement only this stage
Write the classes/functions the plan assigns to this stage — nothing
from a later stage, even if it would be convenient to peek ahead
(e.g. don't reach into database code while doing Stage 1 domain
models, even as a stub). Bottom-up means each stage should be
completable and testable using only what already exists.

### 3. Write and run tests for this stage
Use pytest. Tests must actually run and pass before moving on —
this is a hard gate, not a suggestion. If a test fails, fix the
code (or, if the plan itself has an inconsistency, surface that to
the user rather than silently patching around it) and re-run until
green. Show the user the test output.

### 4. Explain what was built — this is the teaching step, do not skip it
After tests pass, stop and explain, in plain language:
- What each new class/function does and *why it's shaped this way*
  (e.g. why `consume_from_batches` takes a list of batches rather
  than querying the DB directly — because Stage 2 is DB-free by
  design).
- How this stage's pieces connect to what was built in earlier
  stages (or, for Stage 1, why these are the right foundational
  units).
- Any design decision embedded in the plan that's worth calling
  out as a general agent-building pattern (e.g. "this is the
  standard separation between domain models and persistence — it's
  why the tests for Stage 1 don't need a database at all").

Keep the explanation concrete — refer to the actual code just
written, not abstract theory.

### 5. Check understanding before proceeding — this is a real gate, not a formality
End the explanation by directly asking whether it makes sense and
whether the user has questions, and **wait for their response before
writing any Stage N+1 code.** If they ask questions, answer them
fully — including tangents into general agent/software-design
concepts if that's what clarifies things — and ask again if it's
clear now. Do not proceed to the next stage on an ambiguous or
partial acknowledgment; if in doubt, ask.

Signals that mean "proceed": explicit confirmation ("makes sense",
"let's continue", "next stage", "go ahead"), or the user asking a
question and then confirming after your answer.
Signals that mean "do not proceed": no response to the check yet, a
follow-up question (answer it, then check again), or any expression
of confusion.

### 6. Update PROGRESS.md
Once the user confirms understanding, update `PROGRESS.md`: mark the
stage complete, note the date, note any deviations from the plan made
during implementation (and why), and note which stage is next. Then
stop — do not auto-start the next stage in the same turn. Wait for the
user to say they're ready to continue (they may want a break between
sessions).

## Rules that apply throughout

- **Never skip a stage or jump ahead.** If the user asks to jump to a
  later stage before earlier ones are marked complete in
  `PROGRESS.md`, point this out and ask if they really want to skip
  the teaching/testing steps for the intervening stages, rather than
  silently complying.
- **Never combine two stages into one turn**, even if they seem small,
  unless the user explicitly asks to move faster for the rest of the
  session (and even then, still run tests and give at least a brief
  explanation per stage — don't drop the gate entirely).
- **If the plan is ambiguous or missing something needed to implement
  a stage** (this can happen — plans are written before code exists),
  say so explicitly and propose a reasonable resolution, rather than
  quietly inventing behavior that isn't in the plan.
- **Tests live alongside the stage they test** (e.g.
  `tests/test_stage1_domain.py`), so the growing test suite itself
  becomes a visible record of progress.
- If the user asks a question unrelated to the current stage (e.g.
  about deployment, or a totally different topic), answer it normally
  — this skill governs the build loop, not every message in the
  conversation.

## PROGRESS.md template (create this if it doesn't exist)

```markdown
# Build Progress

Plan: docs/grocery-agent-plan.md

## Stage 1 — Domain models
Status: not started
Completed:
Notes:

## Stage 2 — Core business logic / services
Status: not started
Completed:
Notes:

## Stage 3 — Persistence layer
Status: not started
Completed:
Notes:

## Stage 4 — Tool functions
Status: not started
Completed:
Notes:

## Stage 5 — LLM parsing layer
Status: not started
Completed:
Notes:

## Stage 6 — API layer
Status: not started
Completed:
Notes:

## Stage 7 — Voice integration + device registration
Status: not started
Completed:
Notes:
```
