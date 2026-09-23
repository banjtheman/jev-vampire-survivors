# Third-party components and references

This project's original bridge, controller and utilities use the repository's
[MIT license](LICENSE). Third-party products and dependencies retain their own
licenses. The project's license does not grant rights to Vampire Survivors or
its assets. This is an unofficial integration, not an affiliated game release.

## Game and runtime

You need your own installed **Vampire Survivors** Steam copy. No game executable,
patched assembly, extracted or decompiled game source, textures, audio, save or
other commercial asset is distributed here. The launcher compiles against Unity
and JSON assemblies supplied by your installation, then instruments a private
local copy. Those game/runtime assemblies are not vendored in this repository.

**TypeSafe/Jev** is an external service accessed through its
[HTTP API](https://docs.typesafe.ai/api). No TypeSafe SDK is bundled; the client
uses the Python standard library. Live model calls require your own credential.
The [optional TypeSafe skill](https://www.skills.sh/typesafe-ai/skills/typesafe-ai)
is installed separately.

## Separately installed dependencies

| Component | Use | Upstream license or notice |
| --- | --- | --- |
| Python | Controller and standard library | [Python license](https://docs.python.org/3/license.html) |
| .NET SDK/runtime | Compile the bridge and run the local patcher | [.NET notices](https://github.com/dotnet/runtime/blob/main/LICENSE.TXT) |
| Mono.Cecil 0.11.6 | Read and patch the local managed assembly | [MIT license](https://github.com/jbevain/cecil/blob/0.11.6/LICENSE.txt) |
| Pillow | Optional video overlays and posters | [MIT-CMU license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| FFmpeg | Command-line video encoding | [Licensing](https://ffmpeg.org/legal.html); terms depend on the installed build |
| setuptools | Python package build backend | [MIT license](https://github.com/pypa/setuptools/blob/main/LICENSE) |
| actions/checkout | CI checkout | [MIT license](https://github.com/actions/checkout/blob/main/LICENSE) |
| actions/setup-python | CI Python installation | [MIT license](https://github.com/actions/setup-python/blob/main/LICENSE) |

Mono.Cecil is restored by NuGet; its implementation is not committed here. Other
components are likewise installed separately. System fonts or Pillow's fallback
font are used for rendering; no font files are bundled.

## Research references

- [VampireSurvivorsAI](https://github.com/zappybiby/VampireSurvivorsAI) was
  inspected as an example of in-game avoidance. No code from it is included.
- [Vampire Survivors modding reference](https://github.com/lukeod/vampiresurvivors-modding)
  helped identify game/UI API names. Hooks and native field semantics were
  checked against the locally installed build.
- [BepInEx](https://github.com/BepInEx/BepInEx) and
  [MelonLoader](https://github.com/LavaGang/MelonLoader) were considered during
  research. Neither is required or bundled by this launcher.

These are references, not bundled mods or endorsements. Consult each upstream
project for its own code, license and supported platforms.
