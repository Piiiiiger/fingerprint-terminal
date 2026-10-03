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
not reproduce the IP/country observed during preflight. The shipped strict
profile also requires a verified Singapore exit (`SG`, `Asia/Singapore`).

## AI conversation manager

Fingerprint Terminal can manage local Claude Code and Codex conversation histories from the Settings window. Click **AI 对话** to open the visual manager. Claude Code and Codex are separate top-level views, each with its own filtered category counts, active list, and recycle-bin view. It supports custom categories, per-conversation categorization, title-only search, inclusive last-activity date ranges, editable conversation titles, one-click provider-scoped category cleanup, restore, and permanent deletion. Two dynamic categories are always available: **最近使用** shows the five most recently active conversations, and **近三天使用** shows up to five conversations active during the previous 72 hours. These dynamic views never overwrite a conversation's manual category. Title edits are written back to Claude Code's session title records or Codex's thread/index metadata when those provider formats are available.

Moving a conversation to the Fingerprint Terminal trash removes the provider's resumable conversation artifact and its related local indexes. Claude Code project history/sidecars and Codex session indexes/thread rows are handled separately; authentication, settings, plugins, and Codex memories are not modified. Trashed conversations therefore stop appearing to the corresponding CLI while remaining recoverable for 14 days.

Claude activity checks use live processes and verified session registrations,
so leftover registration files after an exit do not block conversation changes.
When an older Claude process cannot be associated with a session, it blocks
changes only in its working directory and the message explains that uncertainty.

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
approved strict share. Configure the vault's directory in `sandbox.shares`
before using the bridge; an unshared host working directory is rejected. The
host HOME itself maps to the private HOME without sharing its contents.
When a configured share lives below the host HOME, the bridge
also re-binds it at the same relative path inside the private HOME.  This lets
an Obsidian vault such as `/home/user/code/vault` remain
`/home/user/code/vault` from the agent's point of view without exposing any
additional host directory. Bridge-only compatibility mountpoints are leased
per running process and removed when the last bridge exits; empty leftovers
from an abnormal exit are collected when the settings window is opened or
refreshed.

For the existing `strict-auto-ip` home, `install.sh` also installs a small
profile-local `obsidian` command. It supports only
`obsidian [vault=<directory-name>] delete path=<relative-file>`, moving a file into the vault's `.trash` without
launching the desktop app. The vault is discovered from the current directory
or its parents using `.obsidian`; an explicit vault name must match that directory.
Other Obsidian commands return an unsupported
result; the host Obsidian installation is unaffected.

## Quick start

```bash
cd ~/code/fingerprint-terminal
./bin/fingerprint-terminal prepare-system strict-auto-ip
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
`/dev`, a private `/sys`, a small private read-only `/etc`, and a scrubbed environment.
The strict profile also uses `sandbox.system=private`: `prepare-system` builds
an independent Arch `/usr` from signed packages in profile storage. It includes
common CLI tools (including `wl-paste` for image clipboard input), en_US.UTF-8 and en_SG.UTF-8 locale data, generic Noto/Liberation fonts, and
one synthetic niri session entry. Its desktop environment variables identify
niri as well. The host `/usr` is not mounted inside this
mode. Its package set can be rebuilt with `prepare-system strict-auto-ip
--refresh`; existing sessions keep their current mount and old releases remain
in profile storage until removed after those sessions end. New strict sessions
fail closed if the private system view is missing or incomplete.
An optional `sandbox.dmi_profile=thinkbook-14-g7-iml` adds Lenovo ThinkBook
14 G7 IML vendor/model fields under the otherwise private `/sys` view. That
model's published specifications are documented in
[Lenovo PSREF](https://psref.lenovo.com/syspool/Sys/PDF/ThinkBook/ThinkBook_14_G7_IML/ThinkBook_14_G7_IML_Spec.pdf).
It does not emulate the hardware: the real CPU, PCI IDs, kernel, and Wayland
display properties remain observable. No serial number or host DMI value is
copied into the sandbox.
The shell receives no `FT_*` variables, and the generated `/etc` identity files
are copied in rather than bind-mounted, so the sandbox's mount table does not
name the profile storage for them.  The persistent HOME is still a bind mount,
so its host-side storage path remains visible in `/proc/self/mountinfo`.
The host's real home directory is not present: the sandbox home pathname is backed by the
profile's private HOME. The shipped strict profile has `sandbox.shares: []`,
so a fresh configuration exposes no host working directories. Add a specific
directory in the settings window or in `sandbox.shares` only when it is needed. Installation
and startup preserve existing configurations without adding any shares.

The private `/usr` removes the host's package, font, and login-session inventory
from the strict filesystem view. It is still an Arch userland: package and tool
versions remain observable, and individual programs can reveal their own build
details. The runtime timezone and locale follow the verified proxy exit. The
shipped strict profile requires Singapore, so its shell uses `Asia/Singapore`
and `en_SG.UTF-8`; an exit in another country is rejected. Custom profiles can
set their own `network.expected_country` and `network.expected_timezone`, or
omit those constraints while retaining the preflight exit lock.

For an approved share that contains app-private metadata, set
`sandbox.hidden_paths` to directory paths relative to that share. Those
directories are covered by empty tmpfs mounts inside strict sessions while
remaining available to host applications. The setting applies to duplicate
bridge mounts of the same share as well.

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

## Repository privacy

Keep real proxy credentials, node names, shared host paths and personal profile
settings outside the checkout. Use `FT_PROFILES_FILE` to select a local configuration;
the committed `config/profiles.json` contains generic defaults only. Existing user
configurations are preserved when the example defaults change.

Before publishing a fork, review tracked files and Git history, including commit
authors and email addresses. Ignoring or deleting a file does not remove earlier
committed copies. Use a private commit email, and keep history backups outside the
repository.
