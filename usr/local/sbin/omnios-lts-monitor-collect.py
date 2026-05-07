#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import fcntl
import calendar
import json
import os
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path

from mirrors_public import repo_url

BASE = Path("/storage/omnios")
MONITOR_DIR = BASE / ".monitor"
STATUS_PATH = MONITOR_DIR / "status.json"
CACHE_PATH = Path("/run/omnios-lts-monitor-cache.json")
LOCK_PATH = Path("/run/lock/omnios-lts-monitor-collect.lock")
STATE_PATH = BASE / ".mirror-state.json"
PROGRESS_PATH = Path("/run/omnios-lts-sync-progress.json")
EVENT_LOG = BASE / "logs/sync-events.log"
SYNC_LOG = BASE / "logs/sync-service.log"
STEP_LOG = Path("/storage/logs/mirrors-sync-omnios.log")
CLIENT_HINT_PATH = BASE / "CLIENT_URLS.txt"
REPOS = ["core", "extra"]
REPO_PUBLISHERS = {"core": "omnios", "extra": "extra.omnios"}
SFE_REPO = "localhostomnios"
SFE_SOURCE = "http://sfe.opencsw.org/localhostomnios"
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


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


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


def parse_curl_rate(value):
    if value is None:
        return None
    match = re.match(r"^\s*([0-9]+(?:[.][0-9]+)?)([kKmMgGtTpP]?)\s*$", str(value))
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    factors = {"": 1, "k": 1000, "m": 1000 ** 2, "g": 1000 ** 3, "t": 1000 ** 4, "p": 1000 ** 5}
    return number * factors.get(suffix, 1)


def parse_curl_progress_speed():
    text = read_tail(STEP_LOG, 65536).replace("\r", "\n")
    zero_speed = None
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        if line.startswith("%") or line.startswith("Dload") or line.startswith("Total "):
            continue
        parts = line.split()
        if len(parts) < 12:
            continue
        if parts[0] not in {"0", "100"} and not parts[0].isdigit():
            continue
        token = parts[-1]
        speed_bps = parse_curl_rate(token)
        if speed_bps is None:
            continue
        result = {
            "bytes_per_second": speed_bps,
            "human": format_rate(speed_bps),
            "raw": token,
            "line": line,
        }
        if speed_bps > 0:
            return result
        if zero_speed is None:
            zero_speed = result
    if zero_speed is not None:
        return zero_speed
    return None


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
        "memory": {"total_bytes": mem_total, "used_bytes": mem_used, "free_bytes": mem_available, "total_human": format_bytes(mem_total), "used_human": format_bytes(mem_used), "free_human": format_bytes(mem_available), "used_pct": round((mem_used / mem_total) * 100, 2) if mem_total else 0.0},
        "swap": {"total_bytes": swap_total, "used_bytes": swap_used, "free_bytes": swap_free, "total_human": format_bytes(swap_total), "used_human": format_bytes(swap_used), "free_human": format_bytes(swap_free), "used_pct": round((swap_used / swap_total) * 100, 2) if swap_total else 0.0},
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
    return {"total_busy_pct": cpu_percent_delta(sample_prev.get("cpu"), sample_current.get("cpu")), "per_cpu": [{"name": name, "busy_pct": cpu_percent_delta(sample_prev.get(name), sample_current.get(name))} for name in names]}


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
        is_sync_script = cmd.startswith("python3 /usr/local/sbin/sync-omnios-lts.py") or cmd.startswith("/usr/bin/python3 /usr/local/sbin/sync-omnios-lts.py")
        is_payload_curl = "curl " in cmd and ("https://pkg.omnios.org" in cmd or "sfe.opencsw.org/localhostomnios" in cmd)
        if is_sync_script or is_payload_curl:
            if "omnios-lts-monitor-collect.py" in cmd:
                continue
            if is_sync_script:
                rank = 0
            elif is_payload_curl:
                rank = 1
            else:
                rank = 2
            matches.append((rank, int(entry.name), cmd))
    if not matches:
        return {"running": False}
    matches.sort()
    _, pid, cmd = matches[0]
    return {"running": True, "pid": pid, "elapsed": process_elapsed(pid), "command": cmd, "children": [{"pid": p, "command": c} for _, p, c in matches[1:]]}


