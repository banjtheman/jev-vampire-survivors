"""Describe movement geometry and exact offered upgrades for Jev to choose.

Unity coordinates: +x right, +y up. Geometry predicts constant velocities for
half a second. It does not establish collision safety or infer hidden enemies.
"""
import math
from collections import deque

POLICY_VERSION = "horde-and-weapons-v4"
EXPERIENCE_KINDS = {"experience", "xp", "gem"}

DIRECTIONS = {
    "north": (0.0, 1.0), "northeast": (2 ** -.5, 2 ** -.5),
    "east": (1.0, 0.0), "southeast": (2 ** -.5, -2 ** -.5),
    "south": (0.0, -1.0), "southwest": (-2 ** -.5, -2 ** -.5),
    "west": (-1.0, 0.0), "northwest": (-2 ** -.5, 2 ** -.5),
    "stay": (0.0, 0.0),
}
INSTRUCTIONS = (
    "Choose the next Vampire Survivors movement. Avoid contact first, then collect "
    "XP to build damage before stronger waves. Negative clearance predicts contact. "
    "If no route meets the clearance margin, prioritize the clearest escape, not "
    "XP or aiming. Minimum clearance includes the starting position; when equal, "
    "prefer larger endpoint clearance. Do not stay while surrounded. "
    "Among routes meeting the margin, prioritize positive experience_progress and "
    "magnet-path pickups instead of maximizing enemy distance. Healing matters "
    "when injured. Incoming gems already fly toward you and are excluded from "
    "navigation: do not chase them. Continue a productive heading; avoid pointless "
    "reversals. Kill enemies to create more XP: among safe routes with similar XP "
    "progress, use weapon aim_mode and forward enemy counts to keep attacks pointed "
    "into a nearby group. Prefer reachable low-HP enemies when practical. If XP is "
    "distant or progress has stalled, position at a horde's edge and aim into it "
    "instead of continually fleeing empty space. Normal KNIFE follows your last "
    "movement direction; stay preserves that aim and can hold a safe firing position. "
    "Turn or dodge as enemies close; never move into contact just to aim. A dense "
    "group is an attack opportunity, not a safe place to stand. Automatic targeting "
    "weapons do not require facing. Do not treat cone counts as guaranteed hits or "
    "assume unobserved weapon behavior. If repeated movement produces almost no "
    "displacement, try another safe heading; terrain may block the route. "
    "Clearance is approximate, not guaranteed safety; leave room for response "
    "delay. Missing distances are unavailable, not zero. North is +y, east +x."
)
MENU_INSTRUCTIONS = (
    "Choose one of the legal Vampire Survivors menu actions supplied in criteria. "
    "Use the observed inventory and exact upgrade descriptions to improve survival "
    "and experience collection. Early levels need immediate killing power and "
    "coverage to generate XP; favor useful weapon upgrades or complementary "
    "coverage over small long-term bonuses while struggling to gain levels. "
    "Consider survival when health is low. Assess offered tradeoffs; "
    "do not invent unobserved stats, evolution requirements or unavailable items. "
    "For a chest or result with a continue option, select that legal option."
)


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def point(entity):
    return number(entity.get("x"), "x"), number(entity.get("y"), "y")


def clearance(start, direction, speed, enemy, player_radius, horizon):
    ex, ey = point(enemy)
    rx, ry = ex - start[0], ey - start[1]
    vx = number(enemy.get("velocity_x", 0), "velocity_x") - direction[0] * speed
    vy = number(enemy.get("velocity_y", 0), "velocity_y") - direction[1] * speed
    squared_speed = vx * vx + vy * vy
    nearest_at = max(0, min(horizon, -(rx * vx + ry * vy) / squared_speed)) if squared_speed else 0
    radii = max(0, number(enemy.get("radius", 0), "radius")) + player_radius
    return (math.hypot(rx + vx * nearest_at, ry + vy * nearest_at) - radii,
            math.hypot(rx + vx * horizon, ry + vy * horizon) - radii)


