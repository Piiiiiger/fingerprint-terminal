# Fingerprint Terminal

Fingerprint Terminal is a profile-aware launcher for a **real local terminal**. It follows the profile/session ideas used by the sibling `chatgpt-fingerprint-desktop` and `claude-fingerprint-desktop` projects, but it deliberately keeps browser-only fingerprint controls out of the terminal layer.

The default terminal backend is `kitty`. `konsole` is also supported as a fallback. The shell that runs inside the terminal is the user's real shell (`$SHELL`), so SSH, Git, Python, Node, aliases, dotfiles, agents, and normal terminal workflows continue to work.

## What a profile controls

- terminal backend (`auto`, `kitty`, `konsole`)
- shell and initial working directory
- timezone/locale environment presented to child processes
- custom environment variables
- HTTP/HTTPS/ALL proxy environment variables and `NO_PROXY`
- optional transparent TCP/UDP isolation through a local host proxy
- optional destination/port-specific direct bypasses that follow the host route while all other traffic remains transparently proxied
- IP-derived exit lock, timezone, locale, city/region metadata, DNS detour, private network interfaces, `/etc/localtime`, and hostname overlays
- whether to inherit the real home directory or use a profile-specific `HOME`

`proxy.mode=environment` is still only an environment-variable proxy.  The
`strict-auto-ip` profile uses the same transparent-gateway design as the
ChatGPT fingerprint desktop: a private application network namespace, veth,
`slirp4netns`, nftables TPROXY, and `sing-box`.  The shell receives no proxy
environment variables, and startup fails if the transparent namespace does
not reproduce the IP/country observed during preflight.

## AI conversation manager

Fingerprint Terminal can manage local Claude Code and Codex conversation histories from the Settings window. Click **AI 对话** to open the visual manager. Claude Code and Codex are separate top-level views, each with its own filtered category counts, active list, and recycle-bin view. It supports custom categories, per-conversation categorization, title-only search, inclusive last-activity date ranges, editable conversation titles, one-click provider-scoped category cleanup, restore, and permanent deletion. Two dynamic categories are always available: **最近使用** shows the five most recently active conversations, and **近三天使用** shows up to five conversations active during the previous 72 hours. These dynamic views never overwrite a conversation's manual category. Title edits are written back to Claude Code's session title records or Codex's thread/index metadata when those provider formats are available.

Moving a conversation to the Fingerprint Terminal trash removes the provider's resumable conversation artifact and its related local indexes. Claude Code project history/sidecars and Codex session indexes/thread rows are handled separately; authentication, settings, plugins, and Codex memories are not modified. Trashed conversations therefore stop appearing to the corresponding CLI while remaining recoverable for 14 days.

Trash is permanently purged after 14 days by a daily user-systemd timer when available. Expiration is also enforced when `strict-auto-ip` is launched and whenever the conversation manager is opened. Maintenance commands:

```bash
fingerprint-terminal conversations status
fingerprint-terminal conversations cleanup
```

## Obsidian / Claudian bridge

GUI applications can launch stdin/stdout-driven agent CLIs inside a profile
without opening another terminal.  The `bridge` command preserves argv and
stdio while applying the same strict filesystem, private HOME, transparent
network, identity, and approved-share rules as an interactive terminal:

```bash
fingerprint-terminal bridge --profile strict-auto-ip \
  --cwd /path/to/shared/vault -- codex app-server --listen stdio://
```

The installer exposes dedicated Claudian-compatible launchers at:

```text
~/.local/libexec/fingerprint-terminal/claude
~/.local/libexec/fingerprint-terminal/codex
```

They forward the caller's current working directory through an already
approved strict share.  When that share lives below the host HOME, the bridge
also re-binds it at the same relative path inside the private HOME.  This lets
an Obsidian vault such as `/home/user/code/vault` remain
`/home/user/code/vault` from the agent's point of view without exposing any
additional host directory.

## Quick start

```bash
cd ~/code/fingerprint-terminal
./bin/fingerprint-terminal doctor
./bin/fingerprint-terminal list
./bin/fingerprint-terminal launch local
./bin/fingerprint-terminal identity strict-auto-ip
./bin/fingerprint-terminal launch strict-auto-ip
```

After `./install.sh`, the same commands can be run from anywhere without the
`./bin/` prefix, for example `fingerprint-terminal manager`.

