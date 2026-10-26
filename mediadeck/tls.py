"""HTTPS local : autorité de certification privée, certificat serveur et contexte TLS.

La tablette fait confiance à la CA : celle-ci est donc limitée (nameConstraints) aux réseaux
privés et aux noms locaux, pour qu'une fuite de sa clé ne permette pas d'usurper un site public.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .config import CONFIG_DIR, Config, ConfigError, load_config

TLS_DIR = CONFIG_DIR / "tls"
CA_KEY = "ca.key"
CA_CERT = "mediadeck-ca.crt"
SERVER_KEY = "server.key"
SERVER_CERT = "server.pem"

CA_DAYS = 3650
SERVER_DAYS = 825

# Plages que la CA a le droit de signer (voir nameConstraints)
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_LOCAL_SUFFIXES = (".local", ".lan")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_permitted(host: str, hostname: str) -> bool:
    """Indique si la CA peut signer ce nom (miroir des nameConstraints de la CA)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name = host.lower()
        return name in ("localhost", hostname.lower()) or name.endswith(_LOCAL_SUFFIXES)
    return any(address in network for network in _PRIVATE_NETWORKS)


def default_hosts(advertised: str) -> list[str]:
    """Noms et adresses inscrits dans le certificat serveur, sans doublon."""
    hostname = socket.gethostname()
    hosts = [advertised, hostname, f"{hostname}.local", "localhost", "127.0.0.1"]
    return list(dict.fromkeys(host for host in hosts if host))


def _openssl(*args: str | Path) -> None:
    try:
        subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ConfigError("openssl not found (apt install openssl)") from exc
    except subprocess.CalledProcessError as exc:
        # openssl verify écrit ses diagnostics sur stdout, les autres commandes sur stderr
        raise ConfigError(f"openssl {args[0]} failed: {(exc.stderr or exc.stdout).strip()}") from exc


def _generate_key(path: Path) -> None:
    _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", path)
    path.chmod(0o600)


def _req_config(common_name: str, extensions: str = "") -> str:
    return (
        "[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = ext\n"
        f"[dn]\nCN = {common_name}\n[ext]\n{extensions}"
    )


def _create_ca(directory: Path, hostname: str) -> None:
    constraints = [f"permitted;IP:{net.network_address}/{net.netmask}" for net in _PRIVATE_NETWORKS]
    names = ["localhost", hostname, *_LOCAL_SUFFIXES]
    constraints += [f"permitted;DNS:{name}" for name in names]
    extensions = (
        "basicConstraints = critical, CA:TRUE, pathlen:0\n"
        "keyUsage = critical, keyCertSign, cRLSign\n"
        "subjectKeyIdentifier = hash\n"
        f"nameConstraints = critical, {', '.join(constraints)}\n"
    )
    label = re.sub(r"[^A-Za-z0-9.-]", "", hostname)
    key = directory / CA_KEY
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "ca.cnf"
        conf.write_text(_req_config(f"MediaDeck local CA {label}", extensions))
        _generate_key(key)
        _openssl(
            "req", "-new", "-x509", "-key", key, "-sha256", "-days", str(CA_DAYS),
            "-config", conf, "-out", directory / CA_CERT,
        )


def _issue_server_cert(directory: Path, hosts: Iterable[str]) -> None:
    san = ", ".join(f"{'IP' if _is_ip(host) else 'DNS'}:{host}" for host in hosts)
    extensions = (
        "basicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectKeyIdentifier = hash\n"
        "authorityKeyIdentifier = keyid\n"
        f"subjectAltName = {san}\n"
    )
    key = directory / SERVER_KEY
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "server.cnf"
        csr = Path(tmp) / "server.csr"
        conf.write_text(_req_config("MediaDeck", extensions))
        _generate_key(key)
        _openssl("req", "-new", "-key", key, "-config", conf, "-out", csr)
        _openssl(
            "x509", "-req", "-in", csr, "-CA", directory / CA_CERT, "-CAkey", directory / CA_KEY,
            "-CAcreateserial", "-CAserial", Path(tmp) / "ca.srl", "-days", str(SERVER_DAYS),
            "-sha256", "-extfile", conf, "-extensions", "ext", "-out", directory / SERVER_CERT,
        )
    # Garde-fou : la CA existante doit couvrir ces noms (le nom de la machine a pu changer)
    try:
        _openssl("verify", "-CAfile", directory / CA_CERT, directory / SERVER_CERT)
    except ConfigError as exc:
        raise ConfigError(f"{exc}; delete {directory} to recreate the CA, then rerun 'make cert'") from exc


