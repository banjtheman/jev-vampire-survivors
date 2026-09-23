from io import BytesIO
import json
import unittest
from unittest.mock import patch

from vampire_agent.bridge import BridgeClient, BridgeError, BridgeRejected


class FakeSocket:
    def __init__(self, reply):
        self.stream = BytesIO(reply)
        self.sent = []
        self.closed = False

    def makefile(self, mode):
        return self.stream

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def client_with(reply):
    connection = FakeSocket(reply)
    with patch("vampire_agent.bridge.socket.create_connection", return_value=connection):
        return BridgeClient(), connection


class VampireBridgeTests(unittest.TestCase):
    def test_protocol_move_and_menu_bind_to_observation_sequence(self):
        replies = [dict(id=1, ok=True, result={"seq": 8}), dict(id=2, ok=True, result={}), dict(id=3, ok=True, result={})]
        client, connection = client_with(("\n".join(map(json.dumps, replies)) + "\n").encode())
        self.assertEqual(client.observe(), {"seq": 8})
        client.move((0, 1), 8)
        client.act("offered-item-2", 9)
        self.assertEqual([json.loads(value) for value in connection.sent], [
            {"id": 1, "cmd": "observe"},
            {"id": 2, "cmd": "move", "dx": 0, "dy": 1, "seq": 8, "ttl_ms": 500},
            {"id": 3, "cmd": "act", "action_id": "offered-item-2", "seq": 9},
        ])
        client.close()

    def test_malformed_frames_or_ids_close_connection(self):
        for reply in (b"", b"not-json\n", b"[]\n", b'{"id":true,"ok":true,"result":{}}\n',
                      b'{"id":2,"ok":true,"result":{}}\n', b'{"id":1,"ok":true}\n',
                      b'{"id":1,"ok":true,"result":{}}', b"x" * 1_048_577 + b"\n"):
            with self.subTest(reply=reply[:80]):
                client, connection = client_with(reply)
                with self.assertRaises(BridgeError):
                    client.request("observe")
                self.assertTrue(connection.closed)

    def test_rejected_stale_action_keeps_connection_for_release(self):
        client, connection = client_with(b'{"id":1,"ok":false,"error":"stale observation"}\n'
                                          b'{"id":2,"ok":true,"result":{}}\n')
        with self.assertRaisesRegex(BridgeRejected, "stale"):
            client.move((1, 0), 8)
        self.assertFalse(connection.closed)
        self.assertEqual(client.release(), {})
        client.close()

    def test_localhost_only_before_connect(self):
        with patch("vampire_agent.bridge.socket.create_connection") as connect:
            with self.assertRaises(ValueError):
                BridgeClient("example.com")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
