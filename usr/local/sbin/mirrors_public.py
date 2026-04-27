# Credits: itinfra7 from GitHub
import os
import re
import socket
import subprocess

CONF = os.environ.get("MIRRORS_SITE_CONF", "/etc/conf.d/mirrors-site")


def _read_conf():
    data = {}
    try:
        with open(CONF, "r") as handle:
            for line in handle:
                match = re.match(r'^\s*([A-Z0-9_]+)=["\']?([^"\']*)["\']?\s*$', line)
                if match:
                    data[match.group(1)] = match.group(2)
    except OSError:
        pass
    return data


def _primary_ipv4():
    try:
        proc = subprocess.run(
            ["ip", "-4", "route", "get", os.environ.get("MIRRORS_ROUTE_PROBE", "1.1.1.1")],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\bsrc\s+([0-9.]+)", proc.stdout or "")
    if match and not match.group(1).startswith("127."):
        return match.group(1)
    return None


def _fallback_host():
    for candidate in (_primary_ipv4(), socket.getfqdn(), socket.gethostname(), "localhost"):
        if candidate and candidate not in {"localhost", "localhost.localdomain"}:
            return candidate
    return "localhost"


def public_origin():
    conf = _read_conf()
    value = os.environ.get("MIRRORS_PUBLIC_URL") or conf.get("MIRRORS_PUBLIC_URL") or ""
    scheme = os.environ.get("MIRRORS_PUBLIC_SCHEME") or conf.get("MIRRORS_PUBLIC_SCHEME") or "http"
    value = value.strip().rstrip("/")
    if value:
        return value if re.match(r"^https?://", value) else f"{scheme}://{value}"
    return f"{scheme}://{_fallback_host()}"


def repo_url(path):
    return public_origin().rstrip("/") + "/" + str(path).lstrip("/")
