#!/usr/bin/env python3
# Credits: itinfra7 from GitHub
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from mirrors_public import repo_url

BASE = Path("/storage/omnios")
LOG_DIR = BASE / "logs"
STATE_PATH = BASE / ".mirror-state.json"
PROGRESS_PATH = Path("/run/omnios-lts-sync-progress.json")
LOCK_PATH = Path("/run/lock/omnios-lts-sync.lock")
CLIENT_HINT_PATH = BASE / "CLIENT_URLS.txt"
UPSTREAM = "https://pkg.omnios.org"
SCHEDULE_URL = "https://omnios.org/schedule"
SERVER_URL = repo_url("omnios")
REPOS = {"core": "omnios", "extra": "extra.omnios"}
SFE_REPO = {
    "repo": "localhostomnios",
    "publisher": "localhostomnios",
    "base_url": "http://sfe.opencsw.org/localhostomnios",
    "label": "SFE IPS Packages for OmniOS",
}
SUPPORTED_VERSION_LINES = ["catalog 0 1", "file 0", "manifest 0", "publisher 0", "search 0 1", "status 0", "versions 0"]
CONNECT_TIMEOUT = "30"
PAYLOAD_WORKERS = max(1, int(os.environ.get("OMNIOS_PAYLOAD_WORKERS", "64")))
DOWNLOAD_RETRIES = max(1, int(os.environ.get("OMNIOS_DOWNLOAD_RETRIES", "5")))
CURL_LOW_SPEED_LIMIT = os.environ.get("OMNIOS_CURL_LOW_SPEED_LIMIT", "1024")
CURL_LOW_SPEED_TIME = os.environ.get("OMNIOS_CURL_LOW_SPEED_TIME", "300")


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


def fetch_text(url, timeout=60):
    req = Request(url, headers={"User-Agent": "omnios-lts-mirror/1.0"})
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_json(url, timeout=180):
    return json.loads(fetch_text(url, timeout=timeout))


def local_lts_versions():
    versions = []
    state_path = BASE / ".mirror-state.json"
    try:
        state = json.loads(state_path.read_text())
        for key in ("latest", "previous"):
            value = state.get(key)
            if isinstance(value, str) and re.fullmatch(r"r151[0-9]{3}", value):
                versions.append(value)
    except Exception:
        pass
    try:
        for item in BASE.iterdir():
            if item.is_dir() and re.fullmatch(r"r151[0-9]{3}", item.name):
                versions.append(item.name)
    except Exception:
        pass
    return sorted(set(versions), key=lambda item: int(item[1:]), reverse=True)


def discover_lts():
    try:
        html = fetch_text(SCHEDULE_URL)
        found = set()
        for match in re.finditer(r"(r151[0-9]{3}).{0,200}?\\bLTS\\b", html, re.I | re.S):
            found.add(match.group(1))
        versions = sorted(found, key=lambda item: int(item[1:]), reverse=True)
        if len(versions) >= 2:
            return versions[:2]
    except Exception as exc:
        event("schedule-fallback", repr(exc))
    versions = local_lts_versions()
    if len(versions) >= 2:
        return versions[:2]
    raise RuntimeError("could not discover latest two OmniOS LTS releases")


