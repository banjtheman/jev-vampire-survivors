# Jev plays Vampire Survivors

A local Vampire Survivors bridge, [TypeSafe Jev](https://docs.typesafe.ai/) agent,
and gameplay recorder. Jev chooses movement and offered upgrades; the game handles
automatic attacks. Recorded videos show the chosen inputs, action probabilities,
response time, token usage and estimated API cost.

The bridge can also be used by your own controller without Jev. It exposes observed
player state, enemies, pickups, inventory and native menu actions over localhost.

**Experimental:** verified on macOS Apple Silicon with the Steam Unity Mono build
**1.16.107 / Steam build 25016043**. Windows, Linux, Intel Macs and other game builds
have not been validated. Offline solo gameplay only.

## Quick start

You need your own installed copy of Vampire Survivors, Python 3.10+, and the
**.NET 8 SDK/runtime**. Video export additionally needs FFmpeg. No game assets or
compiled game assemblies are included. The launcher builds the bridge against
your installation and instruments a private copy under `.local/`; it checks that
the original installed runtime assembly is unchanged. The private copy still uses
the game's normal save, so gameplay can change your save/progression.

```sh
git clone https://github.com/banjtheman/jev-vampire-survivors.git
cd jev-vampire-survivors
# Use an installed Python 3.10+ interpreter for this first command.
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[video]'
cp -n .env.example .env
```

Edit `.env` locally and replace the placeholder with your **JEV_KEY**. A key is
required only for live Jev decisions. Never paste it into a chat or commit it.
The game process does not receive the key. Offline checks need no key or game:

```sh
python -m vampire_agent demo
python -m unittest discover -s tests -v
```

Launch the private game copy and keep this terminal running:

```sh
python tools/launch_vampire.py
```

Select a solo character and stage manually. **Gennaro, Mad Forest, Hyper off**
is the tested setup. Keep the game **windowed**; minimizing can shrink Unity's
capture buffer. Audio is muted by default (`--audio` enables it).

In another terminal, activate the same virtual environment. Once the selected
stage's START button is available, this command records before starting the run:

```sh
python -m vampire_agent run --start-run --session first-test \
  --seconds 360 --max-calls 1400 --max-cost 0.18
```

The controller pauses on a nonterminal limit and releases its movement input.
Session names must be unique. Limits apply to wall time, request count and
estimated known usage; a request already in flight can exceed the cost threshold.
Normal keyboard/controller movement takes priority.

```sh
python tools/render_vampire_timelapse.py recordings/vampire-first-test
```

The export opens directly on gameplay: **combat at 4×, menus at 1×**, with raw
model probabilities and measured telemetry. Recordings stay local and ignored.

See the [full setup and troubleshooting guide](docs/getting-started.md).

## Use it with a coding agent

Install the [TypeSafe AI skill](https://www.skills.sh/typesafe-ai/skills/typesafe-ai):

```sh
npx skills add https://github.com/typesafe-ai/skills --skill typesafe-ai
```

Node.js/`npx` is needed for skill installation, not for the Python harness.
[Copy-paste agent prompts](docs/agent-prompts.md) cover setup, a bounded live run,
policy improvements and video rendering.

## What Jev sees

- Player HP, level, cumulative XP, facing, inventory and exact offered upgrades.
- Movement clearance calculated from nearby enemy positions, velocities and sizes.
- Horde counts by direction, nearby enemy health and predicted forward-cone counts.
- Verified weapon aiming modes, native attack stats and firing-cycle information.
- XP/healing/chest distances, pickup-path counts and recent movement/XP progress.

The current policy is `horde-and-weapons-v4`. Its exact combat and menu prompts
live in [vampire_agent/policy.py](vampire_agent/policy.py). Code computes the
observations and executes legal actions; Jev selects each applied combat action.
There is no hidden scripted combat controller. Cone counts are aiming hints,
not guaranteed hits; obstacle geometry and unseen/future enemies are not modeled.

[The latest six-minute test](docs/experiments.md) reached **5:53 in-game, level 8,
65.6/145.2 HP**, then paused at its limit. It cost approximately **$0.105** with
**241 ms** median API latency. Early leveling and modeled target alignment improved
in that attempt, but late escapes remain weak. This is not a winning policy or
evidence of an improved win rate. API pricing and latency can change.

## Bridge and project layout

| Path | Purpose |
| --- | --- |
| [`vampire_bridge/`](vampire_bridge/README.md) | Original C# bridge, protocol and private-copy patcher |
| [`vampire_agent/`](vampire_agent/) | Jev client, policy, bounded runner and accounting |
| [`tools/launch_vampire.py`](tools/launch_vampire.py) | Build and launch your local game copy |
| [`tools/render_vampire_timelapse.py`](tools/render_vampire_timelapse.py) | Export recordings with telemetry |
| [`examples/bridge_observe.py`](examples/bridge_observe.py) | Read the bridge without a Jev key |
| [`tests/`](tests/) | Offline protocol, policy, client and recording tests |

The bridge listens on **127.0.0.1:4244**, accepts one client, and has no network
authentication. Keep it on loopback and use only trusted local clients. The
[protocol reference](vampire_bridge/README.md) covers observation sequences,
short input leases, freshness checks, menu actions and recording.

## License and contributions

Original harness and bridge source: [MIT](LICENSE). Vampire Survivors and its
assets belong to their respective owners and are not covered by this license.
See [third-party notes](THIRD_PARTY.md), [contribution guidance](CONTRIBUTING.md)
and [security boundaries](SECURITY.md).

Related project: [Jev plays Brotato](https://github.com/banjtheman/jev-brotato).
