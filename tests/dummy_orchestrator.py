"""A dummy orchestrator, for tests only. It is not the Orchestrator and is never shipped.

Purpose: consume the capabilities library the way a real orchestrator would, using only what the registry
exposes, so the tests show what the registry must provide. The flow is:

1. Catalog: list the tools a caller may use and turn each into a model-facing tool definition
   (name, description, input schema), the same shape a model API takes. Each definition is bound to the
   exact version and identity it was built from.
2. Plan: a planner (a stand-in for the model) sees only those definitions and returns tool calls.
3. Dispatch, per call: map the name back to its pinned version, ask the grant stand-in (govsec's job), then
   run it through `run_tool`, which re-resolves, re-checks digests and revocation, validates input, runs it
   in the sandbox, validates output and writes the call record.

`interface_gaps` reports, per tool, what a model-facing orchestrator needs and the manifest does not say.

Ownership follows CAPABILITIES-DESIGN §2: grants are govsec's (here `FakeGrants`), dispatch and run state
are the orchestrator's (here), facts about tools are the registry's.
"""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field

from capability.calltime import CallError
from capability.callrecord import capability_ref, outcome_for
from capability.execute import run_tool
from capability.registry import Registry, RegistryError
from capability.sandbox import SandboxError

# The tool-name rule of the common model APIs (letters, digits, underscore, hyphen; at most 64).
MODEL_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class ToolBinding:
    """What a model-facing name stands for: the exact version and identity the definition was built from."""

    name: str
    version: str
    identity: str
    label: str
    requires: tuple


@dataclass(frozen=True)
class ToolCall:
    """One call a planner asks for: a model-facing tool name and its arguments."""

    name: str
    arguments: object


@dataclass
class StepResult:
    index: int
    name: str
    version: str | None
    call_id: str
    status: str  # ok | tool_failed | input_rejected | output_rejected | refused | error
    code: str | None
    output: str | None = None
    error: str | None = None


@dataclass
class RunReport:
    run_id: str
    principal: str
    steps: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.status == "ok" for step in self.steps)


class FakeGrants:
    """Stand-in for govsec. A principal holds a set of permissions; a tool's `requires` must all be held.

    Returns a grant reference (what the call record stores) or None for a denial. Nothing here is a real
    authorization decision.
    """

    def __init__(self, held: dict):
        self._held = {principal: frozenset(perms) for principal, perms in held.items()}
        self.asked = []

    def grant(self, principal: str, binding: ToolBinding) -> str | None:
        self.asked.append((principal, binding.name, binding.version))
        held = self._held.get(principal, frozenset())
        if not set(binding.requires) <= held:
            return None
        # The call record accepts a reference of [A-Za-z0-9._:/-] only; a real govsec grant id is opaque.
        return f"grant:{principal}/{binding.name}/{binding.version}"


class ScriptedPlanner:
    """Stand-in for a model. `script(definitions)` returns the ToolCalls; it may look only at the definitions."""

    def __init__(self, script):
        self._script = script
        self.seen = None

    def plan(self, definitions: list) -> list:
        self.seen = copy.deepcopy(definitions)
        return list(self._script(self.seen))


