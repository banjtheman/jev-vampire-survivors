"""Localhost-only, sequential NDJSON protocol for the Unity bridge."""
import json
import math
import socket


class BridgeError(RuntimeError):
    pass


class BridgeRejected(BridgeError):
    """A well-formed rejection; the connection can still be used."""


class BridgeClient:
    def __init__(self, host="127.0.0.1", port=4244, timeout=2.0):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("The game bridge is restricted to localhost")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.socket = socket.create_connection((host, port), timeout=timeout)
        self.stream = self.socket.makefile("rb")
        self.sequence = 0

    def request(self, command, **kwargs):
        if "id" in kwargs or "cmd" in kwargs:
            raise ValueError("Request id and cmd are managed by the client")
        self.sequence += 1
        payload = {"id": self.sequence, "cmd": command, **kwargs}
        try:
            self.socket.sendall((json.dumps(payload, allow_nan=False) + "\n").encode())
            line = self.stream.readline(1_048_577)
            if not line or len(line) > 1_048_576 or not line.endswith(b"\n"):
                raise BridgeError("Game bridge disconnected or exceeded message limit")
            reply = json.loads(line)
            if (not isinstance(reply, dict) or type(reply.get("id")) is not int
                    or reply["id"] != self.sequence):
                raise BridgeError("Game bridge response did not match the request")
            if reply.get("ok") is False:
                raise BridgeRejected(str(reply.get("error", "Game bridge rejected request")))
            if reply.get("ok") is not True or "result" not in reply:
                raise BridgeError("Game bridge response lacks ok/result")
            return reply["result"]
        except BridgeRejected:
            raise
        except (OSError, ValueError) as exc:
            self.close()
            raise BridgeError("Game bridge transport failed; reconnect before retrying") from exc
        except BridgeError:
            self.close()
            raise

    def observe(self):
        state = self.request("observe")
        if not isinstance(state, dict) or type(state.get("seq")) is not int:
            raise BridgeError("Observation lacks an integer seq")
        return state

    def move(self, vector, seq, ttl_ms=500):
        return self.request("move", dx=vector[0], dy=vector[1], seq=seq, ttl_ms=ttl_ms)

    def act(self, action_id, seq):
        return self.request("act", action_id=action_id, seq=seq)

    def release(self):
        return self.request("release")

    def close(self):
        self.stream.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self.release()
        except (BridgeError, OSError, ValueError):
            pass
        self.close()
