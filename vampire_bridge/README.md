# Vampire Survivors local bridge

Original Unity Mono integration for the user's installed macOS Steam game.
`tools/launch_vampire.py` builds `Bridge.cs` and uses the Mono.Cecil patcher to add
bootstrap/input hooks to a **private copy** of the runtime assembly. Do not
distribute the copied app, patched assembly, game libraries, or local decompilation
output. The project source contains integration code only.

The installed build used during development is Vampire Survivors **1.16.107**,
Steam build **25016043**, Unity **6000.0.62f1**, on Apple Silicon. Compatibility
with other versions/platforms is not established.

## Protocol

Single-client TCP on `127.0.0.1:4244`, newline-delimited JSON. Unity reads, writes
and callbacks execute on the main thread. Each response repeats the request ID:

```json
{"id":1,"cmd":"observe"}
{"id":1,"ok":true,"result":{"seq":1,"phase":"combat"}}
```

Failed commands return `{"id":1,"ok":false,"error":"reason"}`. An observation
includes `seq`, `time_ms` (bridge realtime), `elapsed` (game seconds), `scene`,
`phase`, player stats, nearby enemies/pickups, inventory, exact menu candidates
and recorder status. Unity coordinates use +x right and +y up. Entity lists are
capped at the closest 256 enemies and 64 pickups.

Observation fields available to the controller:

| Field | Meaning |
| --- | --- |
| `player.facing_x`, `player.facing_y` | Native last movement direction, used to aim normal Knife attacks. Staying still preserves it. |
| `player.pickup_radius` | Native magnet body radius converted to Unity world units. |
| `player.xp` | Cumulative XP, not a percentage of the current level. |
| `player.stats.armor` | Native armor value. |
| `player.stats.cooldown` | Base cooldown multiplier; lower is better, with other modifiers possible for effective weapon intervals. |
| `player.stats.amount` | Additional-projectile bonus, not a weapon's total projectile count. |
| `player.stats.growth` | Native XP growth multiplier. |
| `pickups[].id`, `pickups[].value` | Unity instance ID and native pickup value; gem value precedes XP multipliers. |
| `pickups[].moving_to_player` | Native `GoToPlayer` flag; true means the pickup is already incoming. Missing or unknown native values remain false. |
| `enemies[].id`, `enemies[].type`, `enemies[].hp` | Unity instance ID, native enemy type and current HP. IDs can be reused when pooled objects represent later spawns. |
| `enemy_count_total`, `enemy_count_exported` | Active enemies observed in the native collection, and the number included in this snapshot. |
| `enemy_snapshot_truncated` | True when the nearest-enemy cap omitted enemies. |
| `inventory[].attack` | Verified native attack stats for active weapons; accessories do not receive this object. |

Weapon `attack` contains `power`, `amount`, `area_multiplier`, and
`interval_seconds` when available. `power` includes player modifiers, but critical
hits and target modifiers can change actual damage. `amount` is the weapon's
effective amount stat. The interval parameter is converted from native
milliseconds; weapons may add other timing rules. `cycle_remaining_seconds`
reports an active, unpaused firing timer's remaining game-time seconds. It does
not promise the time of the next projectile because a firing cycle can stagger
several shots. A paused timer exposes `cycle_paused: true` instead; unavailable,
completed or cancelled timers omit the countdown.

Verified `aim_mode` values are `last_movement_direction` for normal Knife,
`upward_arc_with_facing_spread` for normal Axe, `nearest_enemy_on_fire` for Magic
Wand, and `horizontal_alternating_from_facing` for Whip. Native homing variants
use `nearest_enemy_on_fire` for Knife and `nearest_enemy_then_arc` for Axe.
Unknown weapon classes omit the aim mode rather than assuming a targeting rule.

Optional stats are omitted if their native methods are unavailable. Pickup IDs
identify live Unity objects; pooled objects can reuse an ID for a later pickup.
These fields describe observations only. The Python policy computes approximate
clearance, XP progress and an eight-second observation history; Jev still chooses
each movement. See [policy instructions and edit points](../README.md#what-jev-sees).

The v4 policy excludes incoming gems from XP navigation targets. Its
0.6-world-unit clearance margin and path counts for the remaining stationary
targets are heuristics, not collision or collection guarantees. The bridge does
not export an obstacle map, and the world can change during API response delay.
The shorter API prompt/input does not replace full observation logging:
`decisions.jsonl` keeps `state` and combat `candidate_geometry` separately from
the actual compact `decision_state` and `candidates` sent to Jev.

| Command | Parameters and behavior |
| --- | --- |
| `observe` | Return a fresh snapshot and advance its sequence. |
| `move` | `seq`, `dx`, `dy`, `ttl_ms`; only combat, normalized input, lifetime 1–1000 ms. |
| `release` | Cancel the movement lease immediately. |
| `act` | `seq`, `action_id`; exact exported choice, native game callback. |
| `pause` | Enter the native pause state from combat. |
| `record_start` | `path`, `fps`; new `vampire-*` session directly under `JEV_RECORD_ROOT`, at most 4 fps. |
| `record_status` | Active flag, frame count, timestamps and error. |
| `record_stop` | Flush and close the recorder. |
| `quit` | Quit this instrumented game copy through Unity. |

Movement requires an observation less than one second old and the same player,
scene and phase. Menu choices require a matching fingerprint and an observation
under 30 seconds old. Successful actions consume their sequence, rejecting replay.
Real keyboard/controller movement clears the lease and takes priority for one
second. Disconnects clear agent movement.

Menu choices cover offered level-ups, treasure opening/claiming, native resume,
and a narrow initial setup allowlist. Initial setup can confirm the currently
highlighted **available** character and **unlocked** stage. It cannot purchase
characters, change difficulty or invoke arbitrary methods. The Python controller
normally leaves setup manual; `--start-run` can begin the already confirmed stage
after recording starts.

`JEV_MUTE=1` mutes this process. The launcher defaults to muted; `--audio` opts in
to normal audio. The API key is never passed to the game.

## Recording

The recorder captures the actual game framebuffer after rendering, including UI.
It writes `frames/*.png`, `frames.jsonl`, and `capture.json`. The Python controller
owns `metadata.json`, `decisions.jsonl`, and `summary.json`. Capture timestamps and
decision logs share recording elapsed time; the game's clock can pause separately.
The renderer opens at the first combat frame, speeds combat to 4× and keeps menus
at 1×.

Keep the game windowed while recording. One minimized-window test caused Unity's
actual backbuffer to shrink to 73×249; another earlier minimized test worked, so
minimization is not reliable on this setup. The recorder rejects dimensions below
640×360 before starting or writing a frame, and exposes `screen_width`,
`screen_height`, `last_capture_width`, and `last_capture_height` in its status.
Restore a normal window before retrying with a new session name.

The patcher targets .NET 8, so install the .NET 8 SDK/runtime. Bridge compilation references the installed
Unity/JSON assemblies, while the patcher uses [Mono.Cecil](https://github.com/jbevain/cecil)
0.11.6. Short IL branches are widened before injection. The launcher checks the
original assembly hash and rejects writes through symlinks outside the copy.
