#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${HOME}/.local/bin"
applications_dir="${HOME}/.local/share/applications"

mkdir -p "$bin_dir" "$applications_dir"
ln -sfn "$root_dir/bin/fingerprint-terminal" "$bin_dir/fingerprint-terminal"
cp "$root_dir/assets/io.fingerprintterminal.FingerprintTerminal.desktop" \
  "$applications_dir/io.fingerprintterminal.FingerprintTerminal.desktop"
cp "$root_dir/assets/io.fingerprintterminal.FingerprintTerminal.Settings.desktop" \
  "$applications_dir/io.fingerprintterminal.FingerprintTerminal.Settings.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" || true
fi

echo "Installed command: $bin_dir/fingerprint-terminal"
echo "Installed desktop entry: $applications_dir/io.fingerprintterminal.FingerprintTerminal.desktop"
echo "Installed settings entry: $applications_dir/io.fingerprintterminal.FingerprintTerminal.Settings.desktop"

