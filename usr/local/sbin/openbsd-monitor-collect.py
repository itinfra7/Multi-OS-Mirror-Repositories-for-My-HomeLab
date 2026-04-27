#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import fcntl
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from mirrors_public import repo_url

BASE = Path("/storage/openbsd")
MONITOR_DIR = BASE / ".monitor"
STATUS_PATH = MONITOR_DIR / "status.json"
CACHE_PATH = Path("/run/openbsd-monitor-cache.json")
LOCK_PATH = Path("/run/lock/openbsd-monitor-collect.lock")
STATE_PATH = BASE / ".mirror-state"
KEEP_PATH = BASE / ".keep-versions"
PROGRESS_PATH = Path("/run/openbsd-sync-progress")
RSYNC_LOG = BASE / "logs" / "rsync-current.log"
EVENT_LOG = BASE / "logs" / "sync-events.log"
CLIENT_HINT_PATH = BASE / "CLIENT_URLS.txt"
ARCH = "amd64"
SCAN_TTL = 60


def run(command, timeout=5):
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        return result.returncode, result.stdout.strip()
    except Exception:
        return 1, ""


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(0)
    return fd


def read_text(path):
    try:
        return path.read_text()
    except Exception:
        return ""


def read_tail(path, limit=65536):
    if not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(size - limit, 0))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def format_bytes(value):
    try:
        number = float(value)
    except Exception:
        number = 0.0
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if number < 1024.0 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{value} B"


def parse_rate(value):
    if value is None:
        return None
    match = re.match(r"^\s*([0-9]+(?:[.][0-9]+)?)\s*([KMGT]?i?B|[KMGT]?B|B)/s\s*$", str(value), re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "mb": 1000 ** 2,
        "gb": 1000 ** 3,
        "tb": 1000 ** 4,
        "kib": 1024,
        "mib": 1024 ** 2,
        "gib": 1024 ** 3,
        "tib": 1024 ** 4,
    }
    return number * factors.get(unit, 1)


def format_rate(value):
    if value is None:
        return "-"
    try:
        number = float(value)
    except Exception:
        return "-"
    if number >= 1000 ** 3:
        return f"{number / (1000 ** 3):.2f} GB/s"
    if number >= 1000 ** 2:
        return f"{number / (1000 ** 2):.2f} MB/s"
    if number >= 1000:
        return f"{number / 1000:.1f} KB/s"
    return f"{number:.0f} B/s"


def upstream_speed_snapshot(process, speed_text, target=None):
    running = bool(process.get("running"))
    speed_bps = parse_rate(speed_text) if running else 0.0
    source = "rsync-progress" if speed_bps is not None and running else ("waiting" if running else "idle")
    return {
        "bytes_per_second": round(speed_bps, 2) if speed_bps is not None else None,
        "human": format_rate(speed_bps),
        "raw": speed_text,
        "source": source,
        "target": target,
    }


def statvfs(path):
    info = os.statvfs(path)
    block_size = info.f_frsize
    total = block_size * info.f_blocks
    used = max(block_size * (info.f_blocks - info.f_bfree), 0)
    available = block_size * info.f_bavail
    reserved = max(block_size * (info.f_bfree - info.f_bavail), 0)
    return {
        "path": path,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": available,
        "available_bytes": available,
        "reserved_bytes": reserved,
        "total_human": format_bytes(total),
        "used_human": format_bytes(used),
        "free_human": format_bytes(available),
        "available_human": format_bytes(available),
        "reserved_human": format_bytes(reserved),
        "used_pct": round((used / total) * 100, 2) if total else 0.0,
    }


def loadavg_snapshot():
    try:
        l1, l5, l15 = os.getloadavg()
    except OSError:
        l1, l5, l15 = 0.0, 0.0, 0.0
    return {"load1": round(l1, 2), "load5": round(l5, 2), "load15": round(l15, 2), "human": f"{l1:.2f} / {l5:.2f} / {l15:.2f}"}