def active_download(proc):
    commands = [proc.get("command")] + [child.get("command") for child in proc.get("children", [])]
    downloads = []
    for command in [item for item in commands if item]:
        if "curl" not in command or ("https://pkg.omnios.org" not in command and "sfe.opencsw.org/localhostomnios" not in command):
            continue
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        dest = None
        for index, part in enumerate(parts[:-1]):
            if part == "-o":
                dest = Path(parts[index + 1])
                break
        urls = [part for part in parts if part.startswith("https://pkg.omnios.org/") or "sfe.opencsw.org/localhostomnios" in part]
        if dest is None:
            continue
        try:
            dest.relative_to(BASE)
        except ValueError:
            continue
        try:
            size = dest.stat().st_size
        except OSError:
            size = 0
        downloads.append({
            "path": str(dest),
            "target": str(dest.relative_to(BASE)),
            "url": urls[-1] if urls else None,
            "bytes": size,
        })
    if not downloads:
        return None
    first = downloads[0]
    return {
        "path": "|".join(item["path"] for item in downloads),
        "target": first["target"] if len(downloads) == 1 else f"{len(downloads)} active payloads",
        "url": first.get("url"),
        "bytes": sum(int(item.get("bytes") or 0) for item in downloads),
        "downloads": downloads[:12],
    }


