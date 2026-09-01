"""Deterministic, review-only configuration proposal generation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import yaml

from cli.recipe_package import literal_credential_paths

INTENT_SCHEMA_VERSION = "vllm-sr/config-proposal-intent/v1"
PROVENANCE_SCHEMA_VERSION = "vllm-sr/config-proposal-provenance/v1"
VALIDATION_CONTRACT_VERSION = "v1"
PROPOSED_CONFIG_FILENAME = "proposed-config.yaml"
DIFF_FILENAME = "proposal-diff.json"
PROVENANCE_FILENAME = "provenance.json"
VALIDATION_FILENAME = "validation.json"
PROPOSAL_FILES = (
    PROPOSED_CONFIG_FILENAME,
    DIFF_FILENAME,
    PROVENANCE_FILENAME,
    VALIDATION_FILENAME,
)
_FRAGMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*$")
_MAX_INTENT_BYTES = 256 << 10
_MAX_FRAGMENT_BYTES = 4 << 20
_MAX_FRAGMENTS = 32
_MAX_GOAL_LENGTH = 512


class ConfigProposalError(ValueError):
    """Raised when a safe, deterministic proposal cannot be produced."""


@dataclass(frozen=True)
class ConfigProposalResult:
    """Paths and stable digests for one generated proposal."""

    output_dir: Path
    proposed_config: Path
    diff: Path
    provenance: Path
    validation: Path
    base_sha256: str
    proposal_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "base_sha256": self.base_sha256,
            "diff": str(self.diff),
            "output_dir": str(self.output_dir),
            "proposal_sha256": self.proposal_sha256,
            "proposed_config": str(self.proposed_config),
            "provenance": str(self.provenance),
            "validation": str(self.validation),
        }


@dataclass(frozen=True)
class _Intent:
    goal: str
    fragments: tuple[str, ...]
    raw_sha256: str


@dataclass(frozen=True)
class _Fragment:
    fragment_id: str
    document: dict[str, Any]
    sha256: str


ValidationRequest = Callable[[str, str, float], dict[str, Any]]


def propose_config(
    *,
    base_config: Path,
    intent_path: Path,
    fragment_root: Path,
    output_dir: Path,
    validation_endpoint: str = "http://localhost:8080",
    validation_timeout: float = 10.0,
    validation_request: ValidationRequest | None = None,
    force: bool = False,
) -> ConfigProposalResult:
    """Generate review artifacts without changing or applying Router config."""

    base_path = _require_regular_file(base_config, "Base configuration")
    intent_file = _require_regular_file(intent_path, "Proposal intent")
    root = _require_real_directory(fragment_root, "Fragment root")
    intent = _load_intent(intent_file)
    fragments = tuple(
        _load_fragment(root, fragment_id) for fragment_id in intent.fragments
    )

    base_bytes = _read_bounded_file(base_path, "Base configuration", 16 << 20)
    base_document = _load_yaml_mapping(base_bytes, "Base configuration")
    if base_document.get("version") != "v0.3":
        raise ConfigProposalError("Base configuration version must be v0.3")

    proposed = copy.deepcopy(base_document)
    for fragment in fragments:
        proposed = _merge_value(proposed, fragment.document, path="")

    proposed_text = yaml.safe_dump(proposed, sort_keys=False, allow_unicode=True)
    credential_paths = literal_credential_paths(proposed_text.encode("utf-8"))
    if credential_paths:
        raise ConfigProposalError(
            "Proposed configuration contains a literal credential-like value at "
            f"{credential_paths[0]}; use an environment reference"
        )

    request_validation = validation_request or _request_router_validation
    validation = request_validation(
        proposed_text,
        validation_endpoint,
        validation_timeout,
    )
    normalized_yaml = _validated_candidate_yaml(validation)
    diff = _validated_diff(validation)

    base_sha256 = _sha256(base_bytes)
    proposal_bytes = normalized_yaml.encode("utf-8")
    proposal_sha256 = _sha256(proposal_bytes)
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "base": {
            "config_version": "v0.3",
            "sha256": base_sha256,
        },
        "intent": {
            "schema_version": INTENT_SCHEMA_VERSION,
            "sha256": intent.raw_sha256,
            "goal_sha256": _sha256(intent.goal.encode("utf-8")),
        },
        "sources": [
            {
                "kind": "maintained_fragment",
                "id": fragment.fragment_id,
                "revision_sha256": fragment.sha256,
            }
            for fragment in fragments
        ],
        "proposal": {
            "sha256": proposal_sha256,
            "validation_contract_version": validation["contract_version"],
        },
    }

    destination = output_dir.expanduser()
    _prepare_output_dir(destination, force=force)
    artifacts = {
        PROPOSED_CONFIG_FILENAME: proposal_bytes,
        DIFF_FILENAME: _json_bytes(diff),
        PROVENANCE_FILENAME: _json_bytes(provenance),
        VALIDATION_FILENAME: _json_bytes(validation),
    }
    for filename, content in artifacts.items():
        _write_regular_file(destination / filename, content)

    return ConfigProposalResult(
        output_dir=destination,
        proposed_config=destination / PROPOSED_CONFIG_FILENAME,
        diff=destination / DIFF_FILENAME,
        provenance=destination / PROVENANCE_FILENAME,
        validation=destination / VALIDATION_FILENAME,
        base_sha256=base_sha256,
        proposal_sha256=proposal_sha256,
    )


def _request_router_validation(
    proposed_yaml: str,
    endpoint: str,
    timeout: float,
) -> dict[str, Any]:
    url = _validation_url(endpoint)
    try:
        response = requests.post(
            url,
            json={"yaml": proposed_yaml, "compare_to_active": True},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise ConfigProposalError(
            f"Canonical Router validation request failed at {url}"
        ) from error
    try:
        payload = response.json()
    except requests.JSONDecodeError as error:
        raise ConfigProposalError(
            "Canonical Router validation returned invalid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise ConfigProposalError(
            "Canonical Router validation returned an invalid response"
        )
    return payload


def _validation_url(endpoint: str) -> str:
    base = endpoint.strip()
    if not base:
        raise ConfigProposalError("Router validation endpoint is required")
    if base.endswith("/config/router/validate"):
        return base
    return urljoin(base.rstrip("/") + "/", "config/router/validate")


def _validated_candidate_yaml(validation: dict[str, Any]) -> str:
    if validation.get("contract_version") != VALIDATION_CONTRACT_VERSION:
        raise ConfigProposalError(
            "Router validation must provide contract_version v1 from #3477"
        )
    if validation.get("valid") is not True:
        errors = validation.get("errors")
        summary = _diagnostic_summary(errors)
        raise ConfigProposalError(f"Proposed configuration is invalid: {summary}")
    normalized = validation.get("normalized_yaml")
    if not isinstance(normalized, str) or not normalized.strip():
        raise ConfigProposalError("Router validation response omitted normalized_yaml")
    return normalized


def _validated_diff(validation: dict[str, Any]) -> dict[str, Any]:
    diff = validation.get("diff")
    if not isinstance(diff, dict):
        raise ConfigProposalError(
            "Router validation response omitted the active-versus-candidate diff"
        )
    for field in ("added", "removed", "changed"):
        if not isinstance(diff.get(field), list):
            raise ConfigProposalError(
                f"Router validation diff omitted the {field} entries"
            )
    if not any(diff[field] for field in ("added", "removed", "changed")):
        raise ConfigProposalError(
            "The selected fragments do not change the active configuration"
        )
    return diff


def _diagnostic_summary(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "canonical validation failed without diagnostics"
    first = value[0]
    if not isinstance(first, dict):
        return "canonical validation returned malformed diagnostics"
    code = first.get("code")
    field = first.get("field")
    message = first.get("message")
    parts = [str(part) for part in (code, field, message) if part]
    return " | ".join(parts) or "canonical validation failed"


def _load_intent(path: Path) -> _Intent:
    raw = _read_bounded_file(path, "Proposal intent", _MAX_INTENT_BYTES)
    document = _load_yaml_mapping(raw, "Proposal intent")
    allowed = {"schema_version", "goal", "fragments"}
    extra = sorted(set(document) - allowed)
    if extra:
        raise ConfigProposalError(
            "Proposal intent contains unsupported fields: " + ", ".join(extra)
        )
    if document.get("schema_version") != INTENT_SCHEMA_VERSION:
        raise ConfigProposalError(
            f"Proposal intent schema_version must be {INTENT_SCHEMA_VERSION}"
        )
    goal = document.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > _MAX_GOAL_LENGTH:
        raise ConfigProposalError(
            f"Proposal intent goal must be 1-{_MAX_GOAL_LENGTH} characters"
        )
    raw_fragments = document.get("fragments")
    if (
        not isinstance(raw_fragments, list)
        or not raw_fragments
        or len(raw_fragments) > _MAX_FRAGMENTS
    ):
        raise ConfigProposalError(
            f"Proposal intent fragments must contain 1-{_MAX_FRAGMENTS} entries"
        )
    fragments: list[str] = []
    for value in raw_fragments:
        if not isinstance(value, str) or not _FRAGMENT_ID_PATTERN.fullmatch(value):
            raise ConfigProposalError(
                "Fragment IDs must be lowercase relative IDs such as "
                "signal/complexity/escalation"
            )
        if value in fragments:
            raise ConfigProposalError(f"Duplicate fragment ID: {value}")
        fragments.append(value)
    return _Intent(
        goal=goal.strip(),
        fragments=tuple(fragments),
        raw_sha256=_sha256(raw),
    )


def _load_fragment(root: Path, fragment_id: str) -> _Fragment:
    path = root.joinpath(*fragment_id.split("/")).with_suffix(".yaml")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ConfigProposalError(f"Unknown fragment ID: {fragment_id}") from error
    if not resolved.is_relative_to(root.resolve()):
        raise ConfigProposalError(f"Fragment escapes the fragment root: {fragment_id}")
    raw = _read_bounded_file(resolved, f"Fragment {fragment_id}", _MAX_FRAGMENT_BYTES)
    return _Fragment(
        fragment_id=fragment_id,
        document=_load_yaml_mapping(raw, f"Fragment {fragment_id}"),
        sha256=_sha256(raw),
    )


def _merge_value(base: Any, addition: Any, *, path: str) -> Any:
    if isinstance(base, dict) and isinstance(addition, dict):
        merged = copy.deepcopy(base)
        for key, value in addition.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in merged:
                merged[key] = copy.deepcopy(value)
            else:
                merged[key] = _merge_value(merged[key], value, path=child_path)
        return merged

    if isinstance(base, list) and isinstance(addition, list):
        return _merge_list(base, addition, path=path)

    if base == addition:
        return copy.deepcopy(base)

    raise ConfigProposalError(
        f"Fragment conflicts with existing configuration at {path or '<root>'}"
    )


def _merge_list(base: list[Any], addition: list[Any], *, path: str) -> list[Any]:
    if not addition:
        return copy.deepcopy(base)
    combined = [*base, *addition]
    if all(_named_mapping(item) for item in combined):
        merged = copy.deepcopy(base)
        positions = {str(item["name"]): index for index, item in enumerate(merged)}
        for item in addition:
            name = str(item["name"])
            child_path = f"{path}[name={name}]"
            if name in positions:
                index = positions[name]
                merged[index] = _merge_value(merged[index], item, path=child_path)
            else:
                positions[name] = len(merged)
                merged.append(copy.deepcopy(item))
        return merged

    if not base:
        return copy.deepcopy(addition)
    if base == addition:
        return copy.deepcopy(base)
    raise ConfigProposalError(
        f"Fragment list conflicts with existing configuration at {path or '<root>'}; "
        "only lists of mappings with unique name fields can be merged"
    )


def _named_mapping(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and bool(value["name"].strip())
    )


def _prepare_output_dir(path: Path, *, force: bool) -> None:
    if path.is_symlink():
        raise ConfigProposalError(
            f"Proposal output directory must not be a symbolic link: {path}"
        )
    if path.exists() and not path.is_dir():
        raise ConfigProposalError(f"Proposal output path is not a directory: {path}")
    if path.exists():
        artifact_paths = [path / filename for filename in PROPOSAL_FILES]
        unsafe = [
            artifact
            for artifact in artifact_paths
            if artifact.is_symlink() or (artifact.exists() and not artifact.is_file())
        ]
        if unsafe:
            raise ConfigProposalError(
                "Proposal output contains an unsafe artifact path: "
                + ", ".join(str(artifact) for artifact in unsafe)
            )
        existing = [artifact for artifact in artifact_paths if artifact.exists()]
        if existing and not force:
            raise ConfigProposalError(
                "Proposal output already contains generated artifacts; use --force "
                "to replace them"
            )
    path.mkdir(parents=True, exist_ok=True)


def _require_regular_file(path: Path, label: str) -> Path:
    resolved = path.expanduser()
    if resolved.is_symlink() or not resolved.is_file():
        raise ConfigProposalError(f"{label} must be a regular file: {resolved}")
    return resolved


def _require_real_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser()
    if resolved.is_symlink() or not resolved.is_dir():
        raise ConfigProposalError(f"{label} must be a real directory: {resolved}")
    return resolved


def _read_bounded_file(path: Path, label: str, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ConfigProposalError(
            f"{label} must be a regular file within {limit} bytes"
        )
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ConfigProposalError(f"{label} exceeds the {limit}-byte limit")
    return data


def _load_yaml_mapping(raw: bytes, label: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigProposalError(f"{label} is not valid UTF-8 YAML") from error
    if not isinstance(document, dict) or not all(
        isinstance(key, str) for key in document
    ):
        raise ConfigProposalError(f"{label} must contain one YAML mapping")
    return document


def _write_regular_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
