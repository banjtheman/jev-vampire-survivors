# Contributing

Useful contributions include reproducible control fixes, better observed state,
measured policy changes, recording improvements and verified game compatibility.
Start with [setup](docs/getting-started.md), [experiments](docs/experiments.md)
and the [bridge protocol](vampire_bridge/README.md).

## Development

Use a Python 3.10+ virtual environment and your own installed game:

```sh
python -m pip install -e '.[video]'
python -m unittest discover -s tests -v
python -m vampire_agent demo
```

These checks make no API requests. Native build verification additionally needs
the installed macOS Steam Unity Mono game, a .NET SDK and the .NET 8 runtime:

```sh
python tools/launch_vampire.py --build-only
```

Close any running bridged game before rebuilding. Offline tests and compilation
do not establish live compatibility. The verified configuration is macOS Apple
Silicon, game 1.16.107 / Steam build 25016043, offline solo.

For a bug fix, add a meaningful regression test or document a reproducible live
check. Inspect rendered changes in an actual video, including its opening, menu
transition and ending. Label synthetic fixtures clearly.

## Preserve the control and data boundaries

- Keep the bridge on loopback, with short movement leases, manual-input priority,
  fresh observations, one-use sequences and exact legal menu actions.
- Keep credentials in the Python controller. Never log keys or authorization
  headers, commit `.env`, or send the key to the game process.
- Preserve full observations, raw probabilities, selected-versus-applied status
  and unknown-usage accounting. Rendering must respect when decisions completed.
- Keep runtime prompts in [policy.py](vampire_agent/policy.py); update
  `POLICY_VERSION` for experiments. Do not claim Jev selected a scripted action.
- Modify only the private app copy. Do not commit game assemblies, assets,
  decompiled source, saves, compiled output or bulk local recordings.
- Retain required notices for third-party code. Contributions to this project's
  original code use the repository's [MIT license](LICENSE).

## Evidence in a pull request

Describe the concrete failure, changed behavior, tests completed and remaining
limits. For compatibility changes, include OS, architecture, game version,
runtime and an actual launch/control check before calling support verified.

For policy experiments, state setup, powerups, code revision, policy/model,
limits, attempted/applied actions, error categories, usage, unknown-usage count,
latency and every outcome. Retain losses and partial runs. One attempt does not
establish a win rate or isolate a strategy change from encounter variation.

Live requests consume your API allowance. Use explicit time, request and cost
limits; estimated-cost limits are not provider billing caps. Do not change the
policy while a measured run is active. Share only reviewed, sanitized excerpts
or intentionally published artifacts, not links into a private filesystem.
