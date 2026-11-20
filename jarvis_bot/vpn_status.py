"""Non-disruptive qBittorrent/Mullvad runtime checks for the host collector.

No routes, interfaces, firewall rules, torrents or containers are changed. Fresh
HTTPS connections use a fixed numeric target to rule out DNS-only failures.
Blocked bypass checks are not represented as a complete tunnel-outage test.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import time
from typing import Any, Callable


QBIT = "omen-qbittorrent"
VPN = "omen-qbittorrent-vpn"
CHECK_URL = "https://am.i.mullvad.net/json"
PROBE_URL = "https://1.1.1.1/"
CURL_BASE = ("curl", "--disable", "--noproxy", "*", "-4", "--silent", "--show-error", "--connect-timeout", "2", "--max-time", "4")


def result(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def strict_output_rules(text: str, *, version: int, endpoint: tuple[str, int]) -> bool:
    """Recognize Gluetun's strict output allowlist; reject unknown/broad rules.

Default DROP alone is insufficient: an earlier ACCEPT could open the interface.
Allow the configured WireGuard endpoint only, plus local Docker replies,
established flows, loopback, IPv6 neighbour discovery and the VPN interface.
"""
    lines = [shlex.split(line) for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != ["-P", "OUTPUT", "DROP"]:
        return False
    tunnel_rule = False
    for tokens in lines[1:]:
        if tokens[:2] != ["-A", "OUTPUT"]:
            return False
        rule = tokens[2:]
        if rule == ["-o", "tun0", "-j", "ACCEPT"]:
            tunnel_rule = True
            continue
        if rule in (
            ["-o", "lo", "-j", "ACCEPT"],
            ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ):
            continue
        if version == 6 and rule == ["-d", "ff02::1:ff00:0/104", "-o", "eth0", "-j", "ACCEPT"]:
            continue
        # Current Docker subnet exception, never an arbitrary Internet subnet.
        if (version == 4 and len(rule) == 8 and rule[0] == "-s"
                and rule[2] == "-d" and rule[4:] == ["-o", "eth0", "-j", "ACCEPT"]):
            try:
                source = ipaddress.ip_network(rule[1])
                destination = ipaddress.ip_network(rule[3])
                if (source.version == 4 and source.prefixlen == 32
                        and destination.version == 4 and destination.prefixlen >= 24
                        and destination.is_private and source.network_address in destination):
                    continue
            except ValueError:
                pass
        # Only the WireGuard endpoint read from the installed VPN configuration.
        if (len(rule) == 12 and rule[0] == "-d"
                and rule[2:10] == ["-o", "eth0", "-p", "udp", "-m", "udp", "--dport", str(endpoint[1])]
                and rule[10:] == ["-j", "ACCEPT"]):
            try:
                destination = ipaddress.ip_network(rule[1])
                if (destination.version == version and destination.num_addresses == 1
                        and str(destination.network_address) == endpoint[0]):
                    continue
            except ValueError:
                pass
        return False
    return tunnel_rule


def probe(query: Callable[..., str], interface: str) -> tuple[int, int]:
    # Fixed arguments passed positionally, never interpolated shell commands.
    # curl -w emits the HTTP code even on failure; retain curl's distinct exit
    # status so an unavailable binary/invalid interface cannot count as blocked.
    raw = query(
        "exec", QBIT, "sh", "-c",
        '"$@"; code=$?; printf "\\n%s\\n" "$code"', "probe",
        *CURL_BASE, "--interface", interface, "--output", "/dev/null",
        "--write-out", "%{http_code}", PROBE_URL,
    )
    lines = raw.strip().splitlines()
    if len(lines) != 2 or not all(re.fullmatch(r"[0-9]+", line) for line in lines):
        raise ValueError("Invalid probe response")
    return int(lines[0]), int(lines[1])


def collect_vpn_status(query: Callable[..., str], containers: list[dict[str, Any]]) -> dict[str, Any]:
    status: dict[str, Any] = {
        "checked_at": time.time(),
        "vpn": result("unknown", "VPN connectivity not verified"),
        "namespace": result("unknown", "Network isolation not verified"),
        "binding": result("unknown", "Saved qBittorrent interface binding not verified"),
        "firewall": result("unknown", "IPv4/IPv6 egress rules not verified"),
        "bypass": result("unknown", "Direct-network bypass probe not verified"),
        "outage_test": "not performed",
    }
    entries = {item["name"]: item for item in containers}
    if any(entries.get(name, {}).get("state") != "running" for name in (QBIT, VPN)):
        status["vpn"] = result("fail", "qBittorrent or VPN container is not running/present")
        return status

    try:
        qnet = query("exec", QBIT, "readlink", "/proc/self/ns/net").strip()
        vnet = query("exec", VPN, "readlink", "/proc/self/ns/net").strip()
        shared = bool(re.fullmatch(r"net:\[[0-9]+\]", qnet)) and qnet == vnet
        mode = entries[QBIT].get("network_mode")
        isolated = shared and mode == "container:" + entries[VPN]["id"]
        status["namespace"] = result(
            "pass" if isolated else "fail",
            "qBittorrent shares the VPN network namespace" if isolated else "VPN network isolation mismatch",
        )
    except (RuntimeError, ValueError, KeyError):
        pass

    try:
        raw = query(
            "exec", QBIT, "grep", "-E", r"^(Session|Connection)\\Interface=",
            "/config/qBittorrent/config/qBittorrent.conf",
        )
        values = [line.split("=", 1)[1].strip() for line in raw.splitlines() if "=" in line]
        bound = bool(values) and set(values) == {"tun0"}
        status["binding"] = result(
            "pass" if bound else "fail",
            "Saved qBittorrent binding: tun0" if bound else "Saved qBittorrent binding is not exclusively tun0",
        )
    except (RuntimeError, ValueError):
        pass

    try:
        raw = query("exec", QBIT, *CURL_BASE, "--fail", CHECK_URL)
        response = json.loads(raw)
        confirmed = response.get("mullvad_exit_ip") is True
        status["vpn"] = result(
            "pass" if confirmed else "fail",
            "Mullvad confirms qBittorrent's current VPN exit" if confirmed else "Mullvad did NOT confirm a VPN exit for qBittorrent",
        )
    except (RuntimeError, ValueError, AttributeError):
        status["vpn"] = result("unknown", "Mullvad check unreachable/invalid; connectivity unverified")

    try:
        raw = query("exec", VPN, "grep", "-E", r"^[[:space:]]*Endpoint[[:space:]]*=", "/gluetun/wireguard/wg0.conf")
        endpoint_text = raw.strip().split("=", 1)[1].strip()
        address, port = endpoint_text.rsplit(":", 1)
        endpoint = (str(ipaddress.ip_address(address.strip("[]"))), int(port))
        if not 1 <= endpoint[1] <= 65535:
            raise ValueError("Invalid endpoint")
        ipv4 = query("exec", VPN, "iptables", "-S", "OUTPUT")
        ipv6 = query("exec", VPN, "ip6tables", "-S", "OUTPUT")
        strict = strict_output_rules(ipv4, version=4, endpoint=endpoint) and strict_output_rules(ipv6, version=6, endpoint=endpoint)
        status["firewall"] = result(
            "pass" if strict else "fail",
            "IPv4 + IPv6 default DROP; strict egress allowlist verified" if strict else "IPv4/IPv6 egress rules differ from the strict allowlist",
        )
    except (RuntimeError, ValueError, IndexError):
        pass

    # A working tunnel control to the SAME numeric address and TLS port is
    # required before treating a direct-interface timeout as supporting evidence.
    try:
        http, exit_code = probe(query, "tun0")
        if exit_code == 0 and 100 <= http < 600:
            direct_http, direct_exit = probe(query, "eth0")
            if direct_exit == 0 or direct_http > 0:
                status["bypass"] = result("fail", "Direct eth0 Internet connection succeeded — possible VPN bypass")
            elif direct_exit == 28 and direct_http == 0:
                status["bypass"] = result("pass", "Fresh direct eth0 HTTPS blocked; same numeric target reachable through tun0")
            else:
                status["bypass"] = result("unknown", "Direct probe errored; this does not prove firewall blocking")
        else:
            status["bypass"] = result("unknown", "Tunnel control probe failed; blocking result would be inconclusive")
    except (RuntimeError, ValueError):
        pass

    # Re-creation/restarts during a sample invalidate the combined result.
    try:
        current = query("inspect", "--format", "{{.Id}} {{.State.StartedAt}}", QBIT, VPN).strip().splitlines()
        expected = [f"{entries[name]['id']} {entries[name]['started_at']}" for name in (QBIT, VPN)]
        if current != expected:
            raise ValueError("Containers changed during check")
    except (RuntimeError, ValueError, KeyError):
        for key in ("vpn", "namespace", "binding", "firewall", "bypass"):
            status[key] = result("unknown", "Container changed or became unavailable during check; retry next sample")
    return status


def render_vpn_status(payload: Any, *, stale: bool) -> list[str]:
    if not isinstance(payload, dict):
        return ["", "⚪ **qBittorrent VPN protection** · checks not available yet"]
    lines = ["", "**qBittorrent VPN protection**"]
    for key, label in (
        ("vpn", "VPN live"), ("namespace", "Isolation"), ("binding", "Binding"),
        ("firewall", "Kill-switch rules"), ("bypass", "Bypass test"),
    ):
        item = payload.get(key) or {}
        # Only fixed, locally generated descriptions are published by collector.
        icon = "⚪" if stale else {"pass": "🟢", "fail": "🔴"}.get(item.get("status"), "🟡")
        lines.append(f"{icon} {label}: {item.get('detail', 'not verified')}")
    lines.append("Full tunnel-outage test: not performed. Checks above do not interrupt downloads or prove every failure scenario.")
    return lines
