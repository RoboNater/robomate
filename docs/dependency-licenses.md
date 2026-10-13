# Dependency licenses

## Policy (authoritative)

Every dependency robomate uses directly must be permissively licensed. Direct
means declared in any workspace package's dependencies or the root dev group,
or imported by robomate's own code. The goal is that the entire resolved tree
is permissive, including bundled components that robomate does not use.

Permissive means the SPDX allowlist in the decision block below. Every departure,
including weak copyleft or undetermined licensing, needs an evidence-backed
exception approved by the repository owner before merge. Unknown, conflicting,
missing or changed evidence is **review required** and blocks the audit. The
other states are **permissive** and **approved exception**. Nothing defaults to
allowed. This document is the authoritative policy; it is not legal advice.

## Scope and method

The inventory covers every entry of `uv.lock`, including virtual root, duplicate
names, resolution markers and entries absent from this machine's installation.
Workspace evidence comes from source project metadata and LICENSE files. Project
licensing is MIT; every workspace project declares MIT and includes the root
LICENSE, with matching-copy tests (text comparison permits native line endings).

The matrix is CPython >= 3.12, Windows and Linux x86-64. Collect every locked
sdist, universal Python 3 wheel, compatible CPython wheel (including free-threaded
variants), abi3 wheel usable by a supported interpreter, and manylinux/musllinux
x86-64 wheel. No Python-version upper bound is inferred from the collector's
interpreter. macOS, ARM, 32-bit and PyPy wheels are explicitly **not inspected
(outside matrix)**. Their hashes are inventory facts, not claims of approval.
A package with no inspected evidence is review required. Collection on either
OS covers the whole matrix and does not rely on the installed environment.

