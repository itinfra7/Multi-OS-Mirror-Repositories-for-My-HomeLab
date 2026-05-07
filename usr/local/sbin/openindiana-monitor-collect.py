#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import heapq
import json
import os
import re
import socket
import subprocess
import time
import calendar
from pathlib import Path

from mirrors_public import repo_url

BASE = Path("/storage/openindiana")
MONITOR_DIR = BASE / ".monitor"
STATUS_PATH = MONITOR_DIR / "status.json"
CACHE_PATH = Path("/run/openindiana-monitor-cache.json")
LOCK_PATH = Path("/run/lock/openindiana-monitor-collect.lock")
STATE_PATH = BASE / ".mirror-state.json"
PROGRESS_PATH = Path("/run/openindiana-sync-progress.json")
EVENT_LOG = BASE / "logs/sync-events.log"
STEP_LOG = Path("/storage/logs/mirrors-sync-openindiana.log")
SYNC_LOG = BASE / "logs/sync-events.log"
SERVER_URL = repo_url("openindiana")
SCAN_TTL = 180
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MIRROR_SOURCE = "https://pkg.openindiana.org/hipster + https://pkg.openindiana.org/hipster-encumbered + http://sfe.opencsw.org/localhostoih"
MIRROR_POLICY = "OpenIndiana Hipster IPS is rolling. This mirror keeps official publishers plus the third-party SFE localhostoih publisher; no separate previous IPS repository exists upstream."

REPOS = {
    "hipster": {
        "publisher": "openindiana.org",
        "source": "https://pkg.openindiana.org/hipster",
        "title": "OpenIndiana Hipster",
    },
    "hipster-encumbered": {
        "publisher": "hipster-encumbered",
        "source": "https://pkg.openindiana.org/hipster-encumbered",
        "title": "OpenIndiana Hipster Encumbered",
    },
    "localhostoih": {
        "publisher": "localhostoih",
        "source": "http://sfe.opencsw.org/localhostoih",
        "title": "SFE OpenIndiana Hipster Third-Party",
    },
}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def human_bytes(value):
    if value is None:
        return "-"
    n = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}"


def format_elapsed(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def process_elapsed_seconds(pid):
    proc_path = Path("/proc") / str(pid)
    try:
        stat = (proc_path / "stat").read_text()
        fields = stat.rsplit(") ", 1)[1].split()
        start_ticks = int(fields[19])
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        elapsed = int(uptime - (start_ticks / ticks))
        if 0 <= elapsed <= uptime:
            return elapsed
    except Exception:
        pass
    try:
        return max(0, int(time.time() - proc_path.stat().st_ctime))
    except Exception:
        pass
    return None


def parse_etime(value):
    days = 0
    text = value.strip()
    match = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+))?", text)
    if match and (match.group(1) or match.group(2)):
        day_value = int(match.group(1) or 0)
        hour_value = int(match.group(2) or 0)
        minute_value = int(match.group(3) or 0)
        return day_value * 86400 + hour_value * 3600 + minute_value * 60
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            days = 0
    parts = [int(part) for part in text.split(":") if part.isdigit()]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 1:
        h, m, s = 0, 0, parts[0]
    else:
        h, m, s = 0, 0, 0
    return days * 86400 + h * 3600 + m * 60 + s


def storage_info():
    BASE.mkdir(parents=True, exist_ok=True)
    stat = os.statvfs(BASE)
    total = stat.f_blocks * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    free = stat.f_bfree * stat.f_frsize
    used = total - free
    pct = round((used / total) * 100, 2) if total else 0
    return {
        "path": str(BASE),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "available_bytes": available,
        "total_human": human_bytes(total),
        "used_human": human_bytes(used),
        "free_human": human_bytes(free),
        "available_human": human_bytes(available),
        "used_pct": pct,
        "reserved_bytes": free - available,
        "reserved_human": human_bytes(free - available),
    }


def loadavg():
    try:
        values = Path("/proc/loadavg").read_text().split()[:3]
        return {"values": [float(v) for v in values], "human": " / ".join(values)}
    except Exception:
        return {"values": [], "human": "-"}


