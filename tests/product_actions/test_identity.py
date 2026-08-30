from __future__ import annotations

import copy
import json
import pickle
from dataclasses import asdict, replace

import pytest

from offerpilot.product_actions.contracts import (
    HistoricalStoryRouteProof,
    ProductActionContractError,
    ProductActionExecutionAuthorization,
    ProductActionProofRegistryV1,
    ProductActionRouteProof,
    RejectionOnlyRecoveryProof,
    SignalOwnerRecoveryProof,
    StoryOwnerRecoveryProof,
    decode_product_action_route_payload,
    tagged_optional,
)
from tests.product_actions.conftest import raw_json, signal_route, story_route


def test_raw_route_decoder_accepts_only_the_two_exact_closed_unions() -> None:
    signal = decode_product_action_route_payload(
        raw_json(signal_route()),
        action_name="save_review_readiness_signal",
        request_origin="current",
    )
    story = decode_product_action_route_payload(
        raw_json(story_route()),
        action_name="confirm_interview_story",
        request_origin="current",
    )

    assert signal.source_identity == ("review_focus", 44, 5)
    assert story.source_identity == ("story_proposal", 51, 6)
    assert json.loads(signal.canonical_json) == signal_route()
    assert json.loads(story.canonical_json) == story_route()

    with pytest.raises(ProductActionContractError, match="action_source_mismatch"):
        decode_product_action_route_payload(
            raw_json(signal_route()),
            action_name="confirm_interview_story",
            request_origin="current",
        )
    with pytest.raises(ProductActionContractError, match="action_source_mismatch"):
        decode_product_action_route_payload(
            raw_json(story_route()),
            action_name="save_review_readiness_signal",
            request_origin="current",
        )
    with pytest.raises(ProductActionContractError, match="invalid_request_origin"):
        decode_product_action_route_payload(
            raw_json(signal_route()),
            action_name="save_review_readiness_signal",
            request_origin="historical_story_bridge",
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"application_id":1,"application_id":2}',
        b'{"outer":{"key":1,"key":2}}',
        b'{"application_id":NaN}',
        b'{"application_id":Infinity}',
        b'{"application_id":-Infinity}',
        b'"not-an-object"',
        b'{} trailing',
        b'{"user_note":"\\ud800"}',
        b"\xff",
    ],
)
def test_raw_decoder_rejects_lossy_or_ambiguous_json_before_normalization(raw: bytes) -> None:
    with pytest.raises(ProductActionContractError):
        decode_product_action_route_payload(
            raw,
            action_name="save_review_readiness_signal",
            request_origin="current",
        )


def test_raw_decoder_enforces_the_exact_16_kib_request_boundary() -> None:
    base = raw_json(signal_route(user_note=""))
    at_limit = base + b" " * (16_384 - len(base))
    over_limit = at_limit + b" "

    assert len(at_limit) == 16_384
    assert decode_product_action_route_payload(
        at_limit,
        action_name="save_review_readiness_signal",
        request_origin="current",
    ).payload["application_id"] == 41
    with pytest.raises(ProductActionContractError, match="route_payload_too_large"):
        decode_product_action_route_payload(
            over_limit,
            action_name="save_review_readiness_signal",
            request_origin="current",
        )


@pytest.mark.parametrize(
    ("route_factory", "action_name", "field"),
    [
        *[
            (signal_route, "save_review_readiness_signal", field)
            for field in (
                "application_id",
                "event_id",
                "note_id",
                "proposal_id",
                "proposal_schema_version",
                "expected_note_revision",
            )
        ],
        *[
            (story_route, "confirm_interview_story", field)
            for field in (
                "attempt_id",
                "generation_revision",
                "target_story_id",
                "expected_current_version_id",
                "expected_story_revision",
                "product_action_generation",
            )
        ],
    ],
)
@pytest.mark.parametrize("invalid", [True, 1.0, "1"])
def test_every_route_integer_is_exact_before_repository_or_sql(
    route_factory: object,
    action_name: str,
    field: str,
    invalid: object,
) -> None:
    route = route_factory(**{field: invalid})  # type: ignore[operator]
    with pytest.raises(ProductActionContractError, match="exact_integer"):
        decode_product_action_route_payload(
            raw_json(route),
            action_name=action_name,
            request_origin="current",
        )