class DummyOrchestrator:
    def __init__(self, root=None, *, revocation, sandbox, grants, recorder, feed=None, registry=None,
                 languages=("python",), min_label: str = "Declared"):
        """`revocation` is required: an orchestrator never accepts an unchecked resolve.

        `feed`, when given, is polled before every call, as a running orchestrator would keep its client fresh.
        `registry` may be an S-018 StoreRegistry (built with the same revocation client); otherwise the S-002
        Registry over the file index at `root` is used.
        """
        if revocation is None:
            raise ValueError("an orchestrator must check revocation; pass a RevocationClient")
        self.registry = registry if registry is not None else Registry(root, revocation=revocation)
        self.revocation = revocation
        self.feed = feed
        self.sandbox = sandbox
        self.grants = grants
        self.recorder = recorder
        self.languages = tuple(languages)
        self.min_label = min_label

    # 1. Catalog -------------------------------------------------------------------------------------------

    def search_catalog(self, query: str, limit: int = 10) -> tuple[list, dict]:
        """S-018: definitions for the tools a search finds (what an orchestrator does at 100,000 tools)."""
        definitions, bindings = [], {}
        for language in self.languages:
            found = self.registry.search(query, "tool", self.min_label, language, limit)
            for item in found["items"]:
                if item["name"] in bindings:
                    continue
                definitions.append(copy.deepcopy(item["tool_definition"]))
                bindings[item["name"]] = ToolBinding(item["name"], item["version"], item["identity"], item["label"],
                                                     tuple(item["requires"]))
        return definitions, bindings

    def catalog(self) -> tuple[list, dict]:
        """Model-facing definitions for every tool at or above `min_label` with an implementation in one of
        `languages`, and the binding behind each name. Only the newest such version of each tool is offered."""
        newest = {}
        for row in self.registry.list(kind="tool", minLabel=self.min_label):
            if not set(row["languages"]) & set(self.languages):
                continue
            current = newest.get(row["name"])
            if current is None or _key(row["version"]) > _key(current["version"]):
                newest[row["name"]] = row
        definitions, bindings = [], {}
        for name in sorted(newest):
            described = self.registry.describe(name, version=newest[name]["version"], kind="tool")
            manifest = described["manifest"]
            definitions.append({
                "name": name,
                "description": manifest["summary"],
                "input_schema": copy.deepcopy(manifest["spec"]["inputSchema"]),
            })
            bindings[name] = ToolBinding(name, described["version"], described["identity"], described["label"],
                                         tuple(manifest.get("requires", [])))
        return definitions, bindings

    # 2 and 3. Plan and dispatch ---------------------------------------------------------------------------

    def run(self, principal: str, planner, *, project_root, stop_on_failure: bool = True,
            query: str | None = None) -> RunReport:
        """With `query`, the planner is offered what a registry search finds; without it, the whole catalog."""
        definitions, bindings = self.search_catalog(query) if query is not None else self.catalog()
        calls = planner.plan(definitions)
        report = RunReport(run_id=uuid.uuid4().hex[:12], principal=principal)
        for index, call in enumerate(calls):
            step = self._dispatch(report.run_id, index, principal, call, bindings, project_root)
            report.steps.append(step)
            if stop_on_failure and step.status != "ok":
                break
        return report

    def _dispatch(self, run_id, index, principal, call, bindings, project_root) -> StepResult:
        call_id = f"{run_id}.{index}"
        binding = bindings.get(call.name)
        if binding is None:  # the planner named something it was not offered
            return StepResult(index, call.name, None, call_id, "refused", "NotOffered")
        if self.feed is not None:
            self.revocation.poll(self.feed)
        grant = self.grants.grant(principal, binding)
        if grant is None:
            self._record_denial(call_id, binding)
            return StepResult(index, binding.name, binding.version, call_id, "refused", "GrantDenied")
        try:
            result = run_tool(self.registry, binding.name, call.arguments, project_root=project_root,
                              sandbox=self.sandbox, range=binding.version, min_label=self.min_label,
                              languages=self.languages, recorder=self.recorder, call_id=call_id, grant=grant)
        except (RegistryError, SandboxError, CallError) as exc:
            outcome = outcome_for(error=exc)
            return StepResult(index, binding.name, binding.version, call_id, outcome["status"], outcome["code"],
                              error=str(exc))
        outcome = outcome_for(result=result)
        return StepResult(index, binding.name, binding.version, call_id, outcome["status"], outcome["code"],
                          output=result.get("output"), error=result.get("error"))

    def _record_denial(self, call_id, binding):
        """A denied call is still a call: one refused record, resolved under the same checks as a run."""
        try:
            match = self.registry.resolve(binding.name, binding.version, kind="tool", minLabel=self.min_label,
                                          languages=list(self.languages))
        except RegistryError:
            return  # nothing resolved, so there is no capability to name (the same rule as run_tool)
        self.recorder.record(call_id, capability_ref(match, binding.identity),
                             {"status": "refused", "code": "GrantDenied"}, language=match.get("language"),
                             code_digest=match.get("code_digest"), revocation_seq=match.get("revocation_seq"))


def _key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


# Interface findings -----------------------------------------------------------------------------------------

def interface_gaps(definitions: list, manifests: dict) -> dict:
    """What a model-facing orchestrator needs and each tool's manifest does not provide.

    `manifests` maps a tool name to its embedded manifest (from `describe`). Returns {name: [gap, ...]};
    a tool with nothing missing is left out.
    """
    gaps = {}
    for definition in definitions:
        name = definition["name"]
        spec = manifests[name]["spec"]
        found = []
        if MODEL_TOOL_NAME.fullmatch(name) is None:
            found.append("name is not a valid model tool name")
        schema = definition["input_schema"]
        for prop, body in sorted(schema.get("properties", {}).items()):
            if not isinstance(body, dict) or not body.get("description"):
                found.append(f"parameter {prop!r} has no description")
        if "outputSchema" not in spec and "outputDescription" not in spec:
            found.append("no declared output format (the result is an opaque string)")
        if not spec.get("errors"):
            found.append("no declared error cases")
        if found:
            gaps[name] = found
    return gaps