def meminfo():
    data = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            data[key] = int(rest.strip().split()[0]) * 1024
    except Exception:
        pass
    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", 0)
    used = max(total - available, 0)
    swap_total = data.get("SwapTotal", 0)
    swap_free = data.get("SwapFree", 0)
    swap_used = max(swap_total - swap_free, 0)
    return {
        "memory": {"total_bytes": total, "used_bytes": used, "available_bytes": available, "used_pct": round((used / total) * 100, 2) if total else 0},
        "swap": {"total_bytes": swap_total, "used_bytes": swap_used, "available_bytes": swap_free, "used_pct": round((swap_used / swap_total) * 100, 2) if swap_total else 0},
    }


def read_cpu_times():
    rows = {}
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            name = parts[0]
            nums = [int(x) for x in parts[1:]]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums)
            rows[name] = {"idle": idle, "total": total}
    except Exception:
        pass
    return rows


def cpu_info(cache):
    current = read_cpu_times()
    previous = cache.get("cpu_times", {}) if isinstance(cache, dict) else {}
    per_cpu = []
    total_busy = None
    for name, cur in current.items():
        prev = previous.get(name)
        busy = None
        if prev:
            total_delta = cur["total"] - prev.get("total", 0)
            idle_delta = cur["idle"] - prev.get("idle", 0)
            if total_delta > 0:
                busy = round(max(0, min(100, 100 * (1 - idle_delta / total_delta))), 2)
        if name == "cpu":
            total_busy = busy
        else:
            per_cpu.append({"name": name, "busy_pct": busy})
    return {"total_busy_pct": total_busy, "per_cpu": per_cpu, "times": current}


def host_info():
    return {
        "hostname": socket.gethostname(),
        "kernel": " ".join(os.uname()),
        "loadavg": loadavg(),
    }


def service_state(name):
    try:
        result = subprocess.run(["rc-service", name, "status"], capture_output=True, text=True, timeout=5)
        text = (result.stdout + result.stderr).lower()
        if result.returncode == 0 and ("started" in text or "running" in text):
            return "online"
        if "stopped" in text or "inactive" in text:
            return "offline"
        return "error" if result.returncode else "online"
    except Exception:
        return "unknown"


def sync_process():
    procs = []
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,etime,args"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, etime, command = parts
            if not re.search(r"(^|\s)python3\s+/usr/local/sbin/sync-openindiana-hipster\.py(\s|$)", command):
                continue
            elapsed_seconds = process_elapsed_seconds(pid)
            if elapsed_seconds is None:
                elapsed_seconds = parse_etime(etime)
            procs.append({"pid": int(pid), "elapsed_seconds": elapsed_seconds, "elapsed": format_elapsed(elapsed_seconds), "command": command})
    except Exception:
        pass
    if not procs:
        return {"running": False, "pid": None, "elapsed": None, "children": []}
    procs.sort(key=lambda item: item["elapsed_seconds"], reverse=True)
    main = procs[0]
    return {"running": True, "pid": main["pid"], "elapsed": main["elapsed"], "command": main["command"], "children": procs[1:8]}


def live_payload_progress(progress, proc):
    if not proc.get("running") or progress.get("phase") != "payloads" or not proc.get("pid"):
        return progress
    fd_root = Path("/proc") / str(proc["pid"]) / "fd"
    active = []
    try:
        for fd in fd_root.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if not target.endswith(".partial") or str(BASE) not in target:
                continue
            path = Path(target)
            try:
                stat = path.stat()
            except OSError:
                continue
            active.append({"path": str(path.relative_to(BASE)), "bytes": stat.st_size})
    except OSError:
        return progress
    if not active:
        return progress

    cache = read_json(CACHE_PATH, {})
    previous = cache.get("live_payload_progress", {})
    now_epoch = time.time()
    previous_files = previous.get("files", {}) if isinstance(previous, dict) else {}
    previous_epoch = previous.get("seen_at_epoch") if isinstance(previous, dict) else None
    active_files = {item["path"]: item["bytes"] for item in active}
    active_bytes = sum(active_files.values())
    speed = 0.0
    if previous_epoch:
        elapsed = max(now_epoch - float(previous_epoch), 0.001)
        delta = 0
        for path, size in active_files.items():
            old_size = previous_files.get(path)
            if old_size is not None:
                delta += max(0, size - int(old_size))
        speed = delta / elapsed
    cache["live_payload_progress"] = {"seen_at_epoch": now_epoch, "files": active_files}
    write_json(CACHE_PATH, cache)

    updated = dict(progress)
    base_done = float(updated.get("payload_bytes_done") or 0)
    total = float(updated.get("payload_bytes_total") or 0)
    if total > 0:
        updated["payload_bytes_done"] = min(total, base_done + active_bytes)
    else:
        updated["payload_bytes_done"] = base_done + active_bytes
    updated["payload_active_bytes"] = active_bytes
    updated["payload_active_files"] = len(active_files)
    updated["payload_active"] = active
    updated["current"] = active[0]["path"]
    if speed > 0:
        updated["upstream_speed_bps"] = round(speed, 2)
    updated["updated_at"] = now_iso()
    return updated