def generate_certificates(directory: Path, hosts: list[str]) -> tuple[Path, Path, Path]:
    """Crée la CA si besoin puis (ré)émet le certificat serveur ; renvoie (CA, certificat, clé)."""
    hostname = socket.gethostname()
    refused = [host for host in hosts if not is_permitted(host, hostname)]
    if refused:
        raise ConfigError(
            f"the local CA can only sign private addresses and .local/.lan names, not: {', '.join(refused)}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    if not (directory / CA_KEY).exists() or not (directory / CA_CERT).exists():
        _create_ca(directory, hostname)
    _issue_server_cert(directory, hosts)
    return directory / CA_CERT, directory / SERVER_CERT, directory / SERVER_KEY


def enable_tls(config_path: Path, cert: Path, key: Path, ca: Path) -> bool:
    """Déclare le certificat dans [server] ; renvoie False si la configuration le faisait déjà."""
    text = config_path.read_text()
    if re.search(r"(?m)^\s*tls_cert\s*=", text):
        return False
    lines = "".join(
        f"\n{name} = {json.dumps(str(path))}" for name, path in (("tls_cert", cert), ("tls_key", key), ("tls_ca", ca))
    )
    updated, count = re.subn(r"(?m)^port\s*=.*$", lambda match: match.group(0) + lines, text, count=1)
    if count == 0:
        raise ConfigError(f"no 'port' line found in {config_path}: add tls_cert, tls_key and tls_ca manually")
    config_path.write_text(updated)
    return True


def set_http_port(config_path: Path, port: int) -> None:
    """Écrit (port > 0) ou retire (0) `http_port` dans [server] ; restaure le fichier si le résultat est invalide."""
    original = config_path.read_text()
    if port:
        line = f"http_port = {port}"
        existing = r"(?m)^([ \t]*)http_port[ \t]*=.*$"
        if re.search(existing, original):
            updated = re.sub(existing, lambda match: match.group(1) + line, original, count=1)
        else:
            updated, count = re.subn(r"(?m)^port\s*=.*$", lambda match: f"{match.group(0)}\n{line}", original, count=1)
            if count == 0:
                raise ConfigError(f"no 'port' line found in {config_path}: add '{line}' under [server] manually")
    else:
        updated = re.sub(r"(?m)^[ \t]*http_port[ \t]*=.*\n?", "", original)
    config_path.write_text(updated)
    try:
        load_config(config_path)
    except ConfigError:
        config_path.write_text(original)
        raise


def build_ssl_context(config: Config) -> ssl.SSLContext | None:
    """Contexte TLS serveur, ou None si HTTPS n'est pas configuré."""
    if config.tls_cert is None or config.tls_key is None:
        return None
    for label, path in (("tls_cert", config.tls_cert), ("tls_key", config.tls_key), ("tls_ca", config.tls_ca)):
        if path is not None and not path.is_file():
            raise ConfigError(f"server.{label}: file not found: {path} (run 'make cert')")
    # Même règle que pour le fichier de configuration : la clé privée reste personnelle
    if config.tls_key.stat().st_mode & 0o077:
        raise ConfigError(f"{config.tls_key} must not be readable by group/others (chmod 600)")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(config.tls_cert, config.tls_key)
    except (ssl.SSLError, OSError) as exc:
        raise ConfigError(f"cannot load TLS certificate {config.tls_cert}: {exc}") from exc
    return context