@pytest.mark.parametrize(
    "mutated",
    [
        {"domain_idempotency_key": "NOT-A-UUID"},
        {"expected_source_fingerprint": "sha256:" + "A" * 64},
        {"expected_proposal_hash": "hmac-sha256:" + "1" * 64},
        {"expected_candidate_fingerprint": "sha256:short"},
        {"focus_id": ""},
        {"focus_id": "\x00focus"},
        {"unexpected": 1},
    ],
)
def test_signal_route_rejects_malformed_uuid_digest_text_and_extra_fields(
    mutated: dict[str, object],
) -> None:
    with pytest.raises(ProductActionContractError):
        decode_product_action_route_payload(
            raw_json(signal_route(**mutated)),
            action_name="save_review_readiness_signal",
            request_origin="current",
        )


def test_story_route_requires_create_or_append_cas_as_one_exact_shape() -> None:
    create = story_route(
        target_story_id=None,
        expected_current_version_id=None,
        expected_story_revision=None,
    )
    assert decode_product_action_route_payload(
        raw_json(create),
        action_name="confirm_interview_story",
        request_origin="current",
    ).payload["target_story_id"] is None

    for invalid in (
        story_route(target_story_id=None, expected_current_version_id=53),
        story_route(expected_current_version_id=None),
        story_route(expected_story_revision=None),
    ):
        with pytest.raises(ProductActionContractError, match="story_target_cas"):
            decode_product_action_route_payload(
                raw_json(invalid),
                action_name="confirm_interview_story",
                request_origin="current",
            )


def test_tagged_optional_never_uses_bare_null_or_omission() -> None:
    assert tagged_optional(None) == {"state": "absent", "value": None}
    assert tagged_optional(7) == {"state": "present", "value": 7}


def test_five_identity_hmacs_token_and_deterministic_uuids_match_goldens(
    product_core: tuple[object, ...],
) -> None:
    _catalog, _registry, _profiles, signal_issuer, story_issuer = product_core
    signal = signal_issuer.prepare(route_payload_raw=raw_json(signal_route()))
    story = story_issuer.prepare(route_payload_raw=raw_json(story_route()))
    historical = story_issuer.prepare(
        route_payload_raw=raw_json(story_route()),
        request_origin="historical_story_bridge",
        historical_confirmation_token="legacy-token-中文",
    )
    historical_other_token = story_issuer.prepare(
        route_payload_raw=raw_json(story_route()),
        request_origin="historical_story_bridge",
        historical_confirmation_token="legacy-token-other",
    )

    assert signal.identity_projection() == {
        "operation_id": "8da3e238-a39c-5d2b-b646-49e3da5f94a1",
        "action_call_id": "07e593da-ef9c-580e-b870-c73d7e20e73d",
        "route_payload_fingerprint": (
            "hmac-sha256:7e80096753bbd320485a1116f1a1e0782a26a8ee334eec108795c126c7c336e0"
        ),
        "semantic_claim_fingerprint": (
            "hmac-sha256:2e2c71fa3191dab8bf1c9c09f2394fd4fa9a9bd32c1a53472e81c34267f27921"
        ),
        "authorization_scope_fingerprint": (
            "hmac-sha256:3f2ec6af4c59bc4e7d0a0b5d09a8d004be3ca9a99c917c15be244a78f2610126"
        ),
        "route_binding_fingerprint": (
            "hmac-sha256:ecaeeca48b1b735abcc3fd3283d419e58c24d92ce597f811573e3f17d5600646"
        ),
        "request_idempotency_fingerprint": (
            "hmac-sha256:7e23cfc6de8acd5042e15acd5cbf259e3753e6748951ffa5e387ca781cf4fcf9"
        ),
        "proposal_fingerprint": (
            "hmac-sha256:e209dafe9fa2e6a39fa21c0693d12ffbb046cc79a5f06ccbf5ce98f8c33dc1b9"
        ),
        "confirmation_token_fingerprint": (
            "hmac-sha256:3d2847dba9403e67e711254186bb50b891b92b9797bceedffcade9f4944b0bd7"
        ),
        "historical_request_token_fingerprint": None,
    }
    assert signal.confirmation_token == (
        "f5544100b4d9b53b506333f11a989004e6d52d870022270bde726c7e56655b3e"
    )
    assert story.operation_id == "7c89ac2e-d8c3-5a9c-9fc1-e7b04a76dd55"
    assert story.action_call_id == "90267b25-082e-551c-80f8-09e5aebbed1a"
    assert story.confirmation_token == (
        "e7ea5bc7aa7a83a4fc3d9a3446029e4178dca2cf127bfbcd9bd32c5241f6a652"
    )
    assert historical.operation_id == "e45e654b-0c97-5559-a1a5-401eb44d7a34"
    assert historical.action_call_id == "73a9c416-2f7b-56ae-928d-c67e060fb142"
    assert historical.confirmation_token == (
        "d5b812c37e103c2fe59a3cca056777745b351f89c67a520e1896a1c11a132524"
    )
    assert historical.historical_request_token_fingerprint == (
        "hmac-sha256:44681623379df8410caca0d544327ff52491f6a7b2a69296c6b6e644aed0eb19"
    )
    assert historical.request_idempotency_fingerprint == (
        "hmac-sha256:54edf14d02de38ef3d6cfb4256784b5c7538f782911fc1dbf59afe39f2539687"
    )
    assert historical_other_token.operation_id == historical.operation_id
    assert historical.request_idempotency_fingerprint != story.request_idempotency_fingerprint
    assert (
        historical_other_token.request_idempotency_fingerprint
        != historical.request_idempotency_fingerprint
    )
    assert historical.confirmation_token != story.confirmation_token
    assert historical.historical_request_token_fingerprint is not None


