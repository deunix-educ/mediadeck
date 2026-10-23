"""Point d'entrée : python -m mediadeck [--config PATH] [--check | --url | --make-cert]."""

from __future__ import annotations

import argparse
import logging
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from aiohttp import web

from .config import CONFIG_PATH, Config, ConfigError, load_config
from .tls import TLS_DIR, build_ssl_context, default_hosts, enable_tls, generate_certificates


def lan_address() -> str:
    """Adresse IP locale utilisée pour sortir (aucun paquet n'est émis en UDP connect)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def advertised_host(config: Config) -> str:
    # advertise_host prime : la route par défaut peut passer par un VPN (wg0…) et non par le LAN
    return config.advertise_host or (lan_address() if config.host in ("0.0.0.0", "") else config.host)


def base_url(config: Config) -> str:
    scheme = "https" if config.tls_enabled else "http"
    return f"{scheme}://{advertised_host(config)}:{config.port}"


def client_url(config: Config) -> str:
    return f"{base_url(config)}/?token={config.token}"


def print_qr(url: str) -> None:
    # flush : qrencode écrit directement sur la même sortie, l'ordre doit rester URL puis QR code
    print(url, flush=True)
    # QR code dans le terminal si qrencode est installé
    qrencode = shutil.which("qrencode")
    if qrencode:
        subprocess.run([qrencode, "-t", "ansiutf8", url], check=False)
    else:
        print("(apt install qrencode to display a QR code)", file=sys.stderr)


def print_url(config: Config) -> None:
    print_qr(client_url(config))
    if config.tls_ca is not None:
        print(f"CA certificate to install on the tablet (once): {base_url(config)}/ca.crt", file=sys.stderr)


def make_cert(config: Config, config_path: Path) -> None:
    """Génère la CA locale et le certificat serveur, puis active HTTPS dans la configuration."""
    ca, cert, key = generate_certificates(TLS_DIR, default_hosts(advertised_host(config)))
    print(f"CA certificate: {ca}\nserver certificate: {cert}")
    if enable_tls(config_path, cert, key, ca):
        print(f"HTTPS enabled in {config_path}; restart the service (systemctl --user restart mediadeck)")
    else:
        print(f"{config_path} already sets tls_cert; restart the service to use the new certificate")
    ca_url = f"https://{advertised_host(config)}:{config.port}/ca.crt"
    print("\nOn the tablet, open this address, accept the warning once, and install the downloaded CA:")
    print_qr(ca_url)


def main() -> int:
    parser = argparse.ArgumentParser(prog="mediadeck", description="Web remote for a Linux media PC")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="TOML config file")
    parser.add_argument("--check", action="store_true", help="validate the config and exit")
    parser.add_argument("--url", action="store_true", help="print the tablet URL (and QR code) and exit")
    parser.add_argument("--make-cert", action="store_true", help="create the local CA and HTTPS certificate, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.make_cert:
            make_cert(config, args.config)
            return 0
        ssl_context = build_ssl_context(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"config OK: {args.config}" + (" (HTTPS)" if ssl_context else ""))
        return 0
    if args.url:
        print_url(config)
        return 0

    # Import tardif : --check/--url ne nécessitent ni D-Bus ni evdev
    from .server import MediaDeckServer

    server = MediaDeckServer(config)
    logging.getLogger(__name__).info("tablet URL: %s", client_url(config).split("?")[0])
    web.run_app(server.build_app(), host=config.host, port=config.port, ssl_context=ssl_context, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