def curl_download(url, dest, expected_size=None, force=False):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not force and dest.exists() and expected_size is not None and dest.stat().st_size == expected_size:
        return "exists"
    if not force and dest.exists() and expected_size is None and dest.stat().st_size > 0:
        return "exists"

    tmp = dest.parent / (dest.name + ".partial")
    last_error = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if tmp.exists():
            tmp.unlink()
        cmd = [
            "curl",
            "-fL",
            "--silent",
            "--show-error",
            "--retry",
            "5",
            "--retry-delay",
            "5",
            "--connect-timeout",
            CONNECT_TIMEOUT,
            "--speed-limit",
            CURL_LOW_SPEED_LIMIT,
            "--speed-time",
            CURL_LOW_SPEED_TIME,
            "-o",
            str(tmp),
            url,
        ]
        try:
            subprocess.run(cmd, check=True)
            if expected_size is not None and tmp.stat().st_size != expected_size:
                raise RuntimeError(f"size mismatch for {url}: {tmp.stat().st_size} != {expected_size}")
            tmp.replace(dest)
            return "downloaded"
        except Exception as exc:
            last_error = exc
            tmp.unlink(missing_ok=True)
            if attempt >= DOWNLOAD_RETRIES:
                break
            try:
                rel = str(dest.relative_to(BASE))
            except ValueError:
                rel = str(dest)
            event("download-retry", attempt, rel, repr(exc))
            time.sleep(min(30, 2 ** attempt))
    raise last_error


def payload_speed(done_bytes, started_at):
    elapsed = max(time.monotonic() - started_at, 0.001)
    return done_bytes / elapsed


def read_json(path):
    return json.loads(path.read_text())


def write_catalog_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(data)


def link_alias(source, alias):
    if source == alias:
        return
    alias.parent.mkdir(parents=True, exist_ok=True)
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
        if len(parts) < 2 or not re.fullmatch(r"[0-9a-f]{40}", parts[1]):
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
            raise RuntimeError(f"payload size conflict {parts[1]}: {old} != {size}")
        payloads[parts[1]] = size
    return payloads


def repo_entries(repo_root, publisher):
    base_catalog = read_json(repo_root / "catalog/1/catalog.base.C")
    entries = []
    for name, versions in base_catalog.get(publisher, {}).items():
        for item in versions:
            version = item.get("version")
            if version:
                entries.append((name, version))
    return entries


def ips_version_key(version):
    text = str(version or "")
    match = re.search(r":(\d{8}T\d{6}Z)$", text)
    return (match.group(1) if match else "", text)


def catalog_signature(payload):
    unsigned = {key: value for key, value in payload.items() if key != "_SIGNATURE"}
    data = json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def signed_catalog(payload):
    result = {key: value for key, value in payload.items() if key != "_SIGNATURE"}
    result["_SIGNATURE"] = {"sha-1": catalog_signature(result)}
    return result


def write_versions0(path):
    header = "pkg-server static"
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("pkg-server "):
                header = line
                break
    path.write_text(header + "\n" + "\n".join(SUPPORTED_VERSION_LINES) + "\n")


def catalog0_last_modified(attrs):
    value = str(attrs.get("last-modified") or attrs.get("created") or now_iso())
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\.\d+)?Z", value)
    if match:
        year, month, day, hour, minute, second, fraction = match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}{fraction or ''}+00:00"
    return value


def write_catalog0(path, selected_versions, attrs):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"S Last-Modified: {catalog0_last_modified(attrs)}",
        "S prefix: CRSV",
        f"S npkgs: {len(selected_versions)}",
    ]
    for name, version in sorted(selected_versions.items()):
        fmri = name if name.startswith("pkg:/") else f"pkg:/{name}"
        lines.append(f"V {fmri}@{version}")
    path.write_text("\n".join(lines) + "\n")


def newest_catalog_versions(base_catalog, publisher):
    selected = {}
    for name, versions in base_catalog.get(publisher, {}).items():
        candidates = [item for item in versions if item.get("version")]
        if candidates:
            selected[name] = max(candidates, key=lambda item: ips_version_key(item.get("version")))["version"]
    return selected


def prune_catalog_part(catalog, publisher, selected_versions):
    result = {}
    for key, value in catalog.items():
        if key in {"_SIGNATURE", publisher}:
            continue
        result[key] = value

    pruned = {}
    for name, versions in catalog.get(publisher, {}).items():
        selected = selected_versions.get(name)
        if not selected:
            continue
        kept = [item for item in versions if item.get("version") == selected]
        if kept:
            pruned[name] = kept[:1]
    result[publisher] = pruned
    return signed_catalog(result)


