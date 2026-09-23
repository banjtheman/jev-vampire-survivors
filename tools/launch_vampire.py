#!/usr/bin/env python
"""Build an original Unity Mono bridge and launch an isolated macOS game copy.

Requires the user's installed Vampire Survivors and the .NET 8 SDK/runtime. Game assemblies
and the copied app remain under ignored .local/. Nothing is installed in Steam.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = Path.home() / "Library/Application Support/Steam/steamapps/common/Vampire Survivors/Vampire_Survivors.app"
PRIVATE = ROOT / ".local/vampire-runtime"
MANAGED = Path("Contents/Resources/Data/Managed")
ASSEMBLY = "VampireSurvivors.Runtime.dll"


def toolchain(env):
    """Resolve a compiler and the .NET 8 runtime required by the patcher."""
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise RuntimeError("Install the .NET 8 SDK and add dotnet to PATH.")
    sdks = subprocess.check_output([dotnet, "--list-sdks"], text=True, env=env).splitlines()
    if not sdks:
        raise RuntimeError("No .NET SDK found. Install the .NET 8 SDK; a runtime alone cannot compile the bridge.")
    version, location = sdks[-1].split(" ", 1)
    if int(version.split(".", 1)[0]) < 8:
        raise RuntimeError("The patcher requires the .NET 8 SDK or newer, plus the .NET 8 runtime.")
    runtimes = subprocess.check_output([dotnet, "--list-runtimes"], text=True, env=env).splitlines()
    if not any(line.startswith("Microsoft.NETCore.App 8.") for line in runtimes):
        raise RuntimeError("The net8.0 patcher requires the .NET 8 runtime. Install the .NET 8 SDK/runtime alongside newer SDKs.")
    compiler = Path(location.strip("[]")) / version / "Roslyn/bincore/csc.dll"
    return dotnet, compiler


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).hexdigest()


def build(app: Path) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("This launcher supports the macOS Steam Unity Mono build only.")
    managed = app / MANAGED
    original = managed / ASSEMBLY
    if not original.is_file():
        raise RuntimeError("Expected a Unity Mono installation with VampireSurvivors.Runtime.dll.")
    if original.is_symlink() or not original.resolve().is_relative_to(app):
        raise RuntimeError("Game assembly must be a regular file inside the supplied app.")
    build_env = {key: value for key, value in os.environ.items() if key != "JEV_KEY"}
    dotnet, compiler = toolchain(build_env)
    source = ROOT / "vampire_bridge/Bridge.cs"
    if not source.is_file():
        raise RuntimeError("Missing vampire_bridge/Bridge.cs")
    output = ROOT / "dist/vampire"
    output.mkdir(parents=True, exist_ok=True)
    bridge = output / "JevVampireBridge.dll"
    refs = ["mscorlib", "System", "System.Core", "netstandard", "Newtonsoft.Json",
            "UnityEngine", "UnityEngine.CoreModule", "UnityEngine.AudioModule", "UnityEngine.Physics2DModule", "UnityEngine.ImageConversionModule", "UnityEngine.ScreenCaptureModule",
            "UnityEngine.InputLegacyModule", "UnityEngine.UIModule", "UnityEngine.UI", "Unity.TextMeshPro"]
    response = output / "compile.rsp"
    options = ["-nologo", "-target:library", "-nostdlib+", "-langversion:latest", "-optimize+", f'-out:"{bridge}"']
    options.extend(f'-r:"{managed / (name + ".dll")}"' for name in refs)
    options.append(f'"{source}"')
    response.write_text("\n".join(options), encoding="utf-8")
    subprocess.run([dotnet, str(compiler), "@" + str(response)], check=True, env=build_env)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    copied = PRIVATE / app.name
    marker = PRIVATE / "source.json"
    signature = {"source": str(app), "assembly_sha256": sha256(original)}
    previous = json.loads(marker.read_text()) if marker.exists() else None
    if copied.exists() and previous != signature:
        raise RuntimeError("Private game copy differs from installed version. Move .local/vampire-runtime aside and retry.")
    if not copied.exists():
        print("Copying the installed app into .local/vampire-runtime (Steam files remain unchanged).", flush=True)
        shutil.copytree(app, copied, symlinks=True)
        marker.write_text(json.dumps(signature, indent=2) + "\n")
    for target in (copied / MANAGED / ASSEMBLY, copied / MANAGED / bridge.name):
        if copied.is_symlink() or target.is_symlink() or not target.resolve().is_relative_to(copied):
            raise RuntimeError("Refusing to write through a symlink outside the private game copy.")
    patcher = ROOT / "vampire_bridge/Patcher/Patcher.csproj"
    subprocess.run([dotnet, "run", "--project", str(patcher), "--configuration", "Release", "--",
                    str(original), str(copied / MANAGED / ASSEMBLY), str(bridge)], check=True, env=build_env)
    shutil.copy2(bridge, copied / MANAGED / bridge.name)
    if sha256(original) != signature["assembly_sha256"]:
        raise RuntimeError("Original assembly unexpectedly changed during preparation.")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--audio", action="store_true", help="Enable audio; the experimental copy is muted by default")
    parser.add_argument("--log", type=Path, default=ROOT / ".local/vampire-game.log")
    args = parser.parse_args()
    try:
        try:
            with socket.create_connection(("127.0.0.1", 4244), timeout=.2):
                raise RuntimeError("The Vampire bridge is already running. Close that game instance before rebuilding or launching.")
        except OSError:
            pass
        app = build(args.app.expanduser().resolve())
        if args.build_only:
            print("Prepared:", app)
            return 0
        exe = app / "Contents/MacOS/Vampire Survivors"
        env = {key: value for key, value in os.environ.items() if key != "JEV_KEY"}
        env["SteamAppId"] = "1794680"
        env["SteamGameId"] = "1794680"
        env["JEV_RECORD_ROOT"] = str(ROOT / "recordings")
        env["JEV_MUTE"] = "0" if args.audio else "1"
        args.log.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([str(exe), "-screen-fullscreen", "0", "-screen-width", "1280",
                                 "-screen-height", "720", "-logFile", str(args.log.resolve())],
                                cwd=exe.parent, env=env)
        print(f"Vampire Survivors PID {proc.pid}; log: {args.log}", flush=True)
        print("Bridge listens on 127.0.0.1:4244 after the main menu loads.", flush=True)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait(timeout=10)
            return 130
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"Launch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