`strict-auto-ip` layers a Bubblewrap filesystem/process sandbox on top of the
transparent network namespace through the same local mixed proxy used by the
reference ChatGPT desktop: `127.0.0.1:7898`.  It uses a persistent private
profile HOME mounted at `/home/<sandbox-user>`, a profile-specific machine-id, private `/run`, `/tmp`, `/proc`,
`/dev`, an empty `/sys`, a small private `/etc`, and a scrubbed environment.
The host's real home directory is not present: the sandbox home pathname is backed by the
profile's private HOME.  The default example exposes only the host `~/code`,
mounted explicitly under the sandbox home (for example `~/code`); add more `sandbox.shares` entries only
when a host directory is intentionally needed.

The default strict profile can expose only the selected host Wayland socket
for clipboard/image paste support. The host D-Bus session, SSH agent, Docker
socket, and the rest of `/run/user/1000` remain hidden.

Strict mode is stronger isolation, not a VM.  It does **not** virtualize the
kernel or CPU: kernel version, CPU instruction capabilities, timing behavior,
and other syscall/hardware facts may still reveal that sessions run on the
same physical host.  Use a VM when those properties also need to differ.

Open the graphical settings/profile manager:

```bash
./bin/fingerprint-terminal manager
```

The settings window can add host folders with a native folder picker, switch each
share between read/write and read-only, and remove shares.  A separate
`Fingerprint Terminal Settings` / `指纹终端设置` desktop entry is installed in the
application launcher. A window-manager shortcut can launch the strict profile
using `fingerprint-terminal launch strict-auto-ip`.

On first use, the example profiles are copied to:

```text
~/.config/fingerprint-terminal/profiles.json
```

Set `FT_PROFILES_FILE=/path/to/profiles.json` to use another config file. This is also how the tests avoid touching the real user configuration.

## Profile example

```json
{
  "id": "sg-dev",
  "name": "Singapore Dev",
  "terminal": {
    "backend": "auto",
    "shell": "inherit",
    "cwd": "~"
  },
  "identity": {
    "timezone": "Asia/Singapore",
    "locale": "en_SG.UTF-8"
  },
  "environment": {
    "EDITOR": "vim"
  },
  "proxy": {
    "mode": "environment",
    "http": "http://127.0.0.1:7890",
    "https": "http://127.0.0.1:7890",
    "all": "socks5://127.0.0.1:7891",
    "no_proxy": "localhost,127.0.0.1,::1"
  },
  "home": {
    "mode": "inherit"
  }
}
```

Use `proxy.mode = "inherit"` to leave the host proxy environment untouched, or `"off"` to remove common proxy variables from the child shell.

## Useful commands

```bash
./bin/fingerprint-terminal list
./bin/fingerprint-terminal show local
./bin/fingerprint-terminal launch local
./bin/fingerprint-terminal launch local --dry-run
./bin/fingerprint-terminal identity strict-auto-ip
./bin/fingerprint-terminal launch strict-auto-ip
./bin/fingerprint-terminal clone local sg-dev --name "Singapore Dev"
./bin/fingerprint-terminal config
./bin/fingerprint-terminal doctor
```

`shell` is an internal subcommand used by the terminal backend. It can also be useful for diagnostics:

```bash
./bin/fingerprint-terminal shell --profile local --command 'env | sort'
```

## Architecture

```text
GTK/libadwaita profile manager (optional)
                 |
                 v
          profile loader
                 |
                 v
       native terminal adapter
        /                 \
     kitty              konsole
        \                 /
                 v
       fingerprint-terminal shell
                 |
        profile environment
                 +---------------------------+
                 |                           |
          network=inherit             network=transparent
                 |                           |
                 |                user/mount/net/UTS namespace
                 |                    + veth enp2s0
                 |                    + nft TPROXY
                 |                    + sing-box -> host proxy
                 |                    + DNS/timezone/hostname overlays
                 |                    + locked-exit guard
                 |                           |
                 +-------------+-------------+
                               v
                      real local bash/fish/...
```

This keeps the **terminal real and the fingerprint/profile logic as an attached layer**.
The graphical terminal itself stays on the host; only the shell and programs
started from the strict transparent profile enter the private namespaces.
Kitty itself remains the host terminal emulator, but the shell sees the
profile-specific private HOME mounted at `/home/<sandbox-user>`; host dotfiles, SSH
config, Git config and other HOME contents are hidden unless explicitly shared.

Transparent mode intentionally uses a user namespace.  The shell keeps the
normal UID/GID (`1000:1000`) and starts with zero effective capabilities, but
host supplementary groups such as `docker`, `wheel`, or device-specific groups
are not preserved.  Use the `local` profile for host-administration tasks that
depend on those groups; use `strict-auto-ip` for the isolated/IP-aligned terminal.

## Development

Run tests without opening a terminal:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

The project uses only the Python standard library for CLI/runtime code and tests. The optional manager uses the same GTK4/libadwaita stack already present on this host and in the sibling fingerprint desktop projects.

