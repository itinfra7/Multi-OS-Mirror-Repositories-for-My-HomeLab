#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IFACE = os.environ.get("MIRRORS_LIMIT_IFACE", "eth0")
CONF = "/etc/conf.d/mirrors-ingress-limit"
LIMIT_SCRIPT = "/usr/local/sbin/mirrors-ingress-limit.sh"
HOST = "127.0.0.1"
PORT = int(os.environ.get("MIRRORS_TRAFFIC_API_PORT", "18080"))
STORAGE_CACHE_SECONDS = int(os.environ.get("MIRRORS_REPO_STORAGE_CACHE_SECONDS", "300"))
STORAGE_CACHE_FILE = os.environ.get(
    "MIRRORS_REPO_STORAGE_CACHE_FILE",
    "/var/cache/mirrors-traffic-api/repo-storage.json",
)
REPO_PATHS = {
    "alpine": "/storage/alpine",
    "opencsw": "/storage/opencsw",
    "openbsd": "/storage/openbsd",
    "omnios": "/storage/omnios",
    "openindiana": "/storage/openindiana",
}

state_lock = threading.Lock()
storage_lock = threading.Lock()
speed_state = {
    "rx_bytes": 0,
    "bytes_per_second": 0.0,
    "sample_seconds": 0.0,
    "updated_at": None,
}
storage_state = {
    "expires_at": 0.0,
    "payload": {},
    "refreshing": False,
    "last_started_at": None,
    "last_completed_at": None,
    "last_error": None,
}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_rx_bytes():
    with open(f"/sys/class/net/{IFACE}/statistics/rx_bytes", "r") as handle:
        return int(handle.read().strip())


def sampler():
    previous_bytes = read_rx_bytes()
    previous_time = time.monotonic()
    while True:
        time.sleep(1)
        current_bytes = read_rx_bytes()
        current_time = time.monotonic()
        elapsed = max(current_time - previous_time, 0.001)
        rate = max(current_bytes - previous_bytes, 0) / elapsed
        with state_lock:
            speed_state.update(
                {
                    "rx_bytes": current_bytes,
                    "bytes_per_second": rate,
                    "sample_seconds": elapsed,
                    "updated_at": now_iso(),
                }
            )
        previous_bytes = current_bytes
        previous_time = current_time


def parse_conf():
    data = {
        "MIRRORS_LIMIT_ENABLED": "yes",
        "MIRRORS_LIMIT_IFACE": IFACE,
        "MIRRORS_INGRESS_RATE": "16mbit",
        "MIRRORS_INGRESS_BURST": "512k",
    }
    try:
        with open(CONF, "r") as handle:
            for line in handle:
                match = re.match(r'^\s*([A-Z0-9_]+)=["\']?([^"\']*)["\']?\s*$', line)
                if match:
                    data[match.group(1)] = match.group(2)
    except FileNotFoundError:
        pass
    return data


