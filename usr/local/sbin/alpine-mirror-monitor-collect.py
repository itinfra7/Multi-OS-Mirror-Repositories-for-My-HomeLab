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

BASE = Path("/alpine-repo")
MONITOR_DIR = BASE / ".monitor"
STATUS_PATH = MONITOR_DIR / "status.json"
CACHE_PATH = Path("/run/alpine-mirror-monitor-cache.json")
LOCK_PATH = Path("/run/lock/alpine-mirror-monitor-collect.lock")
SYNC_LOG = Path("/var/log/alpine-mirror-sync.log")
STATE_PATH = BASE / ".mirror-state"
PROGRESS_PATH = Path("/tmp/.mirror-progress")
RSYNC_PROGRESS_PATH = Path("/tmp/.mirror-rsync-out")
FILES_DONE_PATH = Path("/tmp/.mirror-files-done")
CLIENT_HINT_PATH = BASE / "CLIENT_URLS.txt"
SOURCE = "rsync://rsync.alpinelinux.org/alpine"
ARCH = "x86_64"
REPO_SCAN_TTL = 60
TAIL_LIMIT = 128 * 1024


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


def read_tail(path, limit=TAIL_LIMIT):
    if not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(size - limit, 0))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def read_text(path):
    try:
        return path.read_text()
    except Exception:
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


def format_duration(seconds):
    try:
        total = int(seconds)
    except Exception:
        total = 0
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def statvfs(path):
    info = os.statvfs(path)
    block_size = info.f_frsize
    total = block_size * info.f_blocks
    used = max(block_size * (info.f_blocks - info.f_bfree), 0)
    available = block_size * info.f_bavail
    reserved = max(block_size * (info.f_bfree - info.f_bavail), 0)
    pct = round((used / total) * 100, 2) if total else 0.0
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
        "used_pct": pct,
    }


def loadavg_snapshot():
    try:
        l1, l5, l15 = os.getloadavg()
    except OSError:
        l1, l5, l15 = 0.0, 0.0, 0.0
    return {
        "load1": round(l1, 2),
        "load5": round(l5, 2),
        "load15": round(l15, 2),
        "human": f"{l1:.2f} / {l5:.2f} / {l15:.2f}",
    }


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
        name = parts[0]
        values = [int(value) for value in parts[1:] if value.isdigit()]
        if len(values) < 4:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        cpu_times[name] = {"idle": idle, "total": total}
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

    overall = cpu_percent_delta(sample_prev.get("cpu"), sample_current.get("cpu"))
    per_cpu = []
    names = [key for key in sample_current.keys() if key != "cpu"]
    names.sort(key=lambda item: int(item[3:]) if item[3:].isdigit() else item)
    for name in names:
        per_cpu.append({"name": name, "busy_pct": cpu_percent_delta(sample_prev.get(name), sample_current.get(name))})
    return {"total_busy_pct": overall, "per_cpu": per_cpu}


def service_state(name):
    rc, _ = run(["rc-service", name, "status"], timeout=4)
    return "online" if rc == 0 else "offline"


def process_cmdline(pid):
    raw = read_text(Path("/proc") / str(pid) / "cmdline")
    if not raw:
        return ""
    return raw.replace("\x00", " ").strip()


def process_elapsed(pid):
    proc_path = Path("/proc") / str(pid)
    stat = read_text(Path("/proc") / str(pid) / "stat")
    try:
        # Field 22 is starttime. The comm field may contain spaces inside
        # parentheses, so parse after the last closing parenthesis.
        after_comm = stat.rsplit(") ", 1)[1]
        fields = after_comm.split()
        start_ticks = int(fields[19])
        ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        uptime = float(read_text(Path("/proc/uptime")).split()[0])
        elapsed = int(uptime - (start_ticks / ticks_per_second))
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
        cmdline = process_cmdline(entry.name)
        if not cmdline:
            continue
        if "alpine-mirror-sync.sh" in cmdline or SOURCE in cmdline:
            if "alpine-mirror-monitor-collect.py" in cmdline:
                continue
            matches.append((int(entry.name), cmdline))
    if not matches:
        return {"running": False}
    matches.sort()
    pid, cmdline = matches[0]
    return {
        "running": True,
        "pid": pid,
        "elapsed": process_elapsed(pid),
        "command": cmdline,
        "children": [{"pid": p, "command": c} for p, c in matches[1:]],
    }


