#!/usr/bin/env bash
set -euo pipefail

: "${FT_RUNTIME_DIR:?}"
: "${FT_SINGBOX_CONFIG:?}"
: "${FT_RESOLV_CONF:?}"
: "${FT_ZONEINFO_FILE:?}"
: "${FT_HOSTNAME_FILE:?}"
: "${FT_HOSTS_FILE:?}"
: "${FT_EXPECTED_IP:?}"
: "${FT_COUNTRY_CODE:?}"
: "${FT_PROXY_PORT:?}"
: "${FT_TPROXY_PORT:=7893}"
: "${FT_DIRECT_BYPASS_RULES:=}"

if [[ $# -lt 1 ]]; then
  printf 'network-supervisor: missing child command\n' >&2
  exit 64
fi

gateway_pid=""
app_pid=""
slirp_pid=""
singbox_pid=""
child_pid=""
guard_pid=""
log_dir="$FT_RUNTIME_DIR/logs"
mkdir -p "$log_dir"

cleanup() {
  trap - EXIT INT TERM
  for pid in "$child_pid" "$guard_pid" "$singbox_pid" "$slirp_pid" "$app_pid" "$gateway_pid"; do
    if [[ -n "$pid" ]]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Keep the user's visible UID while retaining namespace-only capabilities in
# this supervisor.  The CLI creates the outer user namespace with
# --map-current-user --keep-caps.  Capabilities are dropped before the shell.
unshare --net -- /usr/bin/sleep infinity &
gateway_pid=$!
unshare --mount --net --uts --propagation private -- /usr/bin/sleep infinity &
app_pid=$!

for pid in "$gateway_pid" "$app_pid"; do
  for _ in $(seq 1 50); do
    [[ -e "/proc/$pid/ns/net" ]] && break
    sleep 0.05
  done
  [[ -e "/proc/$pid/ns/net" ]] || {
    printf 'network-supervisor: namespace keeper failed: %s\n' "$pid" >&2
    exit 1
  }
done

# Mount sysfs from inside the private network namespace so /sys/class/net and
# netlink agree.  The shell sees only loopback + a conventional Ethernet link.
nsenter -t "$app_pid" -m -n -- \
  mount -t sysfs -o ro,nosuid,nodev,noexec sysfs /sys

nsenter -t "$gateway_pid" -n ip link set lo up
nsenter -t "$app_pid" -n ip link set lo up
nsenter -t "$gateway_pid" -n ip link add enp2s0 type veth peer name eth0
nsenter -t "$gateway_pid" -n ip link set enp2s0 netns "$app_pid"

nsenter -t "$gateway_pid" -n ip address add 192.168.1.1/24 dev eth0
nsenter -t "$gateway_pid" -n ip link set eth0 up
nsenter -t "$app_pid" -n ip address add 192.168.1.105/24 dev enp2s0
nsenter -t "$app_pid" -n ip link set enp2s0 up
nsenter -t "$app_pid" -n ip route add default via 192.168.1.1 dev enp2s0
nsenter -t "$app_pid" -n sysctl -q -w net.ipv6.conf.all.disable_ipv6=1
nsenter -t "$app_pid" -n sysctl -q -w net.ipv6.conf.default.disable_ipv6=1

# Identity-facing system files are private mount-namespace overlays.  Nothing
# here modifies the host /etc.
nsenter -t "$app_pid" -m -- mount --bind "$FT_RESOLV_CONF" /etc/resolv.conf
nsenter -t "$app_pid" -m -- mount --bind "$FT_ZONEINFO_FILE" /etc/localtime
if [[ "${FT_PRIVATE_HOSTNAME:-1}" == "1" ]]; then
  nsenter -t "$app_pid" -u -- /usr/bin/python3 -c \
    'import ctypes, os, sys; name=sys.argv[1].encode(); libc=ctypes.CDLL(None, use_errno=True); rc=libc.sethostname(name, len(name)); rc == 0 or (_ for _ in ()).throw(OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno())))' \
    "$FT_HOSTNAME"
  nsenter -t "$app_pid" -m -- mount --bind "$FT_HOSTNAME_FILE" /etc/hostname
  nsenter -t "$app_pid" -m -- mount --bind "$FT_HOSTS_FILE" /etc/hosts
fi

slirp4netns \
  --configure \
  --mtu=65520 \
  --enable-seccomp \
  "$gateway_pid" tap0 >"$log_dir/slirp4netns.log" 2>&1 &
slirp_pid=$!

for _ in $(seq 1 100); do
  if nsenter -t "$gateway_pid" -n ip link show tap0 >/dev/null 2>&1; then
    break
  fi
  kill -0 "$slirp_pid" 2>/dev/null || {
    printf 'network-supervisor: slirp4netns exited; see %s\n' "$log_dir/slirp4netns.log" >&2
    exit 1
  }
  sleep 0.05
done
nsenter -t "$gateway_pid" -n ip link show tap0 >/dev/null 2>&1 || {
  printf 'network-supervisor: slirp4netns did not create tap0\n' >&2
  exit 1
}

nsenter -t "$gateway_pid" -n ip rule add fwmark 0x1 table 100
nsenter -t "$gateway_pid" -n ip route add local 0.0.0.0/0 dev lo table 100

bypass_prerouting_rules=""
bypass_forward_rules=""
bypass_postrouting_rules=""
bypass_return_rule=""
if [[ -n "$FT_DIRECT_BYPASS_RULES" ]]; then
  while IFS= read -r bypass_rule; do
    [[ -z "$bypass_rule" ]] && continue
    IFS=',' read -r destination protocol port extra <<<"$bypass_rule"
    if [[ -n "${extra:-}" ]] \
      || [[ ! "$destination" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/(2[4-9]|3[0-2])$ ]] \
      || [[ "$protocol" != "tcp" && "$protocol" != "udp" ]] \
      || [[ ! "$port" =~ ^[0-9]+$ ]] \
      || (( port < 1 || port > 65535 )); then
      printf 'network-supervisor: invalid direct bypass rule: %s\n' "$bypass_rule" >&2
      exit 64
    fi
    bypass_prerouting_rules+="    iifname \"eth0\" ip daddr $destination $protocol dport $port accept"$'\n'
    bypass_forward_rules+="    iifname \"eth0\" oifname \"tap0\" ip daddr $destination $protocol dport $port accept"$'\n'
    bypass_postrouting_rules+="    oifname \"tap0\" ip daddr $destination $protocol dport $port masquerade"$'\n'
    bypass_return_rule='    iifname "tap0" oifname "eth0" ct state established,related accept'
  done <<<"$FT_DIRECT_BYPASS_RULES"
fi

nsenter -t "$gateway_pid" -n nft -f - <<NFT
table inet fingerprint_terminal_gateway {
  chain prerouting {
    type filter hook prerouting priority mangle; policy accept;
$bypass_prerouting_rules    iifname "eth0" meta l4proto tcp tproxy ip to 127.0.0.1:$FT_TPROXY_PORT meta mark set 0x1 accept
    iifname "eth0" meta l4proto udp tproxy ip to 127.0.0.1:$FT_TPROXY_PORT meta mark set 0x1 accept
  }

  chain input {
    type filter hook input priority filter; policy drop;
    iifname "lo" accept
    iifname "eth0" accept
    iifname "tap0" ct state established,related accept
  }

  chain forward {
    type filter hook forward priority filter; policy drop;
$bypass_forward_rules$bypass_return_rule
  }

  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
$bypass_postrouting_rules  }

  chain output {
    type filter hook output priority filter; policy drop;
    oifname "lo" accept
    oifname "eth0" ct state established,related accept
    ip daddr 10.0.2.2 tcp dport $FT_PROXY_PORT accept
    ip daddr 10.0.2.2 udp dport $FT_PROXY_PORT accept
  }
}
NFT

nsenter -t "$gateway_pid" -n ip route show >"$log_dir/gateway-route.log" 2>&1 || true
nsenter -t "$gateway_pid" -n nft list table inet fingerprint_terminal_gateway \
  >"$log_dir/gateway-nft.log" 2>&1 || true

nsenter -t "$gateway_pid" -n \
  sing-box run -c "$FT_SINGBOX_CONFIG" \
  >"$log_dir/sing-box.log" 2>&1 &
singbox_pid=$!

for _ in $(seq 1 100); do
  if nsenter -t "$gateway_pid" -n ss -lntup 2>/dev/null | grep -q ":$FT_TPROXY_PORT"; then
    break
  fi
  kill -0 "$singbox_pid" 2>/dev/null || {
    printf 'network-supervisor: sing-box exited; see %s\n' "$log_dir/sing-box.log" >&2
    exit 1
  }
  sleep 0.05
done
nsenter -t "$gateway_pid" -n ss -lntup 2>/dev/null | grep -q ":$FT_TPROXY_PORT" || {
  printf 'network-supervisor: transparent listener did not become ready\n' >&2
  exit 1
}

# The validation is made from the exact namespace the shell will use, with no
# HTTP(S)_PROXY variables.  If the real public exit differs from preflight,
# startup fails rather than silently mixing network and identity fingerprints.
if ! nsenter -t "$app_pid" -m -n -u -- env \
  -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
  -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
  python3 -m fingerprint_terminal.netprobe \
  >"$FT_RUNTIME_DIR/transparent-exit.txt" \
  2>"$FT_RUNTIME_DIR/transparent-exit.error"; then
  printf 'network-supervisor: transparent exit validation failed\n' >&2
  cat "$FT_RUNTIME_DIR/transparent-exit.error" >&2 || true
  exit 1
fi

if [[ "${FT_QUIET:-0}" != "1" ]]; then
  printf 'Fingerprint Terminal: transparent exit locked to %s (%s)\n' \
    "$FT_EXPECTED_IP" "$FT_COUNTRY_CODE" >&2
fi

# Match the reference desktop's locked-exit guard: once per minute, verify the
# public IP/country from the same transparent namespace.  A transient network
# failure is tolerated; a positive IP/country mismatch terminates the session
# instead of leaving a stale timezone/locale identity attached to a new exit.
exit_guard() {
  while true; do
    sleep 60
    set +e
    nsenter -t "$app_pid" -m -n -u -- env \
      -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
      -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
      python3 -m fingerprint_terminal.netprobe \
      >"$FT_RUNTIME_DIR/guard-exit.txt" \
      2>"$FT_RUNTIME_DIR/guard-exit.error"
    probe_status=$?
    set -e
    if [[ "$probe_status" -eq 2 || "$probe_status" -eq 3 ]]; then
      printf 'network-supervisor: locked exit changed; closing terminal session\n' >&2
      cat "$FT_RUNTIME_DIR/guard-exit.error" >&2 || true
      return 42
    fi
  done
}

nsenter -t "$app_pid" -m -n -u -- \
  setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all -- \
  "$@" <&0 &
child_pid=$!
exit_guard &
guard_pid=$!

set +e
finished_pid=""
wait -n -p finished_pid "$child_pid" "$guard_pid"
status=$?
set -e

if [[ "${finished_pid:-}" == "$guard_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
  kill -TERM "$child_pid" 2>/dev/null || true
  wait "$child_pid" 2>/dev/null || true
  status=1
else
  kill -TERM "$guard_pid" 2>/dev/null || true
  wait "$guard_pid" 2>/dev/null || true
fi
child_pid=""
guard_pid=""
exit "$status"
