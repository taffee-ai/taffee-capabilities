"""Label policy v2 (S-001 §7; T-001).

Declared: every valid manifest. The manifest validates and its digests compute.

Validated (tools only): Declared, and the tool has at least one implementation, and a case
file at library/<name>/conformance.json, and every implementation passes every case in the
conformance runner. The digest of the cases is recorded as evidence, so the label names the
exact cases it was earned on. A tool that has cases and fails them refuses the build: the
catalog never lists a tool whose own cases fail, and a label never drops silently.

Tested (tools only): Validated, and an evidence record (capability/evidence.py) that belongs to this exact
tool identity and these exact cases: nothing failed, errored or was skipped, and the fingerprint recomputes.
A tool that changes after its evidence was written has a new identity, so it falls back to Validated.

Trusted (A-002) and Approved (M-001) are not awarded here.

policy/labels.yaml records the same rules. The label is never read from a manifest, so a manifest
cannot claim a label it has not earned.
"""

import json
import os
from pathlib import Path

import yaml

from .conformance import ConformanceError, cases_digest, load_cases, run_cases
from .evidence import EVIDENCE_DIR, check_record
from .implementations import tool_identity

POLICY_VERSION = 2
POLICY_FILE = Path("policy") / "labels.yaml"
CASES_FILE = "conformance.json"


class PolicyError(Exception):
    """The policy file is missing or is not the version this code implements."""


class LabelError(Exception):
    """A tool's own cases could not be run, or failed. The build must refuse it."""


def check_policy(root: Path) -> None:
    """Refuse to label anything under a policy file this code does not implement."""
    path = root / POLICY_FILE
    if not path.is_file():
        raise PolicyError(f"{path}: label policy file is missing")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("policy_version") != POLICY_VERSION:
        raise PolicyError(f"{path}: this code implements policy_version {POLICY_VERSION}")


def label_for(manifest: dict, root: Path) -> str:
    """The label for agents and models: Declared. Their higher labels belong to A-002 and M-001."""
    del manifest, root
    return "Declared"


def tool_label(manifest: dict, root: Path, implementations: list[dict], languages: dict) -> tuple[str, list[str]]:
    """The label and evidence references for a tool, running its cases when it has any.

    Raises LabelError when cases exist and cannot be run or do not all pass.
    """
    cases_path = root / "library" / manifest["name"] / CASES_FILE
    if not os.path.lexists(cases_path):
        return "Declared", []
    # Present is not the same as absent. Absent means no cases (Declared). Present and unusable, or present
    # with nothing to run them on, is a tool that claims cases it cannot run, which refuses the build (D9).
    if cases_path.is_symlink() or not cases_path.is_file():
        raise LabelError(f"{manifest['name']}: {CASES_FILE} is present but is not a regular file "
                         "(a directory, a link, or a broken link)")
    if not implementations:
        raise LabelError(f"{manifest['name']}: {CASES_FILE} is present but the tool has no implementation to run it")
    try:
        cases = load_cases(cases_path)
        for item in implementations:
            language = languages[item["language"]]
            failures = run_cases(root / item["location"], language["entry"], language["runner"],
                                 language["toolchain"], cases, spec=manifest["spec"])
            if failures:
                raise LabelError(f"{manifest['name']} ({item['language']}) fails its conformance cases: "
                                 + "; ".join(failures[:5]) + (" ..." if len(failures) > 5 else ""))
    except ConformanceError as exc:
        raise LabelError(f"{manifest['name']}: conformance could not be run: {exc}") from exc
    digest = cases_digest(cases)
    evidence = ["conformance:" + digest]
    name, version = manifest["name"], manifest["version"]
    identity = tool_identity(manifest, implementations)
    if check_record(root, name, version, identity, digest, POLICY_VERSION) is None:
        summary = json.loads((root / EVIDENCE_DIR / name / version / "summary.json").read_text(encoding="utf-8"))
        return "Tested", evidence + ["tests:" + summary["fingerprint"], "record:" + summary["evidence_digest"]]
    return "Validated", evidence
