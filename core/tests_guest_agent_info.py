from unittest.mock import patch

from django.test import SimpleTestCase

from core.services.guest_agent_info import (
    config_agent_enabled,
    fetch_guest_agent_info,
    parse_interfaces,
    parse_osinfo,
)


class GuestAgentInfoParseTests(SimpleTestCase):
    def test_parse_osinfo_accepts_wrapped_and_unwrapped(self):
        wrapped = {"result": {"name": "ubuntu", "pretty-name": "Ubuntu 24.04 LTS", "version-id": "24.04"}}
        unwrapped = {"name": "ubuntu", "pretty-name": "Ubuntu 24.04 LTS"}
        for payload in (wrapped, unwrapped):
            parsed = parse_osinfo(payload)
            self.assertEqual(parsed["os_name"], "ubuntu")
            self.assertEqual(parsed["os_pretty_name"], "Ubuntu 24.04 LTS")

    def test_parse_osinfo_falls_back_to_name_when_pretty_missing(self):
        self.assertEqual(parse_osinfo({"result": {"name": "alpine"}})["os_pretty_name"], "alpine")

    def test_parse_interfaces_drops_loopback_and_lo(self):
        payload = {
            "result": [
                {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                {
                    "name": "eth0",
                    "hardware-address": "aa:bb",
                    "ip-addresses": [
                        {"ip-address": "127.0.0.1"},
                        {"ip-address": "::1"},
                        {"ip-address": "192.0.2.10"},
                        {"ip-address": "2001:db8::1"},
                    ],
                },
            ]
        }
        ips, interfaces = parse_interfaces(payload)
        self.assertEqual(ips, ["192.0.2.10", "2001:db8::1"])
        self.assertEqual(len(interfaces), 1)
        self.assertEqual(interfaces[0]["name"], "eth0")

    def test_config_agent_enabled_variants(self):
        for value in ("1", "enabled=1", "1,type=virtio", True):
            self.assertTrue(config_agent_enabled({"agent": value}))
        for value in ("0", "", None):
            self.assertFalse(config_agent_enabled({"agent": value}))
        self.assertFalse(config_agent_enabled({}))


class FakeClient:
    def __init__(self, responses):
        self._responses = responses

    def get(self, path, timeout=None):
        for needle, payload in self._responses.items():
            if path.endswith(needle):
                return payload
        return None


class FetchGuestAgentInfoTests(SimpleTestCase):
    def test_fetch_composes_os_hostname_and_ips(self):
        client = FakeClient(
            {
                "agent/get-osinfo": {"result": {"name": "ubuntu", "pretty-name": "Ubuntu 24.04 LTS"}},
                "agent/get-host-name": {"result": {"host-name": "app01"}},
                "agent/network-get-interfaces": {
                    "result": [{"name": "eth0", "ip-addresses": [{"ip-address": "192.0.2.5"}]}]
                },
            }
        )
        with patch("core.services.guest_agent_info.cluster_clients", return_value=[client]):
            summary = fetch_guest_agent_info(cluster=object(), node="pve1", object_type="vm", vmid=100)

        self.assertTrue(summary["running"])
        self.assertEqual(summary["os_pretty_name"], "Ubuntu 24.04 LTS")
        self.assertEqual(summary["hostname"], "app01")
        self.assertEqual(summary["ips"], ["192.0.2.5"])

    def test_fetch_returns_not_running_when_agent_silent(self):
        with patch("core.services.guest_agent_info.cluster_clients", return_value=[FakeClient({})]):
            summary = fetch_guest_agent_info(cluster=object(), node="pve1", object_type="vm", vmid=100)

        self.assertFalse(summary["running"])
        self.assertEqual(summary["ips"], [])

    def test_fetch_returns_empty_without_clients(self):
        with patch("core.services.guest_agent_info.cluster_clients", return_value=[]):
            summary = fetch_guest_agent_info(cluster=object(), node="pve1", object_type="vm", vmid=100)

        self.assertFalse(summary["running"])