def download_sfe_control_files(repo_root):
    base_url = SFE_REPO["base_url"]
    publisher = SFE_REPO["publisher"]
    repo_root.mkdir(parents=True, exist_ok=True)
    for control in ["versions/0", "publisher/0", "status/0"]:
        progress("syncing", release="sfe", repo=SFE_REPO["repo"], current=control)
        curl_download(f"{base_url}/{control}", repo_root / control, force=True)

    attrs_url = f"{base_url}/catalog/1/catalog.attrs"
    progress("syncing", release="sfe", repo=SFE_REPO["repo"], current="catalog/1/catalog.attrs")
    attrs = fetch_json(attrs_url)

    base_part = "catalog.base.C"
    base_catalog = fetch_json(f"{base_url}/catalog/1/{base_part}", timeout=240)
    selected_versions = newest_catalog_versions(base_catalog, publisher)
    signatures = {}
    pruned_base = prune_catalog_part(base_catalog, publisher, selected_versions)
    signatures[base_part] = pruned_base["_SIGNATURE"]["sha-1"]
    write_catalog_json(repo_root / "catalog/1" / base_part, pruned_base)

    for part in sorted(set(attrs.get("parts", {})) - {base_part}):
        progress("syncing", release="sfe", repo=SFE_REPO["repo"], current=f"catalog/1/{part}")
        catalog = fetch_json(f"{base_url}/catalog/1/{part}", timeout=300)
        pruned = prune_catalog_part(catalog, publisher, selected_versions)
        signatures[part] = pruned["_SIGNATURE"]["sha-1"]
        write_catalog_json(repo_root / "catalog/1" / part, pruned)

    attrs["parts"] = {
        part: {
            **attrs.get("parts", {}).get(part, {}),
            "signature-sha-1": signatures[part],
        }
        for part in sorted(signatures)
    }
    attrs["updates"] = {}
    attrs["package-count"] = len(selected_versions)
    attrs["package-version-count"] = len(selected_versions)
    attrs = signed_catalog(attrs)
    write_json(repo_root / "catalog/1/catalog.attrs", attrs)
    write_catalog0(repo_root / "catalog/0", selected_versions, attrs)
    write_versions0(repo_root / "versions/0")
    return selected_versions


def write_client_hints(latest):
    CLIENT_HINT_PATH.write_text(
        "\n".join(
            [
                "# OmniOS IPS publisher examples for this internal mirror.",
                f"# latest-lts currently points to {latest}.",
                f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/latest-lts/core omnios",
                f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/latest-lts/extra extra.omnios",
                f"pkg set-publisher -G '*' -M '*' -g {SERVER_URL}/localhostomnios localhostomnios",
                "pkg refresh --full",
                "",
            ]
        )
    )


def update_release_alias(alias_name, target_name):
    alias = BASE / alias_name
    tmp = BASE / f".{alias_name}.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    if alias.exists() and not alias.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink alias path: {alias}")
    tmp.symlink_to(target_name)
    tmp.replace(alias)


def update_aliases(latest, previous):
    update_release_alias("latest-lts", latest)
    update_release_alias("previous-lts", previous)


def cleanup_old(keep):
    for path in BASE.iterdir():
        if not path.is_dir() or not re.fullmatch(r"r151[0-9]{3}", path.name):
            continue
        if path.name not in keep:
            event("cleanup", path.name)
            shutil.rmtree(path)


def download_control_files(release, repo, publisher, repo_root):
    base_url = f"{UPSTREAM}/{release}/{repo}"
    for control in ["versions/0", "publisher/0", "status/0"]:
        progress("syncing", release=release, repo=repo, current=control)
        curl_download(f"{base_url}/{control}", repo_root / control, force=True)

    progress("syncing", release=release, repo=repo, current="catalog/0")
    curl_download(f"{base_url}/catalog/0", repo_root / "catalog/0", force=True)
    write_versions0(repo_root / "versions/0")

    attrs_path = repo_root / "catalog/1/catalog.attrs"
    progress("syncing", release=release, repo=repo, current="catalog/1/catalog.attrs")
    curl_download(f"{base_url}/catalog/1/catalog.attrs", attrs_path, force=True)
    attrs = read_json(attrs_path)
    for part in sorted(attrs.get("parts", {})):
        progress("syncing", release=release, repo=repo, current=f"catalog/1/{part}")
        curl_download(f"{base_url}/catalog/1/{part}", repo_root / "catalog/1" / part, force=True)


