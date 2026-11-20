import json
import unittest
from unittest.mock import Mock

from jarvis_bot.vpn_status import QBIT, VPN, collect_vpn_status, render_vpn_status, strict_output_rules


IPV4 = """-P OUTPUT DROP
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A OUTPUT -s 172.29.45.2/32 -d 172.29.45.0/28 -o eth0 -j ACCEPT
-A OUTPUT -d 198.51.100.7/32 -o eth0 -p udp -m udp --dport 51820 -j ACCEPT
-A OUTPUT -o tun0 -j ACCEPT
"""
IPV6 = """-P OUTPUT DROP
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A OUTPUT -d ff02::1:ff00:0/104 -o eth0 -j ACCEPT
-A OUTPUT -o tun0 -j ACCEPT
"""
ENDPOINT = ("198.51.100.7", 51820)


def inventory():
    return [
        {"name": QBIT, "state": "running", "id": "qbit-id", "started_at": "start-qbit", "network_mode": "container:vpn-id"},
        {"name": VPN, "state": "running", "id": "vpn-id", "started_at": "start-vpn"},
    ]


class FakeDocker:
    def __init__(self, *, direct="000\n28\n", control="301\n0\n", mullvad=True, firewall=IPV4):
        self.direct, self.control = direct, control
        self.mullvad, self.firewall = mullvad, firewall
        self.queries = []

    def __call__(self, *args):
        self.queries.append(args)
        if args[0] == "inspect":
            return "qbit-id start-qbit\nvpn-id start-vpn\n"
        if "readlink" in args:
            return "net:[1234]\n"
        if "grep" in args:
            return "Session\\Interface=tun0\n" if args[1] == QBIT else "Endpoint = 198.51.100.7:51820\n"
        if "iptables" in args:
            return self.firewall
        if "ip6tables" in args:
            return IPV6
        if "https://am.i.mullvad.net/json" in args:
            return json.dumps({"mullvad_exit_ip": self.mullvad, "ip": "DO NOT PUBLISH", "organization": "DO NOT PUBLISH"})
        if "--interface" in args:
            return self.direct if args[args.index("--interface") + 1] == "eth0" else self.control
        raise AssertionError(args)


class FirewallTests(unittest.TestCase):
    def test_installed_strict_ipv4_and_ipv6_rules_are_accepted(self):
        self.assertTrue(strict_output_rules(IPV4, version=4, endpoint=ENDPOINT))
        self.assertTrue(strict_output_rules(IPV6, version=6, endpoint=ENDPOINT))

    def test_default_drop_with_broad_accept_is_rejected(self):
        for rule in ("-A OUTPUT -j ACCEPT", "-A OUTPUT -o eth0 -j ACCEPT", "-A OUTPUT -p tcp --dport 443 -j ACCEPT"):
            self.assertFalse(strict_output_rules(IPV4 + rule, version=4, endpoint=ENDPOINT))

    def test_wrong_policy_endpoint_and_vpn_interface_are_rejected(self):
        for rules in (
            IPV4.replace("-P OUTPUT DROP", "-P OUTPUT ACCEPT"),
            IPV4.replace("198.51.100.7/32", "198.51.100.8/32"),
            IPV4.replace("--dport 51820", "--dport 443"),
            IPV4.replace("-A OUTPUT -o tun0 -j ACCEPT", ""),
            IPV4.replace("172.29.45.0/28", "0.0.0.0/0"),
            IPV4.replace("172.29.45.0/28", "198.51.100.0/24"),
        ):
            self.assertFalse(strict_output_rules(rules, version=4, endpoint=ENDPOINT))

    def test_ipv6_broad_bypass_is_rejected(self):
        self.assertFalse(strict_output_rules(IPV6 + "-A OUTPUT -o eth0 -j ACCEPT", version=6, endpoint=ENDPOINT))


