"""Evaluator-only OCI archive CAS prototype; no Docker or runner integration.

Every original tar byte is represented by an ordered, hash-checked segment.
Payload segments deduplicate OCI blobs; header/padding segments preserve exact
archive bytes. Originals are read-only and never removed. Only local, trusted,
owner-controlled store directories are supported (not adversarial shared FS).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile

HEX = re.compile(r"[0-9a-f]{64}\Z")
TRIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
OWNER = b'{"format":"capstudy-oci-archive-cas","version":1}\n'
CHUNK = 1024 * 1024
JSON_LIMIT = 8 * CHUNK
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json"}


class StoreError(ValueError):
    """Invalid evidence or insufficient storage; never a native task failure."""


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _regular(path):
    if not stat.S_ISREG(Path(path).lstat().st_mode):
        raise StoreError("Expected a regular non-symlink file")


@contextmanager
def _open_regular(path):
    _regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StoreError("Expected regular file")
        yield stream


def _parts(stream, offset, size):
    stream.seek(offset)
    remaining = size
    while remaining:
        block = stream.read(min(CHUNK, remaining))
        if not block:
            raise StoreError("Truncated content")
        remaining -= len(block)
        yield block


def _range_hash(stream, offset, size):
    h = hashlib.sha256()
    for block in _parts(stream, offset, size):
        h.update(block)
    return h.hexdigest()


def _json_bytes(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StoreError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(StoreError("Nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError("Invalid JSON") from exc


def _bounded_manifest_bytes(stream):
    size = os.fstat(stream.fileno()).st_size
    if size > JSON_LIMIT:
        raise StoreError("Per-trial manifest exceeds supported size")
    data = stream.read(JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        raise StoreError("Per-trial manifest exceeds supported size")
    if len(data) != size or os.fstat(stream.fileno()).st_size != size:
        raise StoreError("Per-trial manifest changed during reading")
    return data


def _safe_name(name):
    if (not name or "\\" in name or "\x00" in name or
            name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")) or
            str(PurePosixPath(name)) != name):
        raise StoreError("Unsafe or noncanonical archive member")
    return name


def inspect_archive(stream, image_id):
    """Validate an uncompressed OCI tar and produce a deterministic exact recipe."""
    if not image_id.startswith("sha256:") or not HEX.fullmatch(image_id[7:]):
        raise StoreError("An exact sha256 Docker image identity is required")
    size = os.fstat(stream.fileno()).st_size
    if size % 512:
        raise StoreError("Archive is not a block-aligned uncompressed tar")
    stream.seek(0)
    members, segments, by_name = [], [], {}
    cursor, last_end = 0, 0

    def segment(offset, length):
        if length:
            segments.append({"offset": offset, "size": length,
                             "sha256": _range_hash(stream, offset, length)})

    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            for member in archive:
                name = _safe_name(member.name.rstrip("/") if member.isdir() else member.name)
                if name in by_name or not (member.isreg() or member.isdir()):
                    raise StoreError("Duplicate, link, or special archive member")
                if member.sparse is not None or any("sparse" in k.lower() for k in member.pax_headers):
                    raise StoreError("Sparse archive members are unsupported")
                if member.isdir() and member.size:
                    raise StoreError("Directory has a payload")
                if member.offset_data < cursor or member.offset_data + member.size > size:
                    raise StoreError("Overlapping or truncated archive member")
                row = {"name": name, "kind": "directory" if member.isdir() else "file",
                       "offset": member.offset_data, "size": member.size}
                if member.isreg():
                    row["sha256"] = _range_hash(stream, member.offset_data, member.size)
                    if name.startswith("blobs/") and name != "blobs/sha256/" + row["sha256"]:
                        raise StoreError("Blob filename does not match sha256 content")
                    segment(cursor, member.offset_data - cursor)
                    segment(member.offset_data, member.size)
                    cursor = member.offset_data + member.size
                by_name[name] = row
                members.append(row)
                last_end = max(last_end, member.offset_data + ((member.size + 511)//512)*512)
    except (tarfile.TarError, OSError) as exc:
        raise StoreError("Invalid uncompressed tar") from exc
    if size - last_end < 1024 or any(any(block) for block in _parts(stream, last_end, size-last_end)):
        raise StoreError("Missing end marker or nonzero hidden trailing archive data")
    segment(cursor, size-cursor)

    def document(name):
        row = by_name.get(name)
        if not row or row["kind"] != "file" or row["size"] > JSON_LIMIT:
            raise StoreError("Missing or oversized OCI JSON document")
        result = _json_bytes(b"".join(_parts(stream, row["offset"], row["size"])))
        if not isinstance(result, dict):
            raise StoreError("OCI JSON document must be an object")
        return result

    if document("oci-layout").get("imageLayoutVersion") != "1.0.0":
        raise StoreError("Unsupported OCI layout version")
    configs, checked = set(), set()

    def descriptor(desc, kind, ancestry=()):
        if not isinstance(desc, dict):
            raise StoreError("Invalid OCI descriptor")
        digest, media = desc.get("digest", ""), desc.get("mediaType", "")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not HEX.fullmatch(digest[7:]):
            raise StoreError("Only sha256 OCI descriptors are supported")
        row = by_name.get("blobs/sha256/" + digest[7:])
        if (not row or row["kind"] != "file" or type(desc.get("size")) is not int or
                desc["size"] != row["size"] or row["sha256"] != digest[7:]):
            raise StoreError("Missing or changed descriptor blob")
        if digest in ancestry or len(ancestry) >= 32:
            raise StoreError("Recursive or excessive OCI index nesting")
        if kind == "config":
            if media not in CONFIG_TYPES:
                raise StoreError("Unsupported config media type")
            document(row["name"])
            configs.add(digest)
        elif kind == "image":
            if media not in INDEX_TYPES | MANIFEST_TYPES:
                raise StoreError("Unsupported image descriptor type")
            if digest not in checked:
                image_document(document(row["name"]), media in INDEX_TYPES, ancestry + (digest,))
                checked.add(digest)
        elif not isinstance(media, str) or not media:
            raise StoreError("Missing layer media type")

    def image_document(doc, is_index, ancestry=()):
        if doc.get("schemaVersion") != 2:
            raise StoreError("Unsupported OCI schema")
        if is_index:
            children = doc.get("manifests")
            if not isinstance(children, list) or not children:
                raise StoreError("Empty or invalid OCI index")
            for child in children:
                descriptor(child, "image", ancestry)
        else:
            descriptor(doc.get("config"), "config", ancestry)
            layers = doc.get("layers")
            if not isinstance(layers, list):
                raise StoreError("Invalid layer list")
            for layer in layers:
                descriptor(layer, "layer", ancestry)

    image_document(document("index.json"), True)
    if image_id not in configs | checked:
        raise StoreError("Original image identity is absent from verified OCI configs/manifests")
    return {"archive_sha256": _range_hash(stream, 0, size), "archive_size": size,
            "image_id": image_id, "image_config_digests": sorted(configs),
            "image_identity_kind": "config_digest" if image_id in configs else "oci_manifest_or_index_digest",
            "members": members, "segments": segments}


@contextmanager
def _locked_store(root):
    root = Path(root)
    if root.is_symlink():
        raise StoreError("Store root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise StoreError("Store root is not a directory")
    owner = root/"ownership.json"
    if not owner.exists():
        if any(root.iterdir()):
            raise StoreError("Refusing to adopt a nonempty unowned directory")
        with owner.open("xb") as f:
            f.write(OWNER)
    with _open_regular(owner) as f:
        if os.fstat(f.fileno()).st_size != len(OWNER) or f.read(len(OWNER) + 1) != OWNER:
            raise StoreError("Store ownership marker mismatch")
    fd = os.open(root/".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise StoreError("Invalid store lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ("objects", "manifests"):
            target = root/name
            if target.is_symlink():
                raise StoreError("Store subdirectory is a symlink")
            target.mkdir(exist_ok=True, mode=0o700)
        yield root


def _inventory(root):
    logical, allocated = 0, 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(parent)/name).is_symlink():
                raise StoreError("Symlink within store")
        for name in files:
            path = Path(parent)/name
            _regular(path)
            info = path.stat()
            logical += info.st_size
            allocated += max(info.st_size, info.st_blocks * 512)
    return {"logical_bytes": logical, "allocated_file_bytes": allocated}


def _capacity(root, additional, quota_bytes, min_free_bytes):
    if (type(quota_bytes) is not int or type(min_free_bytes) is not int or
            quota_bytes < 0 or min_free_bytes < 0):
        raise StoreError("Nonnegative integer quota/headroom required")
    used = _inventory(root)
    if used["allocated_file_bytes"] + additional > quota_bytes:
        raise StoreError("Store quota would be exceeded")
    if shutil.disk_usage(root).free < additional + min_free_bytes:
        raise StoreError("Insufficient physical disk headroom")
    return used


def _verified_object(path, digest, size):
    try:
        with _open_regular(path) as f:
            if os.fstat(f.fileno()).st_size != size or _range_hash(f, 0, size) != digest:
                raise StoreError("Missing or changed content-addressed object")
    except OSError as exc:
        raise StoreError("Missing or unreadable content-addressed object") from exc


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path, blocks, expected_hash=None):
    """Atomic no-overwrite publication; interrupted temps remain quota-accounted."""
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            h = hashlib.sha256()
            for block in blocks:
                f.write(block)
                h.update(block)
            f.flush()
            os.fsync(f.fileno())
        if expected_hash is not None and h.hexdigest() != expected_hash:
            raise StoreError("Source changed before immutable publication")
        os.chmod(temp, 0o444)
        os.link(temp, path)  # Fails rather than replacing existing evidence.
        _sync_directory(path.parent)
    finally:
        os.unlink(temp)


def ingest_archive(archive_path, store_path, trial_id, image_id, *, quota_bytes, min_free_bytes=0):
    """Preserve an immutable per-trial recipe, retaining the untouched source tar."""
    if not TRIAL.fullmatch(trial_id):
        raise StoreError("Unsafe trial identifier")
    with _open_regular(archive_path) as source:
        original_stat = os.fstat(source.fileno())
        recipe = inspect_archive(source, image_id)
        manifest = {"schema_version": 1, "format": "capstudy-oci-exact-archive-v1",
                    "evidence_kind": "storage_prototype_not_regrade_validation",
                    "trial_id": trial_id, **recipe}
        manifest["manifest_sha256"] = _digest(_canonical(manifest))
        encoded = _canonical(manifest)
        if len(encoded) > JSON_LIMIT:
            raise StoreError("Per-trial manifest exceeds supported size")
        with _locked_store(store_path) as root:
            manifest_path = root/"manifests"/(trial_id + ".json")
            if manifest_path.exists() or manifest_path.is_symlink():
                with _open_regular(manifest_path) as f:
                    if _bounded_manifest_bytes(f) != encoded:
                        raise StoreError("Immutable trial identity already binds different content")
            missing = {}
            for row in recipe["segments"]:
                obj = root/"objects"/row["sha256"]
                if obj.exists() or obj.is_symlink():
                    _verified_object(obj, row["sha256"], row["size"])
                else:
                    missing[row["sha256"]] = row
            block_size = max(4096, os.statvfs(root).f_frsize)
            allocation = lambda size: ((size + block_size - 1)//block_size)*block_size
            additional = sum(allocation(row["size"]) for row in missing.values())
            if not manifest_path.exists():
                additional += allocation(len(encoded))
            used = _capacity(root, additional, quota_bytes, min_free_bytes)
            for digest, row in missing.items():
                obj = root/"objects"/digest
                _publish(obj, _parts(source, row["offset"], row["size"]), digest)
                _verified_object(obj, digest, row["size"])
            final_stat = os.fstat(source.fileno())
            if (original_stat.st_size, original_stat.st_mtime_ns, original_stat.st_ctime_ns) != (
                    final_stat.st_size, final_stat.st_mtime_ns, final_stat.st_ctime_ns):
                raise StoreError("Source archive changed during ingestion")
            if _range_hash(source, 0, recipe["archive_size"]) != recipe["archive_sha256"]:
                raise StoreError("Source archive changed during ingestion")
            if not manifest_path.exists():
                _publish(manifest_path, [encoded])
            return {"manifest": manifest, "new_content_objects": len(missing),
                    "store_usage_before": used, "store_usage_after": _inventory(root),
                    "projected_additional_allocated_file_bytes": additional,
                    "quota_bytes": quota_bytes,
                    "original_preserved": True, "scale_gate_passed": False}


def reconstruct_archive(store_path, trial_id, destination, *, min_free_bytes=0):
    """Verify all objects, reconstruct exact bytes, then revalidate OCI identity."""
    if not TRIAL.fullmatch(trial_id):
        raise StoreError("Unsafe trial identifier")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise StoreError("Refusing to overwrite destination")
    with _locked_store(store_path) as root:
        with _open_regular(root/"manifests"/(trial_id + ".json")) as f:
            manifest = _json_bytes(_bounded_manifest_bytes(f))
        if not isinstance(manifest, dict):
            raise StoreError("Invalid stored manifest")
        unsealed = {k:v for k,v in manifest.items() if k != "manifest_sha256"}
        if (manifest.get("manifest_sha256") != _digest(_canonical(unsealed)) or
                manifest.get("format") != "capstudy-oci-exact-archive-v1" or
                manifest.get("schema_version") != 1 or
                manifest.get("trial_id") != trial_id):
            raise StoreError("Manifest identity/seal mismatch")
        if (not isinstance(manifest.get("segments"), list) or
                type(manifest.get("archive_size")) is not int or manifest["archive_size"] <= 0 or
                not isinstance(manifest.get("image_id"), str)):
            raise StoreError("Invalid manifest schema")
        cursor = 0
        for row in manifest["segments"]:
            if (not isinstance(row, dict) or not isinstance(row.get("sha256"), str) or
                    not HEX.fullmatch(row["sha256"]) or type(row.get("size")) is not int or
                    row["size"] <= 0 or type(row.get("offset")) is not int or row["offset"] != cursor):
                raise StoreError("Invalid archive segment recipe")
            _verified_object(root/"objects"/row["sha256"], row["sha256"], row["size"])
            cursor += row["size"]
        if cursor != manifest["archive_size"]:
            raise StoreError("Archive size mismatch")
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise StoreError("Nonnegative integer headroom required")
        if not destination.parent.is_dir() or destination.parent.is_symlink():
            raise StoreError("Existing nonsymlink destination directory required")
        if root.resolve() == destination.parent.resolve() or root.resolve() in destination.parent.resolve().parents:
            raise StoreError("Reconstruction destination must be outside the CAS store")
        if shutil.disk_usage(destination.parent).free < cursor + min_free_bytes:
            raise StoreError("Insufficient reconstruction disk headroom")
        fd, temp = tempfile.mkstemp(prefix=".oci-reconstruct-", dir=destination.parent)
        try:
            with os.fdopen(fd, "w+b") as out:
                for row in manifest["segments"]:
                    with _open_regular(root/"objects"/row["sha256"]) as source:
                        for block in _parts(source, 0, row["size"]):
                            out.write(block)
                out.flush()
                os.fsync(out.fileno())
                recipe = inspect_archive(out, manifest["image_id"])
                if any(recipe[k] != manifest[k] for k in recipe):
                    raise StoreError("Reconstructed archive or OCI identity mismatch")
            os.chmod(temp, 0o444)
            os.link(temp, destination)
            _sync_directory(destination.parent)
        finally:
            os.unlink(temp)
        return {"archive_sha256": manifest["archive_sha256"], "archive_size": cursor,
                "byte_exact": True, "native_regrade_executed": False, "scale_gate_passed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("archive", type=Path)
    ingest.add_argument("store", type=Path)
    ingest.add_argument("trial_id")
    ingest.add_argument("image_id")
    ingest.add_argument("--quota-bytes", type=int, required=True)
    ingest.add_argument("--min-free-bytes", type=int, required=True)
    restore = sub.add_parser("reconstruct")
    restore.add_argument("store", type=Path)
    restore.add_argument("trial_id")
    restore.add_argument("destination", type=Path)
    restore.add_argument("--min-free-bytes", type=int, required=True)
    args = parser.parse_args()
    if args.command == "ingest":
        result = ingest_archive(args.archive, args.store, args.trial_id, args.image_id,
                                quota_bytes=args.quota_bytes, min_free_bytes=args.min_free_bytes)
    else:
        result = reconstruct_archive(args.store, args.trial_id, args.destination,
                                     min_free_bytes=args.min_free_bytes)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