def tail(path, lines=40):
    try:
        return path.read_text(errors="replace").splitlines()[-lines:]
    except Exception:
        return []


def file_info(path, base):
    stat = path.stat()
    return {
        "path": str(path.relative_to(base)),
        "size_bytes": stat.st_size,
        "size_human": human_bytes(stat.st_size),
        "mtime": stat.st_mtime,
    }


def push_latest(heap, item, limit=16):
    value = (item["mtime"], item["path"], item)
    if len(heap) < limit:
        heapq.heappush(heap, value)
    elif value[0] > heap[0][0]:
        heapq.heapreplace(heap, value)


def scan_repo(repo_id, state, proc_running, progress):
    repo_root = BASE / repo_id
    result = {
        "target": f"{repo_id}/{REPOS[repo_id]['publisher']}",
        "repo": repo_id,
        "publisher": REPOS[repo_id]["publisher"],
        "path": str(repo_root),
        "source": REPOS[repo_id]["source"],
        "exists": repo_root.exists(),
        "files": 0,
        "bytes": 0,
        "bytes_human": "0 B",
        "manifests": 0,
        "payloads": 0,
        "partial_files": 0,
        "package_count": None,
        "package_version_count": None,
        "catalog_last_modified": None,
        "status": "missing",
    }
    latest_heap = []
    seen_inodes = set()
    manifest_inodes = set()
    if not repo_root.exists():
        return result, []

    attrs = read_json(repo_root / "catalog/1/catalog.attrs", {})
    result["package_count"] = attrs.get("package-count")
    result["package_version_count"] = attrs.get("package-version-count")
    result["catalog_last_modified"] = attrs.get("last-modified")

    for root, _dirs, files in os.walk(repo_root):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            try:
                stat = path.stat()
            except OSError:
                continue
            result["files"] += 1
            inode = (stat.st_dev, stat.st_ino)
            if inode not in seen_inodes:
                seen_inodes.add(inode)
                result["bytes"] += stat.st_size
            rel = path.relative_to(repo_root)
            if name.endswith(".partial"):
                result["partial_files"] += 1
            if len(rel.parts) >= 3 and rel.parts[0] == "file" and rel.parts[1] == "0" and HEX40.fullmatch(name):
                result["payloads"] += 1
            if len(rel.parts) >= 3 and rel.parts[0] == "manifest" and rel.parts[1] == "0" and not name.endswith(".partial"):
                if inode not in manifest_inodes:
                    manifest_inodes.add(inode)
                    result["manifests"] += 1
            push_latest(latest_heap, file_info(path, BASE))

    result["bytes_human"] = human_bytes(result["bytes"])
    complete_state = repo_id in state.get("repos", {}) or (repo_root / ".repo-complete.json").exists()
    if proc_running and progress.get("repo") == repo_id:
        result["status"] = "running"
    elif proc_running and result["files"] == 0:
        result["status"] = "queued"
    elif result["partial_files"]:
        result["status"] = "partial"
    elif complete_state:
        result["status"] = "ready"
    elif result["files"] > 0:
        result["status"] = "partial"
    else:
        result["status"] = "missing"
    latest = [item for _mtime, _path, item in sorted(latest_heap, reverse=True)]
    return result, latest


def clone_json(value):
    return json.loads(json.dumps(value))


