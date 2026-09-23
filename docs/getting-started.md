# Getting started

This source-only harness builds a local bridge for your own copy of Vampire
Survivors. The bridge can be used without Jev. Live Jev decisions require your
own `JEV_KEY`; observation, offline tests and rendering do not.

## Requirements

- **macOS on Apple Silicon**, with the Steam **Unity Mono** edition installed.
  Development was verified on game **1.16.107**, Steam build **25016043**, Unity
  **6000.0.62f1**. Other game versions, Intel Macs, Windows, Linux and IL2CPP
  builds are unverified.
- Python **3.10+**, and a **.NET 8 SDK with the .NET 8 runtime** available to
  `dotnet`. The patcher targets `net8.0`; a newer SDK/runtime alone is not a
  substitute for the .NET 8 runtime. Check with `dotnet --list-sdks` and
  `dotnet --list-runtimes`.
- Enough disk space for a private copy of the installed app and recordings.
- For videos: `ffmpeg` on `PATH` and the Python `video` extra below.

The launcher compiles our bridge against your installed assemblies, copies the
app into ignored `.local/vampire-runtime/`, then patches that copy. It checks
that the original runtime assembly is unchanged. **The copy still uses your
normal game save**: gameplay can change progress and unlocks. This is for offline
solo play.

## Install the harness

Choose a Python 3.10+ interpreter, then run these commands from a terminal:

```sh
git clone https://github.com/banjtheman/jev-vampire-survivors.git
cd jev-vampire-survivors
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[video]'
python -m unittest discover -s tests -v
python -m vampire_agent demo
```

The demo uses explicitly synthetic game state and makes no API request unless
you add `--live`. It does not prove that your game version is compatible.

For live decisions, copy `.env.example` to `.env` **only if `.env` does not
already exist**, then edit the local file:

```dotenv
JEV_KEY=replace_with_your_own_key
```

Never paste the real key into a chat, issue, screenshot or commit. The controller
loads it locally, and the launcher removes `JEV_KEY` from the game's environment.
For a different credentials file, place the global option before the command:
`python -m vampire_agent --env /path/to/private.env run ...`.

You can install the optional [TypeSafe AI skill](https://www.skills.sh/typesafe-ai/skills/typesafe-ai)
for your coding agent:

```sh
npx skills add https://github.com/typesafe-ai/skills --skill typesafe-ai
```

Node.js/`npx` is only needed for this skill installer. See
[copy-paste agent prompts](agent-prompts.md).

## Launch and observe

Keep Steam available and close any previous bridged game. In terminal one:

```sh
source .venv/bin/activate
python tools/launch_vampire.py --build-only
python tools/launch_vampire.py
```

The first build downloads Mono.Cecil through NuGet. Leave the launcher running.
The game is windowed and muted by default; `--audio` enables sound. For a
nonstandard Steam library, pass `--app "/path/to/Vampire_Survivors.app"` to
both commands.

After the main menu loads, in terminal two:

```sh
source .venv/bin/activate
python -m vampire_agent observe
```

This prints game state from `127.0.0.1:4244` without calling Jev. Only one client
can connect at a time; close custom bridge clients before running the controller.
The [bridge protocol](../vampire_bridge/README.md) documents direct integration.
The loopback socket is unauthenticated: do not expose it through tunnels or port
forwarding.

## Run a recorded test

In the game, select an available character and stage. For a repeatable starting
point, use **Gennaro, Mad Forest, Hyper off**, if unlocked. Record your saved
powerups and other modes when comparing runs. Confirm the stage and leave its
**Start** action offered; the runner can press that exact action after recording
starts.

Keep the game **windowed, not minimized**. On the verified Mac, minimizing
sometimes shrank the actual Unity framebuffer. Captures below 640×360 are
rejected. Window preferences can override the launch resolution.

Start with a short test using a new session name:

```sh
python -m vampire_agent run --start-run --session smoke-01 --seconds 60 --max-calls 120 --max-cost 0.02
```

The first reached time, request or estimated-cost limit ends the test. It may
stop before 60 seconds. `--max-cost` is an estimate based on reported usage, not
a provider billing cap; an in-flight request or unreported usage can exceed it.
The controller requests a pause when a nonterminal test stops during combat.
Ctrl+C releases movement and ends the session; a disconnected game cannot be
guaranteed to pause, but its movement lease expires.

After a successful smoke test, a six-minute experiment can use:

```sh
python -m vampire_agent run --start-run --session forest-01 --seconds 360 --max-calls 1400 --max-cost 0.18
```

Start a fresh game and use an unused session name for each experiment. If you
start combat manually, omit `--start-run`. The controller chooses movement and
offered upgrades/chest actions; setup and unsupported menus remain manual.
Manual keyboard/controller movement takes priority. To cancel injected movement
from a separate command after the controller disconnects, run
`python -m vampire_agent release`.

Outputs are local under `recordings/vampire-forest-01/`:

| File | Contents |
| --- | --- |
| `metadata.json` | Exact prompts, policy version, model and configured limits. |
| `decisions.jsonl` | Observations, actual model inputs, probabilities, usage and applied/rejected status. |
| `summary.json` | Observed outcome, final stats and aggregate measurements. |
| `frames/`, `frames.jsonl`, `capture.json` | Actual game captures and capture timing. |

## Render the video

After the test has stopped:

```sh
python tools/render_vampire_timelapse.py recordings/vampire-forest-01
```

The default export opens on the first combat frame, plays combat at **4×** and
menus at **1×**, and shows actions, probabilities, latency, tokens and estimated
cost. It writes an MP4, PNG poster and edit metadata beside the recording.
Use `--output` for a new export filename when preserving an earlier render.
`--start-at` and `--poster-at` use seconds in the source recording.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing runtime assembly or patch hook | Confirm this is the Steam Unity Mono edition. A game update may require a compatibility change. |
| .NET cannot run the patcher | Confirm both an SDK and `Microsoft.NETCore.App 8.x` appear in the `dotnet` listings. |
| Private copy differs from installed version | Close the bridged game, move `.local/vampire-runtime/` aside, and rebuild from your current installation. Never copy patched files into Steam. |
| Connection refused | Wait for the launched copy's main menu and inspect `.local/vampire-game.log`. |
| Bridge already running or requests hang | Close the previous controller/client; the bridge allows one client. Close the old bridged game before rebuilding. |
| Tiny capture or recording failure | Restore a normal window and retry with a new session name. |
| `start_run_unavailable` | Confirm the character/stage and leave the Start action offered, or start manually and omit `--start-run`. |

See [experiments and known limits](experiments.md) before interpreting a run as
a policy improvement. This harness has not demonstrated a completed stage.