def test_raw_server_token_never_appears_in_repr_or_safe_identity_projection(
    product_core: tuple[object, ...],
) -> None:
    _catalog, _registry, profiles, signal_issuer, _story_issuer = product_core
    prepared = signal_issuer.prepare(route_payload_raw=raw_json(signal_route()))

    assert prepared.confirmation_token not in repr(prepared)
    assert prepared.confirmation_token not in repr(profiles)
    assert prepared.confirmation_token not in json.dumps(prepared.identity_projection())


def test_generation_revision_and_product_action_generation_are_distinct_identity_inputs(
    product_core: tuple[object, ...],
) -> None:
    _catalog, _registry, _profiles, _signal_issuer, story_issuer = product_core
    base = story_issuer.prepare(route_payload_raw=raw_json(story_route()))
    generation_revision = story_issuer.prepare(
        route_payload_raw=raw_json(story_route(generation_revision=7))
    )
    product_generation = story_issuer.prepare(
        route_payload_raw=raw_json(story_route(product_action_generation=9))
    )

    assert len({base.operation_id, generation_revision.operation_id, product_generation.operation_id}) == 3
    assert len(
        {base.action_call_id, generation_revision.action_call_id, product_generation.action_call_id}
    ) == 3
    assert len(
        {
            base.confirmation_token,
            generation_revision.confirmation_token,
            product_generation.confirmation_token,
        }
    ) == 3


PROOF_TYPES = (
    ProductActionRouteProof,
    HistoricalStoryRouteProof,
    SignalOwnerRecoveryProof,
    StoryOwnerRecoveryProof,
    RejectionOnlyRecoveryProof,
    ProductActionExecutionAuthorization,
)


@pytest.mark.parametrize("proof_type", PROOF_TYPES)
def test_proof_union_is_factory_only_opaque_and_nonserializable(proof_type: type[object]) -> None:
    with pytest.raises(TypeError, match="factory"):
        proof_type()  # type: ignore[call-arg]

    registry = ProductActionProofRegistryV1()
    proof = registry._issue(  # noqa: SLF001 - adversarial contract test
        proof_type,
        action_name="confirm_interview_story",
        binding=("owner", 1, 2),
    )
    assert repr(proof) == f"<{proof_type.__name__}>"
    assert not hasattr(proof, "__dict__")
    assert "owner" not in repr(proof)
    for operation in (
        lambda: copy.copy(proof),
        lambda: copy.deepcopy(proof),
        lambda: pickle.dumps(proof),
        lambda: asdict(proof),  # type: ignore[arg-type]
        lambda: replace(proof),  # type: ignore[arg-type]
        lambda: proof.to_json(),  # type: ignore[attr-defined]
    ):
        with pytest.raises((TypeError, ValueError)):
            operation()


