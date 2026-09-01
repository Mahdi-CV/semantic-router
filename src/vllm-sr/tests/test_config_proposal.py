import json
from pathlib import Path

import pytest
import yaml
from cli import config_proposal
from cli.config_proposal import ConfigProposalError, propose_config
from cli.main import main
from click.testing import CliRunner

_REAL_VALIDATION_REQUEST = config_proposal._request_router_validation


@pytest.fixture(autouse=True)
def canonical_validation(monkeypatch: pytest.MonkeyPatch):
    def validate(proposed_yaml: str, endpoint: str, timeout: float) -> dict:
        assert endpoint
        assert timeout > 0
        return {
            "valid": True,
            "contract_version": "v1",
            "normalized_yaml": proposed_yaml,
            "errors": [],
            "warnings": [],
            "diff": {
                "added": [
                    {
                        "field": 'routing.signals.complexity["needs_reasoning"]',
                        "new": {"name": "needs_reasoning"},
                    }
                ],
                "removed": [],
                "changed": [],
                "truncated": False,
            },
        }

    monkeypatch.setattr(config_proposal, "_request_router_validation", validate)


def _write_base(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": "v0.3",
                "listeners": [],
                "providers": {
                    "defaults": {"default_model": "test-model"},
                    "models": [
                        {
                            "name": "test-model",
                            "backend_refs": [
                                {
                                    "endpoint": "127.0.0.1:8000",
                                    "protocol": "http",
                                    "weight": 1,
                                }
                            ],
                        }
                    ],
                },
                "routing": {
                    "modelCards": [{"name": "test-model"}],
                    "signals": {
                        "keywords": [
                            {
                                "name": "existing",
                                "operator": "OR",
                                "method": "bm25",
                                "keywords": ["keep-me"],
                                "case_sensitive": False,
                                "bm25_threshold": 0.1,
                            }
                        ]
                    },
                    "decisions": [
                        {
                            "name": "fallback",
                            "priority": 1,
                            "rules": {"operator": "AND", "conditions": []},
                            "modelRefs": [{"model": "test-model"}],
                        }
                    ],
                },
                "global": {"router": {"config_source": "file"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_fragment(root: Path, fragment_id: str, document: dict) -> None:
    path = root.joinpath(*fragment_id.split("/")).with_suffix(".yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_intent(path: Path, fragments: list[str]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "vllm-sr/config-proposal-intent/v1",
                "goal": "Add bounded routing behavior without activation.",
                "fragments": fragments,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = tmp_path / "config.yaml"
    intent = tmp_path / "intent.yaml"
    fragment_root = tmp_path / "fragments"
    _write_base(base)
    _write_intent(intent, ["signal/complexity/escalation"])
    _write_fragment(
        fragment_root,
        "signal/complexity/escalation",
        {
            "routing": {
                "signals": {
                    "complexity": [
                        {
                            "name": "needs_reasoning",
                            "threshold": 0.1,
                            "hard": {"candidates": ["solve this step by step"]},
                            "easy": {"candidates": ["answer briefly"]},
                        }
                    ]
                }
            }
        },
    )
    return base, intent, fragment_root


def test_proposal_is_deterministic_and_preserves_unrelated_config(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)

    first = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=tmp_path / "first",
    )
    second = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=tmp_path / "second",
    )

    for filename in (
        "proposed-config.yaml",
        "proposal-diff.json",
        "provenance.json",
        "validation.json",
    ):
        assert (first.output_dir / filename).read_bytes() == (
            second.output_dir / filename
        ).read_bytes()

    proposed = yaml.safe_load(first.proposed_config.read_text(encoding="utf-8"))
    assert proposed["routing"]["signals"]["keywords"][0]["keywords"] == ["keep-me"]
    assert proposed["routing"]["signals"]["complexity"][0]["threshold"] == 0.1
    assert base.read_text(encoding="utf-8") == yaml.safe_dump(
        {
            "version": "v0.3",
            "listeners": [],
            "providers": {
                "defaults": {"default_model": "test-model"},
                "models": [
                    {
                        "name": "test-model",
                        "backend_refs": [
                            {
                                "endpoint": "127.0.0.1:8000",
                                "protocol": "http",
                                "weight": 1,
                            }
                        ],
                    }
                ],
            },
            "routing": {
                "modelCards": [{"name": "test-model"}],
                "signals": {
                    "keywords": [
                        {
                            "name": "existing",
                            "operator": "OR",
                            "method": "bm25",
                            "keywords": ["keep-me"],
                            "case_sensitive": False,
                            "bm25_threshold": 0.1,
                        }
                    ]
                },
                "decisions": [
                    {
                        "name": "fallback",
                        "priority": 1,
                        "rules": {"operator": "AND", "conditions": []},
                        "modelRefs": [{"model": "test-model"}],
                    }
                ],
            },
            "global": {"router": {"config_source": "file"}},
        },
        sort_keys=False,
    )


def test_provenance_uses_digests_and_stable_ids_not_local_paths(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    result = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=tmp_path / "proposal",
    )

    provenance_text = result.provenance.read_text(encoding="utf-8")
    provenance = json.loads(provenance_text)
    assert provenance["sources"][0]["id"] == "signal/complexity/escalation"
    assert provenance["sources"][0]["revision_sha256"].startswith("sha256:")
    assert provenance["base"]["config_version"] == "v0.3"
    assert provenance["base"]["sha256"].startswith("sha256:")
    assert provenance["proposal"]["sha256"] == result.proposal_sha256
    assert provenance["proposal"]["validation_contract_version"] == "v1"
    assert str(tmp_path) not in provenance_text
    assert "Add bounded routing behavior" not in provenance_text

    diff_text = result.diff.read_text(encoding="utf-8")
    diff = json.loads(diff_text)
    assert diff["added"][0]["field"] == (
        'routing.signals.complexity["needs_reasoning"]'
    )
    assert str(tmp_path) not in diff_text


def test_conflicting_fragment_fails_without_writing_artifacts(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    _write_fragment(
        fragment_root,
        "signal/complexity/escalation",
        {
            "version": "v0.2",
            "routing": {"signals": {"complexity": []}},
        },
    )
    output = tmp_path / "proposal"

    with pytest.raises(ConfigProposalError, match=r"conflicts.*version"):
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=output,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "fragment_id",
    ["../secret", "/absolute", "Signal/complexity/escalation", "signal//bad"],
)
def test_intent_rejects_unsafe_fragment_ids(tmp_path: Path, fragment_id: str):
    base, _, fragment_root = _fixture(tmp_path)
    intent = tmp_path / "unsafe.yaml"
    _write_intent(intent, [fragment_id])

    with pytest.raises(ConfigProposalError, match="Fragment IDs"):
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=tmp_path / "proposal",
        )


def test_existing_artifacts_require_force(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    output = tmp_path / "proposal"
    first = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=output,
    )

    with pytest.raises(ConfigProposalError, match="use --force"):
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=output,
        )

    forced = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=output,
        force=True,
    )
    assert forced.proposal_sha256 == first.proposal_sha256


