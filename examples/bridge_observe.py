"""Read the running local bridge; no Jev API call or gameplay action.

Run from the repository root after installing the editable project:
    python examples/bridge_observe.py
"""
import json
from vampire_agent.bridge import BridgeClient


def main():
    with BridgeClient() as bridge:
        state = bridge.observe()
        print(json.dumps({
            "phase": state.get("phase"), "elapsed": state.get("elapsed"),
            "player": state.get("player"), "inventory": state.get("inventory"),
            "enemy_count_total": state.get("enemy_count_total"),
            "menu": state.get("menu"),
        }, indent=2))


if __name__ == "__main__":
    main()
