from __future__ import annotations

import unittest

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd


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


if __name__ == "__main__":
    unittest.main()