def sync_repo(release, repo, publisher):
    repo_root = BASE / release / repo
    base_url = f"{UPSTREAM}/{release}/{repo}"
    event("repo-start", release, repo)
    event("payload-workers", release, repo, PAYLOAD_WORKERS)
    download_control_files(release, repo, publisher, repo_root)

    entries = repo_entries(repo_root, publisher)
    manifest_done = 0
    payload_done = 0
    payload_skip = 0
    payload_seen = {}
    manifest_root = repo_root / "manifest/0"
    file_root = repo_root / "file/0"

    for name, version in entries:
        manifest_done += 1
        rel_manifest = Path(*name.split("/")).with_name(name.split("/")[-1] + "@" + version)
        manifest_path = manifest_root / rel_manifest
        manifest_url = f"{base_url}/manifest/0/{quote(name + '@' + version, safe='/:@,.-_+')}"
        progress(
            "syncing",
            release=release,
            repo=repo,
            current=f"manifest:{name}",
            manifests_done=manifest_done,
            manifests_total=len(entries),
            payloads_done=payload_done,
        )
        curl_download(manifest_url, manifest_path)
        link_alias(manifest_path, manifest_root / quote(name + "@" + version, safe=""))
        link_alias(manifest_path, manifest_root / quote(f"pkg://{publisher}/{name}@{version}", safe=""))
        link_alias(manifest_path, manifest_root / "pkg:" / publisher / rel_manifest)

        payloads = parse_payloads(manifest_path.read_text(errors="replace"))
        payload_jobs = []
        for hash_value, size in payloads.items():
            old = payload_seen.get(hash_value)
            if old is not None:
                if old != size:
                    raise RuntimeError(f"payload size conflict {hash_value}: {old} != {size}")
                payload_skip += 1
                continue
            payload_seen[hash_value] = size
            dest = file_root / hash_value
            if dest.exists() and dest.stat().st_size == size:
                payload_skip += 1
                continue
            payload_jobs.append((hash_value, size, dest))

        if not payload_jobs:
            continue

        batch_done = 0
        batch_bytes_done = 0
        batch_bytes_total = sum(size for _, size, _ in payload_jobs)
        batch_started_at = time.monotonic()
        workers = min(PAYLOAD_WORKERS, len(payload_jobs))
        progress(
            "syncing",
            release=release,
            repo=repo,
            current=f"payload-batch:{name}",
            manifests_done=manifest_done,
            manifests_total=len(entries),
            payloads_done=payload_done,
            payloads_known=len(payload_seen),
            payload_batch_done=batch_done,
            payload_batch_total=len(payload_jobs),
            payload_batch_bytes_done=batch_bytes_done,
            payload_batch_bytes_total=batch_bytes_total,
            payload_workers=workers,
            payload_speed_bps=0,
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(curl_download, f"{base_url}/file/0/{hash_value}", dest, size): (hash_value, size)
                for hash_value, size, dest in payload_jobs
            }
            for future in as_completed(future_map):
                hash_value, size = future_map[future]
                result = future.result()
                batch_done += 1
                if result == "downloaded":
                    payload_done += 1
                    batch_bytes_done += size
                else:
                    payload_skip += 1
                progress(
                    "syncing",
                    release=release,
                    repo=repo,
                    current=f"file:{hash_value}",
                    manifests_done=manifest_done,
                    manifests_total=len(entries),
                    payloads_done=payload_done,
                    payloads_known=len(payload_seen),
                    payload_batch_done=batch_done,
                    payload_batch_total=len(payload_jobs),
                    payload_batch_bytes_done=batch_bytes_done,
                    payload_batch_bytes_total=batch_bytes_total,
                    payload_workers=workers,
                    payload_speed_bps=round(payload_speed(batch_bytes_done, batch_started_at), 2),
                )

    result = {"manifests": manifest_done, "payloads_downloaded": payload_done, "payloads_skipped": payload_skip}
    write_json(
        repo_root / ".repo-complete.json",
        {
            "completed_at": now_iso(),
            "publisher": publisher,
            "release": release,
            "repo": repo,
            "source": base_url,
            "result": result,
        },
    )
    event("repo-ok", release, repo, f"manifests={manifest_done}", f"payloads_downloaded={payload_done}", f"payloads_skipped={payload_skip}")
    return result