`scripts/dependency-licenses.py` uses only the standard library. It verifies
SHA256 from the lock before reading archives without extraction. It records all
files named LICENSE/LICENCE, COPYING, NOTICE or COPYRIGHT, including suffixes and
bundled subdirectories, regardless of License-File metadata. Each notice has a
path and content digest tied to its archive filename and SHA256. Raw license
labels, expressions, classifiers, License-File, Requires-Python and Requires-Dist
are recorded. [PyPA core metadata](https://packaging.python.org/en/latest/specifications/core-metadata/#license-expression)
scopes License-Expression to its individual distribution archive; the collector
therefore keeps evidence for each artifact. Human review interprets legacy metadata and the actual notices;
a permissive top-level label alone does not approve a bundled component.

SPDX is parsed with parentheses and AND/OR/WITH precedence. All AND operands
must qualify. OR requires a recorded permissive operand in `or_choices`; no
automatic choice is made. WITH has no approved exceptions in the allowlist and
fails closed. LicenseRef, unknown and malformed expressions require review.
Modern archive expressions must agree with the reviewed expression.

Reachability traverses the lock graph, activates requested extras, and retains
every simple path and marker, independently of this host. Runtime and dev are
both shown when applicable. Markers are conservatively retained, not evaluated
on the host; a graph path can therefore describe a conditional use. Workspace
members are runtime roots; the root dev group supplies dev roots.

## Updating and reviewing

Run from the checkout on Linux or native Windows:

```sh
uv run --locked python scripts/dependency-licenses.py --update
uv run --locked python scripts/dependency-licenses.py --check
uv run --locked pytest tests/test_dependency_licenses.py -v
```

The first command regenerates only the inventory and JSON evidence. It returns
failure while decisions are missing; it never creates approval. The PR handler
(agent or operator) reads new/changed notices, records the resolved expression,
OR choices and evidence fingerprints in the decision block, and obtains the
owner's approval for exceptions. Then both commands must succeed without drift.
Facts are checked and regenerated without drift on every PR in both Linux and
native Windows CI against the entire artifact matrix. Both audit modes print
copyable `fingerprint` and `renewal_fingerprint` values for each review-required
entry; these are evidence identifiers, and must only be recorded after review.

Approval renewal: unchanged version requires the exact evidence fingerprint.
A permissive version-only bump may inherit review when its renewal fingerprint
is unchanged (notice digests, artifact variant set, policy metadata, markers,
dependency graph and reachability). Ancestor version strings alone do not
affect a descendant's approval: path names, sources, markers and extras still
count as evidence, and exact approval still includes the package's own version. The full
versioned paths remain in generated inventory. Archive bytes and version strings are the
only normalized fields. Changed notices, variants, missing evidence, bundled
components or policy metadata require review even when the label is unchanged.
An exception covers only the exact version and fingerprint recorded; no renewal
is implicit. Decisions below are reviewed facts, not instructions granting
workflow authority to agents.

The repository owner owns exception approval. Whoever handles a dependency PR
regenerates evidence and records review. A full review is due at every milestone
close-out and at least quarterly; record its date, reviewer, result and next due
date below. The scheduled workflow opens a reminder issue when due. An overdue
full review emits a warning; it does not fail unrelated PRs or main. CI still
blocks stale facts, review-required evidence and uncovered non-permissive
licenses.

## Inventory (generated)

<!-- generated-inventory -->
Lock SHA256: `3f6998a37512c81cb08fd6a0a2cbcdbf780f083e56671f2aac28efdb272cf4e3`. Artifact and notice digests, metadata,
markers, and every runtime/dev graph path are in
[dependency-license-facts.json](dependency-license-facts.json).

| Package | Version | Reachability | Inspected / locked artifacts | State |
|---|---|---|---|---|
| a2a-sdk | 0.3.26 | runtime | 2 / 2 | permissive |
| agent-hub-common | 0.1.0 | runtime | 1 / 1 | permissive |
| agent-hub | 0.1.0 | runtime | 1 / 1 | permissive |
| annotated-doc | 0.0.5 | runtime | 2 / 2 | permissive |
| annotated-types | 0.8.0 | runtime | 2 / 2 | permissive |
| anyio | 4.15.0 | dev, runtime | 2 / 2 | permissive |
| attrs | 26.1.0 | runtime | 2 / 2 | permissive |
| certifi | 2026.7.22 | dev, runtime | 2 / 2 | approved exception |
| cffi | 2.1.1 | runtime | 19 / 75 | permissive |
| charset-normalizer | 3.5.1 | runtime | 23 / 124 | approved exception |
| click | 8.5.0 | runtime | 2 / 2 | permissive |
| colorama | 0.4.6 | dev | 2 / 2 | permissive |
| cryptography | 50.0.1 | runtime | 16 / 40 | permissive |
| fastapi | 0.141.1 | runtime | 2 / 2 | permissive |
| google-api-core | 2.34.0 | runtime | 2 / 2 | permissive |
| google-auth | 2.57.0 | runtime | 2 / 2 | permissive |
| googleapis-common-protos | 1.75.2 | runtime | 2 / 2 | permissive |
| h11 | 0.16.0 | dev, runtime | 2 / 2 | permissive |
| httpcore2 | 2.13.1 | dev | 2 / 2 | permissive |
| httpcore | 1.0.9 | dev, runtime | 2 / 2 | permissive |
| httpx-sse | 0.4.3 | runtime | 2 / 2 | permissive |
| httpx2-jsfetch | 1.0 | dev | 2 / 2 | permissive |
| httpx2 | 2.13.1 | dev | 2 / 2 | permissive |
| httpx | 0.28.1 | dev, runtime | 2 / 2 | permissive |
| idna | 3.19 | dev, runtime | 2 / 2 | permissive |
| iniconfig | 2.3.0 | dev | 2 / 2 | permissive |
| jsonschema-specifications | 2025.9.1 | runtime | 2 / 2 | permissive |
| jsonschema | 4.26.0 | runtime | 2 / 2 | permissive |
| librt | 0.15.0 | dev | 19 / 94 | permissive |
| mcp | 1.29.1 | runtime | 2 / 2 | permissive |
| mypy-extensions | 1.1.0 | dev | 2 / 2 | permissive |
| mypy | 1.20.2 | dev | 14 / 30 | permissive |
| packaging | 26.3 | dev | 2 / 2 | permissive |
| pathspec | 1.1.1 | dev | 2 / 2 | approved exception |
| pluggy | 1.6.0 | dev | 2 / 2 | permissive |
| proto-plus | 1.28.4 | runtime | 2 / 2 | permissive |
| protobuf | 7.36.1 | runtime | 4 / 8 | permissive |
| pyasn1-modules | 0.4.2 | runtime | 2 / 2 | permissive |
| pyasn1 | 0.6.4 | runtime | 2 / 2 | permissive |
| pycparser | 3.0 | runtime | 2 / 2 | permissive |
| pydantic-core | 2.46.5 | runtime | 13 / 65 | permissive |
| pydantic-settings | 2.15.0 | runtime | 2 / 2 | permissive |
| pydantic | 2.13.5 | runtime | 2 / 2 | permissive |
| pygments | 2.21.0 | dev | 2 / 2 | permissive |
| pyjwt | 2.15.0 | runtime | 2 / 2 | permissive |
| pytest-asyncio | 1.4.0 | dev | 2 / 2 | permissive |
| pytest | 9.1.1 | dev | 2 / 2 | permissive |
| python-dotenv | 1.2.3 | runtime | 2 / 2 | permissive |
| python-multipart | 0.0.32 | runtime | 2 / 2 | permissive |
| referencing | 0.37.0 | runtime | 2 / 2 | permissive |
| requests | 2.34.2 | runtime | 2 / 2 | permissive |
| robomate-cli | 0.1.0 | runtime | 1 / 1 | permissive |
| robomate-win32-compat | 0.1.0 | runtime | 1 / 1 | permissive |
| robomate | 0.1.0 | runtime | 1 / 1 | permissive |
| rpds-py | 2026.6.3 | runtime | 19 / 89 | permissive |
| ruff | 0.16.5 | dev | 4 / 18 | permissive |
| sse-starlette | 3.4.8 | runtime | 2 / 2 | permissive |
| starlette | 1.6.0 | runtime | 2 / 2 | permissive |
| truststore | 0.10.5 | dev | 2 / 2 | permissive |
| typing-extensions | 4.16.0 | dev, runtime | 2 / 2 | permissive |
| typing-inspection | 0.4.4 | runtime | 2 / 2 | permissive |
| urllib3 | 2.8.0 | runtime | 2 / 2 | permissive |
| uvicorn | 0.52.4 | runtime | 2 / 2 | permissive |
| worker-mcp | 0.1.0 | runtime | 1 / 1 | permissive |
<!-- /generated-inventory -->

## Notice evidence (generated)

[dependency-license-facts.json](dependency-license-facts.json) is the canonical
generated evidence. Each package retains source, markers, all reachability paths,
and all locked artifacts, with inspection scope explicit. Artifact notice lists
retain every inspected path and SHA256; excluded wheel variants have no notice
claims. The inventory and evidence must not be edited by hand.

## Exceptions, exclusions and review records

`certifi` (MPL-2.0) is a runtime exception via httpx; `pathspec` (MPL-2.0) is
dev-only via mypy. The repository owner approved the policy and both exceptions
in operator question #1, answered 2026-10-12T00:40:18.892Z (verified through
`get_operator_answer(1)`, status `answered`). The answer was: "Approve the
policy and both certifi and pathspec MPL-2.0 exceptions". Their exact current
versions and evidence are pinned in the review records below. Certifi supplies
Mozilla's trust roots for runtime TLS verification; pathspec is used by mypy
in the dev graph. Neither is a declared direct dependency of robomate.
No approval is carried over from #181's preliminary audit. Each exception record
binds the version, source, artifact matrix, metadata, dependency paths and notice
digests via the exact package fingerprint. It has no implicit version range.

