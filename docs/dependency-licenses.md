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
fingerprint = "0a50a69a089e5f9e1be966f08411a4f6ddf8ec25c25201da09bebf45809f342e"
renewal_fingerprint = "24022b539fcb35eb6c58cc9de50dca6c9f66e5c3aa27178dfb552e7c855a0034"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "agent-hub-common"
version = "0.1.0"
expression = "MIT"
fingerprint = "404b9ca5db24b8e139502032894c45e2bdb9fd43f520db8b32e2c8ea78627adb"
renewal_fingerprint = "6751d3fd9e8c6ff5d5bf552e8d881c0603d251bec7bf649a67619eceb52a031a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "agent-hub"
version = "0.1.0"
expression = "MIT"
fingerprint = "e20187117a83df643ca74254473654c240a2e5986d1ce5ebfc599d4c0285da68"
renewal_fingerprint = "b9ae7539f9bb7a567743966ed0ef4467dac2dc7db676dee0441cc6f0e87386e5"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "annotated-doc"
version = "0.0.5"
expression = "MIT"
fingerprint = "85167ff41b74abdae96c82976603e4aa303c564a4bbd1c1d59be82e2e63ef671"
renewal_fingerprint = "b849ffa56531e73b21d32f5695ad9045c38b8c03e6867d88198bb90455be7858"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "annotated-types"
version = "0.8.0"
expression = "MIT"
fingerprint = "4af43abf30cc937c6397f845e7617a366502a25f90301ae27e05069f4a585c0e"
renewal_fingerprint = "10a41b43be4e596ec524a7e9f9dc52dcbfb88056d02d1b953d0b484ef2b5248b"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "anyio"
version = "4.15.0"
expression = "MIT"
fingerprint = "7a6afa441a99076edabc1d7c90a955993c8f007221dfcfce8e9067924b0cfba9"
renewal_fingerprint = "d17b1a71d99d717a13840ab3e46f4a936e4a5dfca6e521d13d82884945254e1d"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "attrs"
version = "26.1.0"
expression = "MIT"
fingerprint = "4af26fb082dc159be5f987ad02500e1da8e76cff90c3fabef9f46237b2d799d9"
renewal_fingerprint = "635e4b9a37b79cc6220201ab577ae6db278fe04be722307a32182235bc778265"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "certifi"
version = "2026.7.22"
expression = "MPL-2.0"
fingerprint = "4d2dd0950a981807e9e74cdca8f7e16e8e3e59eb47c8dcbddb6154183da2727c"
renewal_fingerprint = "d4230eae29f35b46ef12813cdd08edf4ce03827f995dbbe41571ef8a28ac7de4"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
exception = true
owner_approval = "operator question #1, answered 2026-10-12T00:40:18.892Z; verified through get_operator_answer(1)"

[[review]]
name = "cffi"
version = "2.1.1"
expression = "MIT-0"
fingerprint = "482cf489cdc5d0ec2a8b06e3c18d5994ac522a5d00cffaab1478a4a41b472aa3"
renewal_fingerprint = "5e264df5cd8042b1a9877eed9a486b95aebeea45f7428a1d17fe5221d46e5657"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["MIT"]

[[review]]
name = "charset-normalizer"
version = "3.5.1"
expression = "MIT"
fingerprint = "47d137f0e02220e885905adb3bb45453d97fcf511c77de30aa14b4693032caf3"
renewal_fingerprint = "35dde133e72303e56b680befcdc96040f7fe259bd55368e5b7abb7d5509ad922"
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
fingerprint = "88098bd747bd8c48713a3d31a2efaab0450f710bc3b12d9ee8ab121d1ec2486b"
renewal_fingerprint = "00faeb75806e69a6de756ca67e9c7467abc8839d6b94be872a39593892aa8673"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "e7b29ffad63060ce073de2e914d0cdace060babd138da3bb3c2ef2f3b9ee389a"
renewal_fingerprint = "cd36842d6e8179072f03490eaa5a175c6ea57885b5a81b149003c332ab803792"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
or_choices = ["Apache-2.0"]

