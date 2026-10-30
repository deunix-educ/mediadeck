"""Détection d'une adresse annoncée portée par une interface virtuelle (VPN, pont)."""

import io
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from mediadeck import __main__ as cli
from mediadeck.config import parse_config
from mediadeck.network import advertised_warning, interface_addresses, is_virtual


class FakeSysNet:
    """Arbre /sys/class/net factice : les liens pointent sous /devices/pci… ou /devices/virtual…."""

    def __init__(self, physical: list[str], virtual: list[str]) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.path = root / "class" / "net"
        self.path.mkdir(parents=True)
        for name in physical:
            target = root / "devices" / "pci0000:00" / "net" / name
            target.mkdir(parents=True)
            (self.path / name).symlink_to(target)
        for name in virtual:
            target = root / "devices" / "virtual" / "net" / name
            target.mkdir(parents=True)
            (self.path / name).symlink_to(target)

    def close(self) -> None:
        self._tmp.cleanup()


ADDRESSES = {"lo": "127.0.0.1", "wlp7s0": "192.168.1.34", "wg0": "10.2.0.2"}


class NetworkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sys = FakeSysNet(["wlp7s0"], ["lo", "wg0"])
        self.addCleanup(self.sys.close)

    def test_is_virtual(self) -> None:
        self.assertTrue(is_virtual("wg0", self.sys.path))
        self.assertFalse(is_virtual("wlp7s0", self.sys.path))
        self.assertFalse(is_virtual("absent", self.sys.path))

    def test_warns_for_tunnel_and_suggests_physical_interface(self) -> None:
        message = advertised_warning("10.2.0.2", ADDRESSES, self.sys.path)
        assert message is not None
        self.assertIn("wg0", message)
        self.assertIn("192.168.1.34 (wlp7s0)", message)
        self.assertIn("advertise_host", message)
        self.assertNotIn("127.0.0.1", message)

    def test_no_warning_for_physical_interface(self) -> None:
        self.assertIsNone(advertised_warning("192.168.1.34", ADDRESSES, self.sys.path))

    def test_no_warning_for_unknown_address(self) -> None:
        self.assertIsNone(advertised_warning("203.0.113.9", ADDRESSES, self.sys.path))

    def test_warning_without_candidate(self) -> None:
        message = advertised_warning("10.2.0.2", {"wg0": "10.2.0.2"}, self.sys.path)
        assert message is not None
        self.assertNotIn("candidates", message)

    def test_interface_addresses_lists_loopback(self) -> None:
        self.assertEqual(interface_addresses().get("lo"), "127.0.0.1")


class WarnIfVirtualTest(unittest.TestCase):
    def run_warn(self, toml: str, detected: str = "10.2.0.2") -> str:
        data = tomllib.loads(toml)
        data.setdefault("server", {})["token"] = "x" * 32
        config = parse_config(data)
        buffer = io.StringIO()
        with (
            mock.patch.object(cli, "lan_address", return_value=detected),
            mock.patch.object(cli, "advertised_warning", return_value="warning: virtual") as check,
            redirect_stderr(buffer),
        ):
            cli.warn_if_virtual(config)
        self.last_called = check.called
        return buffer.getvalue()

    def test_warns_when_address_is_detected(self) -> None:
        self.assertIn("warning: virtual", self.run_warn(""))

    def test_silent_when_advertise_host_is_set(self) -> None:
        self.assertEqual(self.run_warn('[server]\nadvertise_host = "10.2.0.2"'), "")
        self.assertFalse(self.last_called)

    def test_silent_when_host_is_explicit(self) -> None:
        self.assertEqual(self.run_warn('[server]\nhost = "127.0.0.1"'), "")
        self.assertFalse(self.last_called)


if __name__ == "__main__":
    unittest.main()