def manifest_alias_paths(publisher, name, version):
    rel_manifest = Path(*name.split("/")).with_name(name.split("/")[-1] + "@" + version)
    return [
        rel_manifest,
        Path(quote(name + "@" + version, safe="")),
        Path(quote(f"pkg://{publisher}/{name}@{version}", safe="")),
        Path("pkg:") / publisher / rel_manifest,
    ]


def prune_sfe_repo(repo_root, desired_manifest_paths, desired_payloads):
    removed_manifests = 0
    manifest_root = repo_root / "manifest/0"
    if manifest_root.exists():
        for root, dirs, files in os.walk(manifest_root, topdown=False):
            root_path = Path(root)
            for name in files:
                path = root_path / name
                rel = str(path.relative_to(manifest_root))
                if name.endswith(".partial") or rel not in desired_manifest_paths:
                    path.unlink(missing_ok=True)
                    removed_manifests += 1
            for dirname in dirs:
                try:
                    (root_path / dirname).rmdir()
                except OSError:
                    pass

    removed_payloads = 0
    file_root = repo_root / "file/0"
    if file_root.exists():
        for path in file_root.iterdir():
            if path.name.endswith(".partial") or (path.is_file() and re.fullmatch(r"[0-9a-f]{40}", path.name) and path.name not in desired_payloads):
                path.unlink(missing_ok=True)
                removed_payloads += 1
    return removed_manifests, removed_payloads


