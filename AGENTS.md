# Fingerprint Terminal development notes

- Keep the terminal emulator native. The default backend is the host `kitty`; do not replace it with a WebView/xterm layer unless there is a concrete requirement.
- Treat browser fingerprinting and terminal process identity as different layers. The terminal layer may align process/system identity with the observed network exit, but it must not claim browser-only Canvas/WebGL/etc. controls.
- Never describe proxy environment variables as transparent network isolation. Programs may ignore them.
- `network.mode=transparent` is a separate path. It must fail closed if the namespace cannot reproduce the preflight exit IP/country, and applications inside it must not receive HTTP(S)_PROXY/ALL_PROXY variables.
- The transparent path must preserve a normal non-root visible UID. Namespace setup capabilities are allowed only in the supervisor and must be dropped before the user shell starts.
- Do not weaken the gateway firewall to permit direct application egress. The only gateway egress is the configured host proxy endpoint.
- Default profiles must preserve the user's real `HOME`, shell configuration, SSH config, Git config, and other normal terminal capabilities.
- Stronger isolation remains opt-in and must state exactly what it isolates. The `local` profile is intentionally unchanged.
- `sandbox.mode=strict` must use an isolated HOME, private runtime/process/filesystem views, and explicit `sandbox.shares`; never expose the host HOME implicitly.
- Strict mode is not a VM: kernel version, CPU capabilities, timing characteristics, and other syscall-level hardware/kernel facts may still be observable.
- Keep the project usable without installation: `./bin/fingerprint-terminal ...` must work from the repository checkout.
- Tests must not launch a graphical terminal or modify the real user config.

