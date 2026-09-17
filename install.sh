#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${HOME}/.local/bin"
libexec_dir="${HOME}/.local/libexec/fingerprint-terminal"
applications_dir="${HOME}/.local/share/applications"
systemd_dir="${HOME}/.config/systemd/user"

mkdir -p "$bin_dir" "$libexec_dir" "$applications_dir" "$systemd_dir"
ln -sfn "$root_dir/bin/fingerprint-terminal" "$bin_dir/fingerprint-terminal"
ln -sfn "$root_dir/bin/fingerprint-terminal-claude-bridge" "$libexec_dir/claude"
ln -sfn "$root_dir/bin/fingerprint-terminal-codex-bridge" "$libexec_dir/codex"
cp "$root_dir/assets/io.fingerprintterminal.FingerprintTerminal.desktop" \
  "$applications_dir/io.fingerprintterminal.FingerprintTerminal.desktop"
cp "$root_dir/assets/io.fingerprintterminal.FingerprintTerminal.Settings.desktop" \
  "$applications_dir/io.fingerprintterminal.FingerprintTerminal.Settings.desktop"
cp "$root_dir/assets/systemd/fingerprint-terminal-conversation-cleanup.service" \
  "$systemd_dir/fingerprint-terminal-conversation-cleanup.service"
cp "$root_dir/assets/systemd/fingerprint-terminal-conversation-cleanup.timer" \
  "$systemd_dir/fingerprint-terminal-conversation-cleanup.timer"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload || true
  systemctl --user enable --now fingerprint-terminal-conversation-cleanup.timer || true
fi

echo "Installed command: $bin_dir/fingerprint-terminal"
echo "Installed Claude bridge: $libexec_dir/claude"
echo "Installed Codex bridge: $libexec_dir/codex"
echo "Installed desktop entry: $applications_dir/io.fingerprintterminal.FingerprintTerminal.desktop"
echo "Installed settings entry: $applications_dir/io.fingerprintterminal.FingerprintTerminal.Settings.desktop"
echo "Installed conversation cleanup timer: $systemd_dir/fingerprint-terminal-conversation-cleanup.timer"