def mem_swap_snapshot():
    values = {}
    for line in read_text(Path("/proc/meminfo")).splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    mem_total = values.get("MemTotal", 0)
    mem_available = values.get("MemAvailable", values.get("MemFree", 0))
    mem_used = max(mem_total - mem_available, 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    swap_used = max(swap_total - swap_free, 0)
    return {
        "memory": {
            "total_bytes": mem_total,
            "used_bytes": mem_used,
            "free_bytes": mem_available,
            "total_human": format_bytes(mem_total),
            "used_human": format_bytes(mem_used),
            "free_human": format_bytes(mem_available),
            "used_pct": round((mem_used / mem_total) * 100, 2) if mem_total else 0.0,
        },
        "swap": {
            "total_bytes": swap_total,
            "used_bytes": swap_used,
            "free_bytes": swap_free,
            "total_human": format_bytes(swap_total),
            "used_human": format_bytes(swap_used),
            "free_human": format_bytes(swap_free),
            "used_pct": round((swap_used / swap_total) * 100, 2) if swap_total else 0.0,
        },
    }


def read_cpu_times():
    cpu_times = {}
    for line in read_text(Path("/proc/stat")).splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        values = [int(value) for value in parts[1:] if value.isdigit()]
        if len(values) < 4:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        cpu_times[parts[0]] = {"idle": idle, "total": sum(values)}
    return cpu_times


def cpu_percent_delta(previous, current):
    if not previous or not current:
        return None
    total_delta = current["total"] - previous["total"]
    idle_delta = current["idle"] - previous["idle"]
    if total_delta <= 0:
        return None
    return round(((total_delta - idle_delta) / total_delta) * 100, 2)


def cpu_snapshot(previous, current):
    sample_prev = previous or {}
    sample_current = current or {}
    if not sample_prev:
        sample_prev = read_cpu_times()
        time.sleep(0.15)
        sample_current = read_cpu_times()
    names = [key for key in sample_current if key != "cpu"]
    names.sort(key=lambda item: int(item[3:]) if item[3:].isdigit() else item)
    return {
        "total_busy_pct": cpu_percent_delta(sample_prev.get("cpu"), sample_current.get("cpu")),
        "per_cpu": [{"name": name, "busy_pct": cpu_percent_delta(sample_prev.get(name), sample_current.get(name))} for name in names],
    }


def service_state(name):
    rc, _ = run(["rc-service", name, "status"], timeout=4)
    return "online" if rc == 0 else "offline"


def process_cmdline(pid):
    raw = read_text(Path("/proc") / str(pid) / "cmdline")
    return raw.replace("\x00", " ").strip()


def format_duration(elapsed):
    elapsed = max(int(elapsed), 0)
    hours, rem = divmod(elapsed, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def process_elapsed(pid):
    proc_path = Path("/proc") / str(pid)
    stat = read_text(Path("/proc") / str(pid) / "stat")
    try:
        fields = stat.rsplit(") ", 1)[1].split()
        start_ticks = int(fields[19])
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        uptime = float(read_text(Path("/proc/uptime")).split()[0])
        elapsed = int(uptime - (start_ticks / ticks))
        if 0 <= elapsed <= uptime:
            return format_duration(elapsed)
    except Exception:
        pass
    try:
        return format_duration(time.time() - proc_path.stat().st_ctime)
    except Exception:
        return None


def sync_process():
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        cmd = process_cmdline(entry.name)
        if not cmd:
            continue
        if "sync-openbsd-amd64.sh" in cmd or "openbsd-sync-loop.sh" in cmd or "rsync://ftp.usa.openbsd.org/ftp" in cmd:
            if "openbsd-monitor-collect.py" not in cmd:
                if "sync-openbsd-amd64.sh" in cmd:
                    rank = 0
                elif "rsync://ftp.usa.openbsd.org/ftp" in cmd:
                    rank = 1
                else:
                    rank = 2
                matches.append((rank, int(entry.name), cmd))
    if not matches:
        return {"running": False}
    matches.sort()
    _, pid, cmd = matches[0]
    return {"running": True, "pid": pid, "elapsed": process_elapsed(pid), "command": cmd, "children": [{"pid": p, "command": c} for _, p, c in matches[1:]]}


def parse_key_file(path):
    data = {}
    for line in read_text(path).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.lower()] = value
    return data


def parse_progress():
    text = read_text(PROGRESS_PATH).strip()
    if not text:
        return {"phase": "idle", "done": 0, "total": 0, "current": None, "message": None}
    parts = text.split("|", 5)
    while len(parts) < 6:
        parts.append("")
    try:
        done = int(parts[2])
    except Exception:
        done = 0
    try:
        total = int(parts[3])
    except Exception:
        total = 0
    return {"updated_at": parts[0], "phase": parts[1], "done": done, "total": total, "current": parts[4], "message": parts[5], "raw": text}


def parse_rsync_progress():
    text = read_tail(RSYNC_LOG, 256 * 1024).replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = {"raw_tail": lines[-30:]}
    pattern = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+([^\s]+/s)\s+([0-9:]+)(?:\s+\(xfr#(\d+),\s*to-chk=(\d+)/(\d+)\))?")
    for line in reversed(lines):
        match = pattern.search(line)
        if not match:
            continue
        result.update({
            "line": line,
            "transferred_bytes": int(match.group(1).replace(",", "")),
            "transferred_human": format_bytes(int(match.group(1).replace(",", ""))),
            "percent": int(match.group(2)),
            "speed": match.group(3),
            "eta": match.group(4),
        })
        if match.group(5):
            result.update({"xfr": int(match.group(5)), "to_chk": int(match.group(6)), "to_chk_total": int(match.group(7))})
        break
    return result


def tree_snapshot(path, limit=8):
    total = 0
    files = 0
    temp_files = 0
    latest = []
    if not path.exists():
        return {"exists": False, "bytes": 0, "bytes_human": "0.0 B", "files": 0, "temp_files": 0, "has_temp": False, "latest_files": []}
    for root, _, names in os.walk(path):
        for name in names:
            full = Path(root) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            files += 1
            total += stat.st_size
            rel_path = str(full.relative_to(path))
            is_temp = (
                rel_path.startswith(".~tmp~/")
                or "/.~tmp~/" in rel_path
                or name.startswith(".")
                or name.endswith(".partial")
            )
            if is_temp:
                temp_files += 1
            latest.append((stat.st_mtime, str(full.relative_to(BASE)), stat.st_size))
    latest.sort(reverse=True)
    return {
        "exists": True,
        "bytes": total,
        "bytes_human": format_bytes(total),
        "files": files,
        "temp_files": temp_files,
        "has_temp": temp_files > 0,
        "latest_files": [{"path": rel, "mtime": mtime, "size_bytes": size, "size_human": format_bytes(size)} for mtime, rel, size in latest[:limit]],
    }


def part_status(version, part_name, info, progress):
    current = progress.get("current") or ""
    phase = progress.get("phase") or "idle"
    labels = {
        "release": f"{version}/{ARCH}",
        "packages": f"{version}/packages/{ARCH}",
        "packages_stable": f"{version}/packages-stable/{ARCH}",
        "syspatch": f"syspatch/{version}/{ARCH}",
        "patches": f"patches/{version}/common",
    }
    label = labels.get(part_name, f"{version}/{part_name}")
    if current == label or info.get("has_temp"):
        return "syncing"
    if info.get("exists") and info.get("files", 0) > 0:
        return "ready"
    if part_name in {"packages_stable", "syspatch", "patches"}:
        return "optional"
    if phase not in {"complete", "idle", "error"}:
        return "queued"
    return "missing"


def discover_versions(state):
    versions = []
    for value in [state.get("previous"), state.get("latest")]:
        if value and value not in versions:
            versions.append(value)
    for line in read_text(KEEP_PATH).splitlines():
        line = line.strip()
        if line and line not in versions:
            versions.append(line)
    try:
        local = [entry.name for entry in BASE.iterdir() if entry.is_dir() and re.match(r"^[0-9]+[.][0-9]+$", entry.name)]
        local.sort(key=lambda item: [int(part) for part in item.split(".")])
        for item in local:
            if item not in versions:
                versions.append(item)
    except OSError:
        pass
    return versions


def scan_mirror(state, progress):
    versions = discover_versions(state)
    releases = []
    latest_files = []
    for version in versions:
        parts = {
            "release": tree_snapshot(BASE / version / ARCH),
            "packages": tree_snapshot(BASE / version / "packages" / ARCH),
            "packages_stable": tree_snapshot(BASE / version / "packages-stable" / ARCH),
            "syspatch": tree_snapshot(BASE / "syspatch" / version / ARCH),
            "patches": tree_snapshot(BASE / "patches" / version / "common"),
        }
        for part_name, info in parts.items():
            info["status"] = part_status(version, part_name, info, progress)
        total_bytes = sum(int(item["bytes"]) for item in parts.values())
        total_files = sum(int(item["files"]) for item in parts.values())
        tags = []
        if version == state.get("latest"):
            tags.append("latest")
        if version == state.get("previous"):
            tags.append("previous")
        for item in parts.values():
            latest_files.extend(item.get("latest_files", []))
        releases.append({"version": version, "tags": tags, "bytes": total_bytes, "bytes_human": format_bytes(total_bytes), "files": total_files, "parts": parts})
    latest_files.sort(key=lambda item: item["mtime"], reverse=True)
    return {"scanned_at": time.time(), "versions": versions, "releases": releases, "latest_files": latest_files[:20]}


def load_cache(now, state, progress):
    previous = {}
    if CACHE_PATH.exists():
        try:
            previous = json.loads(CACHE_PATH.read_text())
        except Exception:
            previous = {}
    mirror_scan = previous.get("mirror_scan")
    if not mirror_scan or now - float(mirror_scan.get("scanned_at", 0)) > SCAN_TTL:
        mirror_scan = scan_mirror(state, progress)
    else:
        for release in mirror_scan.get("releases", []):
            version = release.get("version")
            for part_name, info in release.get("parts", {}).items():
                info["status"] = part_status(version, part_name, info, progress)
    cpu_times = read_cpu_times()
    cache = {"updated_at": now, "previous_cpu_times": previous.get("cpu_times", {}), "cpu_times": cpu_times, "mirror_scan": mirror_scan}
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        pass
    return cache


def last_events():
    text = read_tail(EVENT_LOG, 32768)
    return [line for line in text.splitlines() if line.strip()][-50:]


def client_hints(state):
    text = read_text(CLIENT_HINT_PATH).strip()
    if text:
        return text.splitlines()
    latest = state.get("latest") or "7.8"
    base = repo_url("openbsd")
    return [f"{base}/{latest}/{ARCH}/", f"{base}/{latest}/packages/{ARCH}/"]


def estimate(progress, rsync_progress, proc):
    total = int(progress.get("total") or 0)
    done = int(progress.get("done") or 0)
    part = float(rsync_progress.get("percent") or 0)
    if total:
        overall = min(max(((done * 100.0) + part) / total, 0.0), 100.0)
    else:
        overall = 100.0 if not proc.get("running") else 0.0
    return {"overall_percent": round(overall, 2), "current_percent": part, "eta": rsync_progress.get("eta"), "speed": rsync_progress.get("speed")}


def main():
    fd = acquire_lock()
    now = time.time()
    state = parse_key_file(STATE_PATH)
    progress = parse_progress()
    rsync_progress = parse_rsync_progress()
    proc = sync_process()
    cache = load_cache(now, state, progress)
    resources = mem_swap_snapshot()
    estimate_data = estimate(progress, rsync_progress, proc)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "host": {"hostname": socket.gethostname(), "kernel": " ".join(os.uname()), "loadavg": loadavg_snapshot()},
        "system": {"cpu": cpu_snapshot(cache.get("previous_cpu_times", {}), cache.get("cpu_times", {})), "memory": resources["memory"], "swap": resources["swap"]},
        "storage": statvfs(str(BASE)),
        "mirror": {"base": str(BASE), "arch": ARCH, "state": state, "client_hints": client_hints(state), **cache["mirror_scan"]},
        "sync": {"status": "running" if proc.get("running") else "idle", "process": proc, "progress": progress, "rsync": rsync_progress, "estimate": estimate_data, "upstream_speed": upstream_speed_snapshot(proc, estimate_data.get("speed") or rsync_progress.get("speed"), progress.get("current")), "events": last_events(), "rsync_log_tail": rsync_progress.get("raw_tail", [])},
        "services": {"nginx": service_state("nginx"), "crond": service_state("crond"), "sshd": service_state("sshd"), "openbsd-monitor": service_state("openbsd-monitor"), "openbsd-sync": service_state("openbsd-sync")},
    }
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATUS_PATH)
    os.close(fd)


if __name__ == "__main__":
    main()