def segment_distance(position, start, end):
    """Distance from a stationary pickup to a proposed movement segment."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    squared = dx * dx + dy * dy
    fraction = max(0, min(1, ((position[0] - start[0]) * dx + (position[1] - start[1]) * dy) / squared)) if squared else 0
    return math.dist(position, (start[0] + fraction * dx, start[1] + fraction * dy))


def optional_number(value):
    """Unknown combat attributes stay unknown; zero HP is a measured value."""
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def horde_sectors(start, enemies):
    """Current positions grouped into disjoint compass sectors, within 6 units.

    These are observations of attack opportunities, not chosen targets or paths.
    Only the nearest enemy's measured HP is included to bound the model input.
    """
    sectors = {}
    for enemy in enemies:
        ex, ey = point(enemy)
        dx, dy = ex - start[0], ey - start[1]
        distance = math.hypot(dx, dy)
        if distance > 6:
            continue
        # A coincident center has no bearing; don't assign it an invented one.
        name = (max((name for name in DIRECTIONS if name != "stay"),
                    key=lambda name: dx * DIRECTIONS[name][0] + dy * DIRECTIONS[name][1])
                if distance else "overlapping")
        sector = sectors.setdefault(name, {"count": 0, "within_1_5": 0})
        sector["count"] += 1
        sector["within_1_5"] += distance <= 1.5
        if distance < sector.get("nearest_distance", math.inf):
            sector["nearest_distance"] = distance
            sector.pop("nearest_hp", None)
            hp = optional_number(enemy.get("hp"))
            if hp is not None:
                sector["nearest_hp"] = hp
    return sectors


class PolicyMemory:
    """Observed progress and applied actions, never an alternative controller."""

    def __init__(self):
        self.samples = deque(maxlen=128)
        self.actions = deque(maxlen=8)
        self.last_xp_gain = None

    def applied(self, direction):
        if direction not in DIRECTIONS:
            raise ValueError("Unknown movement direction")
        self.actions.append(direction)

    def observe(self, state):
        elapsed = number(state["elapsed"], "elapsed")
        player = state["player"]
        xp = player.get("xp")
        xp = number(xp, "player.xp") if xp is not None else None
        sample = {"elapsed": elapsed, "xp": xp, "position": point(player)}
        if self.samples and elapsed < self.samples[-1]["elapsed"]:
            self.samples.clear()
            self.actions.clear()
            self.last_xp_gain = None
        previous_xp = self.samples[-1]["xp"] if self.samples else None
        if xp is not None and (self.last_xp_gain is None or (previous_xp is not None and xp > previous_xp)):
            self.last_xp_gain = elapsed
        self.samples.append(sample)
        while len(self.samples) > 1 and self.samples[0]["elapsed"] < elapsed - 8:
            self.samples.popleft()
        first = self.samples[0]
        path_length = sum(math.dist(a["position"], b["position"]) for a, b in zip(self.samples, list(self.samples)[1:]))
        return {
            "window_seconds": round(elapsed - first["elapsed"], 1),
            "xp_gained": round(xp - first["xp"], 3) if xp is not None and first["xp"] is not None else None,
            "seconds_without_xp_gain": round(elapsed - self.last_xp_gain, 1) if xp is not None and self.last_xp_gain is not None else None,
            "distance_traveled": round(path_length, 3),
            "net_displacement": round(math.dist(first["position"], sample["position"]), 3),
            "recent_applied_actions": list(self.actions),
        }


def prepare_decision(state):
    player = state["player"]
    start = point(player)
    speed = number(player.get("speed"), "player.speed")
    if speed <= 0:
        raise ValueError("player.speed must be positive world units per second")
    radius = max(0, number(player.get("radius", 0), "player.radius"))
    horizon = .5
    enemies = state.get("enemies") or []
    pickups = state.get("pickups") or []
    experience = [item for item in pickups if str(item.get("kind", "")).lower() in EXPERIENCE_KINDS
                  and item.get("moving_to_player") is not True]
    xp_positions = [point(item) for item in experience]
    current_xp_distance = min((math.dist(start, pos) for pos in xp_positions), default=None)
    pickup_radius = player.get("pickup_radius")
    pickup_radius = max(0, number(pickup_radius, "pickup_radius")) if pickup_radius is not None else None
    facing = (player.get("facing_x"), player.get("facing_y"))
    facing = tuple(number(v, "facing") for v in facing) if all(v is not None for v in facing) else None
    clearance_margin = .6
    candidates = {}
    for name, direction in DIRECTIONS.items():
        end = (start[0] + direction[0] * speed * horizon, start[1] + direction[1] * speed * horizon)
        distances = [clearance(start, direction, speed, enemy, radius, horizon) for enemy in enemies]
        metrics = {
            "direction": name, "vector": list(direction),
            "minimum_enemy_clearance": round(min(v[0] for v in distances), 3) if distances else None,
            "endpoint_enemy_clearance": round(min(v[1] for v in distances), 3) if distances else None,
            "meets_clearance_margin": min(v[0] for v in distances) >= clearance_margin if distances else True,
        }
        for kind, kinds in (("experience", EXPERIENCE_KINDS),
                            ("healing", {"healing", "health", "food", "chicken"}),
                            ("chest", {"chest"})):
            positions = xp_positions if kind == "experience" else [point(item) for item in pickups if str(item.get("kind", "")).lower() in kinds]
            metrics[f"endpoint_{kind}_distance"] = round(min(math.dist(end, pos) for pos in positions), 3) if positions else None
        metrics["experience_progress"] = round(current_xp_distance - min(math.dist(end, pos) for pos in xp_positions), 3) if xp_positions else None
        metrics["xp_pickups_in_magnet_path"] = sum(segment_distance(pos, start, end) <= pickup_radius for pos in xp_positions) if pickup_radius is not None else None
        heading = facing if name == "stay" else direction
        if heading is not None and math.hypot(*heading) > 0:
            hx, hy = (v / math.hypot(*heading) for v in heading)
            in_front = []
            for enemy in enemies:
                ex, ey = point(enemy)
                dx = ex + number(enemy.get("velocity_x", 0), "velocity_x") * horizon - end[0]
                dy = ey + number(enemy.get("velocity_y", 0), "velocity_y") * horizon - end[1]
                distance = math.hypot(dx, dy)
                # A narrow forward corridor is an aiming hint, not a promised hit.
                if 0 < distance <= 4 and (dx * hx + dy * hy) / distance >= .94:
                    in_front.append((distance, enemy))
            nearest = min(in_front, key=lambda item: item[0]) if in_front else None
            metrics["nearest_enemy_in_firing_direction"] = round(nearest[0], 3) if nearest else None
            metrics["enemies_in_firing_direction"] = len(in_front)
            metrics["nearest_forward_enemy_hp"] = optional_number(nearest[1].get("hp")) if nearest else None
        else:
            metrics["nearest_enemy_in_firing_direction"] = None
            metrics["enemies_in_firing_direction"] = None
            metrics["nearest_forward_enemy_hp"] = None
        candidates[name] = metrics
    context = {
        "game": "Vampire Survivors", "phase": "combat", "elapsed": state.get("elapsed"),
        "player": player, "inventory": state.get("inventory", []),
        "observed_enemy_count": len(enemies), "observed_pickup_count": len(pickups),
        "enemy_count_total": state.get("enemy_count_total", len(enemies)),
        "enemy_snapshot_truncated": state.get("enemy_snapshot_truncated", False),
        "hordes_within_6_units": horde_sectors(start, enemies),
        "current_experience_distance": round(current_xp_distance, 3) if current_xp_distance is not None else None,
        "incoming_experience_pickups": sum(str(item.get("kind", "")).lower() in EXPERIENCE_KINDS and item.get("moving_to_player") is True for item in pickups),
        "clearance_margin": clearance_margin,
        "prediction_horizon_seconds": horizon, "speed_world_units_per_second": speed,
        "geometry_note": "Only observed enemies; missing velocities/radii treated as zero. Obstacles not modeled. Positive experience_progress approaches XP. Magnet-path count assumes stationary gems. Firing direction is a narrow cone within 4 units, not a guaranteed hit.",
    }
    return context, candidates


def compact_decision(context, candidates):
    """Trim redundant model input; retain full observations/geometry in the log."""
    player_fields = {"hp", "max_hp", "level", "xp", "character", "facing_x", "facing_y"}
    model_context = {key: value for key, value in context.items() if key not in {"speed_world_units_per_second", "geometry_note"}}
    model_context["player"] = {key: value for key, value in context["player"].items() if key in player_fields}
    model_context["geometry_note"] = "World units, constant velocity; no obstacles/unseen enemies. Horde sectors use current positions; within_1_5 counts close centers. Forward metrics predict a narrow 4-unit cone after movement, not hits or weapon range. Positive XP progress approaches a stationary gem."
    model_candidates = {
        name: {key: value for key, value in metrics.items()
               if key not in {"direction", "vector", "endpoint_experience_distance"}
               and value is not None}
        for name, metrics in candidates.items()
    }
    def rounded(value):
        if isinstance(value, dict):
            return {key: rounded(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rounded(item) for item in value]
        return round(value, 3) if type(value) is float else value
    return rounded(model_context), rounded(model_candidates)


def menu_decision(state):
    options = (state.get("menu") or {}).get("options") or []
    if not isinstance(options, list) or len(options) > 255:
        raise ValueError("menu.options must be an array of at most 255 legal options")
    candidates = {}
    for option in options:
        action_id = option.get("id")
        if not isinstance(action_id, str) or not action_id or action_id in candidates:
            raise ValueError("Menu option IDs must be unique, nonempty strings")
        candidates[action_id] = {key: value for key, value in option.items() if key != "id"}
    return {
        "game": "Vampire Survivors", "phase": state["phase"], "elapsed": state.get("elapsed"),
        "player": state.get("player"), "inventory": state.get("inventory", []),
    }, candidates


def demo_state():
    """Synthetic test state, never presented as recorded gameplay."""
    return {
        "seq": 1, "time_ms": 1000, "phase": "combat", "elapsed": 10,
        "player": {"x": 0, "y": 0, "hp": 60, "max_hp": 100, "speed": 3, "radius": .2, "level": 1},
        "enemies": [{"x": 1, "y": 0, "radius": .2}],
        "pickups": [{"x": -2, "y": 0, "kind": "experience"}],
        "menu": {"options": []}, "inventory": [],
    }