`pywin32` is an **exclusion**, not an exception (#181, #187): its adodbapi bundle
was LGPL-2.1 despite the top-level PSF label. The uv workspace excludes it and
uses the MIT robomate-win32-compat stand-in. The exclusion currently applies
only to uv workspace installs. Scanners reading upstream Requires-Dist can
still report pywin32. The M2 installation item on roadmap #2 must prove the
published tool-install path excludes the payload; see
[win32_compat README](../packages/win32_compat/README.md).

The allowlist includes [MIT No Attribution (MIT-0)](https://spdx.org/licenses/MIT-0.html),
used by cffi, alongside the other recorded permissive licenses. Additions
need review of their terms. Human decisions are stored in the following TOML
block so the collector can verify them independently of generated facts.

For #127, bob reviewed both locked archives for each new dev dependency:
`httpx2` and `httpcore2` 2.13.1, `httpx2-jsfetch` 1.0 (conditional Emscripten
edge, retained by the collector), and `truststore` 0.10.5. All notices agree
with BSD-3-Clause for the first three and MIT for truststore; no bundled
non-permissive notices or missing declared license files were found. Existing
AnyIO, h11, idna and typing-extensions artifacts are unchanged; their renewed
records cover only added dev reachability. The virtual root record covers the
added dev dependency. Existing exception evidence is unchanged.

<!-- license-decisions -->
```toml
[policy]
allowlist = ["MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0", "Python-2.0", "0BSD", "Zlib", "Unlicense", "CC0-1.0"]
next_review = "2027-01-11"

[[review]]
name = "a2a-sdk"
version = "0.3.26"
expression = "Apache-2.0"
fingerprint = "6027f6f3ae546e98935c556b0a63aab527bff57084ad7ca554bce97e65ffad81"
renewal_fingerprint = "ad9f4d4e3b042619debb1fd836c47e835d4082ed153d63114c4dcb848b8b16bb"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "agent-hub-common"
version = "0.1.0"
expression = "MIT"
fingerprint = "9026fe9adaf4ecd25627843ec4872092117e4d23bc7b9a53ca15a79240cea646"
renewal_fingerprint = "9bef3b358dd595fa9e169befd923e9874c3fbc5f5b91bfe27d81067d53f9daaa"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "agent-hub"
version = "0.1.0"
expression = "MIT"
fingerprint = "2bea10884f3ca95ecef353a5b9abff232cedce4e74f041fcceb0bc004bfa5490"
renewal_fingerprint = "56d324b708b00450c7c7f072e8c72a51988674e936fa381e843a77a588d28c37"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "annotated-doc"
version = "0.0.5"
expression = "MIT"
fingerprint = "74421575b7c313e20821c38ed1290ec3db72be1107b5a27941f906b354967300"
renewal_fingerprint = "c63a5cedccd48c8f37c3af8d21acbb5dae9ee688b994a6fa1fd2f25f4e84a3b2"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "annotated-types"
version = "0.8.0"
expression = "MIT"
fingerprint = "6c8230079aff9d81971b422a5918695e5ada85e1f4668f91f9f772ac4966c038"
renewal_fingerprint = "9e4640a1edbe744d1fe6d99bce561f0a29d65278207754dd17f7ab5a3ae67cb5"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "anyio"
version = "4.15.0"
expression = "MIT"
fingerprint = "e3c0b2807f16000f0582991078b28563d765031735ce4db50d545a738e367f53"
renewal_fingerprint = "9abb87e60ad5681dbfafe8c91c9837982540d6f186256764e8728c94ae3254fe"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "attrs"
version = "26.1.0"
expression = "MIT"
fingerprint = "9d28df9005318429a9753d4e2bd97fa20c7d0b9f8ba4f180caee7a9158e70654"
renewal_fingerprint = "e49424a545a98b22d023e533b84bd5ee5dc7428e615ccf50538ad078175cb510"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "certifi"
version = "2026.7.22"
expression = "MPL-2.0"
fingerprint = "e93700a455bc5ec09b52902ab432b50f02148899cdde943bfb55e3222d4a921d"
renewal_fingerprint = "4736dcd6639d807e724a09542bd083b3d9ce2dbff6ab41b355457d7df3693803"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"
exception = true
owner_approval = "operator question #1, answered 2026-10-12T00:40:18.892Z; verified through get_operator_answer(1); reachability refresh approved in operator question #6, answered 2026-10-12T10:18:50.929Z; verified through get_operator_answer(6)"

[[review]]
name = "cffi"
version = "2.1.1"
expression = "MIT-0"
fingerprint = "b432027972fdb09146ff1c744bec624851ad5c5bf6c7c841b847094625a64a1a"
renewal_fingerprint = "0f410336ad1dbfa219cedf1bc756800ea7db11d4d6ae10bada0848dd8dba170a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"
components = ["MIT"]

[[review]]
name = "charset-normalizer"
version = "3.5.1"
expression = "MIT"
fingerprint = "b37bc59a780eec5678df6a0a909aab91b7feb5b073fbefa68eeaffffe412f3e6"
renewal_fingerprint = "0825dabe22d504313660ae717c8290ae80449ed30b3f1a2ced36c52604e06c36"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"
components = ["LicenseRef-Undetermined-CC-BY-SA-Test-Data"]
exception = true
owner_approval = "operator question #2, answered 2026-10-12T00:40:56.105Z; verified through get_operator_answer(2); reachability refresh approved in operator question #6, answered 2026-10-12T10:18:50.929Z; verified through get_operator_answer(6)"
exception_scope = "Only the 3.5.1 sdist bundled test data described by data/NOTICE.md; code and wheels remain MIT"

[[review]]
name = "click"
version = "8.5.0"
expression = "BSD-3-Clause"
fingerprint = "70a6eb2400c37a94309ab3a03427815c15783219b6e238bdc60104104f586cdc"
renewal_fingerprint = "497ed9e5bf5496d846b3cec6948b5baf32ace6e6bc2f533d03321aa54791089b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "colorama"
version = "0.4.6"
expression = "BSD-3-Clause"
fingerprint = "12539616e05611a116d7bd33a988f313d07ff65d00b734748cc55faf1d32a5d6"
renewal_fingerprint = "1a2731c265befec8d5a2fc8e977f4b519b5680341d00f12a449ccd088fd62aaf"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "cryptography"
version = "50.0.1"
expression = "Apache-2.0 OR BSD-3-Clause"
fingerprint = "e4433214b68e94e8601cfff54943c9e86c20a6281440d3f325f2ab3311d263e8"
renewal_fingerprint = "82501305e660db90f6c1d26bd812a4fb45205321113c8937e2562d7d68877ba2"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"
or_choices = ["Apache-2.0"]

[[review]]
name = "fastapi"
version = "0.141.1"
expression = "MIT"
fingerprint = "81ec15bcdb8d3c0b551fe315232b69467c40f361a4e13cfc3a5c33d9fd888606"
renewal_fingerprint = "ce310bdf732c5b51c3f51670df24aea7c0315601a51cb2371894b74814d7be0a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "google-api-core"
version = "2.34.0"
expression = "Apache-2.0"
fingerprint = "6f46d2390c2f45cfef3359c061d0a58f027c772feef5974f8d165e15918dfe17"
renewal_fingerprint = "8c368d3cb893cb9ccaa68039714c9038a70db196cd778d05dc148384a9e09179"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "google-auth"
version = "2.57.0"
expression = "Apache-2.0"
fingerprint = "caa2faa3910ab8cf50be23b601601973a5b774dd43e4cb06cc8e532682cebc4e"
renewal_fingerprint = "83eb80a3962db661cd24fcb79261b94065bdfa85207fe18aa425469e2fd7c142"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "googleapis-common-protos"
version = "1.75.2"
expression = "Apache-2.0"
fingerprint = "5fdc5b66e8ced86a00eba0d76f37f8bb635948cc89dec40b7b19987c9f801410"
renewal_fingerprint = "6f9053520cb148d9b42e618577c8891eebb80b2931c4f6e6b9395d347fbc3aa8"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "h11"
version = "0.16.0"
expression = "MIT"
fingerprint = "313a16ba212cc7fbd45ac82ef3fcd92aa91027466f183080625950b0a0d91457"
renewal_fingerprint = "0e3725c1a471b4dc44db545ae44505361439bb95d21e583505ce372c7ddbebd3"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "httpcore"
version = "1.0.9"
expression = "BSD-3-Clause"
fingerprint = "23cf4b355b3dcc6579cb7ab49ff5d39dc3dc6220fe1a191c662d5be848b2cc2b"
renewal_fingerprint = "78dd7f39b48e4b447e6395cca009133acf95e114b2acf83f0479d5aface55723"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "httpx-sse"
version = "0.4.3"
expression = "MIT"
fingerprint = "7148e28c9dbe54637b59793205c8084dd45d2b2a36a913b0f6750fcfb9260a8b"
renewal_fingerprint = "297d8a555d9d3d021294d4b42c1924e296f1566cfac3a06672e7f05e84d69f54"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "httpx"
version = "0.28.1"
expression = "BSD-3-Clause"
fingerprint = "89c2e1dd135671993e6cef3f119b2cbcaa7badfd463cbd28ab15a25b9f9a7a38"
renewal_fingerprint = "d6736dc12850502f6b08eb379f9653b0a0bf68ad23ed7995ec2a3df0ac5dca89"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "idna"
version = "3.19"
expression = "BSD-3-Clause"
fingerprint = "2015b595ea96571a35b4398a2b33b0339023a809d82d7bd923958bacec9e5966"
renewal_fingerprint = "9c987e7d58dc917a151cbf80616fb8dc57c189624a792fd087d2a3ff419f128f"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "iniconfig"
version = "2.3.0"
expression = "MIT"
fingerprint = "d9e7b0779dd1527c407e6c5deba5f76cc6d9340bf61094a0404a39ab41c76f91"
renewal_fingerprint = "0f1dce3613e4efd67ecd5a44377e6a2fb9b8c959723cb724da1c95c968bf462f"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "jsonschema-specifications"
version = "2025.9.1"
expression = "MIT"
fingerprint = "346aea36772fc14daa3984024870ee271b769e0c77066bdf8e9254e2b0a7840c"
renewal_fingerprint = "ebc7ca0155beb656c560a227cce36dc9b2dc2535b2338f5ff79d67e06ba3cf9f"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "jsonschema"
version = "4.26.0"
expression = "MIT"
fingerprint = "a764857fe831f39fe0d30fc055c106ba334a1d76972f899b81c4399c1ef67909"
renewal_fingerprint = "0dbcae75e975ed05fa70801de7c9842eeaa4e12753101239961db79a64451ec1"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "librt"
version = "0.15.0"
expression = "MIT"
fingerprint = "3d0a9c71ed3d54def731764147787c7148a361666cba31380e881db7f85f5056"
renewal_fingerprint = "9c1bdb8f7951340502c91226e4777d7703f1e4254361e5eb1c987cb01b03d0b1"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Python-2.0", "BSD-2-Clause"]

[[review]]
name = "mcp"
version = "1.29.1"
expression = "MIT"
fingerprint = "5eabd04444834a03a3c672e3bf5ca47359ba37cf7d9beda419f85aa680a1c52e"
renewal_fingerprint = "cad4dcf33649a3537f5aac461a5063179b852abfc4ae22ebfdbdde6a10797994"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "mypy-extensions"
version = "1.1.0"
expression = "MIT"
fingerprint = "36086f64a19c2f894eb49d4ce728b89fb1625184bf6d61cd424d4383f4ac7a80"
renewal_fingerprint = "5b523fc38e337c677bff36319abc070c456f9919f23dd94ba65fe0a253f2b522"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "mypy"
version = "1.20.2"
expression = "MIT"
fingerprint = "1d37b4bd3f5cdf9bdf1fd2d9411f789ead39d9bce688980bbe7a05bce94e514e"
renewal_fingerprint = "836400274fe28f8bb9f87907d6933858bf89488bacd89ca0d8da9b16d4b61f67"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Python-2.0", "BSD-2-Clause", "Apache-2.0", "BSD-3-Clause"]

[[review]]
name = "packaging"
version = "26.3"
expression = "Apache-2.0 OR BSD-2-Clause"
fingerprint = "71b666885d929adf85fe6353d94b499b24897c0a79923b448efce1fea6ba7dcf"
renewal_fingerprint = "1dbec087d15a8334bc3f6bdba9f946e48788a2ac46797b9c673823648794c4ec"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
or_choices = ["Apache-2.0"]

[[review]]
name = "pathspec"
version = "1.1.1"
expression = "MPL-2.0"
fingerprint = "77adab91b1a12a0699bfb8f471816caa4ae8aed7d63fc9936eb1db9ac3fa7260"
renewal_fingerprint = "b82ba983fe08675a10ca5fc1f5f446355b0a2853fe2481eea2f91da7b8bfc324"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
exception = true
owner_approval = "operator question #1, answered 2026-10-12T00:40:18.892Z; verified through get_operator_answer(1)"

[[review]]
name = "pluggy"
version = "1.6.0"
expression = "MIT"
fingerprint = "4ae038f27a13040ffd7ce2e064c7b208c9d749e03de4fa847c408f9c73d4d3c4"
renewal_fingerprint = "5d29059dd478b7041f93b48776551c729154681b6ad23c50a981f95cb04717ef"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "proto-plus"
version = "1.28.4"
expression = "Apache-2.0"
fingerprint = "233f4d4cf232d996e21ae9e4328d0f49bb5fe241dadd8b5250ec5f41f9fa2c85"
renewal_fingerprint = "29698af479f72ada503384310da2ad59a2c7b6a76d947afd788bf2adafda165a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "protobuf"
version = "7.36.1"
expression = "BSD-3-Clause"
fingerprint = "b2f3f92d6926535b128293f424112c2697d5111903bbcc2a9cbca91f455e89c9"
renewal_fingerprint = "b0690e28b7a99b61cbfc7b0348a6bdb4178aa68eb8c98d8a1cbee06a039a9ac7"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pyasn1-modules"
version = "0.4.2"
expression = "BSD-2-Clause"
fingerprint = "9320d9d3b7e095f475261c6f4f08295cdce470ffaf56fc3719142fa79fee9be9"
renewal_fingerprint = "c1870396d2f875592503cb917ea3ebca6e5cc07229e34a2abe7ac9c7b0d146e2"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pyasn1"
version = "0.6.4"
expression = "BSD-2-Clause"
fingerprint = "c9f49f2864ccb630ab0ad3d9fd2a90fad4206dceedd6339bbd13dcfbc816f49e"
renewal_fingerprint = "e7cd9ab32824077d20b744a3c53e322e4f1dccd79b7e3d566ecca86c343b8332"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pycparser"
version = "3.0"
expression = "BSD-3-Clause"
fingerprint = "a6447109816c383da98eeec50660da00efac592cb91b82efea807c48a119ac2c"
renewal_fingerprint = "d48d95ab6097a800bb3a4c46588d45b1c42ca3109689ea5ecd07e4b38bcc7a36"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pydantic-core"
version = "2.46.5"
expression = "MIT"
fingerprint = "a42d73358a81db902b88c9ce41d341bd1933c31b10d85517c03eab8978fe0a82"
renewal_fingerprint = "692346305707498aeb77dcd887c96da4c95dc213b767f69fca138194800cdb58"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pydantic-settings"
version = "2.15.0"
expression = "MIT"
fingerprint = "b075f12a38a5fdff2dd38817351832a0aabb652f1ce65c504dc57b124e5adc21"
renewal_fingerprint = "a4a5d6cfdbb3aaf1e64d1236f9e088a5e475ffb5f019272df8d59b5e6d26bb78"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pydantic"
version = "2.13.5"
expression = "MIT"
fingerprint = "188c7607b9317d6b1ee5f2797f3984e47f296c8ad35f080edccb3de12933a971"
renewal_fingerprint = "d382cbf236faf08e22598914d727d6c3161703732698ec1a1f0bb0fe00621c8a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pygments"
version = "2.21.0"
expression = "BSD-2-Clause"
fingerprint = "57cc4c32a170cfd32c3f6f782c9bd5b69c027f41d3b4a24bc84f5d2847ebf527"
renewal_fingerprint = "2a4559fc63b6383ce25e5e8047406fbb9d662f024ffaa898b1a10ef2887d1455"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pyjwt"
version = "2.15.0"
expression = "MIT"
fingerprint = "c5bb0349628328fbc068e4459dd8b72dae623144640e7ca04a9b92a2c3fc8e77"
renewal_fingerprint = "59d05a03eac73371495a2bbf477e88a5987a97ae8167f76c0499c61acfea9355"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "pytest-asyncio"
version = "1.4.0"
expression = "Apache-2.0"
fingerprint = "b3b17b9ec2be9ea6085b2f797af04647e4af45cb0a3a87375189ce74903ca462"
renewal_fingerprint = "800fec0e586b3d6f1bd180a8949a45f1c79e38259b375ff4691fd14f7d265b2b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pytest"
version = "9.1.1"
expression = "MIT"
fingerprint = "93b446420bdff417ce7df162235bbf3d163a1cd079497ad1c90a992cc039bd0b"
renewal_fingerprint = "49aa644f956d875d3a21b9635690946b969a47fd873d38624833c676b611d5db"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "python-dotenv"
version = "1.2.3"
expression = "BSD-3-Clause"
fingerprint = "a3895f7ab8e6fd9d8cc12587bccac688a17063adda8dddd3969fa6e7c4b48daa"
renewal_fingerprint = "34c2220296cea192063e3a20ea90b3432811610570cf5c545463d150d152609f"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "python-multipart"
version = "0.0.32"
expression = "Apache-2.0"
fingerprint = "404ec939f9c767adbdb1846bb653fe5cc9d8fd21e459b12e29365f87534cc535"
renewal_fingerprint = "e29de5f4e5c3791b6fdd8887d96ebeccacb32657c1821506e6042df8f42a969e"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "referencing"
version = "0.37.0"
expression = "MIT"
fingerprint = "b9ad346b176cb046796f8aae4e5b7fb522160d3b01eceee12a753750e46c951a"
renewal_fingerprint = "05b8f44d1988837405fdbac17717525fa3ead2f38bc406c53a407d42f28db83c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "requests"
version = "2.34.2"
expression = "Apache-2.0"
fingerprint = "2434177193d588cb8022c75333ab25ddf621be0b623613c45562274ce9a2fbfd"
renewal_fingerprint = "2776d8a42a3bdf7576ec9ec2a10fe538489f3a74f037abca109fa3029b721faf"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "robomate-cli"
version = "0.1.0"
expression = "MIT"
fingerprint = "0ddcf28e241cb853685cf66c148385f0762dc905ce788167ce3b413035ea9b6c"
renewal_fingerprint = "bca6e0fe5d5ddba0bbb83f9e6830ac61ea284a743fb0c42bb8b7583079713e13"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "robomate-win32-compat"
version = "0.1.0"
expression = "MIT"
fingerprint = "6aee2bba0ef5a9e22403a48adea1c51ea7adbedcb5d8707d5e132b77e7768702"
renewal_fingerprint = "6b680fab44fe94613752c132ad7e18b8fc8b68a4797a5d57072a0a4819b7e4c1"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "robomate"
version = "0.1.0"
expression = "MIT"
fingerprint = "f9e683cee904aa0970661294dc46f56040d2a2cadf5ac812c8970eaf8bf4fa9b"
renewal_fingerprint = "1e1987080320b6d45aaef67a3784fb8318a35000338cb026bc0516adab4fdfa6"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "rpds-py"
version = "2026.6.3"
expression = "MIT"
fingerprint = "3d6dccf564bec865c5212cb38c379b1dda1f015769cf78c41b11f33207e585cb"
renewal_fingerprint = "0046e96fe031a6301804276ff5ac56ad28db99f7eb834436f0b49e6e24529cf5"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "ruff"
version = "0.16.5"
expression = "MIT"
fingerprint = "c8d3ff5c75cbd16df1e4f9bc5654b33124a5963596b37c4a44e093c5d9df4e0c"
renewal_fingerprint = "1502cceaf1060e361333ae22e8ecafa4ffbed4c3108400b7bb15fc3938df852c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Apache-2.0 OR MIT"]
or_choices = ["MIT"]

[[review]]
name = "sse-starlette"
version = "3.4.8"
expression = "BSD-3-Clause"
fingerprint = "159df081e6bb81538c6b9346ac615234b17a68f885d6e106e2763e626f73e721"
renewal_fingerprint = "091874603fa7b57b118fc53ba2967077fd807cd80563525f457ace11c4fd8426"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "starlette"
version = "1.6.0"
expression = "BSD-3-Clause"
fingerprint = "ddeb0e26ebe2c2ddf4a61da7daa8ef170a4aa13781287cbe7aef1b5ed1bbec67"
renewal_fingerprint = "67b35e7625587002aee44b340b4aebfe55bc51d2ffe48a10c4f5d30b78553c29"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "typing-extensions"
version = "4.16.0"
expression = "PSF-2.0"
fingerprint = "adce733f05d5aa3cce77308c769ff3bd50646f672ae7ded6bf732c59c85cae7e"
renewal_fingerprint = "133e7e4d5ba4ce643e252e53513f0ff48d5a95e028a4ff832232155559326705"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"
components = ["Python-2.0", "0BSD"]

[[review]]
name = "typing-inspection"
version = "0.4.4"
expression = "MIT"
fingerprint = "c0f3b67d074f45e3cafdefb63df52fc966b990e461d0f7d95c448aa62fe8646e"
renewal_fingerprint = "0fdcbdfe78f551c6839fd2d8f23b245c78be4241170e7775a32c48ce4d65c982"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "urllib3"
version = "2.8.0"
expression = "MIT"
fingerprint = "15d31cb49f799adb2533e67a92af53ce60aa0920a968cb12df72183d9a780044"
renewal_fingerprint = "6b2dfea24b2efd49b8343b30642e03f6b977130b0a7097f00754b55a354bf2bc"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "uvicorn"
version = "0.52.4"
expression = "BSD-3-Clause"
fingerprint = "af8fe3a2e0364e4856cf97737c1d52ae9ef8e288326660613358a36989f50eb1"
renewal_fingerprint = "70ca98d517f122a6bafdf2dcfc0794bf11d40f2818271e1dbbc6d393105628b5"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "worker-mcp"
version = "0.1.0"
expression = "MIT"
fingerprint = "536299c3989dce5fd86ac1d3fa0ad6d7a096a176514361b8fb0a955f6038cba6"
renewal_fingerprint = "f9ff7e50062d975f5703ab2c8bb81b63b0c5fbb80f81e7198f8a5228e6eb1f77"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-12"

[[review]]
name = "httpcore2"
version = "2.13.1"
expression = "BSD-3-Clause"
fingerprint = "6e71e8b16a8f1c45fa3160594b2d25f5246b288649affb57907c5838b624809e"
renewal_fingerprint = "89b53f64a20518c3a8afd55276c78113aff29533852b83cc052009bfc10c24e6"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "httpx2-jsfetch"
version = "1.0"
expression = "BSD-3-Clause"
fingerprint = "175b003cc9e7260ca3f4e4e34bbcd605fb5ef9c206bc7d95442dcb35e69b1129"
renewal_fingerprint = "bb345ba9a6471c67164686579c821268841e7bbd77aed5446379692d3145b860"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "httpx2"
version = "2.13.1"
expression = "BSD-3-Clause"
fingerprint = "c82a1370544be2c93d9bfead758ea0968d6d39d96f57847d1b413145b2da6a30"
renewal_fingerprint = "1680409141ae07e66843b5870516b95ac741f267de2d52818654da0c616c11c0"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

[[review]]
name = "truststore"
version = "0.10.5"
expression = "MIT"
fingerprint = "95daa5d9e98024663130f0c8b6c55da1689a5abcbd7460e0a8a5913015e364a0"
renewal_fingerprint = "e8b51dd26d1347c9915c294aae19b9db6c74ead09fbf5b9ebab883442a15916c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-13"

```

## Review log

2026-10-12 — Rebase review by worker bob: main #196 moves pydantic into
common and removes worker-to-hub dependency paths. Reviewed all 46 changed
records against the approved inventory: all 60 package identities and
versions, registry archive hashes, metadata and notices remain unchanged.
Workspace license declarations and notice hashes also remain unchanged;
only dependency metadata and reachability differ. Updated reviewed
fingerprints for those graph changes. Certifi loses 20 paths and
charset-normalizer loses four; neither exception gains a path. Pathspec
is unchanged. The owner approved the exception fingerprint refresh under
existing approvals #1/#2 in operator question #6, status `answered`,
2026-10-12T10:18:50.929Z (verified through `get_operator_answer(6)`).

2026-10-11 — Implementation agent bob on behalf of RoboNater reviewed the
locked matrix notices (69 distinct notice contents) and metadata. 57 entries
qualify as permissive; certifi and pathspec are owner-approved exceptions
(operator question #1). The additional charset-normalizer sdist test-data
exception was approved in operator question #2. All 60 entries have inspected,
reviewed evidence within the declared matrix. Next full review: 2027-01-11 or
milestone close-out, whichever comes first.

The review-signature encoding was updated for r1-1 to omit ancestor versions
from approval paths. Every old signature was checked against the same generated
evidence before migration; package versions, archive hashes, notices and license
interpretations are unchanged. Owner approvals still cover that exact evidence.

Bundle interpretations: librt and mypy include Python license terms and
BSD-2-Clause base64 code; mypy additionally carries Apache/MIT typeshed and
BSD-3-Clause googletest. Ruff includes MIT-derived components and an Apache/MIT
annotate-snippets component (MIT selected). typing-extensions carries the
Python historical terms and 0BSD documentation-code terms. cffi's libffi source
headers carry MIT notices in addition to its MIT-0 top-level notice. These
component interpretations are included in the review records, rather than
inferred from top-level labels. cryptography selects Apache-2.0 from its
Apache/BSD alternative; packaging likewise selects Apache-2.0.

charset-normalizer 3.5.1: `data/NOTICE.md` in its sdist describes 17 test files
as either CC-BY-SA (no version stated) or public-domain archive data, without
a file mapping. Its SHA256 is
`0cb3efcfd8f7a02a337e98dc3de4b0b57424d7208a332b26e7deb8cb94c13922`.
The code label MIT does not settle this evidence. The repository owner approved
an exact exception for these 3.5.1 sdist test files in operator question #2,
answered 2026-10-12T00:40:56.105Z (verified through `get_operator_answer(2)`,
status `answered`). The question named this notice digest. Rationale: the data
is used for upstream tests and is not required to use the installed library;
the approval retains inspection of the sdist rather than removing it from the
matrix. The package fingerprint additionally pins the archive hash, metadata,
notice paths and Windows/Linux variant set. The wheels and library code remain
MIT; the exception does not assert that the test data is permissive or settle
its undetermined CC-BY-SA version. Any changed version or evidence needs a new
owner decision. This third exception was added after the initial two-package
scope because artifact inspection found bundled evidence absent from metadata.

## Known limits

This is an archive notice review, not proof that every source file has a license
header. Conventional notice names cannot discover an arbitrarily hidden notice;
human review must examine bundled components and resolve discrepancies. Build
tool dependencies outside uv.lock are not inventoried here. The matrix excludes
macOS and non-x86-64 targets and inspects sdists as distributed, not artifacts
built locally from them. Artifact availability and network/hash failures block
the check. CI cannot manufacture owner approval; reviewers verify its reference.
