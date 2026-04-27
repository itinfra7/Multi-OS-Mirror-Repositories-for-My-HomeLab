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

BASE = Path("/storage/opencsw")
MONITOR_DIR = BASE / ".monitor"
STATUS_PATH = MONITOR_DIR / "status.json"
CACHE_PATH = Path("/run/opencsw-monitor-cache.json")
RSYNC_LOG = BASE / "logs" / "rsync-current.log"
EVENT_LOG = BASE / "logs" / "sync-events.log"
TOTAL_PATH = MONITOR_DIR / "remote-total.json"
CLIENT_URL_PATH = BASE / "CLIENT_URL.txt"
APPROX_TOTAL = 30 * 1024 * 1024 * 1024
CHANNEL_NAMES = ["allpkgs", "stable", "testing", "unstable", "releases", "legacy", "bratislava", "dublin", "kiel", "munich"]
LOCK_PATH = Path("/run/lock/opencsw-monitor-collect.lock")
SCAN_TTL = 300


def sh(command: str) -> str:
    return subprocess.run(command, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()


def acquire_lock() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(0)
    return fd


def service_state(name: str) -> str:
    rc = subprocess.run(["rc-service", name, "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "online" if rc.returncode == 0 else "offline"


def read_tail(path: Path, limit: int = 32768) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(size - limit, 0))
        return handle.read().decode("utf-8", "replace")


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    number = float(value)
    for unit in units:
        if number < 1024.0 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{value} B"


def format_duration(elapsed) -> str:
    elapsed = max(int(elapsed), 0)
    hours, rem = divmod(elapsed, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except Exception:
        return ""


def process_elapsed(pid: int) -> str | None:
    proc_path = Path("/proc") / str(pid)
    stat = read_text(proc_path / "stat")
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


def parse_rate(value) -> float | None:
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


def format_rate(value) -> str:
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


def upstream_speed_snapshot(sync_proc: dict, progress: dict, estimate_data: dict) -> dict:
    running = bool(sync_proc.get("running"))
    speed_bps = parse_rate(progress.get("speed")) if running else 0.0
    source = "rsync-progress" if running else "idle"
    if speed_bps is None and running and estimate_data.get("speed_bps") is not None:
        try:
            speed_bps = float(estimate_data.get("speed_bps"))
            source = "file-delta"
        except Exception:
            speed_bps = None
    if speed_bps is None:
        source = "waiting" if running else "idle"
    return {
        "bytes_per_second": round(speed_bps, 2) if speed_bps is not None else None,
        "human": format_rate(speed_bps),
        "raw": progress.get("speed"),
        "source": source,
        "target": progress.get("current_item"),
    }


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def statvfs(path: str) -> dict:
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


def loadavg_snapshot() -> dict:
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


def mem_swap_snapshot() -> dict:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024

    mem_total = values.get("MemTotal", 0)
    mem_available = values.get("MemAvailable", 0)
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


def read_cpu_times() -> dict:
    cpu_times = {}
    for line in Path("/proc/stat").read_text().splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        name = parts[0]
        values = [int(value) for value in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        cpu_times[name] = {"idle": idle, "total": total}
    return cpu_times


def cpu_percent_delta(previous: dict | None, current: dict) -> float | None:
    if not previous:
        return None
    total_delta = current["total"] - previous["total"]
    idle_delta = current["idle"] - previous["idle"]
    if total_delta <= 0:
        return None
    return round(((total_delta - idle_delta) / total_delta) * 100, 2)


def cpu_snapshot(previous: dict, current: dict) -> dict:
    sample_prev = previous or {}
    sample_current = current
    if not sample_prev:
        sample_prev = read_cpu_times()
        time.sleep(0.2)
        sample_current = read_cpu_times()

    overall = cpu_percent_delta(sample_prev.get("cpu"), sample_current.get("cpu", {"idle": 0, "total": 0}))
    per_cpu = []
    for name in sorted([key for key in sample_current.keys() if key != "cpu"], key=lambda item: int(item[3:]) if item[3:].isdigit() else item):
        per_cpu.append({"name": name, "busy_pct": cpu_percent_delta(sample_prev.get(name), sample_current[name])})
    return {"total_busy_pct": overall, "per_cpu": per_cpu}


def tree_snapshot(path: Path, limit: int = 12) -> dict:
    total_bytes = 0
    package_count = 0
    tmp_files = []
    latest = []
    channels = {}
    root_entries = {}

    if not path.exists():
        return {"bytes": 0, "packages": 0, "tmp_files": [], "latest_files": [], "channels": {}, "root_entries": {}}

    for root, _, files in os.walk(path):
        for name in files:
            full = Path(root) / name
            try:
                stat = full.stat()
            except FileNotFoundError:
                continue
            size = stat.st_size
            mtime = stat.st_mtime
            rel = str(full.relative_to(path))
            total_bytes += size
            top = rel.split("/", 1)[0]
            bucket = channels.setdefault(top, {"bytes": 0, "packages": 0, "files": 0})
            bucket["bytes"] += size
            bucket["files"] += 1
            if name.endswith(".pkg") or name.endswith(".pkg.gz"):
                package_count += 1
                bucket["packages"] += 1
            is_temp_like = (
                ".tmp" in name
                or rel.startswith(".~tmp~/")
                or "/.~tmp~/" in rel
                or (name.startswith(".") and ".pkg" in name)
                or name.endswith(".partial")
            )
            if is_temp_like:
                tmp_files.append((mtime, rel, size))
            latest.append((mtime, rel, size))

    for entry in os.scandir(path):
        try:
            stat = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        target = None
        target_name = None
        if entry.is_symlink():
            kind = "symlink"
            try:
                target = os.readlink(entry.path)
                target_name = os.path.basename(target.rstrip("/"))
            except OSError:
                target = None
                target_name = None
        elif entry.is_dir(follow_symlinks=False):
            kind = "dir"
        else:
            kind = "file"
        info = {"kind": kind, "mtime": stat.st_mtime}
        if target is not None:
            info["target"] = target
        if target_name is not None:
            info["target_name"] = target_name
        root_entries[entry.name] = info

    latest.sort(reverse=True)
    tmp_files.sort(reverse=True)
    return {
        "bytes": total_bytes,
        "packages": package_count,
        "channels": channels,
        "root_entries": root_entries,
        "tmp_files": [
            {"path": rel, "size_bytes": size, "size_human": format_bytes(size), "mtime": mtime}
            for mtime, rel, size in tmp_files[:limit]
        ],
        "latest_files": [
            {"path": rel, "size_bytes": size, "size_human": format_bytes(size), "mtime": mtime}
            for mtime, rel, size in latest[:limit]
        ],
    }


def releases_snapshot(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    for entry in sorted(path.iterdir(), reverse=True):
        if entry.is_dir():
            entries.append({"name": entry.name, "mtime": entry.stat().st_mtime})
    return entries[:12]


def load_cache(now: float) -> dict:
    previous = {}
    if CACHE_PATH.exists():
        try:
            previous = json.loads(CACHE_PATH.read_text())
            if now - previous.get("scanned_at", 0) < SCAN_TTL:
                return previous
        except Exception:
            previous = {}

    incoming = tree_snapshot(BASE / "incoming")
    current = tree_snapshot(BASE / "current")
    releases = tree_snapshot(BASE / "releases")
    current_cpu_times = read_cpu_times()
    data = {
        "scanned_at": now,
        "previous_scanned_at": previous.get("scanned_at"),
        "previous_incoming_bytes": previous.get("incoming", {}).get("bytes", 0),
        "previous_cpu_times": previous.get("cpu_times", {}),
        "cpu_times": current_cpu_times,
        "incoming": incoming,
        "current": current,
        "releases": releases,
        "release_dirs": releases_snapshot(BASE / "releases"),
    }
    CACHE_PATH.write_text(json.dumps(data))
    return data


def parse_progress() -> dict:
    log_text = read_tail(RSYNC_LOG)
    if not log_text:
        return {}
    lines = [line.strip() for line in log_text.replace("\r", "\n").splitlines() if line.strip()]
    progress_line = None
    current_item = None
    for line in reversed(lines):
        if "to-chk=" in line and "xfr#" in line:
            progress_line = line
            break

    for line in reversed(lines):
        if line.startswith(("Number of ", "Total ", "Literal data:", "Matched data:", "File list ", "sent ", "total size is ", "probe-start ")):
            continue
        if re.match(r"^[0-9,]+\s+\d+%\s+\S+/s\s+[0-9:]+", line):
            continue
        current_item = line
        break

    result = {"raw_tail": lines[-20:]}
    if current_item:
        result["current_item"] = current_item
    if progress_line is None:
        for line in reversed(lines):
            if re.match(r"^[0-9,]+\s+\d+%\s+\S+/s\s+[0-9:]+", line):
                progress_line = line
                break
        if progress_line is None:
            return result

    pattern = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+([^\s]+/s)\s+([0-9:]+)\s+\(xfr#(\d+),\s*to-chk=(\d+)/(\d+)\)")
    match = pattern.search(progress_line)
    if match:
        transferred = int(match.group(1).replace(",", ""))
        result.update(
            {
                "line": progress_line,
                "transferred_bytes": transferred,
                "percent": int(match.group(2)),
                "speed": match.group(3),
                "eta": match.group(4),
                "xfr": int(match.group(5)),
                "to_chk": int(match.group(6)),
                "to_chk_total": int(match.group(7)),
            }
        )
    else:
        fallback = re.search(r"^\s*([\d,]+)\s+(\d+)%\s+([^\s]+/s)\s+([0-9:]+)$", progress_line)
        if fallback:
            result.update(
                {
                    "line": progress_line,
                    "transferred_bytes": int(fallback.group(1).replace(",", "")),
                    "percent": int(fallback.group(2)),
                    "speed": fallback.group(3),
                    "eta": fallback.group(4),
                }
            )
        else:
            result["line"] = progress_line
    return result


def parse_sync_process() -> dict:
    out = sh("ps -eo pid,etime,args | grep -E 'rsync://mirror.opencsw.org/opencsw-full/|/usr/local/sbin/sync-opencsw-full.sh' | grep -v grep | head -1")
    if not out:
        return {"running": False}
    parts = out.split(None, 2)
    if len(parts) < 3:
        return {"running": True, "raw": out}
    pid = int(parts[0])
    return {"running": True, "pid": pid, "elapsed": process_elapsed(pid) or parts[1], "command": parts[2]}


def load_total() -> dict:
    if TOTAL_PATH.exists():
        try:
            total = json.loads(TOTAL_PATH.read_text())
            total["approximate"] = False
            return total
        except Exception:
            pass
    return {
        "status": "approximate",
        "updated_at": None,
        "source": "official-open-csw-full-approximation",
        "total_bytes": APPROX_TOTAL,
        "file_count": None,
        "approximate": True,
    }


def maybe_eta(progress: dict, total: dict, cache: dict, now: float, events: list, sync_running: bool, current_exists: bool) -> dict:
    total_bytes = int(total.get("total_bytes") or 0)
    if not sync_running:
        if current_exists:
            transferred = total_bytes
            percent = 100.0 if total_bytes else 0.0
        else:
            transferred = int(cache.get("incoming", {}).get("bytes", 0) or 0)
            percent = round((transferred / total_bytes) * 100, 2) if total_bytes and transferred else 0.0
        return {
            "total_bytes": total_bytes,
            "total_human": format_bytes(total_bytes),
            "transferred_bytes": transferred,
            "transferred_human": format_bytes(transferred),
            "percent": percent,
            "eta": None,
            "speed_bps": None,
            "speed_human": None,
            "approximate_total": total.get("approximate", False),
        }

    transferred = progress.get("transferred_bytes")
    if transferred is None:
        transferred = 0
    percent = progress.get("percent")
    if percent is None and total_bytes > 0:
        percent = round((transferred / total_bytes) * 100, 2)
    else:
        percent = float(percent or 0)

    eta = progress.get("eta")
    speed_bps = None
    previous_scanned_at = cache.get("previous_scanned_at")
    previous_incoming_bytes = int(cache.get("previous_incoming_bytes") or 0)
    if previous_scanned_at and transferred >= previous_incoming_bytes:
        elapsed = now - float(previous_scanned_at)
        delta = transferred - previous_incoming_bytes
        if elapsed > 0 and delta > 0:
            speed_bps = delta / elapsed
            if total_bytes > transferred:
                eta = format_duration((total_bytes - transferred) / speed_bps)
    if speed_bps is None:
        started_at = last_start_epoch(events)
        if started_at and now > started_at and transferred > 0:
            speed_bps = transferred / (now - started_at)
            if total_bytes > transferred:
                eta = format_duration((total_bytes - transferred) / speed_bps)
    return {
        "total_bytes": total_bytes,
        "total_human": format_bytes(total_bytes),
        "transferred_bytes": transferred,
        "transferred_human": format_bytes(transferred),
        "percent": percent,
        "eta": eta,
        "speed_bps": speed_bps,
        "speed_human": format_bytes(speed_bps) + "/s" if speed_bps else None,
        "approximate_total": total.get("approximate", False),
    }


def last_events() -> list:
    text = read_tail(EVENT_LOG, 16384)
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip()][-20:]


def last_start_epoch(events: list) -> float | None:
    for line in reversed(events):
        if "|start|" not in line:
            continue
        stamp = line.split("|", 1)[0]
        try:
            return time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            return None
    return None


def client_url() -> str:
    if CLIENT_URL_PATH.exists():
        return CLIENT_URL_PATH.read_text().strip()
    return ""


def channel_snapshot(cache: dict, progress: dict) -> list:
    current_item = progress.get("current_item") or ""
    items = []
    incoming_entries = cache["incoming"].get("root_entries", {})
    current_entries = cache["current"].get("root_entries", {})
    incoming_channels = cache["incoming"].get("channels", {})
    current_channels = cache["current"].get("channels", {})

    def effective_bucket(entry: dict | None, bucket: dict, buckets: dict) -> tuple[dict, str | None]:
        if entry and entry.get("kind") == "symlink":
            target_name = entry.get("target_name")
            if target_name and target_name in buckets:
                return buckets.get(target_name, bucket), target_name
            return bucket, target_name
        return bucket, None

    for name in CHANNEL_NAMES:
        incoming_entry = incoming_entries.get(name)
        current_entry = current_entries.get(name)
        incoming_bucket = incoming_channels.get(name, {"bytes": 0, "packages": 0, "files": 0})
        current_bucket = current_channels.get(name, {"bytes": 0, "packages": 0, "files": 0})
        incoming_bucket, incoming_target = effective_bucket(incoming_entry, incoming_bucket, incoming_channels)
        current_bucket, current_target = effective_bucket(current_entry, current_bucket, current_channels)
        if current_entry:
            status = "ready"
        elif current_item.startswith(f"{name}/") or current_item == name:
            status = "syncing"
        elif incoming_entry or incoming_bucket.get("files", 0) > 0:
            status = "staged"
        else:
            status = "pending"

        alias_target = current_target or incoming_target
        kind = (current_entry or incoming_entry or {}).get("kind", "missing")
        meta = []
        if alias_target:
            meta.append(f"alias -> {alias_target}")
        elif kind == "dir" and current_bucket.get("bytes", 0) == 0 and current_bucket.get("packages", 0) == 0:
            meta.append("directory index")
        meta.append(f"incoming {format_bytes(incoming_bucket.get('bytes', 0))} / current {format_bytes(current_bucket.get('bytes', 0))}")
        meta.append(f"kind {kind}")

        items.append(
            {
                "name": name,
                "status": status,
                "kind": kind,
                "alias_target": alias_target,
                "incoming_bytes": incoming_bucket.get("bytes", 0),
                "incoming_human": format_bytes(incoming_bucket.get("bytes", 0)),
                "incoming_packages": incoming_bucket.get("packages", 0),
                "current_bytes": current_bucket.get("bytes", 0),
                "current_human": format_bytes(current_bucket.get("bytes", 0)),
                "current_packages": current_bucket.get("packages", 0),
                "meta": " / ".join(meta),
            }
        )
    return items


def main() -> None:
    lock_fd = acquire_lock()
    now = time.time()
    cache = load_cache(now)
    progress = parse_progress()
    if "current_item" not in progress and cache["incoming"]["latest_files"]:
        progress["current_item"] = cache["incoming"]["latest_files"][0]["path"]
    sync_proc = parse_sync_process()
    total = load_total()
    storage = statvfs("/storage")
    resources = mem_swap_snapshot()
    cpu = cpu_snapshot(cache.get("previous_cpu_times", {}), cache.get("cpu_times", {}))
    current_exists = (BASE / "current").exists()
    events = last_events()
    estimate_data = maybe_eta(progress, total, cache, now, events, bool(sync_proc.get("running")), current_exists)

    if not sync_proc.get("running"):
        progress = {"raw_tail": progress.get("raw_tail", [])}

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "host": {
            "hostname": socket.gethostname(),
            "kernel": " ".join(os.uname()),
            "uptime_seconds": float(sh("cut -d. -f1 /proc/uptime") or "0"),
            "loadavg": loadavg_snapshot(),
        },
        "system": {
            "cpu": cpu,
            "memory": resources["memory"],
            "swap": resources["swap"],
        },
        "mirror": {
            "base": str(BASE),
            "client_url": client_url(),
            "current_exists": current_exists,
            "incoming": cache["incoming"],
            "current": cache["current"],
            "releases": cache["releases"],
            "release_dirs": cache["release_dirs"],
            "published_releases": len(cache["release_dirs"]),
        },
        "channels": channel_snapshot(cache, progress),
        "storage": storage,
        "sync": {
            "status": "running" if sync_proc.get("running") else "idle",
            "source": "rsync://mirror.opencsw.org/opencsw-full/",
            "process": sync_proc,
            "progress": progress,
            "events": events,
            "rsync_log_tail": progress.get("raw_tail", []),
            "estimate": estimate_data,
            "upstream_speed": upstream_speed_snapshot(sync_proc, progress, estimate_data),
        },
        "probe": total,
        "services": {
            "nginx": service_state("nginx"),
            "crond": service_state("crond"),
            "sshd": service_state("sshd"),
            "opencsw-monitor": service_state("opencsw-monitor"),
            "opencsw-sync": service_state("opencsw-sync"),
        },
    }

    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATUS_PATH)
    os.close(lock_fd)


if __name__ == "__main__":
    main()
