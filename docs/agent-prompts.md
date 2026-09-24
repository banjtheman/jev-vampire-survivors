# Prompts for a coding agent

Paste these tasks into Codex, Claude Code or another coding agent with this
repository open. These instruct the coding agent; the prompts actually sent to
Jev live in [`vampire_agent/policy.py`](../vampire_agent/policy.py).

Install the optional [TypeSafe AI skill](https://www.skills.sh/typesafe-ai/skills/typesafe-ai)
and ask your agent to use it for TypeSafe work:

```sh
npx skills add https://github.com/typesafe-ai/skills --skill typesafe-ai
```

Live requests need your own `TYPESAFE_API_KEY` in a local `.env`. Never paste it into the
conversation. Offline tests, bridge observation and rendering need no key.

## Set up without making model calls

```text
Set up this Jev/Vampire Survivors repository locally. Use the typesafe-ai
skill for Jev-related work. Read README.md, docs/getting-started.md,
pyproject.toml and applicable local instructions first.

Find a Python 3.10+ interpreter, create or reuse .venv, and install the
project with pip install -e '.[video]'. Check ffmpeg, the .NET SDK and the
.NET 8 runtime. Run the offline demo and Python tests. Do not call the API
or start gameplay for this setup task.

Check for .env without printing its contents. Create it from .env.example
only if absent, preserving any existing credential. If the key is missing,
tell me to edit it locally; never ask for it in chat.

Verify the installed game and platform where possible. Only macOS Apple
Silicon Steam Unity Mono, game 1.16.107/build 25016043, has been verified.
Build the private app with tools/launch_vampire.py --build-only if the
requirements are present. Keep all game files and compiled output private.
Report checks actually performed and the commands to launch and observe.
Offline tests and a successful build alone do not prove live compatibility.
```

## Play one bounded, recorded test

```text
Run one recorded Jev/Vampire Survivors test. Use the typesafe-ai skill and
read the setup guide, current CLI and policy first. I authorize one run
capped at 360 seconds, 1400 requests and $0.18 estimated known API cost.
Do not raise these limits or automatically start a replacement run.
Never reveal the API key. Use a fresh session name.

Launch with the repository's native launcher. Use offline solo, windowed
and muted. Select Gennaro, Mad Forest and Hyper off if available; preserve
and report the existing powerups. Do not purchase or unlock anything to
match that setup. If unavailable, report that before running another setup.
Leave the confirmed stage's Start action offered, then use run --start-run
so recording is active when gameplay begins. Keep the policy unchanged.

Let Jev choose movement and offered upgrades. Stop at an actual terminal
state or the first configured limit, release movement, and verify the game
is paused after a nonterminal stop. Preserve the original decision logs
and captures. Report observed game time, level, HP, outcome, attempted and
applied actions, errors, known usage, estimated cost, unknown-usage count
and latency. A time-limited test is not a win or a completed stage.
```

## Improve a specific behavior offline

```text
Investigate a focused improvement to this Vampire Survivors policy. Use the
typesafe-ai skill and current Choice/state guidance. Read docs/experiments.md,
vampire_agent/policy.py, vampire_agent/session.py and the bridge protocol.
Use existing local recordings if present; otherwise use clearly labeled
synthetic fixtures. Do not assume historical recordings ship in this clone.

Separate missing or incorrect observations, model decisions and execution
delays. Inspect the actual decision_state/candidates and applied status.
The v4 test held position too long before late damage; treat that as a
hypothesis to investigate, not proof that every stay action is wrong.

Make one focused change to the runtime prompt or supplied information.
Keep every applied combat action attributable to Jev; do not silently add
a deterministic autopilot. Preserve observation freshness, legality,
manual input priority and raw probabilities. Add meaningful regression
tests, update POLICY_VERSION, and document what remains unmodeled.

Complete the offline change and prepare a bounded comparison command.
Do not launch the game or make paid API calls in this task. A subsequent
comparison should hold stage, character, modes, powerups, game version,
model and limits as constant as practical and retain losses and partial
runs. Do not claim a win-rate improvement from one attempt.
```

## Render a completed recording

```text
Render a completed Vampire Survivors recording in this repository. If
several completed sessions exist, summarize them so I can select one.
Read the selected metadata, summary and timestamps. Do not launch the
game or call Jev for a rendering task.

Use tools/render_vampire_timelapse.py with combat at 4x and menus at 1x.
Start on gameplay, using the first combat frame rather than a fixed trim.
Choose a new --output filename to preserve any previous export.

Keep actual captured pixels and logged telemetry synchronized. Show
probabilities only after a decision completes; distinguish selected from
applied actions and label estimated cost and unknown usage. Do not invent
missing data, game outcomes or prose presented as Jev's thoughts.

Inspect the actual MP4 opening, a level-up/chest section if captured, and
the ending. Check codec, dimensions and duration, and provide video and
poster paths. Explain any capture or timing limitation.
```

## Runtime prompt and information edit points

Both runtime prompts are defined once in
[`vampire_agent/policy.py`](../vampire_agent/policy.py):

| Source | Purpose |
| --- | --- |
| `INSTRUCTIONS` | Movement judgment: survive, collect XP and position weapons toward useful targets. |
| `MENU_INSTRUCTIONS` | Choose among the exact offered upgrades/chest actions. |
| `prepare_decision()` | Calculate approximate movement clearance, pickup progress and attack opportunities. |
| `compact_decision()` | Select and round the fields sent to Jev. |
| `PolicyMemory` | Summarize observed progress and successfully applied actions. |
| `POLICY_VERSION` | Label an experiment's policy. |

The bridge observes player stats, enemies, pickups and inventory. The model gets
compact geometry and horde/weapon context instead of a raw dump of every enemy.
See the [protocol](../vampire_bridge/README.md) for field meanings and limits.
Exact instructions are saved with each session, and `decisions.jsonl` records
the actual request inputs separately from full observations.

Jev returns a typed Choice with probabilities and confidence, not a prose
explanation of its reasoning. Keep execution, arithmetic and game legality in
code. The [Choice documentation](https://docs.typesafe.ai/primitives/choice)
and [TypeSafe API](https://docs.typesafe.ai/api) describe the model interface.
