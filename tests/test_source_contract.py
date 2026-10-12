"""Pure source-evidence checks; all catalog entries below are synthetic."""

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from monitor.source_contract import (
    PRODUCTION_CONTRACT_CATALOG, SourceContract, SourceEvidenceError,
    resolve_source_contract,
)


NOW = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)


def synthetic_contract():
    return SourceContract(
        contract_id="synthetic-offline-contract", actor="lance_api~x-tweet-scraper-api",
        build_id="B" * 17, build_number="0.0.123", target_username="thsottiaux",
        target_user_id="12345",
        official_evidence_urls=("https://apify.com/lance_api/x-tweet-scraper-api",),
        evidence_sha256="a" * 64,
        reviewed_at="2026-10-10T07:00:00Z", expires_at="2026-10-11T07:00:00Z",
    )


class SourceContractTests(unittest.TestCase):
    def lookup(self, entry=None, **changes):
        entry = synthetic_contract() if entry is None else entry
        kwargs = dict(target_username="thsottiaux", target_user_id="12345", now=NOW,
                      catalog={entry.contract_id: entry})
        kwargs.update(changes)
        return resolve_source_contract(entry.contract_id, **kwargs)

    def test_production_catalog_empty_and_caller_true_or_json_cannot_supply_proof(self):
        self.assertEqual(dict(PRODUCTION_CONTRACT_CATALOG), {})
        with self.assertRaises(TypeError):
            PRODUCTION_CONTRACT_CATALOG["synthetic-offline-contract"] = synthetic_contract()
        for catalog in (None, True, {"synthetic-offline-contract": True},
                        {"synthetic-offline-contract": synthetic_contract().__dict__}):
            with self.subTest(catalog_type=type(catalog).__name__), self.assertRaises(SourceEvidenceError):
                self.lookup(catalog=catalog)

    def test_synthetic_reviewed_entry_is_resolved_without_any_io(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
                patch("builtins.open", side_effect=AssertionError("File IO forbidden")):
            self.assertEqual(self.lookup(), synthetic_contract())

    def test_identity_build_and_complete_scope_must_match_reviewed_entry(self):
        invalid = (
            {"actor": "another~actor"}, {"build_id": "latest"}, {"build_number": "latest"},
            {"target_username": "another"}, {"target_user_id": "54321"},
            {"latest_limit": True}, {"latest_limit": 20}, {"include_replies": False},
            {"exclude_retweets": False}, {"contract_id": "another-contract"},
        )
        for change in invalid:
            entry = replace(synthetic_contract(), **change)
            with self.subTest(change=change), self.assertRaises(SourceEvidenceError):
                resolve_source_contract("synthetic-offline-contract", target_username="thsottiaux",
                                        target_user_id="12345", now=NOW,
                                        catalog={"synthetic-offline-contract": entry})

    def test_untrusted_urls_missing_digest_and_bad_dates_are_rejected(self):
        invalid = (
            {"official_evidence_urls": ()},
            {"official_evidence_urls": ("https://evil.example/proof",)},
            {"official_evidence_urls": ("https://apify.com.evil.example/proof",)},
            {"official_evidence_urls": ("https://apify.com/proof?token=synthetic-secret",)},
            {"official_evidence_urls": ["https://apify.com/proof"]},
            {"evidence_sha256": ""}, {"evidence_sha256": "g" * 64},
            {"reviewed_at": "2026-10-10T07:00:00"},
            {"expires_at": "2026-10-18T07:00:01Z"},
            {"expires_at": "2026-10-10T07:00:00Z"},
        )
        for change in invalid:
            with self.subTest(fields=tuple(change)), self.assertRaises(SourceEvidenceError) as caught:
                self.lookup(replace(synthetic_contract(), **change))
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_expired_and_future_reviews_block_use_at_exact_boundaries(self):
        for now in (datetime(2026, 10, 10, 6, 59, tzinfo=timezone.utc),
                    datetime(2026, 10, 11, 7, tzinfo=timezone.utc)):
            with self.subTest(now=now), self.assertRaises(SourceEvidenceError) as caught:
                self.lookup(now=now)
            self.assertEqual(caught.exception.code, "APIFY_SOURCE_CONTRACT_EXPIRED")
        self.lookup(now=datetime(2026, 10, 10, 7, tzinfo=timezone.utc))

    def test_target_id_must_be_explicit_safe_uint64_string(self):
        for identifier in (None, "", "0", "01", 12345, True, "18446744073709551616"):
            with self.subTest(identifier=identifier), self.assertRaises(SourceEvidenceError):
                self.lookup(target_user_id=identifier)


if __name__ == "__main__":
    unittest.main()
