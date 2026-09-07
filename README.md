# Grocery Tracker Agent — Claude Code Project Bundle

## What's in this folder

```
grocery-agent-project/
├── README.md                          ← you are here
├── PROGRESS.md                        ← tracks which stage is done (Claude Code updates this)
├── docs/
│   └── grocery-agent-plan.md          ← the full design plan (what to build)
└── .claude/
    └── skills/
        └── bottom-up-agent-build/
            └── SKILL.md               ← the build process (how to build it)
```

Claude Code automatically discovers skills placed under
`.claude/skills/` in a project — you don't need to install or
register anything. As long as you launch `claude` from inside this
folder (or a parent of it), the skill will be available and should
trigger automatically once you start talking about the plan/stages.

## How to use this

1. Copy this whole `grocery-agent-project/` folder to wherever you
   want to develop (e.g. `~/dev/grocery-agent-project`).
2. `cd` into it and run `claude` (or open it in Claude Code's
   IDE/desktop integration).
3. Use the kickoff prompt below as your first message.

## Kickoff prompt (copy-paste this as your first message to Claude Code)

```
I want to build the grocery tracker agent described in
docs/grocery-agent-plan.md, using the bottom-up-agent-build skill.

My goal is to learn the standard process of building an AI agent, not
just get working code — so please actually follow the skill's
per-stage loop: implement one stage, write and run pytest tests for
it, then stop and explain what you built and why, in plain terms, and
wait for me to confirm I understand before moving to the next stage.

Start by reading the plan and PROGRESS.md, then begin with Stage 1.
```

## What to expect

- Claude Code will read the plan and `PROGRESS.md`, then work through
  the stages **one at a time**, in the bottom-up order defined in the
  plan (domain models → core logic → persistence → tool functions →
  LLM parsing → API → Telegram integration). Note: the plan has since
  evolved past its original Stage 1–7 scope — see `PROGRESS.md` for
  what's actually been built (Telegram/receipt input replaced the
  originally-planned Siri Shortcut, plus a sandboxed code-execution
  fallback was added).
- After each stage, it will run pytest and show you the results, then
  explain what it built and why, then explicitly wait for you to say
  you understand before continuing.
- `PROGRESS.md` gets updated after each confirmed stage — so if you
  close Claude Code and come back later (even in a new terminal
  session), just say "continue the build" and it will pick up from
  wherever `PROGRESS.md` says you left off.
- If something in the plan turns out to be ambiguous or inconsistent
  once actual code is being written (this is normal — plans are
  written before code exists), Claude Code should flag it to you
  rather than silently guessing.

## If you want to adjust the process

Edit `.claude/skills/bottom-up-agent-build/SKILL.md` directly — for
example, if you decide you want two stages combined, or want the
explanations shorter/longer, or want to skip the confirmation gate
for later stages once you're comfortable. It's a plain markdown file.

## If you want to adjust the design

Edit `docs/grocery-agent-plan.md` directly, or ask Claude (in this
chat or a fresh one) to help you revise it before feeding it back into
Claude Code.