def sync_sfe_repo():
    repo = SFE_REPO["repo"]
    publisher = SFE_REPO["publisher"]
    base_url = SFE_REPO["base_url"]
    repo_root = BASE / repo
    event("repo-start", "sfe", repo, "latest-entity-only")
    download_sfe_control_files(repo_root)
    entries = repo_entries(repo_root, publisher)
    manifest_done = 0
    payload_done = 0
    payload_skip = 0
    payload_seen = {}
    desired_payloads = set()
    desired_manifest_paths = set()
    manifest_root = repo_root / "manifest/0"
    file_root = repo_root / "file/0"

    for name, version in entries:
        manifest_done += 1
        alias_paths = manifest_alias_paths(publisher, name, version)
        manifest_path = manifest_root / alias_paths[0]
        manifest_url = f"{base_url}/manifest/0/{quote(name + '@' + version, safe='/:@,.-_+')}"
        progress(
            "syncing",
            release="sfe",
            repo=repo,
            current=f"manifest:{name}",
            manifests_done=manifest_done,
            manifests_total=len(entries),
            payloads_done=payload_done,
        )
        curl_download(manifest_url, manifest_path)
        for rel_path in alias_paths:
            desired_manifest_paths.add(str(rel_path))
        for alias in alias_paths[1:]:
            link_alias(manifest_path, manifest_root / alias)

        payloads = parse_payloads(manifest_path.read_text(errors="replace"))
        payload_jobs = []
        for hash_value, size in payloads.items():
            desired_payloads.add(hash_value)
            old = payload_seen.get(hash_value)
            if old is not None:
                if old != size:
                    raise RuntimeError(f"SFE payload size conflict {hash_value}: {old} != {size}")
                payload_skip += 1
                continue
            payload_seen[hash_value] = size
            dest = file_root / hash_value
            if dest.exists() and dest.stat().st_size == size:
                payload_skip += 1
                continue
            payload_jobs.append((hash_value, size, dest))

        if not payload_jobs:
            continue

        batch_done = 0
        batch_bytes_done = 0
        batch_bytes_total = sum(size for _, size, _ in payload_jobs)
        batch_started_at = time.monotonic()
        workers = min(PAYLOAD_WORKERS, len(payload_jobs))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(curl_download, f"{base_url}/file/0/{hash_value}", dest, size): (hash_value, size)
                for hash_value, size, dest in payload_jobs
            }
            for future in as_completed(future_map):
                hash_value, size = future_map[future]
                result = future.result()
                batch_done += 1
                if result == "downloaded":
                    payload_done += 1
                    batch_bytes_done += size
                else:
                    payload_skip += 1
                progress(
                    "syncing",
                    release="sfe",
                    repo=repo,
                    current=f"file:{hash_value}",
                    manifests_done=manifest_done,
                    manifests_total=len(entries),
                    payloads_done=payload_done,
                    payloads_known=len(payload_seen),
                    payload_batch_done=batch_done,
                    payload_batch_total=len(payload_jobs),
                    payload_batch_bytes_done=batch_bytes_done,
                    payload_batch_bytes_total=batch_bytes_total,
                    payload_workers=workers,
                    payload_speed_bps=round(payload_speed(batch_bytes_done, batch_started_at), 2),
                )

    progress("cleanup", release="sfe", repo=repo, current="prune")
    removed_manifests, removed_payloads = prune_sfe_repo(repo_root, desired_manifest_paths, desired_payloads)
    result = {
        "manifests": manifest_done,
        "payloads_downloaded": payload_done,
        "payloads_skipped": payload_skip,
        "removed_manifests": removed_manifests,
        "removed_payloads": removed_payloads,
        "policy": "latest entity only per package",
    }
    write_json(
        repo_root / ".repo-complete.json",
        {
            "completed_at": now_iso(),
            "publisher": publisher,
            "release": "sfe",
            "repo": repo,
            "source": base_url,
            "policy": "latest entity only per package",
            "result": result,
        },
    )
    event("repo-ok", "sfe", repo, f"manifests={manifest_done}", f"payloads_downloaded={payload_done}", f"payloads_skipped={payload_skip}")
    return result


def main():
    fd = acquire_lock()
    BASE.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    keep = discover_lts()
    latest = keep[0]
    previous = keep[1]
    state = {
        "latest": latest,
        "previous": previous,
        "keep": keep,
        "source": f"{UPSTREAM} + {SFE_REPO['base_url']}",
        "repos": list(REPOS.keys()),
        "sfe_repo": SFE_REPO["repo"],
        "sfe_policy": "latest entity only per package",
        "sync_status": "running",
        "last_sync": None,
        "started_at": now_iso(),
    }
    write_json(STATE_PATH, state)
    update_aliases(latest, previous)
    write_client_hints(latest)
    progress("starting", keep=keep)
    event("run-start", f"latest={latest}", f"previous={previous}")
    cleanup_old(set(keep))

    results = {}
    for release in keep:
        for repo, publisher in REPOS.items():
            results[f"{release}/{repo}"] = sync_repo(release, repo, publisher)
    results[SFE_REPO["repo"]] = sync_sfe_repo()

    cleanup_old(set(keep))
    state.update({"sync_status": "complete", "last_sync": now_iso(), "results": results})
    write_json(STATE_PATH, state)
    progress("complete", keep=keep, results=results)
    event("run-complete", f"latest={latest}", f"previous={previous}")
    os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        event("fatal", repr(exc))
        progress("error", error=repr(exc))
        raise