def test_output_symlink_is_rejected_before_any_artifact_is_written(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    output = tmp_path / "proposal"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("sentinel\n", encoding="utf-8")
    (output / "proposal-diff.json").symlink_to(outside)

    with pytest.raises(ConfigProposalError, match="unsafe artifact path"):
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=output,
            force=True,
        )

    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert not (output / "proposed-config.yaml").exists()


def test_literal_credentials_are_rejected_without_exposing_the_value(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    secret = "do-not-print-this-secret"
    _write_fragment(
        fragment_root,
        "signal/complexity/escalation",
        {
            "global": {
                "model_catalog": {
                    "external": [
                        {
                            "name": "unsafe",
                            "endpoint": {
                                "base_url": "https://example.com",
                                "api_key": secret,
                            },
                        }
                    ]
                }
            }
        },
    )

    with pytest.raises(ConfigProposalError) as captured:
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=tmp_path / "proposal",
        )

    assert "literal credential-like value" in str(captured.value)
    assert secret not in str(captured.value)
    assert not (tmp_path / "proposal").exists()


def test_validation_contract_is_required_before_writing_artifacts(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    output = tmp_path / "proposal"

    def legacy_validation(proposed_yaml: str, endpoint: str, timeout: float) -> dict:
        return {"valid": True, "normalized_yaml": proposed_yaml}

    with pytest.raises(ConfigProposalError, match="contract_version v1"):
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=output,
            validation_endpoint="http://router:8080",
            validation_request=legacy_validation,
        )

    assert not output.exists()


