# Experiments and known limits

The latest local test exercised the `horde-and-weapons-v4` policy on
**2026-09-22**. Gennaro reached **5:53 in-game, level 8, 65.6 / 145.2 HP**
before the six-minute wall-clock limit. The game was paused afterward.
This was a partial test, not a completed stage or a win.

These are sanitized historical results. Raw recordings, saves, local filesystem
paths, game assemblies and API credentials are not included in this repository.
Follow [getting started](getting-started.md) to create your own recordings.

## Two Mad Forest tests

Both used game 1.16.107 / Steam build 25016043 on macOS Apple Silicon, Gennaro,
Mad Forest, Hyper off, existing saved powerups, windowed 1920×1022 captures and
the same six-minute limit. Encounters and offered upgrades differed. These are
descriptive observations, not a controlled comparison.

| Measure | XP/facing v3 | Horde/weapon v4 |
| --- | ---: | ---: |
| Last observed game time | 5:54 | 5:53 |
| Final level | 8 | 8 |
| First observed level 2 | 1:18 | 0:59 |
| First observed level 4 | 3:18 | 2:53 |
| First observed level 8 | 5:25 | 5:10 |
| Final observed HP | 145.2 | 65.6 |
| First observed damage | 1:04 | 2:06 |
| Median API latency | 219.3 ms | 241.3 ms |
| API requests | 1,155 | 1,008 |
| Estimated known API cost | $0.07975 | $0.10484 |
| Calls with unknown usage | 1 | 0 |

The v4 run applied 983 combat movements and seven upgrade choices. It used
2,496,256 input and 91,602 output tokens. Nine responses expired, six actions
were rejected after phase changes, and three inconsistent Choice responses
were rejected because their choice did not match the highest probability.
Rejected responses were not applied; known usage was still counted.

The figures use the last controller observation, which can precede the final
captured frame or pause by a small interval. Historical estimated costs used
the price recorded with each session, not a promise of current service pricing.

## What v4 changed

The prompt asks Jev to aim into nearby groups from a safe edge, keep useful
facing while collecting XP, consider low-HP enemies, and change heading if
movement stalls. Escape takes priority when no candidate meets the clearance
margin. Jev still selects every applied combat action.

The bridge added enemy HP/type/instance IDs and native weapon power, amount,
area, firing-cycle timing and verified aim modes. Python summarizes enemies
into compass sectors, reporting counts, nearby counts, nearest distance and
nearest HP. Candidate movements include predicted forward-cone counts and
the nearest enemy's HP. See [the protocol](../vampire_bridge/README.md) for exact
field meanings.

An audit of the first 180 game seconds recomputed both runs with the same
geometry. When any candidate met the 0.6-unit clearance margin and had an enemy
in its forward cone, v4 selected one on 266/347 decisions (76.7%), versus
172/316 (54.4%) for v3. This measures modeled alignment, not successful hits;
nearby XP can legitimately make another direction preferable.

Early leveling and modeled alignment improved in this attempt. Final health
was worse. Before late damage, Jev repeatedly held position as clearance
shrank and escaped too late. The logs support investigating delayed escape
and excessive stationary firing; they do not isolate causality or establish
better survival. No win rate has been established.

## Important limitations

- Geometry predicts constant movement for half a second. It does not model
  terrain, obstacles, future spawns or all projectile effects.
- The game advances while Jev responds. A 500-ms movement lease can expire
  during a slow request, leaving the character stationary. Responses older
  than 850 ms are discarded.
- A clearance margin is a heuristic, not a safety guarantee. Candidate
  predictions can become stale before application.
- The bridge exports only the closest 256 enemies and 64 pickups. It reports
  enemy truncation, but unseen enemies can still affect play.
- Forward-cone counts do not model weapon range, line of sight, travel time or
  confirmed hits. Weapon power is not guaranteed damage, and the firing-cycle
  countdown is not necessarily the time to the next staggered projectile.
- Unverified weapon aim modes and unavailable numerical fields are omitted.
  Pooled Unity instance IDs are not permanent identities across spawns.
- Chest actions are implemented but were not exercised in these tests.
- Minimizing the game sometimes produced a tiny framebuffer. The recorder
  rejects dimensions below 640×360; keep the window restored.

## Comparing your own changes

Keep stage, character, modes, saved powerups, game version, model and limits as
constant as practical. Change one hypothesis at a time, label `POLICY_VERSION`,
and retain every result. Record setup separately when the bridge cannot verify
it. Runs on different stages or inherited powerups cannot isolate prompt effects.

Inspect `summary.json` alongside `decisions.jsonl`. Compare observed survival,
growth, HP, actual applied actions, errors, latency and cost. The logs retain
full `state` and `candidate_geometry` separately from compact `decision_state`
and `candidates`, so observation and selection errors can be distinguished.
Use multiple runs before claiming an improvement. Offline tests alone cannot
verify that a runtime hook works in a newly updated game.
