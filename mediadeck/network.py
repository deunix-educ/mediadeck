"""Interfaces réseau : repère une adresse annoncée qui appartient à un tunnel ou un pont, injoignable par la tablette."""

from __future__ import annotations

import fcntl
import os
import socket
import struct
from pathlib import Path

SIOCGIFADDR = 0x8915
SYS_NET = Path("/sys/class/net")


def interface_addresses() -> dict[str, str]:
    """Adresse IPv4 principale de chaque interface qui en a une (ioctl, sans dépendance ni commande externe)."""
    result: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                request = struct.pack("256s", name.encode()[:15])
                reply = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
            except OSError:
                continue  # interface sans adresse IPv4
            result[name] = socket.inet_ntoa(reply[20:24])
    return result


def is_virtual(name: str, sys_net: Path = SYS_NET) -> bool:
    """Vrai pour un tunnel (wireguard, tun), un pont ou un veth : Linux les place sous /sys/devices/virtual."""
    try:
        return "/devices/virtual/" in os.path.realpath(sys_net / name)
    except OSError:
        return False


def advertised_warning(address: str, addresses: dict[str, str] | None = None, sys_net: Path = SYS_NET) -> str | None:
    """Message d'avertissement si `address` est celle d'une interface virtuelle, sinon None.

    La route par défaut passe par un VPN actif : l'adresse détectée est alors celle du tunnel, que la tablette
    ne peut pas joindre (et que le certificat ne couvrirait pas). On propose les adresses des interfaces réelles.
    """
    addresses = interface_addresses() if addresses is None else addresses
    owner = next((name for name, ip in addresses.items() if ip == address), None)
    if owner is None or not is_virtual(owner, sys_net):
        return None
    physical = [f"{ip} ({name})" for name, ip in addresses.items() if not is_virtual(name, sys_net) and name != "lo"]
    hint = f"; candidates: {', '.join(physical)}" if physical else ""
    return (
        f"warning: the detected address {address} belongs to the virtual interface {owner} (VPN tunnel or bridge), "
        f"which the tablet probably cannot reach{hint}. Set server.advertise_host in config.toml"
        " (before 'make cert': the certificate covers that address)."
    )