def refresh_cached_scan(scan, state, proc, progress):
    scan = clone_json(scan)
    repositories = scan.get("repositories", [])
    current_repo = progress.get("repo")
    completed_repos = state.get("repos") or {}
    completed = set(completed_repos.keys())
    for row in repositories:
        repo_id = row.get("repo")
        if not repo_id:
            continue
        repo_root = BASE / repo_id
        row["exists"] = repo_root.exists()
        repo_state = completed_repos.get(repo_id) or read_json(repo_root / ".repo-complete.json", {})
        if repo_state:
            row["publisher"] = repo_state.get("publisher", row.get("publisher"))
            row["source"] = repo_state.get("source", row.get("source"))
            row["manifests"] = repo_state.get("manifest_count", row.get("manifests"))
            row["payloads"] = repo_state.get("unique_payload_count", row.get("payloads"))
            row["package_count"] = repo_state.get("package_count", row.get("package_count"))
            row["package_version_count"] = repo_state.get("package_version_count", row.get("package_version_count"))
            unique_bytes = (repo_state.get("payload_bytes") or 0) + (repo_state.get("manifest_bytes") or 0)
            if unique_bytes and unique_bytes > int(row.get("bytes") or 0):
                row["bytes"] = unique_bytes
                row["bytes_human"] = human_bytes(unique_bytes)
        if proc.get("running") and current_repo == repo_id:
            row["status"] = "running"
        elif repo_id in completed or (repo_root / ".repo-complete.json").exists():
            row["partial_files"] = 0
            row["status"] = "ready"
        elif proc.get("running") and not row.get("files"):
            row["status"] = "queued"

    total_bytes = sum(item.get("bytes", 0) for item in repositories)
    total_files = sum(item.get("files", 0) for item in repositories)
    payloads = sum(item.get("payloads", 0) for item in repositories)
    release_status = "syncing" if proc.get("running") else ("ready" if repositories and all(item.get("status") == "ready" for item in repositories) else "partial")
    scan["releases"] = [
        {
            "version": "hipster",
            "status": release_status,
            "tags": ["rolling", "current"],
            "bytes": total_bytes,
            "bytes_human": human_bytes(total_bytes),
            "files": total_files,
            "payloads": payloads,
        }
    ]
    return scan


def status_scan_cache():
    status = read_json(STATUS_PATH, {})
    mirror = status.get("mirror", {}) if isinstance(status, dict) else {}
    if not mirror.get("repositories"):
        return None
    return {
        "scanned_at": mirror.get("scanned_at", status.get("generated_at", now_iso())),
        "scanned_at_epoch": mirror.get("scanned_at_epoch", 0),
        "repositories": mirror.get("repositories", []),
        "releases": mirror.get("releases", []),
        "latest_files": mirror.get("latest_files", []),
    }


def scan_mirror(state, proc, progress):
    cache = read_json(CACHE_PATH, {})
    scan = cache.get("mirror_scan")
    if proc.get("running"):
        if scan:
            return refresh_cached_scan(scan, state, proc, progress)
        status_scan = status_scan_cache()
        if status_scan:
            return refresh_cached_scan(status_scan, state, proc, progress)
    if state.get("sync_status") == "complete":
        if scan:
            return refresh_cached_scan(scan, state, proc, progress)
        status_scan = status_scan_cache()
        if status_scan:
            return refresh_cached_scan(status_scan, state, proc, progress)
    if scan and time.time() - scan.get("scanned_at_epoch", 0) < SCAN_TTL:
        return refresh_cached_scan(scan, state, proc, progress)

    repositories = []
    latest_files = []
    for repo_id in REPOS:
        row, files = scan_repo(repo_id, state, proc.get("running"), progress)
        repositories.append(row)
        latest_files.extend(files)
    latest_files.sort(key=lambda item: item["mtime"], reverse=True)
    total_bytes = sum(item["bytes"] for item in repositories)
    total_files = sum(item["files"] for item in repositories)
    scan = {
        "scanned_at": now_iso(),
        "scanned_at_epoch": time.time(),
        "repositories": repositories,
        "releases": [
            {
                "version": "hipster",
                "status": "ready" if all(item["status"] == "ready" for item in repositories) else ("syncing" if proc.get("running") else "partial"),
                "tags": ["rolling", "current"],
                "bytes": total_bytes,
                "bytes_human": human_bytes(total_bytes),
                "files": total_files,
                "payloads": sum(item["payloads"] for item in repositories),
            }
        ],
        "latest_files": latest_files[:16],
    }
    cache["mirror_scan"] = scan
    cache["cpu_times"] = cache.get("cpu_times", {})
    write_json(CACHE_PATH, cache)
    return scan


def client_hints():
    return [
        f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/hipster openindiana.org",
        f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/hipster-encumbered hipster-encumbered",
        f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/localhostoih localhostoih",
        "pkg refresh --full",
    ]


