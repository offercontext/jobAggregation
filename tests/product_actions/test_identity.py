from __future__ import annotations

import copy
import json
import os
import pickle
import subprocess
import sys
import textwrap
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import offerpilot.product_actions.issuer as issuer_module
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


def test_story_route_binding_scope_uses_raw_integer_when_present_and_tagged_null_when_absent(
    product_core: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalog, _registry, _profiles, _signal_issuer, story_issuer = product_core
    original_hmac = issuer_module._hmac  # noqa: SLF001 - canonical-envelope golden
    route_bindings: list[dict[str, object]] = []

    def capture_hmac(key: object, domain: str, value: dict[str, object]) -> str:
        if domain == "product-action-route-binding-v1":
            route_bindings.append(value)
        return original_hmac(key, domain, value)  # type: ignore[arg-type]

    monkeypatch.setattr(issuer_module, "_hmac", capture_hmac)
    story_issuer.prepare(route_payload_raw=raw_json(story_route()))
    story_issuer.prepare(
        route_payload_raw=raw_json(
            story_route(
                target_story_id=None,
                expected_current_version_id=None,
                expected_story_revision=None,
            )
        )
    )

    assert route_bindings[0]["application_scope"] == {"kind": "story", "id": 52}
    assert route_bindings[1]["application_scope"] == {
        "kind": "story",
        "id": {"state": "absent", "value": None},
    }


def test_five_identity_hmacs_token_and_deterministic_uuids_match_goldens(
    product_core: tuple[object, ...],
) -> None:
    _catalog, _registry, _profiles, signal_issuer, story_issuer = product_core
    signal = signal_issuer.prepare(route_payload_raw=raw_json(signal_route()))
    story = story_issuer.prepare(route_payload_raw=raw_json(story_route()))

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
        "044bb2bf8ff27e81ce9b7d2c3e2f65cd999f23b7adc91033ebe26ec79e12bd3f"
    )


