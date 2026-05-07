#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("IPS_SEARCH_PROXY_PORT", "18081"))
TIMEOUT = int(os.environ.get("IPS_SEARCH_PROXY_TIMEOUT", "45"))
OMNIOS_BASE = Path("/storage/omnios")
OPENINDIANA_UPSTREAMS = {
    "hipster": "https://pkg.openindiana.org/hipster",
    "hipster-encumbered": "https://pkg.openindiana.org/hipster-encumbered",
    "localhostoih": "http://sfe.opencsw.org/localhostoih",
}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def resolve_omnios_release(name):
    if re.fullmatch(r"r151[0-9]{3}", name):
        return name
    state = read_json(OMNIOS_BASE / ".mirror-state.json")
    if name == "latest-lts":
        return state.get("latest") or resolve_alias(name)
    if name == "previous-lts":
        return state.get("previous") or resolve_alias(name)
    return None


def resolve_alias(name):
    path = OMNIOS_BASE / name
    try:
        if path.is_symlink():
            target = os.readlink(path)
            return Path(target).name
    except OSError:
        pass
    return None


def upstream_url(local_path):
    path = urllib.parse.urlsplit(local_path).path
    match = re.fullmatch(r"/omnios/(latest-lts|previous-lts|r151[0-9]{3})/(core|extra)/search/([01])/(.+)", path)
    if match:
        release = resolve_omnios_release(match.group(1))
        if not release:
            return None
        return f"https://pkg.omnios.org/{release}/{match.group(2)}/search/{match.group(3)}/{match.group(4)}"

    match = re.fullmatch(r"/omnios/localhostomnios/search/([01])/(.+)", path)
    if match:
        return f"http://sfe.opencsw.org/localhostomnios/search/{match.group(1)}/{match.group(2)}"

    match = re.fullmatch(r"/openindiana/(hipster|hipster-encumbered|localhostoih)/search/([01])/(.+)", path)
    if match:
        return f"{OPENINDIANA_UPSTREAMS[match.group(1)]}/search/{match.group(2)}/{match.group(3)}"

    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ips-search-proxy/1.0"

    def log_message(self, fmt, *args):
        return

    def do_HEAD(self):
        self.proxy_request(send_body=False)

    def do_GET(self):
        self.proxy_request(send_body=True)

    def proxy_request(self, send_body):
        target = upstream_url(self.path)
        if not target:
            self.send_error(404, "Unsupported IPS search path")
            return
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": "multi-os-mirror-ips-search/1.0",
                "Accept": "text/plain,*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "text/plain;charset=utf-8"))
                self.send_header("Cache-Control", response.headers.get("Cache-Control", "public, max-age=300"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "text/plain;charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
        except Exception as exc:
            body = f"IPS search proxy error: {exc}\n".encode("utf-8", "replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain;charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
