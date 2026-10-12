"""Offline lock/artifact fixtures exercise the audit's approval boundary."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dependency_licenses", ROOT / "scripts/dependency-licenses.py"
)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache = root / "cache"
        (root / "docs").mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname="app"\nversion="1"\nlicense="MIT"\nlicense-files=["LICENSE"]\n'
        )
        (root / "LICENSE").write_text("MIT notice\n")
        self.version = "1.0"
        self.expression = "MIT"
        self.notice = b"MIT fixture notice"
        self.platform = "any"
        self.extra_package = ""
        self.marker = ""
        self.write_lock()
        self.approve()

    def write_lock(self) -> None:
        filename = f"fixture-{self.version}-py3-none-{self.platform}.whl"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("fixture.dist-info/licenses/LICENSE", self.notice)
            archive.writestr(
                "fixture.dist-info/METADATA",
                f"Name: fixture\nVersion: {self.version}\n"
                f"License-Expression: {self.expression}\nLicense-File: LICENSE\n",
            )
            # Collected even though metadata declares only one License-File.
            archive.writestr("fixture/vendor/NOTICE", b"Bundled MIT notice")
        artifact = self.root / filename
        artifact.write_bytes(stream.getvalue())
        lock = f'''version = 1
[[package]]
name = "app"
version = "1"
source = {{ virtual = "." }}
dependencies = [{{ name = "fixture"{self.marker} }}]
[package.dev-dependencies]
dev = [{{ name = "fixture" }}]
[[package]]
name = "fixture"
version = "{self.version}"
source = {{ registry = "https://example.invalid" }}
wheels = [{{ url = "{artifact.as_uri()}", hash = "sha256:{AUDIT.digest(stream.getvalue())}" }}]
{self.extra_package}
'''
        (self.root / "uv.lock").write_text(lock)

    def approve(self, choices: list[str] | None = None, exception: bool = False) -> None:
        self.facts = AUDIT.collect(self.root, self.cache)
        reviews = []
        for record in self.facts["packages"]:
            review = {
                "name": record["name"],
                "version": record["version"],
                "expression": self.expression if record["name"] == "fixture" else "MIT",
                "fingerprint": AUDIT.fingerprint(record),
                "renewal_fingerprint": AUDIT.renewal_fingerprint(record),
                "or_choices": choices or [],
            }
            if exception and record["name"] == "fixture":
                review.update(exception=True, owner_approval="fixture owner decision")
            reviews.append(review)
        self.policy: dict[str, Any] = {
            "policy": {"allowlist": ["MIT", "BSD-3-Clause"], "next_review": "2099-01-01"},
            "review": reviews,
        }
        self.write_policy()

    def write_policy(self) -> None:
        text = "<!-- generated-inventory -->\n<!-- /generated-inventory -->\n"
        text += "<!-- license-decisions -->\n```toml\n[policy]\n"
        text += 'allowlist = ["MIT", "BSD-3-Clause"]\nnext_review = "2099-01-01"\n'
        for review in self.policy["review"]:
            text += "\n[[review]]\n"
            for key, value in review.items():
                text += f"{key} = {json.dumps(value)}\n"
        text += "```\n"
        (self.root / "docs/dependency-licenses.md").write_text(text)

    def update(self) -> list[str]:
        return list(AUDIT.audit(self.root, self.cache, True))

    def check(self) -> list[str]:
        return list(AUDIT.audit(self.root, self.cache))


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_current_evidence_and_cli_modes(fixture: Fixture) -> None:
    assert fixture.update() == []
    assert fixture.check() == []
    before = (fixture.root / "docs/dependency-licenses.md").read_bytes()
    for mode in ("--check", "--update"):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/dependency-licenses.py"),
                mode,
                "--root",
                str(fixture.root),
                "--cache",
                str(fixture.cache),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert (fixture.root / "docs/dependency-licenses.md").read_bytes() == before


def test_new_package_is_stale_and_requires_review(fixture: Fixture) -> None:
    assert fixture.update() == []
    fixture.extra_package = (
        '\n[[package]]\nname="new"\nversion="1"\nsource={registry="https://example.invalid"}\n'
    )
    fixture.write_lock()
    errors = fixture.check()
    assert any("stale" in e for e in errors)
    assert any("review required: new@" in e for e in errors)
    assert any("review required: new@" in e for e in fixture.update())


def test_notice_change_on_same_spdx_label_needs_review(fixture: Fixture) -> None:
    assert fixture.update() == []
    fixture.notice = b"Changed MIT notice"
    fixture.write_lock()
    assert any("review required: fixture@" in e for e in fixture.update())
    assert any("review required: fixture@" in e for e in fixture.check())


def test_or_with_one_permissive_operand_needs_recorded_choice(fixture: Fixture) -> None:
    fixture.expression = "MIT OR GPL-3.0-only"
    fixture.write_lock()
    fixture.approve()
    assert any("review required: fixture@" in e for e in fixture.update())
    fixture.approve(choices=["GPL-3.0-only"])
    assert fixture.update()
    fixture.approve(choices=["MIT"])
    assert fixture.update() == []


@pytest.mark.parametrize(
    "expression", ["LicenseRef-Private", "Unknown-License", "MIT WITH Unknown-exception"]
)
def test_unknown_reference_and_with_require_review(fixture: Fixture, expression: str) -> None:
    fixture.expression = expression
    fixture.write_lock()
    fixture.approve()
    assert any("review required: fixture@" in e for e in fixture.update())


def test_exception_covers_exact_version_and_digest(fixture: Fixture) -> None:
    fixture.expression = "MPL-2.0"
    fixture.write_lock()
    fixture.approve(exception=True)
    assert fixture.update() == []
    fixture.version = "1.1"
    fixture.write_lock()
    assert fixture.update()
    fixture.approve(exception=True)
    assert fixture.update() == []
    fixture.notice += b" change"
    fixture.write_lock()
    assert fixture.update()


def test_permissive_version_only_renewal(fixture: Fixture) -> None:
    assert fixture.update() == []
    fixture.version = "1.1"
    fixture.write_lock()
    assert fixture.update() == []
    fixture.marker = ", marker = \"sys_platform == 'win32'\""
    fixture.write_lock()
    assert fixture.update()


def test_outside_matrix_cannot_be_presented_as_audited(fixture: Fixture) -> None:
    fixture.platform = "macosx_11_0_arm64"
    fixture.write_lock()
    fixture.approve()
    assert fixture.update()
    path = fixture.root / "docs/dependency-license-facts.json"
    facts = json.loads(path.read_text())
    foreign = next(p for p in facts["packages"] if p["name"] == "fixture")
    assert foreign["artifacts"][0]["inspection"] == "not inspected (outside matrix)"
    foreign["artifacts"][0].update(
        inspection="inspected", notices=[{"path": "LICENSE", "sha256": "fake"}]
    )
    path.write_bytes(AUDIT.canonical(facts))
    assert any("stale" in e for e in fixture.check())


def test_archive_hash_mismatch_blocks_collection(fixture: Fixture) -> None:
    lock = fixture.root / "uv.lock"
    lock.write_text(lock.read_text().replace("sha256:", "sha256:" + "0" * 64 + "#"))
    with pytest.raises(ValueError, match="locked SHA256"):
        fixture.check()


def test_cache_is_rehashed(fixture: Fixture) -> None:
    for path in fixture.cache.iterdir():
        path.write_bytes(b"corrupted cache")
    with pytest.raises(ValueError, match="cached artifact hash mismatch"):
        fixture.check()


def test_notice_collection_ignores_declared_count_and_checks_missing_files(
    fixture: Fixture,
) -> None:
    record = next(p for p in fixture.facts["packages"] if p["name"] == "fixture")
    paths = {n["path"] for n in record["artifacts"][0]["notices"]}
    assert paths == {"fixture.dist-info/licenses/LICENSE", "fixture/vendor/NOTICE"}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("demo.dist-info/METADATA", "Name: demo\nLicense-File: unusual.txt\n")
        archive.writestr("demo.dist-info/licenses/unusual.txt", "MIT")
    inspected = AUDIT.inspect_archive(stream.getvalue(), "demo.whl")
    assert inspected["missing_license_files"] == []
    assert inspected["notices"][0]["path"].endswith("unusual.txt")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("demo.dist-info/METADATA", "License-File: absent\n")
    assert AUDIT.inspect_archive(stream.getvalue(), "demo.whl")["missing_license_files"] == [
        "absent"
    ]


def test_sdist_notices_and_traversal_are_not_extracted(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        content = b"MIT"
        member = tarfile.TarInfo("../../LICENSE")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    inspected = AUDIT.inspect_archive(stream.getvalue(), "demo.tar.gz")
    assert inspected["notices"] == [{"path": "../../LICENSE", "sha256": AUDIT.digest(b"MIT")}]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "filename, selected",
    [
        ("p-1-py3-none-any.whl", True),
        ("p-1-py310-none-any.whl", True),
        ("p-1-py39-none-any.whl", True),
        ("p-1-cp312-cp312-win_amd64.whl", True),
        ("p-1-cp315-cp315t-manylinux_2_28_x86_64.whl", True),
        ("p-1-cp39-abi3-manylinux_2_17_x86_64.whl", True),
        ("p-1-cp314-cp314-musllinux_1_2_x86_64.whl", True),
        ("p-1-cp311-cp311-win_amd64.whl", False),
        ("p-1-pp312-pypy312_pp73-manylinux_2_17_x86_64.whl", False),
        ("p-1-cp312-cp312-win_arm64.whl", False),
        ("p-1-cp312-cp312-macosx_11_0_x86_64.whl", False),
    ],
)
def test_matrix(filename: str, selected: bool) -> None:
    assert AUDIT.wheel_in_matrix(filename) is selected


@pytest.mark.parametrize(
    "expression, choices, expected",
    [
        ("MIT AND BSD-3-Clause", [], True),
        ("MIT OR BSD-3-Clause", [], False),
        ("MIT OR BSD-3-Clause", ["MIT"], True),
        ("(MIT OR GPL-3.0-only) AND BSD-3-Clause", ["MIT"], True),
        ("MIT AND GPL-3.0-only OR BSD-3-Clause", ["MIT"], False),
        ("MIT WITH LLVM-exception", [], False),
    ],
)
def test_spdx_precedence(expression: str, choices: list[str], expected: bool) -> None:
    assert AUDIT.spdx(expression, {"MIT", "BSD-3-Clause"}, set(choices)) is expected


@pytest.mark.parametrize(
    "expression", ["", "MIT GPL", "MIT OR", "(MIT", "MIT WITH", "MIT)", "MIT;GPL"]
)
def test_malformed_spdx(expression: str) -> None:
    with pytest.raises(ValueError):
        AUDIT.spdx(expression, {"MIT"}, set())


def test_reachability_keeps_extras_markers_duplicates_and_both_scopes() -> None:
    packages = [
        {
            "name": "app",
            "version": "1",
            "source": {"virtual": "."},
            "dependencies": [{"name": "demo", "version": "1", "extra": ["feature"]}],
            "dev-dependencies": {"dev": [{"name": "leaf"}]},
        },
        {
            "name": "demo",
            "version": "1",
            "source": {"registry": "a"},
            "optional-dependencies": {
                "feature": [{"name": "leaf", "marker": "sys_platform == 'win32'"}]
            },
        },
        {"name": "demo", "version": "2", "source": {"registry": "b"}},
        {"name": "leaf", "version": "1", "source": {"registry": "a"}},
    ]
    paths = AUDIT.reachability(packages)
    assert paths[AUDIT.package_id(packages[2])] == []
    leaf = paths[AUDIT.package_id(packages[3])]
    assert {p["kind"] for p in leaf} == {"runtime", "dev"}
    assert any(p["path"][-1]["marker"] == "sys_platform == 'win32'" for p in leaf)


def test_all_workspace_projects_carry_root_license() -> None:
    for path in [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]:
        project = tomllib.loads(path.read_text())["project"]
        assert project["license"] == "MIT", project["name"]
        assert project["license-files"] == ["LICENSE"], project["name"]
        assert (path.parent / "LICENSE").read_text() == (ROOT / "LICENSE").read_text()


def test_exception_requires_recorded_version_and_owner_reference(fixture: Fixture) -> None:
    fixture.expression = "MPL-2.0"
    fixture.write_lock()
    fixture.approve(exception=True)
    review = next(r for r in fixture.policy["review"] if r["name"] == "fixture")
    review["version"] = "0.9"
    fixture.write_policy()
    assert fixture.update()
    review["version"] = fixture.version
    review.pop("owner_approval")
    fixture.write_policy()
    assert fixture.update()


def test_modern_expression_conflict_cannot_be_overridden(fixture: Fixture) -> None:
    fixture.expression = "GPL-3.0-only"
    fixture.write_lock()
    fixture.approve()
    review = next(r for r in fixture.policy["review"] if r["name"] == "fixture")
    review["expression"] = "MIT"
    fixture.write_policy()
    assert fixture.update()


@pytest.mark.parametrize("component", ["MPL-2.0", "LicenseRef-Undetermined-Data"])
def test_nonpermissive_bundled_component_needs_exception(fixture: Fixture, component: str) -> None:
    review = next(r for r in fixture.policy["review"] if r["name"] == "fixture")
    review["components"] = [component]
    fixture.write_policy()
    assert fixture.update()
    review.update(exception=True, owner_approval="fixture owner decision")
    fixture.write_policy()
    assert fixture.update() == []


def test_lock_and_local_license_line_endings_do_not_change_facts(fixture: Fixture) -> None:
    assert fixture.update() == []
    for filename in ("uv.lock", "LICENSE"):
        path = fixture.root / filename
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        assert b"\r\r\n" not in path.read_bytes()
    assert fixture.check() == []


def test_due_review_blocks_check(fixture: Fixture) -> None:
    assert fixture.update() == []
    path = fixture.root / "docs/dependency-licenses.md"
    path.write_text(path.read_text().replace("2099-01-01", "2000-01-01"))
    assert fixture.check() == ["full dependency license review is due"]


def test_vendor_notice_cannot_substitute_for_declared_package_notice() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("demo.dist-info/METADATA", "License-File: LICENSE\n")
        archive.writestr("demo/vendor/LICENSE", "MIT")
    inspected = AUDIT.inspect_archive(stream.getvalue(), "demo.whl")
    assert inspected["missing_license_files"] == ["LICENSE"]