def upstream_speed_snapshot(now, proc, previous, progress):
    if not proc.get("running"):
        return {
            "bytes_per_second": 0.0,
            "human": "0 B/s",
            "source": "idle",
            "target": None,
        }, None

    progress_speed = progress.get("payload_speed_bps")
    if progress_speed is not None:
        try:
            speed_bps = float(progress_speed)
        except Exception:
            speed_bps = None
        progress_age = None
        try:
            progress_age = now - calendar.timegm(time.strptime(progress.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            progress_age = None
        if speed_bps is not None and speed_bps > 0 and (progress_age is None or progress_age < 15):
            return {
                "bytes_per_second": round(speed_bps, 2),
                "human": format_rate(speed_bps),
                "source": "sync-progress",
                "target": progress.get("current"),
                "payload_workers": progress.get("payload_workers"),
                "payload_batch_done": progress.get("payload_batch_done"),
                "payload_batch_total": progress.get("payload_batch_total"),
                "payload_batch_bytes_done": progress.get("payload_batch_bytes_done"),
                "payload_batch_bytes_total": progress.get("payload_batch_bytes_total"),
            }, previous.get("download_sample")

    current = active_download(proc)
    if not current:
        return {
            "bytes_per_second": None,
            "human": "-",
            "source": "waiting",
            "target": None,
        }, None

    previous_sample = previous.get("download_sample") or {}
    speed_bps = None
    if previous_sample.get("path") == current["path"]:
        elapsed = now - float(previous_sample.get("at") or now)
        delta = int(current["bytes"]) - int(previous_sample.get("bytes") or 0)
        if elapsed > 0 and delta >= 0:
            speed_bps = delta / elapsed

    source = "file-delta" if speed_bps is not None else "sampling"
    raw = None
    curl_progress = parse_curl_progress_speed()
    if (speed_bps is None or speed_bps <= 0) and curl_progress:
        speed_bps = curl_progress["bytes_per_second"]
        source = "curl-progress"
        raw = curl_progress.get("raw")
    sample = {"at": now, **current}
    return {
        "bytes_per_second": round(speed_bps, 2) if speed_bps is not None else None,
        "human": format_rate(speed_bps),
        "raw": raw,
        "source": source,
        "target": current.get("target"),
        "url": current.get("url"),
        "bytes": current.get("bytes"),
        "bytes_human": format_bytes(current.get("bytes") or 0),
    }, sample


def tree_snapshot(path, limit=8):
    total = 0
    files = 0
    partial = 0
    payloads = 0
    manifests = 0
    latest = []
    seen = set()
    payload_seen = set()
    manifest_seen = set()
    if not path.exists():
        return {"exists": False, "complete": False, "completed_at": None, "bytes": 0, "bytes_human": "0.0 B", "files": 0, "partial_files": 0, "payloads": 0, "manifests": 0, "latest_files": []}
    marker = read_json(path / ".repo-complete.json", {})
    complete = bool(marker.get("completed_at"))
    for root, _, names in os.walk(path):
        for name in names:
            full = Path(root) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            inode = (stat.st_dev, stat.st_ino)
            if inode not in seen:
                seen.add(inode)
                total += stat.st_size
                files += 1
            rel = str(full.relative_to(path))
            if name.endswith(".partial"):
                partial += 1
            if rel.startswith("file/0/") and re.fullmatch(r"[0-9a-f]{40}", name) and inode not in payload_seen:
                payload_seen.add(inode)
                payloads += 1
            if rel.startswith("manifest/0/") and not name.endswith(".partial") and inode not in manifest_seen:
                manifest_seen.add(inode)
                manifests += 1
            latest.append((stat.st_mtime, str(full.relative_to(BASE)), stat.st_size))
    latest.sort(reverse=True)
    return {"exists": True, "complete": complete, "completed_at": marker.get("completed_at"), "bytes": total, "bytes_human": format_bytes(total), "files": files, "partial_files": partial, "payloads": payloads, "manifests": manifests, "latest_files": [{"path": rel, "mtime": mtime, "size_bytes": size, "size_human": format_bytes(size)} for mtime, rel, size in latest[:limit]]}


def repo_status(release, repo, info, progress, proc):
    current_release = progress.get("release")
    current_repo = progress.get("repo")
    phase = progress.get("phase")
    if info.get("partial_files"):
        return "syncing"
    if proc.get("running") and current_release == release and current_repo == repo:
        return "syncing"
    if info.get("complete") or (BASE / release / repo / ".repo-complete.json").exists():
        return "ready"
    if int(info.get("files") or 0) == 0:
        return "queued" if proc.get("running") and phase not in {"complete", "error"} else "missing"
    if info.get("exists"):
        return "partial"
    if proc.get("running") and phase not in {"complete", "error"}:
        return "queued"
    return "missing"


def discover_versions(state):
    versions = []
    for value in [state.get("latest"), state.get("previous")]:
        if value and value not in versions:
            versions.append(value)
    for item in state.get("keep", []):
        if item and item not in versions:
            versions.append(item)
    try:
        local = [entry.name for entry in BASE.iterdir() if entry.is_dir() and re.fullmatch(r"r151[0-9]{3}", entry.name)]
        local.sort(key=lambda item: int(item[1:]), reverse=True)
        for item in local:
            if item not in versions:
                versions.append(item)
    except OSError:
        pass
    return versions


def scan_mirror(state, progress, proc):
    versions = discover_versions(state)
    releases = []
    repos = []
    latest_files = []
    for version in versions:
        release_bytes = 0
        release_files = 0
        release_payloads = 0
        tags = []
        if version == state.get("latest"):
            tags.append("latest-lts")
        if version == state.get("previous"):
            tags.append("previous-lts")
        for repo in REPOS:
            info = tree_snapshot(BASE / version / repo)
            status = repo_status(version, repo, info, progress, proc)
            info.update({
                "release": version,
                "repo": repo,
                "publisher": REPO_PUBLISHERS.get(repo),
                "source": f"https://pkg.omnios.org/{version}/{repo}",
                "target": f"{version}/{repo}",
                "status": status,
            })
            repos.append(info)
            release_bytes += int(info.get("bytes", 0))
            release_files += int(info.get("files", 0))
            release_payloads += int(info.get("payloads", 0))
            latest_files.extend(info.get("latest_files", []))
        releases.append({"version": version, "tags": tags, "bytes": release_bytes, "bytes_human": format_bytes(release_bytes), "files": release_files, "payloads": release_payloads})

    sfe_info = tree_snapshot(BASE / SFE_REPO)
    sfe_status = repo_status("sfe", SFE_REPO, sfe_info, progress, proc)
    sfe_info.update({
        "release": "sfe",
        "repo": SFE_REPO,
        "publisher": SFE_REPO,
        "source": SFE_SOURCE,
        "target": SFE_REPO,
        "status": sfe_status,
        "policy": "latest entity only per package",
    })
    repos.append(sfe_info)
    latest_files.extend(sfe_info.get("latest_files", []))
    releases.append({
        "version": SFE_REPO,
        "tags": ["sfe", "third-party", "latest-only"],
        "bytes": int(sfe_info.get("bytes", 0)),
        "bytes_human": format_bytes(sfe_info.get("bytes", 0)),
        "files": int(sfe_info.get("files", 0)),
        "payloads": int(sfe_info.get("payloads", 0)),
    })
    latest_files.sort(key=lambda item: item["mtime"], reverse=True)
    return {"scanned_at": time.time(), "versions": versions, "releases": releases, "repositories": repos, "latest_files": latest_files[:20]}


def status_mirror_scan():
    status = read_json(STATUS_PATH, {})
    mirror = status.get("mirror", {}) if isinstance(status, dict) else {}
    if not mirror.get("repositories"):
        return None
    return {
        "scanned_at": mirror.get("scanned_at", 0),
        "versions": mirror.get("versions", []),
        "releases": mirror.get("releases", []),
        "repositories": mirror.get("repositories", []),
        "latest_files": mirror.get("latest_files", []),
    }


def repo_root_for_row(row):
    release = row.get("release")
    repo = row.get("repo")
    if repo == SFE_REPO or row.get("target") == SFE_REPO or release == "sfe":
        return BASE / SFE_REPO
    if release and repo:
        return BASE / str(release) / str(repo)
    target = str(row.get("target") or "").strip("/")
    if "/" in target:
        first, second = target.split("/", 1)
        return BASE / first / second
    return None


def refresh_completion_marker(row):
    root = repo_root_for_row(row)
    if root is None:
        return row
    release = row.get("release")
    repo = row.get("repo")
    if release and repo in REPO_PUBLISHERS:
        row["publisher"] = REPO_PUBLISHERS.get(repo)
        row["source"] = f"https://pkg.omnios.org/{release}/{repo}"
    marker = read_json(root / ".repo-complete.json", {})
    completed_at = marker.get("completed_at")
    if completed_at:
        row["complete"] = True
        row["completed_at"] = completed_at
    return row


def refresh_mirror_statuses(mirror_scan, progress, proc):
    mirror_scan = json.loads(json.dumps(mirror_scan))
    for repo in mirror_scan.get("repositories", []):
        refresh_completion_marker(repo)
        repo["status"] = repo_status(repo.get("release"), repo.get("repo"), repo, progress, proc)
    for release in mirror_scan.get("releases", []):
        version = release.get("version")
        rows = [
            repo for repo in mirror_scan.get("repositories", [])
            if repo.get("release") == version or (version == SFE_REPO and repo.get("repo") == SFE_REPO)
        ]
        statuses = {str(repo.get("status", "")).lower() for repo in rows}
        if "syncing" in statuses:
            release["status"] = "syncing"
        elif rows and statuses <= {"ready"}:
            release["status"] = "ready"
        elif rows and ("queued" in statuses or "partial" in statuses):
            release["status"] = "queued" if statuses == {"queued"} else "partial"
    return mirror_scan


def sfe_snapshot(progress, proc):
    sfe_info = tree_snapshot(BASE / SFE_REPO)
    sfe_status = repo_status("sfe", SFE_REPO, sfe_info, progress, proc)
    sfe_info.update({
        "release": "sfe",
        "repo": SFE_REPO,
        "publisher": SFE_REPO,
        "source": SFE_SOURCE,
        "target": SFE_REPO,
        "status": sfe_status,
        "policy": "latest entity only per package",
    })
    return sfe_info


def cached_sfe_incomplete(mirror_scan):
    for repo in mirror_scan.get("repositories", []):
        if repo.get("repo") == SFE_REPO or repo.get("target") == SFE_REPO:
            status = str(repo.get("status", "")).lower()
            if status != "ready" or not repo.get("complete") or int(repo.get("partial_files") or 0) > 0:
                return True
            return False
    return True


def refresh_sfe_snapshot(mirror_scan, progress, proc):
    active_sfe = proc.get("running") and progress.get("release") == "sfe" and progress.get("repo") == SFE_REPO
    if not active_sfe:
        return mirror_scan
    completed_sfe_changed_cache = (BASE / SFE_REPO / ".repo-complete.json").exists() and cached_sfe_incomplete(mirror_scan)
    if not (active_sfe or completed_sfe_changed_cache):
        return mirror_scan
    mirror_scan = json.loads(json.dumps(mirror_scan))
    sfe_info = sfe_snapshot(progress, proc)
    repos = mirror_scan.setdefault("repositories", [])
    replaced = False
    for index, repo in enumerate(repos):
        if repo.get("repo") == SFE_REPO or repo.get("target") == SFE_REPO:
            repos[index] = sfe_info
            replaced = True
            break
    if not replaced:
        repos.append(sfe_info)

    releases = mirror_scan.setdefault("releases", [])
    release_row = {
        "version": SFE_REPO,
        "tags": ["sfe", "third-party", "latest-only"],
        "bytes": int(sfe_info.get("bytes", 0)),
        "bytes_human": format_bytes(sfe_info.get("bytes", 0)),
        "files": int(sfe_info.get("files", 0)),
        "payloads": int(sfe_info.get("payloads", 0)),
        "status": sfe_info.get("status"),
    }
    replaced = False
    for index, release in enumerate(releases):
        if release.get("version") == SFE_REPO:
            release.update(release_row)
            replaced = True
            break
    if not replaced:
        releases.append(release_row)

    latest_files = [item for item in mirror_scan.get("latest_files", []) if not str(item.get("path", "")).startswith(f"{SFE_REPO}/")]
    latest_files.extend(sfe_info.get("latest_files", []))
    latest_files.sort(key=lambda item: item["mtime"], reverse=True)
    mirror_scan["latest_files"] = latest_files[:20]
    return mirror_scan


def load_cache(now, state, progress, proc):
    previous = read_json(CACHE_PATH, {})
    upstream_speed, download_sample = upstream_speed_snapshot(now, proc, previous, progress)
    mirror_scan = previous.get("mirror_scan")
    avoid_scan = proc.get("running") or state.get("sync_status") == "complete" or progress.get("phase") == "complete"
    if avoid_scan and mirror_scan:
        mirror_scan = refresh_mirror_statuses(mirror_scan, progress, proc)
    elif avoid_scan:
        mirror_scan = status_mirror_scan()
        if mirror_scan:
            mirror_scan = refresh_mirror_statuses(mirror_scan, progress, proc)
    if not mirror_scan or (not avoid_scan and now - float(mirror_scan.get("scanned_at", 0)) > SCAN_TTL):
        mirror_scan = scan_mirror(state, progress, proc)
    else:
        mirror_scan = refresh_mirror_statuses(mirror_scan, progress, proc)
    mirror_scan = refresh_sfe_snapshot(mirror_scan, progress, proc)
    cpu_times = read_cpu_times()
    cache = {"updated_at": now, "previous_cpu_times": previous.get("cpu_times", {}), "cpu_times": cpu_times, "mirror_scan": mirror_scan, "upstream_speed": upstream_speed, "download_sample": download_sample}
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        pass
    return cache


def last_events():
    text = read_tail(EVENT_LOG, 32768)
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        lines.append(re.sub(r"\|rate=[^|]+", "", line))
    return lines[-50:]


def client_hints(state):
    text = read_text(CLIENT_HINT_PATH).strip()
    if text:
        return text.splitlines()
    latest = state.get("latest") or "latest-lts"
    base = repo_url("omnios")
    return [
        f"pkg set-publisher -G '*' -M '*' -g {base}/{latest}/core omnios",
        f"pkg set-publisher -G '*' -M '*' -g {base}/{latest}/extra extra.omnios",
        f"pkg set-publisher -G '*' -M '*' -g {base}/localhostomnios localhostomnios",
        "pkg refresh --full",
    ]


def main():
    fd = acquire_lock()
    now = time.time()
    state = read_json(STATE_PATH, {})
    progress = read_json(PROGRESS_PATH, {"phase": "idle"})
    proc = sync_process()
    cache = load_cache(now, state, progress, proc)
    resources = mem_swap_snapshot()
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "host": {"hostname": socket.gethostname(), "kernel": " ".join(os.uname()), "loadavg": loadavg_snapshot()},
        "system": {"cpu": cpu_snapshot(cache.get("previous_cpu_times", {}), cache.get("cpu_times", {})), "memory": resources["memory"], "swap": resources["swap"]},
        "storage": statvfs(str(BASE)),
        "mirror": {"base": str(BASE), "source": f"https://pkg.omnios.org + {SFE_SOURCE}", "state": state, "client_hints": client_hints(state), **cache["mirror_scan"]},
        "sync": {"status": "running" if proc.get("running") else ("error" if progress.get("phase") == "error" else ("complete" if progress.get("phase") == "complete" or state.get("sync_status") == "complete" else "idle")), "process": proc, "progress": progress, "upstream_speed": cache.get("upstream_speed"), "events": last_events(), "service_log_tail": read_tail(SYNC_LOG, 32768).splitlines()[-40:]},
        "services": {"nginx": service_state("nginx"), "crond": service_state("crond"), "sshd": service_state("sshd"), "omnios-lts-monitor": service_state("omnios-lts-monitor"), "omnios-lts-sync": service_state("omnios-lts-sync")},
    }
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATUS_PATH)
    os.close(fd)


if __name__ == "__main__":
    main()