[[review]]
name = "fastapi"
version = "0.141.1"
expression = "MIT"
fingerprint = "6783bc65e2fc85f7e779ffbdd43edf8284d6e3ed6daed0a406c6e0a61e388f6d"
renewal_fingerprint = "bd2acb52bb4ea5cb4fd9a69f3ff527a4e1970d28102dd8c4fbb982c75c80ba94"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "google-api-core"
version = "2.34.0"
expression = "Apache-2.0"
fingerprint = "b0c3e006807b40d20c8028df061de7fcb8562d474eba30665e19358bb72f88d4"
renewal_fingerprint = "e89e98cef715dbca123c2f7be28b841106ba78807cf8ec14bf6cbad457976efa"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "google-auth"
version = "2.57.0"
expression = "Apache-2.0"
fingerprint = "3b80f982b62c3abc043748775a53a4e14e98ea3236c89fffa5241d8f9a88ab26"
renewal_fingerprint = "d6a3921312e980294b4ebbcad64619d18e587bdf483c602ec7f550cacb9092f0"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "googleapis-common-protos"
version = "1.75.2"
expression = "Apache-2.0"
fingerprint = "87223bee0402d2be5613e9e3817d6e55ae4612bd586b317a7fba4aada354ca03"
renewal_fingerprint = "84eb8b0b189b758933c6a056737253698fba708ab974dca19f0025dc2277bac8"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "h11"
version = "0.16.0"
expression = "MIT"
fingerprint = "84d705040306d903fa443b9a193b0e782c662335c8a0fddd1720a427c290286f"
renewal_fingerprint = "4fc9952688a5294fd8bfcabd830eb093441c0cfaee2f2f0e1a8549f7789c0506"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "httpcore"
version = "1.0.9"
expression = "BSD-3-Clause"
fingerprint = "ace939da8c85e0772426b5600b2e78a756c9e85176bbc4639e1388120a9b9c97"
renewal_fingerprint = "918fbe842194276673858bba3d049c741fb87df3f13bfbf190b3ed78181bcc7a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "httpx-sse"
version = "0.4.3"
expression = "MIT"
fingerprint = "7b98861e9b17ea16c81b1a47dc94c422344ece3e25bded6983da42df0acdd437"
renewal_fingerprint = "81f0b163d4c0bb304bb0d6e2cd658e9e56560c0e3e0b6a40f734cfea30c0ec84"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "httpx"
version = "0.28.1"
expression = "BSD-3-Clause"
fingerprint = "dd8f6697bdaf408df9431403b6d2602762a2c89685e6c0b0047d11fdf8e60fb7"
renewal_fingerprint = "004de98b8167b9d4b623c9fde058af06470bc9fb302d33bc464c71774b5ff2f4"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "idna"
version = "3.19"
expression = "BSD-3-Clause"
fingerprint = "112fb50e6a6a213e43fd418dae859d1ba2cdfc9329e0b475c82ba5c367136748"
renewal_fingerprint = "21d83568f6207663e47252e96289cee13c20245c40611b5d0301ca15eb29b7eb"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "c3e927aa08569da642841e04ecb212ea96b03cb8bae8c9863e5f11aa407b7bcb"
renewal_fingerprint = "7ac0fce7bd91169c0003fb473f4c9c572f0a68abbce64cac63d8cbd62396d9ea"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "jsonschema"
version = "4.26.0"
expression = "MIT"
fingerprint = "85dc1c2acb936e90b5a665d0c57f095919103d979c722744884d8f0eb027bfd9"
renewal_fingerprint = "ce91fe0a1258e99ff42687e861ddafdfb3d54bc936807d7e5df21e2c4c0441f5"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "578334ec0fa3cdc230505e43e99044c8cbb1f90f7139e6984cb65742b2db65b1"
renewal_fingerprint = "8190d82ae01e0dc7cff33315c00a2a462dbc3016318c59d7a1241e4c98acc9e8"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "8e94a15a5727d795b410930edb240c78c071c9e72d41d9924aed336a5c52762a"
renewal_fingerprint = "e2e5675331d04baea00a10def1aa39e0768bdcf74bf72892b905e449e428a555"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "protobuf"
version = "7.36.1"
expression = "BSD-3-Clause"
fingerprint = "78cd3b47928f14d6a3727f457472aeb2213e6e9668c5a92380dc9cac83b89b23"
renewal_fingerprint = "ee8aef83875b20a809dbdddf91dba0e6e6e01985fd2575e3c49d02e53fa63e97"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pyasn1-modules"
version = "0.4.2"
expression = "BSD-2-Clause"
fingerprint = "2138a9455a5fef951733123c6b1376cc4873d791927791aee958473dc77c2787"
renewal_fingerprint = "7eed209e877ee335dd8445e86674e6b101f36c43e54a77293304dcad1eb3a95c"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pyasn1"
version = "0.6.4"
expression = "BSD-2-Clause"
fingerprint = "07bb2f8a66e0093a6775affe18261de1f9504693a626b4dff8ba42995ee3c71f"
renewal_fingerprint = "ac5fe226f9788a605cf5d68cf1ccd74c3033207bd004cb31347a7db923adbf66"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pycparser"
version = "3.0"
expression = "BSD-3-Clause"
fingerprint = "059ce1aefb7ea1970e9baf8b7c1f16b943fdcba44bcff11ff31b271cfd0b4dc7"
renewal_fingerprint = "6e605ce4fa612df59f9d9a54841520bfeae840c37604ba423e29d31c40e22396"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pydantic-core"
version = "2.46.5"
expression = "MIT"
fingerprint = "346518a2e1831e804c291ac3d49a5ecbfc1596a00909d1bbb5a71d65bb360c68"
renewal_fingerprint = "9ee0f2ebea3511dfaa3d6b5c1b6774d0964687bf7dfb6a581fc2bd80fa448202"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pydantic-settings"
version = "2.15.0"
expression = "MIT"
fingerprint = "a69267a3760fa0e902779460392193c2acad7772ee744d8238c205d840e108d6"
renewal_fingerprint = "c38e9aaadb826ba7b8e2244528fe03a37790ea79092bc4cb9f7c5e13898c3723"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "pydantic"
version = "2.13.5"
expression = "MIT"
fingerprint = "414120d2b5352054df35dc460a49d64f44c838d0c7a3dc27beb81d53cf3748bb"
renewal_fingerprint = "c0a61686df9d402ad4c6fb3e88ba1249e1516a88050de375cde1a08579c54ce5"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "59256ec6d64b8851f99b8cabd5e31c1c34f782f8a0aae7750c9f998b98d7412c"
renewal_fingerprint = "94e464368f23e3f3a3622d3c1bbdf6e470882a8aaa1746ed4731a0a03e79bc78"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "4d60946a57d7c8d072faa658f79caa6513b029155b11f9e4f507d2f4e50a3970"
renewal_fingerprint = "ce0c2e0d707acce0dd39da92eee46886dc663ec56e51e80c74d6985822bcac04"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "python-multipart"
version = "0.0.32"
expression = "Apache-2.0"
fingerprint = "f8e5d26cd1da2ba018a2c26e7bf856548cce2a96fdc285d043499fd8b04ab3bf"
renewal_fingerprint = "a1858e37cb1d387eb543c565ed284aca31675a5c6d59897c3744c968fd99b953"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "referencing"
version = "0.37.0"
expression = "MIT"
fingerprint = "ceee46321b5edbfabadbfa4f6b0b47fbb4db79ac87fb33af47c79861ddfdca71"
renewal_fingerprint = "a10cd19e765738d0db1b642aa6d9e076271a40c15ad9c50e0a98e3a55ba9281a"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "requests"
version = "2.34.2"
expression = "Apache-2.0"
fingerprint = "42432796cc86995a99ba67460d36d21950e5286b2815bb5f19865744f509e131"
renewal_fingerprint = "9e5b2777e91f27cecfaaa97c575d002f575607b7ef4306781772b244a8859342"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "0aaa8b0c69f7474d071ad73fa1fc7caf1682bf982e824d46f31f6d5f56b10836"
renewal_fingerprint = "a43825ec700d42d25b9932be25d4bfce99720e800cac275e313bb616fefadc72"
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
fingerprint = "62a7915abd5ae36b730fc032ddf4ed804574e42b28ca2b88e1c9eb9281a28480"
renewal_fingerprint = "c3617b813917a5a9bbb5bed972b629d69fcbbb0154eec71af3a0250c22bc2437"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

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
fingerprint = "a44793fc70821cfb86b86b964468c5502f60155b05c84b86afba8853191c91c8"
renewal_fingerprint = "74acadcfc61cd143ef43f9487edc01f05e7ed2acd3280c8a55ff89e67116724e"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "starlette"
version = "1.6.0"
expression = "BSD-3-Clause"
fingerprint = "dd9c92fe129f885394216d02e3589f93e2bebe6565f0723bc285e31d477fb410"
renewal_fingerprint = "8321ed461bcc2802d05743481d0734ba2c7965b9256eabe2e6b2f437aa754cf8"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "typing-extensions"
version = "4.16.0"
expression = "PSF-2.0"
fingerprint = "5399ea9caa58574bcd9b631cff1f868d480b48a3aedf97aa4faf474c2b7bc378"
renewal_fingerprint = "5b346c04087078e6cdf0ba2f86a6e079298c161c9d1b7738e6223ef0ef9b77e2"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"
components = ["Python-2.0", "0BSD"]

