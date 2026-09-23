from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import launch_vampire


class LauncherTests(unittest.TestCase):
    def test_no_sdk_gives_actionable_error(self):
        with patch.object(launch_vampire.shutil, "which", return_value="/test/dotnet"), \
                patch.object(launch_vampire.subprocess, "check_output", return_value="") as inspect:
            with self.assertRaisesRegex(RuntimeError, "No .NET SDK"):
                launch_vampire.toolchain({"PATH": "/test"})
        self.assertEqual(inspect.call_count, 1)

    def test_newer_sdk_without_net8_runtime_is_rejected(self):
        with patch.object(launch_vampire.shutil, "which", return_value="/test/dotnet"), \
                patch.object(launch_vampire.subprocess, "check_output", side_effect=[
                    "9.0.100 [/test/sdk]\n", "Microsoft.NETCore.App 9.0.0 [/test/runtime]\n"]):
            with self.assertRaisesRegex(RuntimeError, "requires the .NET 8 runtime"):
                launch_vampire.toolchain({})

    def test_compiler_resolution_preserves_supplied_environment(self):
        env = {"PATH": "/test"}
        with patch.object(launch_vampire.shutil, "which", return_value="/test/dotnet"), \
                patch.object(launch_vampire.subprocess, "check_output", side_effect=[
                    "8.0.100 [/test/sdk]\n", "Microsoft.NETCore.App 8.0.0 [/test/runtime]\n"]) as inspect:
            dotnet, compiler = launch_vampire.toolchain(env)
        self.assertEqual(dotnet, "/test/dotnet")
        self.assertEqual(compiler, Path("/test/sdk/8.0.100/Roslyn/bincore/csc.dll"))
        self.assertTrue(all(call.kwargs["env"] is env for call in inspect.call_args_list))

    def test_build_filters_key_from_all_children_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            app = root / "OwnedTest.app"
            original = app / launch_vampire.MANAGED / launch_vampire.ASSEMBLY
            original.parent.mkdir(parents=True)
            original.write_bytes(b"synthetic owned test assembly")
            source = root / "vampire_bridge/Bridge.cs"
            source.parent.mkdir()
            source.write_text("// synthetic test bridge source\n")
            stack.enter_context(patch.object(launch_vampire, "ROOT", root))
            stack.enter_context(patch.object(launch_vampire, "PRIVATE", root / "private"))
            stack.enter_context(patch.object(launch_vampire.sys, "platform", "darwin"))
            stack.enter_context(patch.dict(launch_vampire.os.environ, {"JEV_KEY": "not-a-real-api-key"}))
            compiler = stack.enter_context(patch.object(launch_vampire, "toolchain", return_value=("/test/dotnet", Path("/test/csc.dll"))))
            def fake_build(*args, **kwargs):
                (root / "dist/vampire/JevVampireBridge.dll").write_bytes(b"synthetic compiled output")
            processes = stack.enter_context(patch.object(launch_vampire.subprocess, "run", side_effect=fake_build))
            stack.enter_context(patch("builtins.print"))
            copied = launch_vampire.build(app)
            self.assertNotEqual(copied, app)
            self.assertEqual(original.read_bytes(), b"synthetic owned test assembly")
            self.assertNotIn("JEV_KEY", compiler.call_args.args[0])
            self.assertEqual(processes.call_count, 2)
            for call in processes.call_args_list:
                self.assertNotIn("JEV_KEY", call.kwargs["env"])

    def test_unsupported_platform_stops_before_accessing_game(self):
        with patch.object(launch_vampire.sys, "platform", "linux"), \
                patch.object(launch_vampire, "toolchain") as compiler:
            with self.assertRaisesRegex(RuntimeError, "macOS Steam"):
                launch_vampire.build(Path("/not-a-real-game"))
        compiler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
