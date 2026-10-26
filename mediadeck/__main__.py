"""Point d'entrée : python -m mediadeck [--config PATH] [--check | --url | --qr | --make-cert | --http-port PORT]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

from .config import CONFIG_PATH, Config, ConfigError, load_config
from .qrpage import QrCard, write_page
from .tls import TLS_DIR, build_ssl_context, default_hosts, enable_tls, generate_certificates, set_http_port


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


def base_url(config: Config, plain: bool = False) -> str:
    """URL de base ; plain=True désigne l'écoute HTTP en clair facultative (http_port)."""
    scheme, port = ("http", config.http_port) if plain else ("https" if config.tls_enabled else "http", config.port)
    return f"{scheme}://{advertised_host(config)}:{port}"


def client_url(config: Config, plain: bool = False) -> str:
    return f"{base_url(config, plain)}/?token={config.token}"


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
    if config.http_port:
        print("Secure (HTTPS, encrypted):", file=sys.stderr)
    print_qr(client_url(config))
    if config.http_port:
        print("\nUnsecured (plain HTTP, trusted local network only):", file=sys.stderr)
        print_qr(client_url(config, plain=True))
    if config.tls_ca is not None:
        print(f"CA certificate to install on the tablet (once): {base_url(config)}/ca.crt", file=sys.stderr)


def access_cards(config: Config) -> list[QrCard]:
    """Codes à scanner : HTTPS et/ou HTTP en clair selon la configuration, puis la CA à installer."""
    cards = []
    if config.tls_enabled:
        cards.append(QrCard("https", base_url(config), client_url(config)))
    if config.http_port:
        cards.append(QrCard("http", base_url(config, plain=True), client_url(config, plain=True)))
    elif not config.tls_enabled:
        cards.append(QrCard("http", base_url(config), client_url(config)))
    if config.tls_ca is not None:
        # En HTTP si possible : aucun avertissement de certificat à accepter pour récupérer la CA
        ca_url = f"{base_url(config, plain=bool(config.http_port))}/ca.crt"
        cards.append(QrCard("ca", ca_url, ca_url))
    return cards


def open_qr_page(config: Config) -> None:
    """Écrit la page des QR codes (mode 600) et l'ouvre dans le navigateur du PC."""
    path = write_page(access_cards(config))
    print(f"QR codes page: {path}")
    if not webbrowser.open(path.as_uri()):
        print("no browser could be opened: open that file by hand", file=sys.stderr)


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
    if not config.http_port:
        print(
            "\nTo also offer unsecured plain HTTP next to HTTPS (no CA to install), run 'make http-on'",
            file=sys.stderr,
        )


def configure_http(config_path: Path, port: int) -> None:
    """Active (port > 0) ou désactive (0) l'écoute HTTP en clair dans la configuration, puis affiche l'adresse."""
    set_http_port(config_path, port)
    config = load_config(config_path)
    if not config.http_port:
        print(f"plain HTTP disabled in {config_path}; restart the service (systemctl --user restart mediadeck)")
        return
    print(f"plain HTTP enabled on port {config.http_port} in {config_path}")
    print("restart the service (systemctl --user restart mediadeck), and open the port if a firewall is active:")
    print(f"  make firewall HTTP_PORT={config.http_port}\n")
    print_url(config)


def main() -> int:
    parser = argparse.ArgumentParser(prog="mediadeck", description="Web remote for a Linux media PC")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="TOML config file")
    parser.add_argument("--check", action="store_true", help="validate the config and exit")
    parser.add_argument("--url", action="store_true", help="print the tablet URL (and QR code) and exit")
    parser.add_argument("--qr", action="store_true", help="show the QR codes in the PC browser, then exit")
    parser.add_argument("--make-cert", action="store_true", help="create the local CA and HTTPS certificate, then exit")
    parser.add_argument(
        "--http-port", type=int, metavar="PORT", help="serve unsecured plain HTTP on PORT too (0 = off), then exit"
    )
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
        if args.http_port is not None:
            configure_http(args.config, args.http_port)
            return 0
        ssl_context = build_ssl_context(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        mode = "HTTPS" if ssl_context else "HTTP"
        if config.http_port:
            mode += f" + plain HTTP on port {config.http_port}"
        print(f"config OK: {args.config} ({mode})")
        return 0
    if args.url:
        print_url(config)
        return 0
    if args.qr:
        open_qr_page(config)
        return 0

    # Import tardif : --check/--url ne nécessitent ni D-Bus ni evdev
    from .server import MediaDeckServer, serve

    server = MediaDeckServer(config)
    log = logging.getLogger(__name__)
    log.info("tablet URL: %s", base_url(config))
    if config.http_port:
        log.info("tablet URL (unsecured, plain HTTP): %s", base_url(config, plain=True))
    asyncio.run(serve(server.build_app(), config, ssl_context))
    return 0


if __name__ == "__main__":
    sys.exit(main())
