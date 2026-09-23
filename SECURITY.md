# Security

This is an experimental local game bridge and controller. Security fixes target
the current default branch.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/banjtheman/jev-vampire-survivors/security/advisories/new)
if available. Otherwise, open an issue requesting a private reporting channel
without exploit details or credentials. Include the affected commit and a
minimal sanitized reproduction when reporting privately.

Never include a real `JEV_KEY`, `.env`, authorization header or private game
file. Revoke an exposed key with its provider before sharing a reproduction.

## Local control boundary

The bridge listens on **127.0.0.1:4244** and accepts one client. It has **no
authentication**: another local process can connect when the socket is available
and control the instrumented game. Use it on a trusted machine; do not expose
the port through a tunnel, port forward or public listener.

Observation freshness, sequence consumption, exact menu candidates and expiring
movement constrain commands. They do not authenticate a client or provide a
sandbox against malicious local software. Native control remains limited to
the implemented commands; do not add arbitrary reflection or command execution.

## Credentials and game files

The Python controller sends structured gameplay state and candidate actions to
TypeSafe over HTTPS. It loads `JEV_KEY` from the environment or local credentials
file. The supplied launcher strips `JEV_KEY` from compiler, patcher and game subprocess environments. The
bridge, offline tests and renderer do not need this credential.

The launcher patches a private app copy and verifies the original assembly hash.
That copy still uses the normal game save; it does not isolate save progress.
Keep the copied app, decompiled files, saves, build output, `.env`, logs and local
recordings out of commits. Review staged files rather than relying only on
ignore rules. Captures and logs may reveal profile names, setup and local paths.

Offline tests use synthetic fixtures, mocked service responses and loopback
sockets. They require no live key, game launch or paid API calls. Dependency
installation and native NuGet restore use the network.