def test_identity_goldens_are_exact_across_fresh_processes_hash_seeds_and_sqlite_uow(
    tmp_path: Path,
) -> None:
    script = textwrap.dedent(
        r'''
        import json
        import sys
        from pathlib import Path
        from sqlalchemy import text
        from offerpilot.ai.write_operations import LedgerKeyDomain
        from offerpilot.db import init_database
        from offerpilot.models import InterviewStoryProposalAttempt
        from offerpilot.product_actions.catalog import ProductActionCatalogV1
        from offerpilot.product_actions.contracts import ProductActionProofRegistryV1
        from offerpilot.product_actions.issuer import (
            InterviewStoryActionIssuer,
            LedgerKeyProfileStoreV1,
            ReviewReadinessActionIssuer,
        )
        from offerpilot.product_actions.repository import ProductActionProposalRepository
        from tests.product_actions.conftest import raw_json, signal_route, story_route

        key = LedgerKeyDomain("11111111-1111-4111-8111-111111111111", b"1" * 32)
        registry = ProductActionProofRegistryV1()
        catalog = ProductActionCatalogV1(registry)
        profiles = LedgerKeyProfileStoreV1((key,), active_key_id=key.key_id)
        signal_issuer = ReviewReadinessActionIssuer(catalog, registry, profiles)
        story_issuer = InterviewStoryActionIssuer(catalog, registry, profiles)
        sessions = init_database(Path(sys.argv[1]))
        repository = ProductActionProposalRepository(
            sessions,
            catalog=catalog,
            proof_registry=registry,
            key_profiles=profiles,
        )

        values = {
            "signal": signal_issuer.prepare(route_payload_raw=raw_json(signal_route())),
            "story_present": story_issuer.prepare(
                route_payload_raw=raw_json(story_route())
            ),
            "story_null": story_issuer.prepare(route_payload_raw=raw_json(story_route(
                target_story_id=None,
                expected_current_version_id=None,
                expected_story_revision=None,
            ))),
            "story_gr7": story_issuer.prepare(route_payload_raw=raw_json(story_route(
                generation_revision=7,
            ))),
            "story_pg9": story_issuer.prepare(route_payload_raw=raw_json(story_route(
                product_action_generation=9,
            ))),
        }
        with sessions() as session:
            session.add(InterviewStoryProposalAttempt(
                id=51,
                target_story_id=52,
                idempotency_key="historical_story_attempt_0051",
                entrypoint="ui",
                entry_context_json="{}",
                attempt_status="ready",
                generation_revision=6,
                provider_call_token="",
                provider_lease_until=None,
                input_snapshot_json="{}",
                source_fingerprint="sha256:" + "5" * 64,
                proposal_json="{}",
                proposal_hash="sha256:" + "4" * 64,
                repair_count=0,
                failure_category="",
                confirmation_token_hash="",
                confirmation_payload_hash="",
                confirmed_story_id=None,
                confirmed_story_version_id=None,
                product_action_operation_id=None,
                product_action_generation=0,
                confirmed_at=None,
            ))
            session.commit()
        with sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            published = repository.publish_historical_story_bridge_in_session(
                session,
                issuer=story_issuer,
                route_payload_raw=raw_json(story_route(product_action_generation=1)),
                legacy_confirmation_token="legacy_token_0001",
            )
            assert published.bundle is not None
            operation = published.bundle.operation
            route = published.bundle.route
            historical = {
                "operation_id": operation.id,
                "action_call_id": operation.tool_call_id,
                "route_payload_fingerprint": route.route_payload_fingerprint,
                "semantic_claim_fingerprint": route.semantic_claim_fingerprint,
                "authorization_scope_fingerprint": operation.authorization_scope_fingerprint,
                "route_binding_fingerprint": route.route_binding_fingerprint,
                "request_idempotency_fingerprint": route.request_idempotency_fingerprint,
                "proposal_fingerprint": operation.proposal_fingerprint,
                "confirmation_token_fingerprint": operation.confirmation_token_fingerprint,
                "historical_request_token_fingerprint": (
                    route.historical_request_token_fingerprint
                ),
                "confirmation_token": published.confirmation_token,
            }
            session.rollback()
        projection = {
            name: {**item.identity_projection(), "confirmation_token": item.confirmation_token}
            for name, item in values.items()
        }
        projection["historical"] = historical
        print(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        '''
    )
    expected = json.loads(
        r'''{
          "historical":{"action_call_id":"54d5d001-cdbd-5f54-ac1f-55022d826b52","authorization_scope_fingerprint":"hmac-sha256:b633f2ac8954cdb972a38802ebdae0065674292477e79e310c12e5382b215df6","confirmation_token":"1e4259b14a4d95a5ca011ea5827135c34d35d56a7cdf55693f86d8500217c371","confirmation_token_fingerprint":"hmac-sha256:6c011aa5862cd42e606d5a150d544d7f6256b89dda28a51634aae28cf8603906","historical_request_token_fingerprint":"hmac-sha256:39ab15c84619dfd4643587f155f027cd34df131be6da9b67d626676e80397e0a","operation_id":"f75a0ec2-909b-5abb-bae9-5010b7da41e4","proposal_fingerprint":"hmac-sha256:e03327d9e0e10715d2ce7285a72975b0a692c6b5b167b7a2d29e9aafa7e10350","request_idempotency_fingerprint":"hmac-sha256:c80ee692b6cd1ae149d03ae1c4d307dcde53110b5be2532f9a39fb3105968656","route_binding_fingerprint":"hmac-sha256:bbcfb2ca8b1e4ae56b7f4c58508d54820a2427efa17b0d6fc5f2354403158efc","route_payload_fingerprint":"hmac-sha256:785fce85478a2739f53489ad43d008e05b7049877371f32cf07ef52175138149","semantic_claim_fingerprint":null},
          "signal":{"action_call_id":"07e593da-ef9c-580e-b870-c73d7e20e73d","authorization_scope_fingerprint":"hmac-sha256:3f2ec6af4c59bc4e7d0a0b5d09a8d004be3ca9a99c917c15be244a78f2610126","confirmation_token":"f5544100b4d9b53b506333f11a989004e6d52d870022270bde726c7e56655b3e","confirmation_token_fingerprint":"hmac-sha256:3d2847dba9403e67e711254186bb50b891b92b9797bceedffcade9f4944b0bd7","historical_request_token_fingerprint":null,"operation_id":"8da3e238-a39c-5d2b-b646-49e3da5f94a1","proposal_fingerprint":"hmac-sha256:e209dafe9fa2e6a39fa21c0693d12ffbb046cc79a5f06ccbf5ce98f8c33dc1b9","request_idempotency_fingerprint":"hmac-sha256:7e23cfc6de8acd5042e15acd5cbf259e3753e6748951ffa5e387ca781cf4fcf9","route_binding_fingerprint":"hmac-sha256:ecaeeca48b1b735abcc3fd3283d419e58c24d92ce597f811573e3f17d5600646","route_payload_fingerprint":"hmac-sha256:7e80096753bbd320485a1116f1a1e0782a26a8ee334eec108795c126c7c336e0","semantic_claim_fingerprint":"hmac-sha256:2e2c71fa3191dab8bf1c9c09f2394fd4fa9a9bd32c1a53472e81c34267f27921"},
          "story_present":{"action_call_id":"90267b25-082e-551c-80f8-09e5aebbed1a","authorization_scope_fingerprint":"hmac-sha256:4be5ea6e528b264178e9f768d53f9827f8afbd52c071ce83e3e16278d49a5860","confirmation_token":"044bb2bf8ff27e81ce9b7d2c3e2f65cd999f23b7adc91033ebe26ec79e12bd3f","confirmation_token_fingerprint":"hmac-sha256:ddf107723491ec069a781b8986fc9680cb21bd2da5e74b517611f6dbe5636f05","historical_request_token_fingerprint":null,"operation_id":"7c89ac2e-d8c3-5a9c-9fc1-e7b04a76dd55","proposal_fingerprint":"hmac-sha256:ffc9497202092204708dca3946218c766984bdddd39d7c64f187ee8e32005b25","request_idempotency_fingerprint":"hmac-sha256:93eb0d782a4b9dd4a9ba2ea446954dbd8eb3587eb0b4d4698553892e7e99a3ab","route_binding_fingerprint":"hmac-sha256:6bdb7466a64c63e8110390b71b8996a49f291ca5c7273ecf0b15a57630ee4a74","route_payload_fingerprint":"hmac-sha256:d11247c9bc968529766a73bdbfde9f4d6fbdf369292f6916d13d5754af58b928","semantic_claim_fingerprint":null},
          "story_gr7":{"action_call_id":"ebff98df-8f85-5647-91c8-918967a47f0a","authorization_scope_fingerprint":"hmac-sha256:c049b799b9a84712c2816c3c3a27aaa26987ce06be5fd21558d4427d9f6485ab","confirmation_token":"7f3578a472b881f1325685ac34b2da40a985c7f532fcf015960020a9dbc0bb1f","confirmation_token_fingerprint":"hmac-sha256:e5e62ffa33ac32c7b80f517c04d5a312047c56c91afe090c6f6bd73467f20957","historical_request_token_fingerprint":null,"operation_id":"4c9e63c7-955e-5756-89bf-e913e2963658","proposal_fingerprint":"hmac-sha256:3b43dd675bc3e2cbc6d22bc6d92601b01106de73ae3e2db694654abed2449523","request_idempotency_fingerprint":"hmac-sha256:7a54c1f0196bf876b134928ee27abec93b9a4465ffe2fbe95689d6c83b3adbfb","route_binding_fingerprint":"hmac-sha256:a029d382b80601c2812fb5a6fc9cddba8e41b484f9924d95b4ea372736ccc504","route_payload_fingerprint":"hmac-sha256:a603c365b8302c36b38d2410cfa7fbe6d14247f566a22821d0a10c0a123fcc8b","semantic_claim_fingerprint":null},
          "story_null":{"action_call_id":"90267b25-082e-551c-80f8-09e5aebbed1a","authorization_scope_fingerprint":"hmac-sha256:1852f815b79d7425d807dcaa9a5b87e5fadde195372f9e6526119661a1b529e3","confirmation_token":"bb376d6cb51aacdaeac48c5917303d109b9beb613d974b931764df8e0c3bb9e7","confirmation_token_fingerprint":"hmac-sha256:3c838d01520e9a1d709a8b6e5c08b37c4f6c5acd234e3b76ae76dcd1115a6f61","historical_request_token_fingerprint":null,"operation_id":"7c89ac2e-d8c3-5a9c-9fc1-e7b04a76dd55","proposal_fingerprint":"hmac-sha256:b51b656de64f532a7c38ea25860bcd9b0a6ea51afd3c6643f6f5413cc45bfbdf","request_idempotency_fingerprint":"hmac-sha256:d75f203ec547ea16a31a7dc2c3ee4044a3509197bb3110ef7b83bbcd5f05dc3c","route_binding_fingerprint":"hmac-sha256:7bae6f0085dfe6540633e924d9379b355e5d62c4d38d2cd7aff0198e197dc32b","route_payload_fingerprint":"hmac-sha256:18d3608a9dccdb16de56467d99a94641c8ff340c8981163130a357ea9b457faa","semantic_claim_fingerprint":null},
          "story_pg9":{"action_call_id":"d369a5af-0c96-5375-9095-6f8d5576dc34","authorization_scope_fingerprint":"hmac-sha256:f761299a2ac91563f98ce52d96d1cf88123348a41157b3a619919a1c66643da9","confirmation_token":"04e9fad16ffeaaabc69ca20f4bb26cc3f6c1412c163380b6ddb2e8ee09928f1a","confirmation_token_fingerprint":"hmac-sha256:336117ec4474193334dc02d901e401e5d2b0218b4dbb188ca5b283167fc06413","historical_request_token_fingerprint":null,"operation_id":"cf6917d8-cd46-5277-8ab3-150b0aa8edb7","proposal_fingerprint":"hmac-sha256:1773d70d33673641ccda2cb88d6896ab157622d79eadd2eea80583a79fe76196","request_idempotency_fingerprint":"hmac-sha256:0865580b91174167a4efc7d747c1843e4c7d4dacf1deb5a2b8ad309a95e9322d","route_binding_fingerprint":"hmac-sha256:2f45ad3f626aea71840a1a4c39ebba3b9e9a11d531520f8757520b3c398e5704","route_payload_fingerprint":"hmac-sha256:c4e580e471b03a4b9c5780e4eb7d259e201cf0e5ff7ccc2adfc8a8f192df94fc","semantic_claim_fingerprint":null}
        }'''
    )
    outputs = []
    for seed in ("1", "8675309"):
        child_env = os.environ.copy()
        child_env["PYTHONHASHSEED"] = seed
        database_path = str(tmp_path / f"golden-{seed}.sqlite3")
        completed = subprocess.run(
            [sys.executable, "-c", script, database_path],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=child_env,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(json.loads(completed.stdout))
    assert outputs == [expected, expected]


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