class VPNChecksTests(unittest.TestCase):
    def test_live_exit_rules_binding_and_numeric_bypass_evidence(self):
        docker = FakeDocker()
        payload = collect_vpn_status(docker, inventory())
        for key in ("vpn", "namespace", "binding", "firewall", "bypass"):
            self.assertEqual(payload[key]["status"], "pass", key)
        self.assertEqual(payload["outage_test"], "not performed")
        self.assertNotIn("DO NOT PUBLISH", json.dumps(payload))
        self.assertNotIn("stop", {arg for call in docker.queries for arg in call})
        self.assertNotIn("restart", {arg for call in docker.queries for arg in call})
        probes = [call for call in docker.queries if "--interface" in call]
        self.assertEqual(len(probes), 2)
        self.assertTrue(all(call[-1] == "https://1.1.1.1/" for call in probes))

    def test_working_direct_connection_is_failure(self):
        payload = collect_vpn_status(FakeDocker(direct="200\n0\n"), inventory())
        self.assertEqual(payload["bypass"]["status"], "fail")

    def test_http_response_despite_curl_error_still_counts_as_bypass(self):
        payload = collect_vpn_status(FakeDocker(direct="403\n22\n"), inventory())
        self.assertEqual(payload["bypass"]["status"], "fail")

    def test_bad_interface_or_curl_cannot_count_as_protection(self):
        for direct in ("000\n45\n", "127\n", "000\n7\n"):
            payload = collect_vpn_status(FakeDocker(direct=direct), inventory())
            self.assertEqual(payload["bypass"]["status"], "unknown")

    def test_failed_control_does_not_test_or_claim_blocked_bypass(self):
        docker = FakeDocker(control="000\n28\n")
        payload = collect_vpn_status(docker, inventory())
        self.assertEqual(payload["bypass"]["status"], "unknown")
        self.assertFalse(any("eth0" in call and "--interface" in call for call in docker.queries))

    def test_provider_negative_is_red(self):
        payload = collect_vpn_status(FakeDocker(mullvad=False), inventory())
        self.assertEqual(payload["vpn"]["status"], "fail")

    def test_namespace_mismatch_cannot_pass(self):
        entries = inventory()
        entries[0]["network_mode"] = "bridge"
        self.assertEqual(collect_vpn_status(FakeDocker(), entries)["namespace"]["status"], "fail")

    def test_container_restart_invalidates_combined_evidence(self):
        entries = inventory()
        entries[0]["started_at"] = "old-start"
        payload = collect_vpn_status(FakeDocker(), entries)
        self.assertTrue(all(payload[key]["status"] == "unknown" for key in ("vpn", "namespace", "binding", "firewall", "bypass")))

    def test_container_down_short_circuits_without_probes(self):
        entries = inventory()
        entries[0]["state"] = "exited"
        query = Mock()
        payload = collect_vpn_status(query, entries)
        query.assert_not_called()
        self.assertEqual(payload["vpn"]["status"], "fail")

    def test_probe_errors_remain_unknown(self):
        payload = collect_vpn_status(Mock(side_effect=RuntimeError("not accessible")), inventory())
        self.assertTrue(all(payload[key]["status"] == "unknown" for key in ("vpn", "namespace", "binding", "firewall", "bypass")))

    def test_firewall_drift_is_not_green(self):
        payload = collect_vpn_status(FakeDocker(firewall=IPV4 + "-A OUTPUT -o eth0 -j ACCEPT"), inventory())
        self.assertEqual(payload["firewall"]["status"], "fail")

    def test_mismatched_or_missing_saved_binding_is_not_green(self):
        docker = FakeDocker()
        for setting in ("Session\\Interface=eth0\n", "", "Session\\Interface=tun0\nConnection\\Interface=eth0\n"):
            def query(*args):
                if args[:3] == ("exec", QBIT, "grep"):
                    return setting
                return docker(*args)
            self.assertEqual(collect_vpn_status(query, inventory())["binding"]["status"], "fail")

    def test_stale_report_cannot_show_green_protection(self):
        payload = collect_vpn_status(FakeDocker(), inventory())
        text = "\n".join(render_vpn_status(payload, stale=True))
        self.assertNotIn("🟢", text)
        self.assertIn("Full tunnel-outage test: not performed", text)


if __name__ == "__main__":
    unittest.main()
