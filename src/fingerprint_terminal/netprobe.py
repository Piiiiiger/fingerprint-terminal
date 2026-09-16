"""Probe the public exit from inside the transparent application namespace."""

from __future__ import annotations

import os
import sys
import urllib.request

from .identity import parse_cloudflare_trace


def main() -> int:
    expected_ip = os.environ.get("FT_EXPECTED_IP", "").strip()
    expected_country = os.environ.get("FT_COUNTRY_CODE", "").strip().upper()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        "https://cloudflare-dns.com/cdn-cgi/trace",
        headers={"User-Agent": "fingerprint-terminal-transparent-probe/0.2"},
    )
    try:
        with opener.open(request, timeout=12) as response:
            trace_text = response.read().decode("utf-8", "replace")
    except OSError as exc:
        print(f"transparent probe failed: {exc}", file=sys.stderr)
        return 1
    trace = parse_cloudflare_trace(trace_text)
    observed_ip = trace.get("ip", "")
    observed_country = trace.get("loc", "").upper()
    if expected_ip and observed_ip != expected_ip:
        print(
            f"transparent IP mismatch: expected {expected_ip}, got {observed_ip or 'unknown'}",
            file=sys.stderr,
        )
        return 2
    if expected_country and observed_country != expected_country:
        print(
            "transparent country mismatch: "
            f"expected {expected_country}, got {observed_country or 'unknown'}",
            file=sys.stderr,
        )
        return 3
    print(
        f"ip={observed_ip} country={observed_country} colo={trace.get('colo', '')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