[[review]]
name = "typing-inspection"
version = "0.4.4"
expression = "MIT"
fingerprint = "c43e44716c8b39190686e3f9bfd9b936bab9d6cd184c3a8d01893f9f0335ab1b"
renewal_fingerprint = "73da283cca5e096784a695b3a25159637faa5c12542872962460a97ed9819f57"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "urllib3"
version = "2.8.0"
expression = "MIT"
fingerprint = "b637589a4fedc8f6730bedc97d86b230352ed69d6078524a14260e5246b4ebe5"
renewal_fingerprint = "342a0ded8ecb3bd867ad3aa5611279ef13af112a6842aa4ae4947987726e8930"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "uvicorn"
version = "0.52.4"
expression = "BSD-3-Clause"
fingerprint = "3b3142466cd79bf2f5bc4094334c15f2e831d7353d197e13c5dabca855484849"
renewal_fingerprint = "4238ac294ab86afcad5977f1864d4ab96b6af4db87368f1f532c93afbddcaacc"
reviewer = "Implementation agent bob on behalf of RoboNater"
review_date = "2026-10-11"

[[review]]
name = "worker-mcp"
version = "0.1.0"
expression = "MIT"
fingerprint = "185aa435438ab5ebda4a31982195228682c053b0fff608944949359ad074d462"
renewal_fingerprint = "a9fbe0866ed7fe386a84df68cd5f71bacb71d83f46b56eec39a083c6770ce0d4"
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