def test_invalid_candidate_surfaces_structured_diagnostic_without_writing(
    tmp_path: Path,
):
    base, intent, fragment_root = _fixture(tmp_path)
    output = tmp_path / "proposal"

    def invalid_validation(proposed_yaml: str, endpoint: str, timeout: float) -> dict:
        return {
            "valid": False,
            "contract_version": "v1",
            "normalized_yaml": "",
            "errors": [
                {
                    "code": "CONFIG_REFERENCE_ERROR",
                    "field": "routing.decisions",
                    "message": "references unknown model",
                }
            ],
            "warnings": [],
        }

    with pytest.raises(
        ConfigProposalError,
        match=r"CONFIG_REFERENCE_ERROR.*routing\.decisions",
    ):
        propose_config(
            base_config=base,
            intent_path=intent,
            fragment_root=fragment_root,
            output_dir=output,
            validation_endpoint="http://router:8080",
            validation_request=invalid_validation,
        )

    assert not output.exists()


def test_validation_request_uses_only_side_effect_free_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "valid": True,
                "contract_version": "v1",
                "normalized_yaml": "version: v0.3\n",
                "errors": [],
                "warnings": [],
                "diff": {
                    "added": [{"field": "routing.signals", "new": {}}],
                    "removed": [],
                    "changed": [],
                    "truncated": False,
                },
            }

    calls: list[tuple[str, dict, float]] = []

    def post(url: str, *, json: dict, timeout: float) -> Response:
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(config_proposal.requests, "post", post)
    base, intent, fragment_root = _fixture(tmp_path)

    propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=tmp_path / "proposal",
        validation_endpoint="http://router:8080/",
        validation_request=_REAL_VALIDATION_REQUEST,
    )

    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "http://router:8080/config/router/validate"
    assert payload["compare_to_active"] is True
    assert payload["yaml"]
    assert timeout == 10.0


def test_cli_generates_review_artifacts(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    output = tmp_path / "proposal"

    result = CliRunner().invoke(
        main,
        [
            "config",
            "propose",
            "--config",
            str(base),
            "--intent",
            str(intent),
            "--fragment-root",
            str(fragment_root),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Configuration proposal generated" in result.output
    assert (output / "proposed-config.yaml").is_file()
    assert (output / "proposal-diff.json").is_file()
    assert (output / "provenance.json").is_file()
    assert (output / "validation.json").is_file()


def test_cli_reports_unsupported_fragment_without_traceback(tmp_path: Path):
    base, intent, fragment_root = _fixture(tmp_path)
    _write_intent(intent, ["signal/unsupported/example"])

    result = CliRunner().invoke(
        main,
        [
            "config",
            "propose",
            "--config",
            str(base),
            "--intent",
            str(intent),
            "--fragment-root",
            str(fragment_root),
            "--output",
            str(tmp_path / "proposal"),
        ],
    )

    assert result.exit_code != 0
    assert "Unknown fragment ID" in result.output
    assert "Traceback" not in result.output


def test_checked_in_example_generates_a_valid_deterministic_proposal(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[3]
    base = repository_root / "config/recipes/knowledge/config.yaml"
    intent = repository_root / "config/proposals/keyword-signals.yaml"
    fragment_root = repository_root / "config/fragments"

    first = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=tmp_path / "first",
    )
    second = propose_config(
        base_config=base,
        intent_path=intent,
        fragment_root=fragment_root,
        output_dir=tmp_path / "second",
    )

    assert first.proposal_sha256 == second.proposal_sha256
    proposed = yaml.safe_load(first.proposed_config.read_text(encoding="utf-8"))
    keyword_names = {
        signal["name"] for signal in proposed["routing"]["signals"]["keywords"]
    }
    assert {"code_keywords", "urgent_keywords"} <= keyword_names
