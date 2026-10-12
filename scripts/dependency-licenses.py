#!/usr/bin/env python3
"""Collect lock-pinned license evidence; decisions live in dependency-licenses.md.

Only the standard library is used. Archives are inspected in memory, never extracted.
--update changes generated facts only; it cannot approve evidence.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import email.parser
import hashlib
import io
import json
import re
import sys
import tarfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX = {
    "python": "CPython >= 3.12 (including free-threaded ABIs represented in uv.lock)",
    "os": ["Linux x86-64 (manylinux and musllinux)", "Windows x86-64"],
    "artifacts": "all sdists and every compatible locked wheel; no environment sampling",
}
NOTICE = re.compile(r"^(licen[sc]e|copying|notice|copyright)([._-].*|$)", re.I)
TOKENS = re.compile(r"\s*(\(|\)|AND\b|OR\b|WITH\b|[A-Za-z0-9][A-Za-z0-9.+:-]*)")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def spdx(expression: str, allow: set[str], choices: set[str]) -> bool:
    """Parse SPDX precedence; an OR needs an explicitly recorded selected operand.

    Unknown identifiers, LicenseRef and WITH are fail-closed. We currently allow no
    SPDX exceptions (WITH), even when the base license is permissive.
    """
    tokens: list[str] = []
    pos = 0
    while pos < len(expression.rstrip()):
        match = TOKENS.match(expression, pos)
        if not match:
            raise ValueError("invalid SPDX expression")
        tokens.append(match[1])
        pos = match.end()
    index = 0

    def atom() -> tuple[bool, str]:
        nonlocal index
        if index >= len(tokens):
            raise ValueError("missing SPDX operand")
        token = tokens[index]
        index += 1
        if token == "(":
            result, text = either()
            if index >= len(tokens) or tokens[index] != ")":
                raise ValueError("unclosed SPDX group")
            index += 1
            return result, f"({text})"
        if token in {"AND", "OR", "WITH", ")"}:
            raise ValueError("invalid SPDX operand")
        result = token in allow and not token.startswith(("LicenseRef-", "DocumentRef-"))
        if index < len(tokens) and tokens[index] == "WITH":
            index += 1
            if index >= len(tokens) or tokens[index] in {"AND", "OR", "WITH", "(", ")"}:
                raise ValueError("missing SPDX exception")
            token += " WITH " + tokens[index]
            index += 1
            result = False
        return result, token

    def both() -> tuple[bool, str]:
        nonlocal index
        result, text = atom()
        while index < len(tokens) and tokens[index] == "AND":
            index += 1
            right, label = atom()
            result = result and right
            text += " AND " + label
        return result, text

    def either() -> tuple[bool, str]:
        nonlocal index
        alternatives = [both()]
        while index < len(tokens) and tokens[index] == "OR":
            index += 1
            alternatives.append(both())
        if len(alternatives) == 1:
            return alternatives[0]
        return (
            any(ok and label in choices for ok, label in alternatives),
            " OR ".join(label for _, label in alternatives),
        )

    result, _ = either()
    if index != len(tokens) or not tokens:
        raise ValueError("trailing or empty SPDX expression")
    return result


def wheel_in_matrix(filename: str) -> bool:
    try:
        python, abi, platform = filename.removesuffix(".whl").rsplit("-", 3)[1:]
    except ValueError:
        return False
    platforms = platform.split(".")
    supported_os = any(
        tag == "any"
        or tag == "win_amd64"
        or (tag.startswith(("manylinux", "musllinux", "linux_")) and tag.endswith("_x86_64"))
        for tag in platforms
    )
    if not supported_os:
        return False
    for tag in python.split("."):
        if tag == "py3" or (tag.startswith("py3") and tag[2:].isdigit()):
            return abi == "none"
        if tag.startswith("cp") and tag[2:].isdigit():
            version = int(tag[2:])
            if version >= 312 or (version >= 32 and "abi3" in abi.split(".")):
                return True
    return False


def policy_from(document: str) -> dict[str, Any]:
    match = re.search(r"<!-- license-decisions -->\s*```toml\n(.*?)\n```", document, re.S)
    if not match:
        raise ValueError("missing license-decisions TOML block")
    return tomllib.loads(match[1])


def artifact_bytes(artifact: dict[str, Any], cache: Path) -> bytes:
    expected = artifact["hash"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("artifact must have a locked SHA256")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / expected.split(":")[1]
    if path.exists():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(artifact["url"], timeout=60) as response:
            data = response.read()
        if digest(data) != expected.split(":")[1]:
            raise ValueError(f"artifact hash mismatch: {artifact['url']}")
        path.write_bytes(data)
    if digest(data) != expected.split(":")[1]:
        raise ValueError(f"cached artifact hash mismatch: {path}")
    return data


def inspect_archive(data: bytes, filename: str) -> dict[str, Any]:
    """Keep all conventionally named notices, including vendored subdirectories."""
    notices: list[dict[str, str]] = []
    metadata: list[dict[str, Any]] = []

    def inspect(path: str, content: bytes) -> None:
        basename = path.rsplit("/", 1)[-1]
        if basename not in {"METADATA", "PKG-INFO"}:
            notices.append({"path": path, "sha256": digest(content)})
        if basename in {"METADATA", "PKG-INFO"}:
            message = email.parser.BytesParser().parsebytes(content)
            metadata.append(
                {
                    "path": path,
                    "name": message.get("Name"),
                    "version": message.get("Version"),
                    "license_expression": message.get("License-Expression"),
                    "license": message.get("License"),
                    "classifiers": sorted(message.get_all("Classifier", [])),
                    "license_files": sorted(message.get_all("License-File", [])),
                    "requires_dist": sorted(message.get_all("Requires-Dist", [])),
                    "requires_python": message.get("Requires-Python"),
                }
            )

    def scan(names: list[str], read: Any) -> None:
        for path in names:
            if path.rsplit("/", 1)[-1] in {"METADATA", "PKG-INFO"}:
                inspect(path, read(path))
        declared: dict[str, set[str]] = {}
        for record in metadata:
            directory = record["path"].rsplit("/", 1)[0]
            for entry in record["license_files"]:
                if filename.endswith(".whl"):
                    # PEP 639 and the earlier setuptools dist-info convention.
                    candidates = {f"{directory}/licenses/{entry}", f"{directory}/{entry}"}
                else:
                    candidates = {f"{record['path'].split('/')[0]}/{entry}"}
                declared.setdefault(entry, set()).update(candidates)
        selected = set().union(*declared.values()) if declared else set()
        for path in names:
            if NOTICE.match(path.rsplit("/", 1)[-1]) or path in selected:
                inspect(path, read(path))
        found = {n["path"] for n in notices}
        missing.extend(
            entry for entry, candidates in sorted(declared.items()) if not found & candidates
        )

    missing: list[str] = []
    if filename.endswith((".whl", ".zip")):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            scan([item.filename for item in archive.infolist() if not item.is_dir()], archive.read)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:

            def read_member(path: str) -> bytes:
                stream = archive.extractfile(path)
                if stream is None:
                    raise ValueError(f"missing archive member: {path}")
                return stream.read()

            scan([member.name for member in archive.getmembers() if member.isfile()], read_member)
    return {
        "notices": sorted(notices, key=lambda x: x["path"]),
        "metadata": sorted(metadata, key=lambda x: x["path"]),
        "missing_license_files": missing,
    }


def package_id(package: dict[str, Any]) -> str:
    return f"{package['name']}@{package['version']}:{json.dumps(package['source'], sort_keys=True)}"


def reachability(packages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Retain every simple path, with edge markers and activated extras.

    Markers are kept conservatively instead of evaluated on the collecting host.
    Duplicate name/version/source entries remain distinct graph nodes.
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        by_name.setdefault(package["name"], []).append(package)
    paths: dict[str, list[dict[str, Any]]] = {package_id(p): [] for p in packages}

    def visit(edge: dict[str, Any], kind: str, trail: list[dict[str, Any]]) -> None:
        for target in by_name.get(edge["name"], []):
            if edge.get("version", target["version"]) != target["version"]:
                continue
            if edge.get("source", target["source"]) != target["source"]:
                continue
            key = package_id(target)
            if any(item["package"] == key for item in trail):
                continue
            path = [
                *trail,
                {"package": key, "marker": edge.get("marker"), "extras": edge.get("extra", [])},
            ]
            record = {"kind": kind, "path": path}
            if record not in paths[key]:
                paths[key].append(record)
            children = list(target.get("dependencies", []))
            for extra in edge.get("extra", []):
                children.extend(target.get("optional-dependencies", {}).get(extra, []))
            for child in children:
                visit(child, kind, path)

    for package in packages:
        if "editable" in package["source"] or "virtual" in package["source"]:
            visit(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "source": package["source"],
                },
                "runtime",
                [],
            )
            for child in package.get("dev-dependencies", {}).get("dev", []):
                visit(
                    child, "dev", [{"package": package_id(package), "marker": None, "extras": []}]
                )
    return paths


def collect(root: Path, cache: Path) -> dict[str, Any]:
    raw = (root / "uv.lock").read_bytes().replace(b"\r\n", b"\n")
    lock = tomllib.loads(raw.decode())
    paths = reachability(lock["package"])

    def collect_package(package: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": package_id(package),
            "name": package["name"],
            "version": package["version"],
            "source": package["source"],
            "resolution_markers": package.get("resolution-markers", []),
            "dependencies": package.get("dependencies", []),
            "optional_dependencies": package.get("optional-dependencies", {}),
            "dev_dependencies": package.get("dev-dependencies", {}),
            "reachability": paths[package_id(package)],
            "artifacts": [],
        }
        source = package["source"]
        local = source.get("editable", source.get("virtual"))
        if local is not None:
            home = root / local
            project = tomllib.loads((home / "pyproject.toml").read_text())["project"]
            notices = [
                {
                    "path": (Path(local) / p).as_posix(),
                    "sha256": digest((home / p).read_text().replace("\r\n", "\n").encode()),
                }
                for p in project.get("license-files", [])
            ]
            record["artifacts"].append(
                {
                    "filename": local,
                    "inspection": "workspace source",
                    "notices": notices,
                    "metadata": [project],
                }
            )
        else:
            artifacts = ([package["sdist"]] if "sdist" in package else []) + package.get(
                "wheels", []
            )
            for artifact in artifacts:
                filename = urllib.parse.unquote(
                    urllib.parse.urlparse(artifact["url"]).path.rsplit("/", 1)[-1]
                )
                selected = not filename.endswith(".whl") or wheel_in_matrix(filename)
                evidence = {
                    "filename": filename,
                    "url": artifact["url"],
                    "sha256": artifact["hash"].removeprefix("sha256:"),
                    "inspection": "inspected" if selected else "not inspected (outside matrix)",
                }
                if selected:
                    evidence.update(inspect_archive(artifact_bytes(artifact, cache), filename))
                record["artifacts"].append(evidence)
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(collect_package, lock["package"]))
    return {
        "schema": 1,
        "lock_sha256": digest(raw),
        "matrix": MATRIX,
        "packages": sorted(records, key=lambda x: x["id"]),
    }


def fingerprint(record: dict[str, Any]) -> str:
    return digest(canonical(record))


def renewal_fingerprint(record: dict[str, Any]) -> str:
    """A permissive version bump can inherit review only if policy evidence is identical.

    Remove archive bytes/version, keeping variant tags, metadata, graph and notices.
    An unchanged version still requires its exact fingerprint.
    """
    normalized = json.loads(canonical(record))
    version = record["version"]
    normalized.pop("id")
    normalized.pop("version")
    for artifact in normalized["artifacts"]:
        artifact.pop("sha256", None)
        artifact.pop("url", None)
        artifact["filename"] = artifact["filename"].replace(version, "<version>")
        for notice in artifact.get("notices", []):
            notice["path"] = notice["path"].replace(version, "<version>")
        for metadata in artifact.get("metadata", []):
            metadata.pop("version", None)
            if "path" in metadata:
                metadata["path"] = metadata["path"].replace(version, "<version>")
    # Only this package's version is normalized; dependency changes still need review.
    for path in normalized["reachability"]:
        for step in path["path"]:
            if step["package"] == record["id"]:
                step["package"] = step["package"].replace("@" + version + ":", "@<version>:")
    return digest(canonical(normalized))


def statuses(facts: dict[str, Any], policy: dict[str, Any]) -> dict[str, str]:
    allow = set(policy["policy"]["allowlist"])
    result: dict[str, str] = {}
    for record in facts["packages"]:
        state = "review required"
        for review in policy.get("review", []):
            if review["name"] != record["name"]:
                continue
            exact = review.get("version") == record["version"] and review.get(
                "fingerprint"
            ) == fingerprint(record)
            renewed = review.get("version") != record["version"] and review.get(
                "renewal_fingerprint"
            ) == renewal_fingerprint(record)
            if not (exact or renewed):
                continue
            inspected = [
                a
                for a in record["artifacts"]
                if a["inspection"] in {"inspected", "workspace source"}
            ]
            if not inspected or any(
                not a.get("notices") or not a.get("metadata") or a.get("missing_license_files")
                for a in inspected
            ):
                continue
            expression = review["expression"]
            # A modern artifact's expression must agree with the reviewed interpretation.
            expressions = {
                m.get("license_expression", m.get("license"))
                for a in inspected
                for m in a["metadata"]
                if m.get("license_expression") or a["inspection"] == "workspace source"
            }
            if expressions and expressions != {expression}:
                continue
            try:
                permissive = spdx(expression, allow, set(review.get("or_choices", [])))
            except ValueError:
                continue
            for component in review.get("components", []):
                try:
                    permissive = (
                        spdx(component, allow, set(review.get("or_choices", []))) and permissive
                    )
                except ValueError:
                    permissive = False
            if permissive:
                state = "permissive"
            elif exact and review.get("exception") and review.get("owner_approval"):
                state = "approved exception"
            break
        result[record["id"]] = state
    return result


def render_inventory(facts: dict[str, Any], states: dict[str, str]) -> str:
    lines = [
        "<!-- generated-inventory -->",
        f"Lock SHA256: `{facts['lock_sha256']}`. Artifact and notice digests, metadata,",
        "markers, and every runtime/dev graph path are in",
        "[dependency-license-facts.json](dependency-license-facts.json).",
        "",
        "| Package | Version | Reachability | Inspected / locked artifacts | State |",
        "|---|---|---|---|---|",
    ]
    for record in facts["packages"]:
        kinds = ", ".join(sorted({p["kind"] for p in record["reachability"]})) or "unreachable"
        count = sum(
            a["inspection"] != "not inspected (outside matrix)" for a in record["artifacts"]
        )
        lines.append(
            f"| {record['name']} | {record['version']} | {kinds} | "
            f"{count} / {len(record['artifacts'])} | {states[record['id']]} |"
        )
    lines.append("<!-- /generated-inventory -->")
    return "\n".join(lines)


def audit(root: Path, cache: Path, update: bool = False) -> list[str]:
    document_path = root / "docs/dependency-licenses.md"
    document = document_path.read_text(encoding="utf-8")
    policy = policy_from(document)
    facts = collect(root, cache)
    states = statuses(facts, policy)
    replacement = render_inventory(facts, states)
    generated = re.sub(
        r"<!-- generated-inventory -->.*?<!-- /generated-inventory -->",
        lambda _: replacement,
        document,
        flags=re.S,
    )
    if generated == document and "<!-- generated-inventory -->" not in document:
        raise ValueError("missing generated inventory block")
    facts_path = root / "docs/dependency-license-facts.json"
    encoded = canonical(facts)
    errors = []
    if update:
        facts_path.write_bytes(encoded)
        document_path.write_text(generated, encoding="utf-8", newline="\n")
    elif (
        not facts_path.exists()
        or facts_path.read_bytes().replace(b"\r\n", b"\n") != encoded
        or document != generated
    ):
        errors.append("generated license facts are stale; run --update and review changes")
    for key, state in states.items():
        if state == "review required":
            errors.append(f"review required: {key}")
    if date.fromisoformat(policy["policy"]["next_review"]) <= date.today():
        errors.append("full dependency license review is due")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache/dependency-licenses")
    args = parser.parse_args()
    try:
        errors = audit(args.root, args.cache, args.update)
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"license audit failed: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print("Dependency license evidence is current and reviewed.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
