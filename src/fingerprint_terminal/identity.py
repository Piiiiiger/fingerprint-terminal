"""Resolve an exit identity through a configured host proxy.

The important invariant is that we never invent the public IP.  The IP is
observed through the proxy first, then geolocation metadata is accepted only
when it describes that same IP.  This keeps timezone/locale hints aligned with
the real network exit used by the transparent namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from typing import Any, Mapping


class IdentityError(RuntimeError):
    """Raised when the proxy exit cannot be resolved consistently."""


_COUNTRY_LOCALES = {
    "AU": "en_AU.UTF-8",
    "CA": "en_CA.UTF-8",
    "CH": "de_CH.UTF-8",
    "CN": "zh_CN.UTF-8",
    "DE": "de_DE.UTF-8",
    "ES": "es_ES.UTF-8",
    "FR": "fr_FR.UTF-8",
    "GB": "en_GB.UTF-8",
    "HK": "zh_HK.UTF-8",
    "IE": "en_IE.UTF-8",
    "IN": "en_IN.UTF-8",
    "IT": "it_IT.UTF-8",
    "JP": "ja_JP.UTF-8",
    "KR": "ko_KR.UTF-8",
    "MY": "en_MY.UTF-8",
    "NL": "nl_NL.UTF-8",
    "NZ": "en_NZ.UTF-8",
    "PL": "pl_PL.UTF-8",
    "RU": "ru_RU.UTF-8",
    "SE": "sv_SE.UTF-8",
    "SG": "en_SG.UTF-8",
    "TW": "zh_TW.UTF-8",
    "US": "en_US.UTF-8",
}


def locale_for_country(country_code: str) -> str:
    return _COUNTRY_LOCALES.get(country_code.upper(), "en_US.UTF-8")


def _clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        env.pop(key, None)
    return env


def _proxy_url(network: Mapping[str, Any]) -> str:
    host = str(network.get("proxy_host", "127.0.0.1"))
    port = int(network.get("proxy_port", 7898))
    protocol = str(network.get("proxy_protocol", "mixed")).lower()
    scheme = "socks5h" if protocol == "socks5" else "http"
    return f"{scheme}://{host}:{port}"


def _curl_text(url: str, network: Mapping[str, Any], *, timeout: int = 12) -> str:
    protocol = str(network.get("proxy_protocol", "mixed")).lower()
    if protocol in {"mixed", "http"}:
        proxy = _proxy_url(network)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "fingerprint-terminal-preflight/0.2"},
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except OSError as exc:
            raise IdentityError(f"proxy identity request failed: {url}: {exc}") from exc

    curl = shutil.which("curl")
    if not curl:
        raise IdentityError("curl is required for proxy identity preflight")
    command = [
        curl,
        "-fsS",
        "--connect-timeout",
        "5",
        "--max-time",
        str(timeout),
        "--proxy",
        _proxy_url(network),
        "--user-agent",
        "fingerprint-terminal-preflight/0.2",
        url,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=_clean_environment(),
            timeout=timeout + 3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityError(f"proxy identity request failed: {url}: {exc}") from exc
    return result.stdout


def _curl_json(url: str, network: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(_curl_text(url, network))
    except json.JSONDecodeError as exc:
        raise IdentityError(f"identity provider returned invalid JSON: {url}") from exc
    if not isinstance(value, dict):
        raise IdentityError(f"identity provider returned a non-object: {url}")
    return value


def parse_cloudflare_trace(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _normalized_ip(payload: Mapping[str, Any]) -> str:
    return str(payload.get("ip") or payload.get("query") or "").strip()


def detect_exit_identity(network: Mapping[str, Any]) -> dict[str, Any]:
    """Return IP-derived identity metadata and fail closed on proxy rotation."""

    trace_before = parse_cloudflare_trace(
        _curl_text("https://cloudflare-dns.com/cdn-cgi/trace", network)
    )
    locked_ip = trace_before.get("ip", "")
    country_code = trace_before.get("loc", "").upper()
    if not locked_ip or not country_code:
        raise IdentityError("Cloudflare trace did not return both ip and loc")

    ipinfo: dict[str, Any] = {}
    ipwho: dict[str, Any] = {}
    provider_errors: list[str] = []
    for name, url in (
        ("ipinfo", "https://ipinfo.io/json"),
        ("ipwho", "https://ipwho.is/"),
    ):
        try:
            payload = _curl_json(url, network)
        except IdentityError as exc:
            provider_errors.append(f"{name}: {exc}")
            continue
        payload_ip = _normalized_ip(payload)
        if payload_ip and payload_ip != locked_ip:
            provider_errors.append(
                f"{name}: exit rotated from {locked_ip} to {payload_ip}"
            )
            continue
        if name == "ipinfo":
            ipinfo = payload
        else:
            ipwho = payload

    trace_after = parse_cloudflare_trace(
        _curl_text("https://cloudflare-dns.com/cdn-cgi/trace", network)
    )
    after_ip = trace_after.get("ip", "")
    after_country = trace_after.get("loc", "").upper()
    if bool(network.get("lock_exit", True)) and after_ip != locked_ip:
        raise IdentityError(
            f"proxy exit changed during preflight: {locked_ip} -> {after_ip or 'unknown'}"
        )
    if after_country and after_country != country_code:
        raise IdentityError(
            f"proxy country changed during preflight: {country_code} -> {after_country}"
        )

    if not ipinfo and not ipwho:
        detail = "; ".join(provider_errors) or "no provider data"
        raise IdentityError(f"could not resolve IP metadata for {locked_ip}: {detail}")

    who_tz = ipwho.get("timezone")
    if isinstance(who_tz, dict):
        who_timezone = str(who_tz.get("id") or "")
    else:
        who_timezone = str(who_tz or "")
    timezone = str(ipinfo.get("timezone") or who_timezone or "UTC")

    connection = ipwho.get("connection")
    if not isinstance(connection, dict):
        connection = {}
    org = str(ipinfo.get("org") or connection.get("org") or connection.get("isp") or "")
    asn = connection.get("asn")
    if not asn and org.upper().startswith("AS"):
        token = org.split(maxsplit=1)[0][2:]
        asn = int(token) if token.isdigit() else ""

    identity = {
        "ip": locked_ip,
        "country_code": country_code,
        "country": str(ipwho.get("country") or country_code),
        "city": str(ipinfo.get("city") or ipwho.get("city") or ""),
        "region": str(ipinfo.get("region") or ipwho.get("region") or ""),
        "postal": str(ipinfo.get("postal") or ipwho.get("postal") or ""),
        "timezone": timezone,
        "locale": locale_for_country(country_code),
        "latitude": ipwho.get("latitude", ""),
        "longitude": ipwho.get("longitude", ""),
        "asn": asn or "",
        "organization": org,
        "cloudflare_colo": trace_before.get("colo", ""),
    }
    return identity


def identity_environment(identity: Mapping[str, Any]) -> dict[str, str]:
    timezone = str(identity.get("timezone") or "UTC")
    locale = str(identity.get("locale") or "en_US.UTF-8")
    result = {
        "TZ": timezone,
        "LANG": locale,
        "LC_ALL": locale,
        "LANGUAGE": locale.split(".", 1)[0],
        "FT_TIMEZONE": timezone,
        "FT_LOCALE": locale,
        "FT_EXIT_IP": str(identity.get("ip") or ""),
        "FT_COUNTRY_CODE": str(identity.get("country_code") or ""),
        "FT_GEO_CITY": str(identity.get("city") or ""),
        "FT_GEO_REGION": str(identity.get("region") or ""),
        "FT_GEO_POSTAL": str(identity.get("postal") or ""),
        "FT_GEO_LATITUDE": str(identity.get("latitude") or ""),
        "FT_GEO_LONGITUDE": str(identity.get("longitude") or ""),
        "FT_ASN": str(identity.get("asn") or ""),
        "FT_ORGANIZATION": str(identity.get("organization") or ""),
        "FT_CLOUDFLARE_COLO": str(identity.get("cloudflare_colo") or ""),
    }
    return result
