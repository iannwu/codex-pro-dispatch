from __future__ import annotations

import unittest
from unittest import mock

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd
from codex_pro_dispatch.collection import strict_json_object


def evidence(*, observed_at: str = "2026-09-02T00:00:00.000Z") -> dict[str, object]:
    return {
        "schema": cpd.NATIVE_COLLECTION_SCHEMA,
        "adapter_contract_id": "codex-desktop-native-collection/v1",
        "requested_conversation_id": "worker-1",
        "loaded_conversation_id": "worker-1",
        "assistant_message_id": "assistant-1",
        "submitted_user_message_id": "user-1",
        "role": "assistant",
        "generation_status": "completed",
        "generation_finality_provenance": "native-message-status",
        "truncated": False,
        "selected_result_outer_integrity": {
            "truncated": False,
            "provenance": "native-result-envelope",
        },
        "text": "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-1]\nready",
        "observed_at": observed_at,
    }


class NativeCollectionEvidenceTests(unittest.TestCase):
    def test_observation_time_is_not_content_identity(self) -> None:
        first = cpd.NativeCollectionEvidence.from_mapping(evidence())
        second = cpd.NativeCollectionEvidence.from_mapping(
            evidence(observed_at="2026-09-02T00:01:00.000Z")
        )
        self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(first.content_identity_sha256, second.content_identity_sha256)

    def test_omitted_truncation_is_unknown_for_current_adapter(self) -> None:
        value = evidence()
        value.pop("truncated")
        parsed = cpd.NativeCollectionEvidence.from_mapping(value)
        self.assertEqual(parsed.raw_truncated, "omitted")
        self.assertIsNone(parsed.normalized_truncated)
        self.assertFalse(parsed.complete_and_untruncated)

    def test_duplicate_json_keys_and_unknown_adapter_fail_closed(self) -> None:
        with self.assertRaises(cpd.CollectionEvidenceError):
            cpd.NativeCollectionEvidence.from_json_bytes(
                b'{"schema":"a","schema":"b"}'
            )
        value = evidence()
        value["adapter_contract_id"] = "untrusted-host/v1"
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.NativeCollectionEvidence.from_mapping(value)
        self.assertEqual(raised.exception.error_code, "collection_adapter_unsupported")

    def test_message_and_outer_truncation_are_independent_fail_closed_signals(self) -> None:
        message_truncated = evidence()
        message_truncated["truncated"] = True
        parsed_message = cpd.NativeCollectionEvidence.from_mapping(message_truncated)
        self.assertTrue(parsed_message.normalized_truncated)
        self.assertFalse(parsed_message.complete_and_untruncated)

        outer_truncated = evidence()
        outer_truncated["selected_result_outer_integrity"] = {
            "truncated": True,
            "provenance": "native-result-envelope",
        }
        parsed_outer = cpd.NativeCollectionEvidence.from_mapping(outer_truncated)
        self.assertTrue(parsed_outer.normalized_outer_truncated)
        self.assertFalse(parsed_outer.complete_and_untruncated)

    def test_item_role_finality_and_outer_provenance_are_not_inferred(self) -> None:
        for field, replacement, expected_code in (
            ("role", "user", "collection_evidence_invalid"),
            ("generation_status", "streaming", "collection_message_not_complete"),
            (
                "generation_finality_provenance",
                "enclosing-turn",
                "collection_message_not_complete",
            ),
        ):
            with self.subTest(field=field):
                value = evidence()
                value[field] = replacement
                with self.assertRaises(cpd.CollectionEvidenceError) as raised:
                    cpd.NativeCollectionEvidence.from_mapping(value)
                self.assertEqual(raised.exception.error_code, expected_code)
        value = evidence()
        value["selected_result_outer_integrity"] = {
            "truncated": False,
            "provenance": "enclosing-turn",
        }
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.NativeCollectionEvidence.from_mapping(value)
        self.assertEqual(raised.exception.error_code, "collection_evidence_invalid")

    def test_requested_and_loaded_worker_ids_must_match(self) -> None:
        value = evidence()
        value["loaded_conversation_id"] = "worker-2"
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.NativeCollectionEvidence.from_mapping(value)
        self.assertEqual(raised.exception.error_code, "collection_evidence_invalid")

    def test_omission_can_only_normalize_through_a_helper_allowlisted_contract(self) -> None:
        contract = cpd.NativeAdapterContract(
            adapter_contract_id="inspected-host/v9",
            host="fixture-host",
            host_contract_version="v9",
            omitted_message_truncated_is_false=True,
            omitted_outer_truncated_is_false=True,
            supports_complete_reread_upgrade=False,
            generation_finality_provenance=frozenset({"fixture-finality"}),
            outer_integrity_provenance=frozenset({"fixture-outer"}),
        )
        value = evidence()
        value["adapter_contract_id"] = contract.adapter_contract_id
        value["generation_finality_provenance"] = "fixture-finality"
        value.pop("truncated")
        value["selected_result_outer_integrity"] = {"provenance": "fixture-outer"}
        with mock.patch.dict(
            cpd.NATIVE_ADAPTER_CONTRACTS,
            {contract.adapter_contract_id: contract},
            clear=False,
        ):
            parsed = cpd.NativeCollectionEvidence.from_mapping(value)
        self.assertEqual(parsed.raw_truncated, "omitted")
        self.assertFalse(parsed.normalized_truncated)
        self.assertEqual(parsed.raw_outer_truncated, "omitted")
        self.assertFalse(parsed.normalized_outer_truncated)
        self.assertTrue(parsed.complete_and_untruncated)

    def test_strict_json_rejects_bom_nonfinite_and_trailing_data(self) -> None:
        for raw in (
            b'\xef\xbb\xbf{}',
            b'{"schema": NaN}',
            b'{} trailing',
            b'{}\x0b',
            b'{}\x1c',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(cpd.CollectionEvidenceError):
                    strict_json_object(raw)


if __name__ == "__main__":
    unittest.main()
