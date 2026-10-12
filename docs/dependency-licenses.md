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
Facts are checked on every PR in Linux CI against the entire artifact matrix.

Approval renewal: unchanged version requires the exact evidence fingerprint.
A permissive version-only bump may inherit review when its renewal fingerprint
is unchanged (notice digests, artifact variant set, policy metadata, markers,
dependency graph and reachability). Archive bytes and version strings are the
only normalized fields. Changed notices, variants, missing evidence, bundled
components or policy metadata require review even when the label is unchanged.
An exception covers only the exact version and fingerprint recorded; no renewal
is implicit. Decisions below are reviewed facts, not instructions granting
workflow authority to agents.

The repository owner owns exception approval. Whoever handles a dependency PR
regenerates evidence and records review. A full review is due at every milestone
close-out and at least quarterly; record its date, reviewer, result and next due
date below. The scheduled workflow opens a reminder issue when due.

## Inventory (generated)

<!-- generated-inventory -->
Lock SHA256: `69606c2a79a43657ddf6e1c3bfd2412e17b9bb67c24b470826d10c3a299ac282`. Artifact and notice digests, metadata,
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
| httpcore | 1.0.9 | dev, runtime | 2 / 2 | permissive |
| httpx-sse | 0.4.3 | runtime | 2 / 2 | permissive |
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

