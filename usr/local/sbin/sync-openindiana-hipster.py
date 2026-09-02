#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from mirrors_public import repo_url

BASE = Path("/storage/openindiana")
LOG_DIR = BASE / "logs"
STATE_PATH = BASE / ".mirror-state.json"
PROGRESS_PATH = Path("/run/openindiana-sync-progress.json")
LOCK_PATH = Path("/run/lock/openindiana-sync.lock")
CLIENT_HINT_PATH = BASE / "CLIENT_URLS.txt"
SERVER_URL = repo_url("openindiana")
USER_AGENT = "openindiana-hipster-mirror/1.0"
CONNECT_TIMEOUT = int(os.environ.get("OPENINDIANA_CONNECT_TIMEOUT", "30"))
READ_RETRIES = int(os.environ.get("OPENINDIANA_READ_RETRIES", "5"))
MANIFEST_WORKERS = max(1, int(os.environ.get("OPENINDIANA_MANIFEST_WORKERS", "24")))
PAYLOAD_WORKERS = max(1, int(os.environ.get("OPENINDIANA_PAYLOAD_WORKERS", "64")))
MAX_PAYLOAD_FUTURES = max(PAYLOAD_WORKERS * 16, int(os.environ.get("OPENINDIANA_MAX_PAYLOAD_FUTURES", "2048")))
UPSTREAM_PREFLIGHT_TIMEOUT = int(os.environ.get("OPENINDIANA_UPSTREAM_PREFLIGHT_TIMEOUT", "10"))
OPTIONAL_PREFLIGHT_TIMEOUT = int(os.environ.get("OPENINDIANA_OPTIONAL_PREFLIGHT_TIMEOUT", "10"))
PROGRESS_LOCK = threading.Lock()
SUPPORTED_VERSION_LINES = ["catalog 0 1", "file 0", "manifest 0", "publisher 0", "search 0 1", "status 0", "versions 0"]

REPOS = {
    "hipster": {
        "publisher": "openindiana.org",
        "base_url": "https://pkg.openindiana.org/hipster",
        "label": "OpenIndiana Hipster",
    },
    "hipster-encumbered": {
        "publisher": "hipster-encumbered",
        "base_url": "https://pkg.openindiana.org/hipster-encumbered",
        "label": "OpenIndiana Hipster Encumbered",
    },
    "localhostoih": {
        "publisher": "localhostoih",
        "base_url": "http://sfe.opencsw.org/localhostoih",
        "label": "SFE OpenIndiana Hipster Third-Party",
        "optional": True,
    },
}

HEX40 = re.compile(r"^[0-9a-f]{40}$")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def event(*parts):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "sync-events.log").open("a") as handle:
        handle.write(now_iso() + "|" + "|".join(str(part) for part in parts) + "\n")


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def progress(phase, **payload):
    data = {"updated_at": now_iso(), "phase": phase, **payload}
    with PROGRESS_LOCK:
        write_json(PROGRESS_PATH, data)


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        event("already-running")
        raise SystemExit(0)
    return fd


def request(url):
    return Request(url, headers={"User-Agent": USER_AGENT})


def urlopen_retry(url, timeout=120):
    last = None
    for attempt in range(1, READ_RETRIES + 1):
        try:
            return urlopen(request(url), timeout=timeout)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            if attempt == READ_RETRIES:
                break
            time.sleep(min(30, 2 ** attempt))
    raise last