def conf_quote(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def write_conf(conf):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp_path = f"{CONF}.tmp"
    fields = [
        "MIRRORS_LIMIT_ENABLED",
        "MIRRORS_LIMIT_IFACE",
        "MIRRORS_INGRESS_RATE",
        "MIRRORS_INGRESS_BURST",
    ]
    with open(tmp_path, "w") as handle:
        for field in fields:
            handle.write(f'{field}="{conf_quote(conf[field])}"\n')
    os.replace(tmp_path, CONF)


def run_command(args, timeout=None):
    try:
        return subprocess.run(
            args,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return subprocess.CompletedProcess(args, 124, stdout, f"{stderr}\ntimeout after {timeout}s".strip())


def human_bytes(byte_count):
    if byte_count is None:
        return "-"
    value = float(byte_count)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{value:.1f} {units[unit]}"


def transient_du_warning(stderr):
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    return bool(lines) and all("No such file or directory" in line for line in lines)


def directory_usage(path, scanned_at):
    item = {
        "path": path,
        "exists": os.path.exists(path),
        "scanned_at": scanned_at,
    }
    if not item["exists"]:
        item["error"] = "path missing"
        return item

    proc = None
    for attempt in range(3):
        proc = run_command(["du", "-sk", path], timeout=180)
        if proc.returncode == 0 or not transient_du_warning(proc.stderr):
            break
        time.sleep(0.2 * (attempt + 1))

    try:
        kib = int((proc.stdout or "").split()[0])
    except (IndexError, ValueError):
        item["error"] = (proc.stderr or "du output parse failed")[-500:]
        item["raw"] = (proc.stdout or "")[-500:]
        return item

    used_bytes = kib * 1024
    item.update(
        {
            "used_bytes": used_bytes,
            "used_human": human_bytes(used_bytes),
        }
    )
    if proc.returncode != 0 and not transient_du_warning(proc.stderr):
        item["warning"] = (proc.stderr or "du completed with warnings")[-500:]
    return item


def pending_storage_payload(scanned_at):
    return {
        repo: {
            "path": path,
            "exists": os.path.exists(path),
            "scanning": True,
            "scanned_at": scanned_at,
        }
        for repo, path in REPO_PATHS.items()
    }


def load_storage_cache():
    try:
        with open(STORAGE_CACHE_FILE, "r") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return

    payload = data.get("repo_storage") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return
    with storage_lock:
        storage_state["payload"] = payload
        storage_state["expires_at"] = 0.0
        storage_state["last_completed_at"] = data.get("generated_at")
        storage_state["last_error"] = None


def save_storage_cache(payload):
    try:
        os.makedirs(os.path.dirname(STORAGE_CACHE_FILE), exist_ok=True)
        tmp_path = f"{STORAGE_CACHE_FILE}.tmp"
        with open(tmp_path, "w") as handle:
            json.dump({"generated_at": now_iso(), "repo_storage": payload}, handle, sort_keys=True)
        os.replace(tmp_path, STORAGE_CACHE_FILE)
    except OSError:
        pass


def refresh_storage_cache():
    scanned_at = now_iso()
    try:
        payload = {repo: directory_usage(path, scanned_at) for repo, path in REPO_PATHS.items()}
        last_error = None
    except Exception as error:
        payload = None
        last_error = str(error)

    with storage_lock:
        if payload is not None:
            storage_state["payload"] = payload
            storage_state["expires_at"] = time.monotonic() + STORAGE_CACHE_SECONDS
        storage_state["refreshing"] = False
        storage_state["last_completed_at"] = now_iso()
        storage_state["last_error"] = last_error
    if payload is not None:
        save_storage_cache(payload)



def ensure_storage_refresh(force=False):
    now = time.monotonic()
    with storage_lock:
        if not force and storage_state["payload"] and now < storage_state["expires_at"]:
            return
        if storage_state["refreshing"]:
            return
        storage_state["refreshing"] = True
        storage_state["last_started_at"] = now_iso()

    threading.Thread(target=refresh_storage_cache, daemon=True).start()


def repo_storage_payload():
    ensure_storage_refresh()
    with storage_lock:
        if storage_state["payload"]:
            return dict(storage_state["payload"])
        return pending_storage_payload(storage_state["last_started_at"] or now_iso())


def storage_cache_status():
    with storage_lock:
        expires_in = max(0.0, storage_state["expires_at"] - time.monotonic())
        return {
            "refreshing": storage_state["refreshing"],
            "cache_seconds": STORAGE_CACHE_SECONDS,
            "expires_in_seconds": round(expires_in, 1),
            "last_started_at": storage_state["last_started_at"],
            "last_completed_at": storage_state["last_completed_at"],
            "last_error": storage_state["last_error"],
        }


def tc_status():
    proc = run_command(["tc", "filter", "show", "dev", IFACE, "parent", "ffff:"])
    output = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"police .*?rate\s+([0-9A-Za-z.]+)", output)
    return {
        "active": bool(match),
        "tc_rate": match.group(1) if match else None,
        "raw": output[-2000:],
    }


def rate_to_bps(rate):
    text = str(rate or "").strip().lower()
    match = re.match(r"^([0-9.]+)\s*([a-z]+)$", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    factors = {
        "bit": 1,
        "kbit": 1000,
        "mbit": 1000 * 1000,
        "gbit": 1000 * 1000 * 1000,
    }
    for suffix, factor in factors.items():
        if unit == suffix:
            return value * factor / 8
    return None


def mbps_to_rate(mbps):
    try:
        value = float(mbps)
    except (TypeError, ValueError):
        raise ValueError("mbps must be a number") from None
    if not value > 0:
        raise ValueError("mbps must be greater than 0")
    if value > 10000:
        raise ValueError("mbps is too large")
    mbit = value * 8
    rate = f"{mbit:.6f}".rstrip("0").rstrip(".")
    return value, f"{rate}mbit"


def human_rate(bytes_per_second):
    if bytes_per_second is None:
        return "-"
    value = float(bytes_per_second)
    if value >= 1000 * 1000:
        return f"{value / 1000 / 1000:.2f} MB/s"
    if value >= 1000:
        return f"{value / 1000:.1f} KB/s"
    return f"{value:.0f} B/s"


def status_payload():
    conf = parse_conf()
    tc = tc_status()
    configured_rate = conf.get("MIRRORS_INGRESS_RATE", "16mbit")
    configured_bps = rate_to_bps(configured_rate)
    with state_lock:
        speed = dict(speed_state)
    repo_storage = repo_storage_payload()
    repo_storage_cache = storage_cache_status()
    return {
        "generated_at": now_iso(),
        "iface": IFACE,
        "speed": {
            **speed,
            "human": human_rate(speed.get("bytes_per_second")),
        },
        "limit": {
            "enabled": tc["active"],
            "configured_enabled": str(conf.get("MIRRORS_LIMIT_ENABLED", "yes")).lower() in ["yes", "true", "1", "on"],
            "rate": configured_rate,
            "rate_bytes_per_second": configured_bps,
            "rate_megabytes_per_second": configured_bps / 1000 / 1000 if configured_bps is not None else None,
            "rate_human": human_rate(configured_bps),
            "burst": conf.get("MIRRORS_INGRESS_BURST", "512k"),
            "tc_rate": tc["tc_rate"],
        },
        "repo_storage_cache": repo_storage_cache,
        "repo_storage": repo_storage,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "mirrors-traffic-api/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, code, payload, include_body=True):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def post_params(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items() if values}
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return params
        raw = self.rfile.read(min(length, 8192))
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            data = json.loads(raw.decode("utf-8") or "{}")
            if isinstance(data, dict):
                params.update(data)
            return params
        params.update({key: values[-1] for key, values in urllib.parse.parse_qs(raw.decode("utf-8")).items() if values})
        return params

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/status":
            self.send_json(200, status_payload())
            return
        self.send_json(404, {"error": "not found"})

    def do_HEAD(self):
        if self.path.split("?", 1)[0] == "/status":
            self.send_json(200, status_payload(), include_body=False)
            return
        self.send_json(404, {"error": "not found"}, include_body=False)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/toggle":
            proc = run_command([LIMIT_SCRIPT, "toggle"])
            payload = status_payload()
            payload["toggle"] = {
                "rc": proc.returncode,
                "stdout": proc.stdout[-1000:],
                "stderr": proc.stderr[-1000:],
            }
            self.send_json(200 if proc.returncode == 0 else 500, payload)
            return
        if path == "/set-rate":
            try:
                params = self.post_params()
                mbps, rate = mbps_to_rate(params.get("mbps"))
                conf = parse_conf()
                conf["MIRRORS_LIMIT_ENABLED"] = "yes"
                conf["MIRRORS_INGRESS_RATE"] = rate
                write_conf(conf)
                proc = run_command([LIMIT_SCRIPT, "start"], timeout=10)
                payload = status_payload()
                payload["set_rate"] = {
                    "rc": proc.returncode,
                    "mbps": mbps,
                    "rate": rate,
                    "stdout": proc.stdout[-1000:],
                    "stderr": proc.stderr[-1000:],
                }
                self.send_json(200 if proc.returncode == 0 else 500, payload)
            except (json.JSONDecodeError, ValueError, OSError) as error:
                self.send_json(400, {"error": "invalid set-rate request", "set_rate": {"error": str(error)}})
            return
        self.send_json(404, {"error": "not found"})


def main():
    threading.Thread(target=sampler, daemon=True).start()
    load_storage_cache()
    ensure_storage_refresh(force=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