def parse_state_file():
    data = {}
    for line in read_text(STATE_PATH).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip().lower()] = value.strip()
    latest_link = None
    latest_link_exists = False
    try:
        latest_link = os.readlink(str(BASE / "latest-stable"))
        latest_link_exists = True
    except OSError:
        pass
    return {
        "latest": data.get("latest"),
        "previous": data.get("prev"),
        "last_sync": data.get("last_sync"),
        "latest_link": latest_link,
        "latest_link_exists": latest_link_exists,
    }


def parse_log():
    text = read_tail(SYNC_LOG, 256 * 1024)
    lines = [line for line in text.splitlines() if line.strip()]
    start_index = 0
    for idx, line in enumerate(lines):
        if "Alpine Mirror Sync START" in line:
            start_index = idx
    current_run = lines[start_index:]

    latest = None
    previous = None
    events = lines[-80:]
    results = {}
    failures = []
    completed = False
    ok_pattern = re.compile(r"\[(OK|FAIL)\]\s+([^\s]+)\s+\(([^)]*)\)")
    for line in current_run:
        match = re.search(r"Latest stable\s*:\s*(\S+)", line)
        if match:
            latest = match.group(1)
        match = re.search(r"Previous stable:\s*(\S+)", line)
        if match:
            previous = match.group(1)
        match = ok_pattern.search(line)
        if match:
            state = match.group(1).lower()
            target = match.group(2)
            detail = match.group(3)
            results[target] = {"state": state, "detail": detail, "line": line}
            if state == "fail":
                failures.append({"target": target, "detail": detail, "line": line})
        if "Alpine Mirror Sync COMPLETE" in line:
            completed = True
    return {
        "current_run_latest": latest,
        "current_run_previous": previous,
        "current_run_complete": completed,
        "events": events,
        "recent_results": results,
        "recent_failures": failures[-12:],
    }


def parse_progress_file():
    text = read_text(PROGRESS_PATH).strip()
    if not text:
        return {"job_done": 0, "job_total": 0, "current_job": None}
    parts = text.split(None, 2)
    try:
        done = int(parts[0])
    except Exception:
        done = 0
    try:
        total = int(parts[1])
    except Exception:
        total = 0
    current = parts[2] if len(parts) > 2 else None
    return {"job_done": done, "job_total": total, "current_job": current, "raw": text}