def test_proof_registry_rejects_cross_container_owner_source_action_union_reuse_and_aba() -> None:
    registry = ProductActionProofRegistryV1()
    foreign = ProductActionProofRegistryV1()
    binding = ("owner", 1, "source", 2)
    proof = registry._issue(  # noqa: SLF001 - adversarial contract test
        ProductActionRouteProof,
        action_name="save_review_readiness_signal",
        binding=binding,
    )

    with pytest.raises(ValueError, match="provenance"):
        foreign.claim(
            proof,
            proof_type=ProductActionRouteProof,
            action_name="save_review_readiness_signal",
            expected_binding=binding,
        )
    for wrong_type, wrong_action, wrong_binding in (
        (StoryOwnerRecoveryProof, "save_review_readiness_signal", binding),
        (ProductActionRouteProof, "confirm_interview_story", binding),
        (ProductActionRouteProof, "save_review_readiness_signal", ("owner", 9, "source", 2)),
        (ProductActionRouteProof, "save_review_readiness_signal", ("owner", 1, "source", 9)),
    ):
        with pytest.raises((TypeError, ValueError), match="type|action|binding|provenance"):
            registry.claim(
                proof,
                proof_type=wrong_type,
                action_name=wrong_action,
                expected_binding=wrong_binding,
            )

    with registry.claim(
        proof,
        proof_type=ProductActionRouteProof,
        action_name="save_review_readiness_signal",
        expected_binding=binding,
    ):
        with pytest.raises(ValueError, match="in_flight"):
            registry.claim(
                proof,
                proof_type=ProductActionRouteProof,
                action_name="save_review_readiness_signal",
                expected_binding=binding,
            )
    with pytest.raises(ValueError, match="consumed"):
        registry.claim(
            proof,
            proof_type=ProductActionRouteProof,
            action_name="save_review_readiness_signal",
            expected_binding=binding,
        )

    second = registry._issue(  # noqa: SLF001 - adversarial contract test
        ProductActionRouteProof,
        action_name="save_review_readiness_signal",
        binding=binding,
    )
    registry.revoke(second)
    replacement = registry._issue(  # noqa: SLF001 - adversarial contract test
        ProductActionRouteProof,
        action_name="save_review_readiness_signal",
        binding=binding,
    )
    with pytest.raises(ValueError, match="revoked"):
        registry.claim(
            second,
            proof_type=ProductActionRouteProof,
            action_name="save_review_readiness_signal",
            expected_binding=binding,
        )
    with registry.claim(
        replacement,
        proof_type=ProductActionRouteProof,
        action_name="save_review_readiness_signal",
        expected_binding=binding,
    ):
        pass


def test_base_exception_revokes_in_flight_proof() -> None:
    registry = ProductActionProofRegistryV1()
    binding = ("owner", 1)
    proof = registry._issue(  # noqa: SLF001 - lifecycle contract test
        ProductActionRouteProof,
        action_name="confirm_interview_story",
        binding=binding,
    )

    with pytest.raises(KeyboardInterrupt):
        with registry.claim(
            proof,
            proof_type=ProductActionRouteProof,
            action_name="confirm_interview_story",
            expected_binding=binding,
        ):
            raise KeyboardInterrupt
    with pytest.raises(ValueError, match="revoked"):
        registry.claim(
            proof,
            proof_type=ProductActionRouteProof,
            action_name="confirm_interview_story",
            expected_binding=binding,
        )


def test_signal_and_story_recovery_proof_field_contracts_are_not_nullable_unions() -> None:
    assert SignalOwnerRecoveryProof.binding_fields == (
        "canonical_owner",
        "application_id",
        "event_id",
        "note_id",
        "proposal_id",
        "source_revision",
        "semantic_claim_fingerprint",
        "operation_id",
        "action_call_id",
        "route_payload_fingerprint",
        "route_binding_fingerprint",
        "request_origin",
        "allowed_decisions",
    )
    assert "generation" not in " ".join(SignalOwnerRecoveryProof.binding_fields)
    assert StoryOwnerRecoveryProof.binding_fields[-4:] == (
        "route_binding_fingerprint",
        "request_origin",
        "allowed_decisions",
        "product_action_generation",
    )
    assert "generation_revision" in StoryOwnerRecoveryProof.binding_fields
    assert StoryOwnerRecoveryProof.allowed_decisions == ("approve", "modify", "reject")
    assert RejectionOnlyRecoveryProof.live_source_state == "not_observed"
    assert RejectionOnlyRecoveryProof.allowed_decisions == ("reject",)
    signal_rejection = RejectionOnlyRecoveryProof.binding_fields_for(
        "save_review_readiness_signal"
    )
    story_rejection = RejectionOnlyRecoveryProof.binding_fields_for(
        "confirm_interview_story"
    )
    assert "generation" not in " ".join(signal_rejection)
    assert "generation_revision" in story_rejection
    assert "product_action_generation" in story_rejection
    assert signal_rejection != story_rejection
    with pytest.raises(ValueError, match="action"):
        RejectionOnlyRecoveryProof.binding_fields_for("unknown")