def upstream_speed(progress, proc):
    updated_at = progress.get("updated_at")
    age = None
    if updated_at:
        try:
            age = time.time() - calendar.timegm(time.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            age = None
    bps = progress.get("upstream_speed_bps") if proc.get("running") and (age is None or age < 60) else 0
    try:
        bps = float(bps or 0)
    except Exception:
        bps = 0
    return {
        "bytes_per_second": bps,
        "human": format_rate(bps),
        "source": "sync-progress" if bps else "idle",
        "target": " ".join(str(progress.get(key, "")) for key in ("repo", "current")).strip(),
    }


def format_rate(value):
    n = float(value or 0)
    if n >= 1000 * 1000:
        return f"{n / 1000 / 1000:.2f} MB/s"
    if n >= 1000:
        return f"{n / 1000:.1f} KB/s"
    return f"{n:.0f} B/s"


def estimate(progress):
    phase = progress.get("phase")
    if phase == "payloads" and progress.get("payload_bytes_total"):
        done = float(progress.get("payload_bytes_done") or 0)
        total = float(progress.get("payload_bytes_total") or 0)
        percent = (done / total) * 100 if total else 0
        return {"overall_percent": min(99.9, 25 + percent * 0.75), "current_percent": percent, "speed": format_rate(progress.get("upstream_speed_bps", 0))}
    if phase == "manifests" and progress.get("manifests_total"):
        percent = (float(progress.get("manifests_done") or 0) / float(progress.get("manifests_total") or 1)) * 100
        return {"overall_percent": min(24.9, 5 + percent * 0.20), "current_percent": percent, "speed": format_rate(progress.get("upstream_speed_bps", 0))}
    if phase in ("control", "catalog") and progress.get("total"):
        percent = (float(progress.get("done") or 0) / float(progress.get("total") or 1)) * 100
        return {"overall_percent": min(5, percent * 0.05), "current_percent": percent, "speed": format_rate(progress.get("upstream_speed_bps", 0))}
    if phase == "complete":
        return {"overall_percent": 100, "current_percent": 100, "speed": "0 B/s"}
    return {"overall_percent": None, "current_percent": None, "speed": format_rate(progress.get("upstream_speed_bps", 0))}


def main():
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    cache = read_json(CACHE_PATH, {})
    cpu = cpu_info(cache)
    cache["cpu_times"] = cpu.pop("times", {})
    write_json(CACHE_PATH, cache)

    state = read_json(STATE_PATH, {
        "sync_status": "pending",
        "latest": "hipster",
        "previous": "not-applicable",
        "source": MIRROR_SOURCE,
        "policy": MIRROR_POLICY,
        "repos": {},
    })
    progress = read_json(PROGRESS_PATH, {"phase": "idle", "updated_at": None, "upstream_speed_bps": 0})
    proc = sync_process()
    progress = live_payload_progress(progress, proc)
    mirror_scan = scan_mirror(state, proc, progress)
    mem = meminfo()
    payload = {
        "generated_at": now_iso(),
        "host": host_info(),
        "system": {"cpu": cpu, **mem},
        "storage": storage_info(),
        "mirror": {
            "base": str(BASE),
            "source": MIRROR_SOURCE,
            "client_url": SERVER_URL,
            "client_hints": client_hints(),
            "state": {
                "latest": state.get("latest", "hipster"),
                "previous": state.get("previous", "not-applicable"),
                "last_sync": state.get("last_sync"),
                "source": MIRROR_SOURCE,
                "policy": MIRROR_POLICY,
                "rate_limit": "aggregate only: mirrors-ingress-limit 16mbit when enabled",
                "keep": ["hipster", "hipster-encumbered", "localhostoih"],
            },
            "versions": ["hipster"],
            **mirror_scan,
        },
        "sync": {
            "status": "running" if proc.get("running") else ("error" if progress.get("phase") == "error" else ("complete" if state.get("sync_status") == "complete" else "idle")),
            "process": proc,
            "progress": progress,
            "estimate": estimate(progress),
            "upstream_speed": upstream_speed(progress, proc),
            "events": tail(EVENT_LOG, 24),
            "service_log_tail": tail(STEP_LOG, 40),
        },
        "services": {
            "nginx": service_state("nginx"),
            "crond": service_state("crond"),
            "sshd": service_state("sshd"),
            "openindiana-monitor": service_state("openindiana-monitor"),
            "openindiana-sync": service_state("openindiana-sync"),
            "mirrors-sync": service_state("mirrors-sync"),
        },
        "alerts": [],
        "errors": [],
    }
    if state.get("sync_status") == "error" and not proc.get("running"):
        payload["errors"].append(state.get("last_error", "sync error"))
    write_json(STATUS_PATH, payload)


if __name__ == "__main__":
    main()