def parse_rsync_progress():
    text = read_tail(RSYNC_PROGRESS_PATH, 256 * 1024).replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = {"raw_tail": lines[-30:]}
    pattern = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+([^\s]+/s)\s+([0-9:]+)(?:\s+\(xfr#(\d+),\s*to-chk=(\d+)/(\d+)\))?")
    progress_line = None
    match = None
    for line in reversed(lines):
        match = pattern.search(line)
        if match:
            progress_line = line
            break
    if not match:
        return result
    result.update(
        {
            "line": progress_line,
            "transferred_bytes": int(match.group(1).replace(",", "")),
            "percent": int(match.group(2)),
            "speed": match.group(3),
            "eta": match.group(4),
        }
    )
    if match.group(5):
        result.update(
            {
                "xfr": int(match.group(5)),
                "to_chk": int(match.group(6)),
                "to_chk_total": int(match.group(7)),
            }
        )
    return result


def parse_files_done(rsync_progress):
    done = 0
    total = 0
    text = read_text(FILES_DONE_PATH).strip()
    if text:
        parts = text.split()
        try:
            done = int(parts[0])
        except Exception:
            done = 0
        try:
            total = int(parts[1])
        except Exception:
            total = done
    current_done = None
    if rsync_progress.get("to_chk") is not None and rsync_progress.get("to_chk_total") is not None:
        current_done = max(rsync_progress["to_chk_total"] - rsync_progress["to_chk"], 0)
    return {
        "completed_before_current": done,
        "known_total": total,
        "current_done": current_done,
        "overall_done_estimate": done + (current_done or 0),
    }


def repo_plan(version, state, current_job, process_running, results, snapshot):
    if version == "edge":
        repos = ["main", "community", "releases", "testing"]
    else:
        repos = ["main", "community", "releases"]
    rows = []
    for repo in repos:
        target = f"{version}/{repo}/{ARCH}"
        path = BASE / version / repo / ARCH
        info = snapshot.get("repos", {}).get(target, empty_repo_info(target, path))
        result = results.get(target)
        if process_running and (current_job == target or info.get("has_temp")):
            status = "syncing"
        elif result and result.get("state") == "fail":
            status = "error"
        elif info.get("files", 0) > 0:
            status = "ready"
        elif process_running:
            status = "queued"
        else:
            status = "missing"
        rows.append(
            {
                "target": target,
                "version": version,
                "repo": repo,
                "arch": ARCH,
                "status": status,
                "last_result": result,
                **info,
            }
        )
    return rows


def empty_repo_info(target, path):
    return {
        "target": target,
        "path": str(path),
        "exists": path.exists(),
        "bytes": 0,
        "bytes_human": "0.0 B",
        "files": 0,
        "apk_packages": 0,
        "temp_files": 0,
        "has_temp": False,
        "latest_mtime": None,
        "latest_mtime_iso": None,
        "apkindex": None,
    }


def scan_repo(target, path):
    total_bytes = 0
    files = 0
    apk_packages = 0
    temp_files = 0
    latest_mtime = None
    latest_files = []
    apkindex = None

    if not path.exists():
        return empty_repo_info(target, path)

    for root, _, names in os.walk(path):
        for name in names:
            full = Path(root) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            files += 1
            total_bytes += stat.st_size
            rel_path = str(full.relative_to(path))
            is_temp = (
                rel_path.startswith(".~tmp~/")
                or "/.~tmp~/" in rel_path
                or name.startswith(".")
                or name.endswith(".partial")
            )
            if is_temp:
                temp_files += 1
            if name.endswith(".apk"):
                apk_packages += 1
            mtime = stat.st_mtime
            if latest_mtime is None or mtime > latest_mtime:
                latest_mtime = mtime
            rel = str(full.relative_to(BASE))
            latest_files.append((mtime, rel, stat.st_size))
            if name == "APKINDEX.tar.gz":
                apkindex = {
                    "path": rel,
                    "size_bytes": stat.st_size,
                    "size_human": format_bytes(stat.st_size),
                    "mtime": mtime,
                    "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
                }

    latest_files.sort(reverse=True)
    return {
        "target": target,
        "path": str(path),
        "exists": True,
        "bytes": total_bytes,
        "bytes_human": format_bytes(total_bytes),
        "files": files,
        "apk_packages": apk_packages,
        "temp_files": temp_files,
        "has_temp": temp_files > 0,
        "latest_mtime": latest_mtime,
        "latest_mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(latest_mtime)) if latest_mtime else None,
        "apkindex": apkindex,
        "latest_files": [
            {"path": rel, "size_bytes": size, "size_human": format_bytes(size), "mtime": mtime}
            for mtime, rel, size in latest_files[:8]
        ],
    }


def discover_versions(state, log_info):
    versions = []
    for name in ["edge"]:
        if (BASE / name).exists():
            versions.append(name)

    preferred = [
        log_info.get("current_run_latest"),
        log_info.get("current_run_previous"),
        state.get("latest_link"),
        state.get("latest"),
        state.get("previous"),
    ]
    for name in preferred:
        if name and name != "none" and name not in versions:
            versions.append(name)

    local_versions = []
    try:
        for entry in BASE.iterdir():
            if entry.is_dir() and re.match(r"^v[0-9]+[.][0-9]+$", entry.name):
                local_versions.append(entry.name)
    except OSError:
        pass
    local_versions.sort(key=lambda item: [int(part) for part in item[1:].split(".")])
    for name in local_versions:
        if name not in versions:
            versions.append(name)
    return versions


def scan_mirror(state, log_info):
    versions = discover_versions(state, log_info)
    repos = {}
    branch_rows = []
    latest_files = []

    for version in versions:
        version_bytes = 0
        version_files = 0
        version_packages = 0
        repo_names = ["main", "community", "releases", "testing"] if version == "edge" else ["main", "community", "releases"]
        for repo in repo_names:
            target = f"{version}/{repo}/{ARCH}"
            info = scan_repo(target, BASE / version / repo / ARCH)
            repos[target] = info
            version_bytes += int(info.get("bytes", 0))
            version_files += int(info.get("files", 0))
            version_packages += int(info.get("apk_packages", 0))
            for item in info.get("latest_files", []):
                latest_files.append((item["mtime"], item["path"], item["size_bytes"], item["size_human"]))

        tags = []
        if version == "edge":
            tags.append("edge")
        if version == log_info.get("current_run_latest"):
            tags.append("current-run-latest")
        if version == log_info.get("current_run_previous"):
            tags.append("current-run-previous")
        if version == state.get("latest_link"):
            tags.append("latest-stable-link")
        if version == state.get("latest"):
            tags.append("state-latest")
        if version == state.get("previous"):
            tags.append("state-previous")

        branch_rows.append(
            {
                "name": version,
                "tags": tags,
                "bytes": version_bytes,
                "bytes_human": format_bytes(version_bytes),
                "files": version_files,
                "apk_packages": version_packages,
                "path": str(BASE / version),
            }
        )

    latest_files.sort(reverse=True)
    return {
        "scanned_at": time.time(),
        "versions": versions,
        "repos": repos,
        "branches": branch_rows,
        "latest_files": [
            {"path": rel, "size_bytes": size, "size_human": size_human, "mtime": mtime}
            for mtime, rel, size, size_human in latest_files[:20]
        ],
    }


def load_cache(now, state, log_info):
    previous = {}
    if CACHE_PATH.exists():
        try:
            previous = json.loads(CACHE_PATH.read_text())
        except Exception:
            previous = {}

    repo_scan = previous.get("repo_scan")
    if not repo_scan or now - float(repo_scan.get("scanned_at", 0)) > REPO_SCAN_TTL:
        repo_scan = scan_mirror(state, log_info)

    current_cpu_times = read_cpu_times()
    cache = {
        "updated_at": now,
        "previous_cpu_times": previous.get("cpu_times", {}),
        "cpu_times": current_cpu_times,
        "repo_scan": repo_scan,
    }
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        pass
    return cache


def sync_estimate(progress, rsync_progress, process):
    total = int(progress.get("job_total") or 0)
    done = int(progress.get("job_done") or 0)
    job_percent = float(rsync_progress.get("percent") or 0)
    if not process.get("running") and total > 0 and done >= total:
        job_percent = 100.0
    if total <= 0:
        overall = 100.0 if not process.get("running") else 0.0
    else:
        overall = ((done * 100.0) + job_percent) / total
        overall = min(max(overall, 0.0), 100.0)
    eta = rsync_progress.get("eta")
    elapsed_text = process.get("elapsed")
    if process.get("running") and overall > 0 and elapsed_text:
        try:
            hours, minutes, seconds = [int(part) for part in elapsed_text.split(":")]
            elapsed_seconds = hours * 3600 + minutes * 60 + seconds
            remaining = elapsed_seconds * (100.0 - overall) / overall
            eta = format_duration(remaining)
        except Exception:
            pass
    return {
        "overall_percent": round(overall, 2),
        "job_percent": job_percent,
        "eta": eta,
        "speed": None if not process.get("running") else rsync_progress.get("speed"),
        "transferred_bytes": rsync_progress.get("transferred_bytes"),
        "transferred_human": format_bytes(rsync_progress.get("transferred_bytes") or 0),
    }


def http_snapshot():
    active = 0
    for proc_file in [Path("/proc/net/tcp"), Path("/proc/net/tcp6")]:
        for line in read_text(proc_file).splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[1].endswith(":0050") and parts[3] == "01":
                active += 1
    access_log = Path("/var/log/nginx/access.log")
    access_count = None
    if access_log.exists():
        rc, out = run(["wc", "-l", str(access_log)], timeout=5)
        if rc == 0 and out:
            try:
                access_count = int(out.split()[0])
            except Exception:
                access_count = None
    return {"active_connections": active, "access_count": access_count}


def client_hint():
    text = read_text(CLIENT_HINT_PATH).strip()
    if text:
        return text.splitlines()
    base = repo_url("alpine")
    return [
        f"{base}/latest-stable/main",
        f"{base}/latest-stable/community",
        f"{base}/edge/main",
        f"{base}/edge/community",
        f"{base}/edge/testing",
    ]


def main():
    lock_fd = acquire_lock()
    now = time.time()
    state = parse_state_file()
    log_info = parse_log()
    cache = load_cache(now, state, log_info)
    process = sync_process()
    progress = parse_progress_file()
    rsync_progress = parse_rsync_progress()
    files = parse_files_done(rsync_progress)
    repo_scan = cache["repo_scan"]
    current_job = progress.get("current_job")
    process_running = bool(process.get("running"))
    progress_for_payload = dict(progress)
    if not process_running and int(progress.get("job_total") or 0) > 0 and int(progress.get("job_done") or 0) >= int(progress.get("job_total") or 0):
        progress_for_payload["current_job"] = "complete"
    repository_rows = []
    for version in repo_scan.get("versions", []):
        repository_rows.extend(repo_plan(version, state, current_job, process_running, log_info.get("recent_results", {}), repo_scan))

    resources = mem_swap_snapshot()
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "host": {
            "hostname": socket.gethostname(),
            "kernel": " ".join(os.uname()),
            "uptime_seconds": float(read_text(Path("/proc/uptime")).split()[0]) if read_text(Path("/proc/uptime")) else 0,
            "loadavg": loadavg_snapshot(),
            "http": http_snapshot(),
        },
        "system": {
            "cpu": cpu_snapshot(cache.get("previous_cpu_times", {}), cache.get("cpu_times", {})),
            "memory": resources["memory"],
            "swap": resources["swap"],
        },
        "storage": statvfs(str(BASE)),
        "mirror": {
            "base": str(BASE),
            "source": SOURCE,
            "arch": ARCH,
            "last_updated": read_text(BASE / "last-updated").strip(),
            "client_hints": client_hint(),
            "state": state,
            "log": {
                "current_run_latest": log_info.get("current_run_latest"),
                "current_run_previous": log_info.get("current_run_previous"),
                "recent_failures": log_info.get("recent_failures", []),
            },
            "branches": repo_scan.get("branches", []),
            "repositories": repository_rows,
            "latest_files": repo_scan.get("latest_files", []),
            "repo_scan_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(repo_scan.get("scanned_at", now))),
        },
        "sync": {
            "status": "running" if process_running else ("complete" if log_info.get("current_run_complete") else "idle"),
            "source": SOURCE,
            "process": process,
            "progress": progress_for_payload,
            "rsync": rsync_progress,
            "files": files,
            "estimate": sync_estimate(progress, rsync_progress, process),
            "upstream_speed": upstream_speed_snapshot(process, rsync_progress.get("speed"), current_job),
            "events": log_info.get("events", [])[-40:],
            "rsync_log_tail": rsync_progress.get("raw_tail", []),
        },
        "services": {
            "nginx": service_state("nginx"),
            "crond": service_state("crond"),
            "sshd": service_state("sshd"),
            "alpine-mirror-monitor": service_state("alpine-mirror-monitor"),
        },
    }

    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATUS_PATH)
    os.close(lock_fd)


if __name__ == "__main__":
    main()