<!-- license-decisions -->
```toml
[policy]
allowlist = ["MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0", "Python-2.0", "0BSD", "Zlib", "Unlicense", "CC0-1.0"]
next_review = "2027-01-11"

[[review]]
name = "a2a-sdk"
version = "0.3.26"
expression = "Apache-2.0"
fingerprint = "ea51972ea2b0a9f84e7d4d71468d69fa43385cefa7b1ac86ae0e007728cae341"
renewal_fingerprint = "7b8c2c1483edf97a7b9f8c334bb0257cbf8538dd22a6b768f97e9083245d46a0"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "agent-hub-common"
version = "0.1.0"
expression = "MIT"
fingerprint = "19dcd21b299139c7bc447c031a9c2a9f81347d825591b241048b11d30daba088"
renewal_fingerprint = "987c6a7450feb11a2fe8e74abbd01878c79bab0e0defe4d1de1c3da2bb90657c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "agent-hub"
version = "0.1.0"
expression = "MIT"
fingerprint = "ce0da8d398fd931396e27a3862383837a54df585467f496ef32f45e0097d582b"
renewal_fingerprint = "2c72afe1081f67f678bf2be449697f02611bbdf875918bee7369ff32c86614ad"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "annotated-doc"
version = "0.0.5"
expression = "MIT"
fingerprint = "674e6b6454239ab1c5dad846ae5469e28aba04210cfcb54303345814ca5a3398"
renewal_fingerprint = "86254ef83d9f35d45aef7923c7f8fda6946993635692a5988ed4a81ac72bb075"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "annotated-types"
version = "0.8.0"
expression = "MIT"
fingerprint = "5bdc60d77491a75d9af7ad36d131377a0b8bbbf7b299bed55b24121e3f93d3d7"
renewal_fingerprint = "877f5c92535aaa1a9b67895ae1879d8dcc8d9aaf84350485a4eeaf78bf0e950c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "anyio"
version = "4.15.0"
expression = "MIT"
fingerprint = "10a79a04a7060ad9af8e7c383e0dda3512fc3a6d8bc63285059befae430788ee"
renewal_fingerprint = "0ad46a4c0ef4d0511f88410b48343f1e2171d7600ccb1a7fb1d44de6ae7bf790"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "attrs"
version = "26.1.0"
expression = "MIT"
fingerprint = "a11fb0328fe016b0d9f316b1c5a67dcaed0307b13ea3486b4ebdf3e756e9888a"
renewal_fingerprint = "39de4206a14cbff3ae398fc022257501233f12cfa3edf73a4449d12a3d4e198b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "certifi"
version = "2026.7.22"
expression = "MPL-2.0"
fingerprint = "3d901875e24c876fd315537915eaa3fe9938abc35a78d7083b075f8e4842fc87"
renewal_fingerprint = "8af950564399c92db92c0b7c284e62abe928656e196e1e2441e147278590d960"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
exception = true
owner_approval = "operator question #1, answered 2026-10-12T00:40:18.892Z; verified through get_operator_answer(1)"

[[review]]
name = "cffi"
version = "2.1.1"
expression = "MIT-0"
fingerprint = "05bcf5219bc644c3ca917b0236fe9cc50e44c32dc038d15f9a5f9fe156ab88b6"
renewal_fingerprint = "3bc7d6aeef7938c44cc6429623cead1ce265bce75c08038cfc7306ea0f0df667"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["MIT"]

[[review]]
name = "charset-normalizer"
version = "3.5.1"
expression = "MIT"
fingerprint = "8c0a9cd90bcdb8c1ad0b02aa9eb0a27028319449522613b7e6597ecfb53bcf49"
renewal_fingerprint = "ff71cd1610c0180e7fbd30f4fbd2c3d099ff05ab5de0c53578a013785c023f3d"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["LicenseRef-Undetermined-CC-BY-SA-Test-Data"]
exception = true
owner_approval = "operator question #2, answered 2026-10-12T00:40:56.105Z; verified through get_operator_answer(2)"
exception_scope = "Only the 3.5.1 sdist bundled test data described by data/NOTICE.md; code and wheels remain MIT"

[[review]]
name = "click"
version = "8.5.0"
expression = "BSD-3-Clause"
fingerprint = "9a4c5d0300552e74e69b6e6e87d9dbb7ded12ad36549a9fb3c534b3222897bb1"
renewal_fingerprint = "246cddb66fa3c48e5c7a6d260dd4f7aa5e333c801ee1ba2126a6c0850439c66b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "colorama"
version = "0.4.6"
expression = "BSD-3-Clause"
fingerprint = "853107c7b93b028ff27d982b59241565b6964010cf1e1c84d75677b215da0aac"
renewal_fingerprint = "a3a73297f003923a8b2cb121cfee183239fcdbc4eee69dbcdf447f57de595f58"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "cryptography"
version = "50.0.1"
expression = "Apache-2.0 OR BSD-3-Clause"
fingerprint = "6891562e47e4f93f95d43350eb7a499fa8e4698a488eb36cbd102fc3918fa7db"
renewal_fingerprint = "d5c0a653aa62c4d6e4839e7c800d07f658a0dfcb489d6d85645fb6183b4e785c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
or_choices = ["Apache-2.0"]

[[review]]
name = "fastapi"
version = "0.141.1"
expression = "MIT"
fingerprint = "99bf863af68fd11d0c3b99a050b176992fb71fba237c33f0b941f484458fec2b"
renewal_fingerprint = "69942fb7c97282755694e8c1b6c7d5d41da8293da203d358a242447311d6c061"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "google-api-core"
version = "2.34.0"
expression = "Apache-2.0"
fingerprint = "3b9fdf740f0a72f24afcd0fa3b0779de85c56dbf59713268d3fc1a9eb251415e"
renewal_fingerprint = "5626aa42e7615b068b7063011d0e7441cfd179fa18755bd87c69ebb405fd4f19"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "google-auth"
version = "2.57.0"
expression = "Apache-2.0"
fingerprint = "07508b04c50eaff0167d7117759099486fb6a0da9a3a2568bb4b98c9286619a2"
renewal_fingerprint = "454a0a96eb6d253a60ceb1cd3ac2f28318a3f75a99cccf343dfc3091b18e54ae"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "googleapis-common-protos"
version = "1.75.2"
expression = "Apache-2.0"
fingerprint = "37c798ca4768f511b810bfcc864bc5d94bd44e2f284dba4d0f1996d3a325b86e"
renewal_fingerprint = "47f1461f2df7c437b57e63392af69d27fe318d9d80bb6b283b08c7cff35a96e9"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "h11"
version = "0.16.0"
expression = "MIT"
fingerprint = "48b793ceff485925e2eb5b67870f411d07e47da794eace4982bbbd8df13de349"
renewal_fingerprint = "7d21e5ceed067b5150e11b95b3d1b20f4f4c18c0501c43a0c2a58ae8c77a170d"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "httpcore"
version = "1.0.9"
expression = "BSD-3-Clause"
fingerprint = "3695929cdaa5bfdc08b9b14ff3318e762f956d005f294cd7fc82318266cfc2d5"
renewal_fingerprint = "e9068b9bf878f59c3428ee96e79cd5f7f5807a87c187539a8b4c30c2d0ac12aa"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "httpx-sse"
version = "0.4.3"
expression = "MIT"
fingerprint = "284c542d1d52a08d9d84cf6073de870fda0e156ec2b765d983c0c8f14483402c"
renewal_fingerprint = "0a961e8595460b6572b7edf61d593e342305d37c29cbeb69f5bdea1678ed4646"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "httpx"
version = "0.28.1"
expression = "BSD-3-Clause"
fingerprint = "df12169f8ea989d8a4255af679ebcf23151bf430eea95191c3d473efecf3f9ad"
renewal_fingerprint = "53fc66cb4b2c5258798a65f556018913f4c4d5d371b214aa80e359c6a4d25e7b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "idna"
version = "3.19"
expression = "BSD-3-Clause"
fingerprint = "4f4e1d2667eddb212e680973f34d50d153a0673b8e3024603fec385607bcb5d3"
renewal_fingerprint = "d30106c5698cd3419290ab9d648a79eacb7446c946c674bf43ca8a5eb0188b4a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "iniconfig"
version = "2.3.0"
expression = "MIT"
fingerprint = "2db8882928f4f732ca325883c44bfe709ca97ed18e9cf30af33122162edbe0c1"
renewal_fingerprint = "ca95d9aa9cbb83c8a6bf46fe8baa68d8300f56bac6315ea14850ce0f8522e244"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "jsonschema-specifications"
version = "2025.9.1"
expression = "MIT"
fingerprint = "357817bb4b18df5af41cafe2a98091c733eed7dd70da406c9f59491018bcad31"
renewal_fingerprint = "79c841f6de88d11075c71a79d1c9aa675b2546da4232c9dc05ea240eebfb5453"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "jsonschema"
version = "4.26.0"
expression = "MIT"
fingerprint = "b408e253eacf2ea61dfef5f3725cf57d35df4fe58652c7aaaa8784a37b1aec8a"
renewal_fingerprint = "cf9729b5c70fc3d33cc41ea8e580331e0b90509167c4859adbb736983e926b3f"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "librt"
version = "0.15.0"
expression = "MIT"
fingerprint = "8b4c56f4a5b12fd2e83c61d7d1256f27d7410b4096caeb5d226696bb944f29fc"
renewal_fingerprint = "0e2677482ebdbfc511fedbfa5143c37366c490c310ca335b87a89344789be33f"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Python-2.0", "BSD-2-Clause"]

[[review]]
name = "mcp"
version = "1.29.1"
expression = "MIT"
fingerprint = "582e887269214b92fc1a3a11d75655ac93ee6a0fb8545770e7297a74de205717"
renewal_fingerprint = "b8203938dc1b5ee19ef0cef8887df230d9f0697a34be65213e1a8f7e96d2a7c9"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "mypy-extensions"
version = "1.1.0"
expression = "MIT"
fingerprint = "c53af61d20ef10465ba4063a2cb1a5df92b261f50e2c22833cf9af370ca6a003"
renewal_fingerprint = "7f8076502cd94dbf066b2eef80bbe018abda11ee3df3196b7d3d71f57003f27e"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "mypy"
version = "1.20.2"
expression = "MIT"
fingerprint = "6d2a06c1852321e476b6c6df3c6477e4e3ef17ffcb2ebeb97c39c023a85e998d"
renewal_fingerprint = "e577218cd590d18b194bfb361be189ad5f1da24f1fe64f974e234855139ab112"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Python-2.0", "BSD-2-Clause", "Apache-2.0", "BSD-3-Clause"]

[[review]]
name = "packaging"
version = "26.3"
expression = "Apache-2.0 OR BSD-2-Clause"
fingerprint = "dc1c6e811b15234514f4f73ca84197c181639725a96354816a37795ef9f77509"
renewal_fingerprint = "a267ec3b7779e7f62cfcfd7212da38456df8d5b9127165a59aee97b4871b3de7"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
or_choices = ["Apache-2.0"]

[[review]]
name = "pathspec"
version = "1.1.1"
expression = "MPL-2.0"
fingerprint = "7c6b19b238c20a989fec27f3585cc285c4d39310180cd9888e8ae4452a873705"
renewal_fingerprint = "ca3e6d7033c7cbe783801417788530e816028068cc1e87cb4b096113b76c5fcb"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
exception = true
owner_approval = "operator question #1, answered 2026-10-12T00:40:18.892Z; verified through get_operator_answer(1)"

[[review]]
name = "pluggy"
version = "1.6.0"
expression = "MIT"
fingerprint = "d991371bbd1712b66c71fe74edd32130ffc46e01c633c5ecc4657464af718919"
renewal_fingerprint = "a6975fd5ccb94bd42b0fd8e8147cf7e5d8c9f29952246255a089f740d84d94ec"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "proto-plus"
version = "1.28.4"
expression = "Apache-2.0"
fingerprint = "ce6afc52fe21b86ec7cebb9a0fbcfdaa4d4ddf90ee59bfe0aa38e79ca23f8a6c"
renewal_fingerprint = "353b21eb0cbfec04c23c00893722e22bf5ebc178c45d40b190672b3966632486"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "protobuf"
version = "7.36.1"
expression = "BSD-3-Clause"
fingerprint = "a658a73eefe073b5ca661b444e8409c5e3c1d1376004574eb61185bbeb40c2cb"
renewal_fingerprint = "0e7d3542c47c2969959a2e7235d64b9016ce1d206cb8b7fedd61471f257abaf8"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pyasn1-modules"
version = "0.4.2"
expression = "BSD-2-Clause"
fingerprint = "5a9cb2c0b85289301a64a5d353eeb1fd58e33b636f3d1ba9c78b2f23d5d9a8d9"
renewal_fingerprint = "7bdbd8d2a04c5214954b5119596480ab53531de1d300b5d1789b9c69dd739510"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pyasn1"
version = "0.6.4"
expression = "BSD-2-Clause"
fingerprint = "4d62d7fce45a47044922b6e1f8c1cfe2b7e4198e0aedfce59321419290e9d5e8"
renewal_fingerprint = "cd21ffbc3c3594cc9d30889aba7b7652ece451f43503186085bde5fca8cfa398"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pycparser"
version = "3.0"
expression = "BSD-3-Clause"
fingerprint = "39581c1e3efe10c85b79af6310637a229aa15763c07cc27eb0c1a15b346bd2ae"
renewal_fingerprint = "0a756b8c59877851b4284829fe38c2270fb0ddd48303b8ef860a8487189735d6"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pydantic-core"
version = "2.46.5"
expression = "MIT"
fingerprint = "30db0f847b423cc6d7da78c95682590ac18dda04dd1e54bf75f56c9b72e8891b"
renewal_fingerprint = "594448ab98e2ff5360a8483ea269c1044f5d550e52bc5959d1af4176341ba1f0"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pydantic-settings"
version = "2.15.0"
expression = "MIT"
fingerprint = "466463b4da53d39b13d491d7a37a6b9ffe9e366c49e072f4df750fb56ac50d24"
renewal_fingerprint = "1a3e250a08f4011669a95535ba1f440a13bf0a29283cf97614c93ff1a66c3379"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pydantic"
version = "2.13.5"
expression = "MIT"
fingerprint = "252d54ae632d89450a5c79b75aa700297f931ee130d2a578f0d0eff4d9158636"
renewal_fingerprint = "6c9f51ee016745b97f42b912b6df00adf23a7c284630c114607d83604439a745"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pygments"
version = "2.21.0"
expression = "BSD-2-Clause"
fingerprint = "f7d7389cc060c7384fcfbe0337791536024cd451b6a9da976a667c47deb5d04d"
renewal_fingerprint = "09e0e0a0fc776df4e5b695ce235eafb673881189692e5c9523d5b13159f1c00c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pyjwt"
version = "2.15.0"
expression = "MIT"
fingerprint = "5e668c718550073c9af83308867607bceee3e78244552538e247f8dfb104eaf9"
renewal_fingerprint = "50657a468aec9e457436ca8390b29343c3e242b5bb9aa2c33fd214203f2e35b1"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pytest-asyncio"
version = "1.4.0"
expression = "Apache-2.0"
fingerprint = "d809de3b59eea847912f90b8a51140ce307d5fdf781d32d60e361b2f6873fa74"
renewal_fingerprint = "e2957eb1b4913fd2beba8fb69bdcf22e225dee81cc9649dfa240afd4c9cc47c2"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pytest"
version = "9.1.1"
expression = "MIT"
fingerprint = "5d1c3d2d640560ec1a35bfb5c3e41fbbb14a643c940feded688c23823cd761a9"
renewal_fingerprint = "222b2e3ab29b49705506c5681c0e6b10095ffbb4344259ac81a218a96ca68c80"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "python-dotenv"
version = "1.2.3"
expression = "BSD-3-Clause"
fingerprint = "f2a806a61574c57c864ef9b945d07533aec532c71e7ac0998bdc87a2e36de865"
renewal_fingerprint = "779ec04375369a4ed1f87b58fc9dcc2fc68a8603d83ff0b16f539d7228999ecd"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "python-multipart"
version = "0.0.32"
expression = "Apache-2.0"
fingerprint = "53142cc4f572a11bd1374bd05230de0c58a62c1e617b32f00402f6da6ba034eb"
renewal_fingerprint = "e06e7baeb1d6e0a594b416107782b9e89e31032f5bfee4abdc829bc088c1f651"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "referencing"
version = "0.37.0"
expression = "MIT"
fingerprint = "462fe664fcc0a28fb42877098ab8e87e56dca42770234a0385ecf65acd90e820"
renewal_fingerprint = "da8ab37f8ff2e6c2831fa3e94d429b5b9717d847e96ea6d87390f1bee390f997"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "requests"
version = "2.34.2"
expression = "Apache-2.0"
fingerprint = "885bcc4b9647d04b05c33ef4f54516b08fddadad036e8abb85b9f26d6caca256"
renewal_fingerprint = "3d8628f77791914f0a1695eafd9a5c819e987d4e7fc04366bed87e7bc2735140"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "robomate-cli"
version = "0.1.0"
expression = "MIT"
fingerprint = "3da2b6a4d44a8839366efc88fffbe00b41b93817a1b65e445506b476858e5b17"
renewal_fingerprint = "20b0677f8459a7f0006ffc8a3f31a636e0ba85f52c5cfc9e9856200a99ab66b2"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "robomate-win32-compat"
version = "0.1.0"
expression = "MIT"
fingerprint = "c021336faa8f42986e29a11511f128a495fc66c62ca7b9331905b4b99ce11b7c"
renewal_fingerprint = "b9190d2e03c98ed2e0943015b846f5309685fef2f584ab54a5b98b80cfeaa26e"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "robomate"
version = "0.1.0"
expression = "MIT"
fingerprint = "b339d9452ab89b76beaadfc369e0514351ad22bd336c6e583b1e37f726d3ba9c"
renewal_fingerprint = "7ad65681903a42987228ded78da3cf6de0a9f9060afe21d8f7aa95b9140177cc"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "rpds-py"
version = "2026.6.3"
expression = "MIT"
fingerprint = "c2434a90224cb1255704d4dcdb33612e105b55209dc16a7297f0a5813871f2ab"
renewal_fingerprint = "793a1bd0c3228b430c1eec07d7519e186eae87949cc2dab3c8db5c7b0b4e2a9a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "ruff"
version = "0.16.5"
expression = "MIT"
fingerprint = "48328448b38e00ce634f34c38c83e411d9d35d5d88e07010d6aea70dbf89853c"
renewal_fingerprint = "7b8141caf52a4186ee0d4a357a18a275c8c65b58a2c80d0c23f45d1e050d4e88"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Apache-2.0 OR MIT"]
or_choices = ["MIT"]

[[review]]
name = "sse-starlette"
version = "3.4.8"
expression = "BSD-3-Clause"
fingerprint = "627d0652cb43b89911e79148241776916d0fdd9fc4106eab7d27b81225dcd468"
renewal_fingerprint = "6d1f0723bac0df5ec4db553c8e0cae5e20fb896d5c356200f0cc8934519bf657"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "starlette"
version = "1.6.0"
expression = "BSD-3-Clause"
fingerprint = "009d0806814cf6d5e70059f511cf4bf8323b7dacb23304604f27623c5c012bc2"
renewal_fingerprint = "a3da0ef2bbebf1a72d5179b36969e759fd7ecb74fe0aa48260a94d0700c54948"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "typing-extensions"
version = "4.16.0"
expression = "PSF-2.0"
fingerprint = "e0e187ffd5e12099170259d5b578b9e1a04b4c4cf1294aac355831fb7d77ec6a"
renewal_fingerprint = "a26655d0db7882ddc6db5cf0190f91be31d2cb5f757d5e8129876579b3c4a833"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Python-2.0", "0BSD"]

[[review]]
name = "typing-inspection"
version = "0.4.4"
expression = "MIT"
fingerprint = "ff704c0f028029d5fec507a07d2b3fceca2a050a7305ec1cf65ac37dcf715332"
renewal_fingerprint = "a9be847a55739b2de54cf80d309efa151adb86ae241dfe900a82526ffd2fa75d"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "urllib3"
version = "2.8.0"
expression = "MIT"
fingerprint = "6149e5b5f033b721b500bbf88712e9e2c15631fef3d1c3444afeb42914b463eb"
renewal_fingerprint = "33d4868024f95b6da6d4b66d6e52afb00a9cdac3c641a3681fa212e93bcfa946"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "uvicorn"
version = "0.52.4"
expression = "BSD-3-Clause"
fingerprint = "ec7427d8448952bb4418d84ec28ea220cbf39a25660824f8f6ff6c11dbfb43d2"
renewal_fingerprint = "7720a286cc40a28d7a848ca8992cb0f916cece680e679bb16fd4761a399bd33c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "worker-mcp"
version = "0.1.0"
expression = "MIT"
fingerprint = "ea8011599561e8407633680736db9569bf6cdf8b579edc700fc931ea077764fd"
renewal_fingerprint = "9c21b977a5e69e679e1d9c431f8fc02fcf5d1aa35b995ddf53f0d5e0efc1232b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
```

## Review log

2026-10-11 — Implementation agent bob on behalf of RoboNater reviewed the
locked matrix notices (69 distinct notice contents) and metadata. 57 entries
qualify as permissive; certifi and pathspec are owner-approved exceptions
(operator question #1). The additional charset-normalizer sdist test-data
exception was approved in operator question #2. All 60 entries have inspected,
reviewed evidence within the declared matrix. Next full review: 2027-01-11 or
milestone close-out, whichever comes first.

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