def sha1_file(path):
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_once(url, dest, tmp, expected_size=None, expected_sha1=None, speed=None, progress_hook=None):
    bytes_done = 0
    digest = hashlib.sha1() if expected_sha1 is not None else None
    started = time.monotonic()
    last_progress = 0.0
    with urlopen_retry(url, timeout=180) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
            if digest is not None:
                digest.update(chunk)
            bytes_done += len(chunk)
            if speed is not None:
                speed["bytes"] += len(chunk)
                speed["elapsed"] = max(time.monotonic() - started, 0.001)
            if progress_hook is not None:
                now = time.monotonic()
                if now - last_progress >= 1:
                    progress_hook(bytes_done, max(now - started, 0.001))
                    last_progress = now

    actual = tmp.stat().st_size
    if expected_size is not None and actual != expected_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"size mismatch for {url}: {actual} != {expected_size}")
    if digest is not None:
        actual_sha1 = digest.hexdigest()
        if actual_sha1 != expected_sha1:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-1 mismatch for {url}: {actual_sha1} != {expected_sha1}")
    tmp.replace(dest)
    if progress_hook is not None:
        progress_hook(actual, max(time.monotonic() - started, 0.001))
    return actual, "downloaded"


def download(url, dest, expected_size=None, expected_sha1=None, force=False, speed=None, progress_hook=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not force and dest.exists():
        if expected_sha1 is not None:
            actual_sha1 = sha1_file(dest)
            if actual_sha1 == expected_sha1:
                return 0, "exists"
            event(
                "integrity-mismatch",
                str(dest.relative_to(BASE) if dest.is_relative_to(BASE) else dest),
                f"expected={expected_sha1}",
                f"actual={actual_sha1}",
            )
        elif expected_size is None and dest.stat().st_size > 0:
            return 0, "exists"
        elif expected_size is not None and dest.stat().st_size == expected_size:
            return 0, "exists"

    tmp = dest.parent / (dest.name + ".partial")
    last = None
    for attempt in range(1, READ_RETRIES + 1):
        if tmp.exists():
            tmp.unlink()
        try:
            return download_once(
                url,
                dest,
                tmp,
                expected_size=expected_size,
                expected_sha1=expected_sha1,
                speed=speed,
                progress_hook=progress_hook,
            )
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            last = exc
            tmp.unlink(missing_ok=True)
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            if attempt == READ_RETRIES:
                break
            event("download-retry", attempt, str(dest.relative_to(BASE) if dest.is_relative_to(BASE) else dest), repr(exc))
            time.sleep(min(30, 2 ** attempt))
    raise last


def fetch_json(url):
    with urlopen_retry(url, timeout=180) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def read_json(path):
    return json.loads(path.read_text())


def completed_repo_result(repo_id, spec, warning):
    marker = BASE / repo_id / ".repo-complete.json"
    if not marker.exists():
        return None
    try:
        data = read_json(marker)
    except Exception:
        data = {}
    payload = dict(data) if isinstance(data, dict) else {}
    payload.update(
        {
            "publisher": payload.get("publisher", spec["publisher"]),
            "source": payload.get("source", spec["base_url"]),
            "completed_at": payload.get("completed_at"),
            "stale": True,
            "warning": warning,
        }
    )
    return payload


def check_upstream(repo_id, spec):
    timeout = OPTIONAL_PREFLIGHT_TIMEOUT if spec.get("optional") else UPSTREAM_PREFLIGHT_TIMEOUT
    with urlopen(request(f"{spec['base_url']}/versions/0"), timeout=timeout) as response:
        response.read(1)


def hardlink_or_copy(source, alias, manifest_root, desired):
    alias.parent.mkdir(parents=True, exist_ok=True)
    desired.add(str(alias.relative_to(manifest_root)))
    if source == alias:
        return
    try:
        if alias.exists() or alias.is_symlink():
            if alias.stat().st_ino == source.stat().st_ino:
                return
            alias.unlink()
        os.link(source, alias)
    except OSError:
        shutil.copy2(source, alias)


def parse_payloads(manifest_text):
    payloads = {}
    for line in manifest_text.splitlines():
        if not (line.startswith("file ") or line.startswith("license ") or line.startswith("signature ")):
            continue
        parts = line.split()
        if len(parts) < 2 or not HEX40.fullmatch(parts[1]):
            continue
        size = None
        for part in parts[2:]:
            if part.startswith("pkg.csize="):
                try:
                    size = int(part.split("=", 1)[1])
                except ValueError:
                    size = None
                break
        if size is None:
            continue
        old = payloads.get(parts[1])
        if old is not None and old != size:
            raise RuntimeError(f"payload size conflict inside manifest {parts[1]}: {old} != {size}")
        payloads[parts[1]] = size
    return payloads


def repo_entries(repo_root, publisher):
    base_catalog = read_json(repo_root / "catalog/1/catalog.base.C")
    entries = []
    for name, versions in base_catalog.get(publisher, {}).items():
        for item in versions:
            version = item.get("version")
            manifest_sha1 = item.get("signature-sha-1")
            if not version:
                continue
            if not isinstance(manifest_sha1, str) or not HEX40.fullmatch(manifest_sha1):
                raise RuntimeError(f"catalog entry has no valid manifest SHA-1: {publisher}/{name}@{version}")
            entries.append((name, version, manifest_sha1))
    return entries


def write_client_hints():
    CLIENT_HINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_HINT_PATH.write_text(
        "\n".join(
            [
                "# OpenIndiana Hipster IPS publisher examples for this internal mirror.",
                "# Hipster is rolling; upstream does not expose separate latest/previous IPS repositories.",
                "# localhostoih is the third-party SFE publisher.",
                f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/hipster openindiana.org",
                f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/hipster-encumbered hipster-encumbered",
                f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/localhostoih localhostoih",
                "pkg refresh --full",
                "",
            ]
        )
    )


def write_versions0(path, catalog0_available=True):
    header = "pkg-server static"
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("pkg-server "):
                header = line
                break
    lines = list(SUPPORTED_VERSION_LINES)
    if not catalog0_available:
        lines[0] = "catalog 1"
    path.write_text(header + "\n" + "\n".join(lines) + "\n")


def download_control_files(repo_id, spec, repo_root):
    base_url = spec["base_url"]
    controls = ["versions/0", "publisher/0", "status/0", "catalog/0", "catalog/1/catalog.attrs"]
    catalog0_available = True
    for idx, control in enumerate(controls, start=1):
        progress("control", repo=repo_id, current=control, done=idx - 1, total=len(controls), upstream_speed_bps=0)
        def hook(done_bytes, elapsed, control=control, idx=idx):
            progress("control", repo=repo_id, current=control, done=idx - 1, total=len(controls), downloaded_bytes=done_bytes, upstream_speed_bps=round(done_bytes / elapsed, 2))
        try:
            download(f"{base_url}/{control}", repo_root / control, force=True, progress_hook=hook)
        except HTTPError as exc:
            if control == "catalog/0":
                catalog0_available = False
                event("control-skip", repo_id, control, f"http={exc.code}")
            else:
                raise
    write_versions0(repo_root / "versions/0", catalog0_available)

    attrs = read_json(repo_root / "catalog/1/catalog.attrs")
    catalog_files = sorted(set(attrs.get("parts", {})) | set(attrs.get("updates", {})))
    for idx, part in enumerate(catalog_files, start=1):
        progress("catalog", repo=repo_id, current=f"catalog/1/{part}", done=idx - 1, total=len(catalog_files), upstream_speed_bps=0)
        def hook(done_bytes, elapsed, part=part, idx=idx):
            progress("catalog", repo=repo_id, current=f"catalog/1/{part}", done=idx - 1, total=len(catalog_files), downloaded_bytes=done_bytes, upstream_speed_bps=round(done_bytes / elapsed, 2))
        download(f"{base_url}/catalog/1/{part}", repo_root / "catalog/1" / part, force=True, progress_hook=hook)
    expected = {"catalog.attrs", *catalog_files}
    catalog_root = repo_root / "catalog/1"
    for path in catalog_root.iterdir():
        if path.is_file() and path.name not in expected:
            path.unlink(missing_ok=True)
    return attrs


def manifest_target(manifest_root, name, version):
    parts = name.split("/")
    return manifest_root.joinpath(*parts[:-1], parts[-1] + "@" + version)


def download_manifest(repo_id, spec, repo_root, entry):
    name, version, manifest_sha1 = entry
    base_url = spec["base_url"]
    publisher = spec["publisher"]
    manifest_root = repo_root / "manifest/0"
    dest = manifest_target(manifest_root, name, version)
    encoded = quote(name + "@" + version, safe="/:@,.-_+")
    url = f"{base_url}/manifest/0/{encoded}"
    download(url, dest, expected_sha1=manifest_sha1)

    desired_paths = set()
    desired_paths.add(str(dest.relative_to(manifest_root)))
    hardlink_or_copy(dest, manifest_root / quote(name + "@" + version, safe=""), manifest_root, desired_paths)
    hardlink_or_copy(dest, manifest_root / quote(f"pkg://{publisher}/{name}@{version}", safe=""), manifest_root, desired_paths)
    hardlink_or_copy(dest, manifest_root / "pkg:" / publisher / Path(*name.split("/")).with_name(name.split("/")[-1] + "@" + version), manifest_root, desired_paths)
    payloads = parse_payloads(dest.read_text(errors="replace"))
    return dest.stat().st_size, payloads, desired_paths


def sync_manifests(repo_id, spec, repo_root, entries):
    manifest_count = len(entries)
    manifest_bytes = 0
    payloads = {}
    desired_manifest_paths = set()
    started = time.monotonic()
    done = 0

    def task(entry):
        return download_manifest(repo_id, spec, repo_root, entry)

    pending = {}
    iterator = iter(entries)

    def submit_next(executor):
        try:
            entry = next(iterator)
        except StopIteration:
            return False
        pending[executor.submit(task, entry)] = entry
        return True

    with ThreadPoolExecutor(max_workers=MANIFEST_WORKERS) as executor:
        while len(pending) < MANIFEST_WORKERS * 16:
            if not submit_next(executor):
                break

        while pending:
            complete, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in complete:
                pending.pop(future)
                size, found_payloads, manifest_paths = future.result()
                done += 1
                manifest_bytes += size
                desired_manifest_paths.update(manifest_paths)
                for hash_value, csize in found_payloads.items():
                    old = payloads.get(hash_value)
                    if old is not None and old != csize:
                        raise RuntimeError(f"payload size conflict {hash_value}: {old} != {csize}")
                    payloads[hash_value] = csize

            while len(pending) < MANIFEST_WORKERS * 16:
                if not submit_next(executor):
                    break

            if done == manifest_count or done % 500 == 0:
                elapsed = max(time.monotonic() - started, 0.001)
                progress(
                    "manifests",
                    repo=repo_id,
                    current="manifest/0",
                    manifests_done=done,
                    manifests_total=manifest_count,
                    payloads_known=len(payloads),
                    upstream_speed_bps=round(manifest_bytes / elapsed, 2),
                )

    return manifest_bytes, payloads, desired_manifest_paths


def payload_destination(repo_root, hash_value):
    return repo_root / "file/0" / hash_value


def sync_payloads(repo_id, spec, repo_root, payloads):
    base_url = spec["base_url"]
    items = sorted(payloads.items())
    total = len(items)
    bytes_total = sum(size for _, size in items)
    bytes_done = 0
    downloaded_bytes = 0
    existing_bytes = 0
    done = 0
    skipped = 0
    started = time.monotonic()
    pending = {}
    iterator = iter(items)
    active_lock = threading.Lock()
    active_payloads = {}
    last_live_progress = 0.0

    def live_payload_progress(hash_value, active_bytes, force=False):
        nonlocal last_live_progress
        now = time.monotonic()
        with active_lock:
            active_payloads[hash_value] = active_bytes
            if not force and now - last_live_progress < 5:
                return
            last_live_progress = now
            active_total = sum(active_payloads.values())
            active_files = len(active_payloads)
        elapsed = max(now - started, 0.001)
        progress(
            "payloads",
            repo=repo_id,
            current=f"file/0/{hash_value}",
            payloads_done=done,
            payloads_known=total,
            payload_bytes_done=min(bytes_total, bytes_done + active_total),
            payload_bytes_total=bytes_total,
            payload_downloaded_bytes=downloaded_bytes + active_total,
            payload_existing_bytes=existing_bytes,
            payload_active_bytes=active_total,
            payload_active_files=active_files,
            payload_skip=skipped,
            payload_workers=PAYLOAD_WORKERS,
            payload_queue_limit=MAX_PAYLOAD_FUTURES,
            upstream_speed_bps=round((downloaded_bytes + active_total) / elapsed, 2),
        )

    def payload_task(hash_value, size):
        dest = payload_destination(repo_root, hash_value)
        if dest.exists() and dest.stat().st_size == size:
            return hash_value, size, 0, "exists"
        url = f"{base_url}/file/0/{hash_value}"
        def hook(active_bytes, _elapsed, hash_value=hash_value):
            live_payload_progress(hash_value, active_bytes)
        try:
            downloaded, status = download(url, dest, expected_size=size, progress_hook=hook)
        finally:
            with active_lock:
                active_payloads.pop(hash_value, None)
        return hash_value, size, downloaded, status

    def submit_next(executor):
        try:
            hash_value, size = next(iterator)
        except StopIteration:
            return False
        pending[executor.submit(payload_task, hash_value, size)] = (hash_value, size)
        return True

    with ThreadPoolExecutor(max_workers=PAYLOAD_WORKERS) as executor:
        progress(
            "payloads",
            repo=repo_id,
            current="file/0",
            payloads_done=done,
            payloads_known=total,
            payload_bytes_done=bytes_done,
            payload_bytes_total=bytes_total,
            payload_downloaded_bytes=downloaded_bytes,
            payload_existing_bytes=existing_bytes,
            payload_skip=skipped,
            payload_workers=PAYLOAD_WORKERS,
            payload_queue_limit=MAX_PAYLOAD_FUTURES,
            upstream_speed_bps=0,
        )
        while len(pending) < min(MAX_PAYLOAD_FUTURES, total):
            if not submit_next(executor):
                break

        while pending:
            complete, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in complete:
                pending.pop(future)
                _hash, size, downloaded, status = future.result()
                done += 1
                bytes_done += size
                if status == "exists":
                    skipped += 1
                    existing_bytes += size
                else:
                    downloaded_bytes += downloaded
                while len(pending) < MAX_PAYLOAD_FUTURES:
                    if not submit_next(executor):
                        break
            if done == total or done % 500 == 0:
                elapsed = max(time.monotonic() - started, 0.001)
                progress(
                    "payloads",
                    repo=repo_id,
                    current="file/0",
                    payloads_done=done,
                    payloads_known=total,
                    payload_bytes_done=bytes_done,
                    payload_bytes_total=bytes_total,
                    payload_downloaded_bytes=downloaded_bytes,
                    payload_existing_bytes=existing_bytes,
                    payload_skip=skipped,
                    payload_workers=PAYLOAD_WORKERS,
                    payload_queue_limit=MAX_PAYLOAD_FUTURES,
                    upstream_speed_bps=round(downloaded_bytes / elapsed, 2),
                )

    return {"payload_count": total, "payload_bytes": bytes_total, "payload_skipped": skipped}


def prune_payloads(repo_root, payloads):
    file_root = repo_root / "file/0"
    removed = 0
    if not file_root.exists():
        return removed
    for path in file_root.iterdir():
        if path.name.endswith(".partial"):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if path.is_file() and HEX40.fullmatch(path.name) and path.name not in payloads:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def prune_manifests(repo_root, desired_paths):
    manifest_root = repo_root / "manifest/0"
    removed = 0
    if not manifest_root.exists():
        return removed
    for root, dirs, files in os.walk(manifest_root, topdown=False):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            rel = str(path.relative_to(manifest_root))
            if rel not in desired_paths or name.endswith(".partial"):
                path.unlink(missing_ok=True)
                removed += 1
        for dirname in dirs:
            dpath = root_path / dirname
            try:
                dpath.rmdir()
            except OSError:
                pass
    return removed


def sync_repo(repo_id, spec):
    repo_root = BASE / repo_id
    repo_root.mkdir(parents=True, exist_ok=True)
    event("repo-start", repo_id)
    attrs = download_control_files(repo_id, spec, repo_root)
    entries = repo_entries(repo_root, spec["publisher"])
    progress("manifests", repo=repo_id, current="manifest/0", manifests_done=0, manifests_total=len(entries), payloads_known=0, upstream_speed_bps=0)
    manifest_bytes, payloads, desired_manifest_paths = sync_manifests(repo_id, spec, repo_root, entries)
    payload_result = sync_payloads(repo_id, spec, repo_root, payloads)
    progress("cleanup", repo=repo_id, current="prune", payloads_done=payload_result["payload_count"], payloads_known=payload_result["payload_count"], upstream_speed_bps=0)
    removed_payloads = prune_payloads(repo_root, payloads)
    removed_manifests = prune_manifests(repo_root, desired_manifest_paths)

    result = {
        "publisher": spec["publisher"],
        "source": spec["base_url"],
        "completed_at": now_iso(),
        "last_modified": attrs.get("last-modified"),
        "package_count": attrs.get("package-count"),
        "package_version_count": attrs.get("package-version-count"),
        "manifest_count": len(entries),
        "manifest_bytes": manifest_bytes,
        "unique_payload_count": len(payloads),
        "payload_bytes": payload_result["payload_bytes"],
        "payload_skipped": payload_result["payload_skipped"],
        "removed_payloads": removed_payloads,
        "removed_manifests": removed_manifests,
    }
    write_json(repo_root / ".repo-complete.json", result)
    event("repo-complete", repo_id, f"manifests={len(entries)}", f"payloads={len(payloads)}")
    return result


def initial_state():
    return {
        "sync_status": "running",
        "started_at": now_iso(),
        "last_sync": None,
        "source": "https://pkg.openindiana.org/hipster + https://pkg.openindiana.org/hipster-encumbered + http://sfe.opencsw.org/localhostoih",
        "latest": "hipster",
        "previous": "not-applicable",
        "policy": "OpenIndiana Hipster IPS is rolling. This mirror keeps official publishers plus the third-party SFE localhostoih publisher; upstream does not expose separate latest/previous versioned repositories.",
        "repos": {},
    }


def main():
    fd = acquire_lock()
    BASE.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write_client_hints()
    state = initial_state()
    write_json(STATE_PATH, state)
    progress("starting", repo="", current="init", upstream_speed_bps=0)
    try:
        results = {}
        warnings = []
        for repo_id, spec in REPOS.items():
            try:
                check_upstream(repo_id, spec)
                results[repo_id] = sync_repo(repo_id, spec)
            except Exception as exc:
                warning = f"{repo_id} upstream unavailable; serving last completed mirror"
                fallback = completed_repo_result(repo_id, spec, warning)
                if fallback is None:
                    raise
                event("repo-stale", repo_id, repr(exc))
                warnings.append(f"{warning}: {repr(exc)}")
                progress("warning", repo=repo_id, current="upstream unavailable", warning=warning, upstream_speed_bps=0)
                results[repo_id] = fallback
            state.update({"sync_status": "running", "repos": results, "warnings": warnings})
            write_json(STATE_PATH, state)
        state.update({"sync_status": "complete", "last_sync": now_iso(), "repos": results, "warnings": warnings})
        write_json(STATE_PATH, state)
        progress("complete", repo="", current="done", upstream_speed_bps=0, payloads_done=0, payloads_known=0, warnings=warnings)
        event("sync-complete")
    except Exception as exc:
        state.update({"sync_status": "error", "last_error": repr(exc), "failed_at": now_iso()})
        write_json(STATE_PATH, state)
        progress("error", repo="", current=repr(exc), upstream_speed_bps=0)
        event("sync-error", repr(exc))
        raise
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
