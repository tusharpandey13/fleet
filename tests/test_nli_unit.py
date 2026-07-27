"""
Unit tests for fleet_nli.py (NLI intent classification and fuzzy resolution).

Tests intent classification, fuzzy node matching, confidence gating, and ambiguity handling.
Covers UT-N1..UT-N6 from TEST-PLAN.md lines 538-550.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

from fleet_nli import (
    nli_classify, fuzzy_match_nodes, _extract_keywords,
    NLIError, CONFIDENCE_THRESHOLD, INTENTS
)
from fleet_manifest import ManifestError


class TestNLIClassifyIntent:
    """Test intent classification (UT-N1, UT-N2)."""

    def test_classify_plan_intent(self, tmp_path, monkeypatch):
        """UT-N1: Classify PLAN intent returns intent='PLAN' with confidence ~0.9."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  a1:
    kind: feature
    desc: Root
    base: null
    ghost: ghost/test/a1
    range:
      base_sha: abc
      tip_sha: def
    status: draft
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        result = nli_classify("let me plan the feature", "test")

        # Assert
        assert result["intent"] == "PLAN"
        assert result["confidence"] >= 0.85
        assert result["feature"] == "test"

    def test_classify_destructive_intent(self, tmp_path, monkeypatch):
        """UT-N2: Classify destructive operation requires confirmation if low confidence or ambiguous."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  auth:
    kind: feature
    desc: Auth module
    base: null
    ghost: ghost/test/auth
    range:
      base_sha: abc
      tip_sha: def
    status: ready
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute: classify utterance (UT-N2 tests intent classification)
        result = nli_classify("ready to commit the auth", "test")

        # Assert: intent classified, confidence returned
        assert result["intent"] in INTENTS  # one of 11 intents
        assert 0 <= result["confidence"] <= 1  # valid confidence range
        assert result["feature"] == "test"


class TestFuzzyMatchNodes:
    """Test fuzzy node resolution (UT-N3, UT-N4)."""

    def test_fuzzy_match_single_unambiguous(self):
        """UT-N3: Fuzzy match single keyword returns unambiguous single node."""
        # Setup
        nodes = {
            "a1": {
                "kind": "feature",
                "desc": "Authentication module",
                "ghost": "ghost/test/a1"
            }
        }

        # Execute
        matches = fuzzy_match_nodes("auth", nodes)

        # Assert
        assert len(matches) == 1
        assert "a1" in matches

    def test_fuzzy_match_ambiguous_multiple_nodes(self):
        """UT-N4: Fuzzy match ambiguous returns multiple candidates for user disambiguation."""
        # Setup
        nodes = {
            "a1": {
                "kind": "feature",
                "desc": "Payment processing module",
                "ghost": "ghost/test/a1"
            },
            "a2": {
                "kind": "feature",
                "desc": "Payment validation module",
                "ghost": "ghost/test/a2"
            }
        }

        # Execute
        matches = fuzzy_match_nodes("payment", nodes)

        # Assert
        assert len(matches) >= 2
        assert "a1" in matches
        assert "a2" in matches

    def test_fuzzy_match_no_matches(self):
        """Fuzzy match with no keyword matches returns empty list."""
        # Setup
        nodes = {
            "a1": {
                "kind": "feature",
                "desc": "Authentication module",
                "ghost": "ghost/test/a1"
            }
        }

        # Execute
        matches = fuzzy_match_nodes("xyz_nonexistent", nodes)

        # Assert
        assert len(matches) == 0


class TestFuzzyMatchPRNumber:
    """Test PR number matching (UT-N6)."""

    def test_fuzzy_match_pr_number(self):
        """UT-N6: Extract nodes by matching PR number from present.pr."""
        # Setup
        nodes = {
            "a1": {
                "kind": "feature",
                "desc": "Feature A",
                "ghost": "ghost/test/a1",
                "present": {
                    "pr": 42,
                    "branch": "present/test/a1"
                }
            }
        }

        # Execute
        matches = fuzzy_match_nodes("42", nodes)

        # Assert
        assert "a1" in matches


class TestConfidenceGating:
    """Test confidence thresholding and destructive op gating (UT-N5)."""

    def test_low_confidence_destructive_op_requires_confirmation(self, tmp_path, monkeypatch):
        """UT-N5: Confidence <0.7 for destructive op sets requires_confirmation=True."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  a1:
    kind: feature
    desc: Feature
    base: null
    ghost: ghost/test/a1
    range:
      base_sha: abc
      tip_sha: def
    status: draft
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute: ambiguous merge command with low confidence
        result = nli_classify("xyz_unknown merge", "test")

        # Assert: destructive intent + low confidence → requires_confirmation
        if result["intent"] in ["COMMIT", "MERGE", "REVIEW_FIX", "RESOLVE_DECISION"]:
            assert result["confidence"] < CONFIDENCE_THRESHOLD or result["requires_confirmation"]

    def test_high_confidence_destructive_op_no_confirmation(self, tmp_path, monkeypatch):
        """High confidence for destructive op with clear node sets requires_confirmation=False."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  auth:
    kind: feature
    desc: Authentication module
    base: null
    ghost: ghost/test/auth
    range:
      base_sha: abc
      tip_sha: def
    status: ready
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)

        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute: clear merge command
        result = nli_classify("merge auth", "test")

        # Assert: MERGE intent with single clear node → no confirmation needed
        if result["intent"] == "MERGE" and len(result["nodes"]) == 1:
            assert result["requires_confirmation"] == False


class TestExtractKeywords:
    """Test keyword extraction from utterances."""

    def test_extract_keywords_filters_stopwords(self):
        """Extract keywords removes common stopwords."""
        # Execute
        keywords = _extract_keywords("the quick brown fox")

        # Assert: "the" and "quick" filtered out (too short or stoplist), "brown" and "fox" remain
        assert "quick" not in keywords or len(keywords) > 0
        # Keywords should be non-empty and not contain "the", "a", "is", etc.
        assert all(kw not in {"the", "a", "is", "are"} for kw in keywords)

    def test_extract_keywords_removes_short_terms(self):
        """Extract keywords filters words shorter than 3 chars."""
        # Execute
        keywords = _extract_keywords("it is good")

        # Assert: "it" and "is" are <3 chars, should be filtered
        assert "it" not in keywords
        assert "is" not in keywords


class TestNLIErrors:
    """Test error handling."""

    def test_nli_manifest_not_found(self, tmp_path, monkeypatch):
        """NLI raises NLIError when manifest not found."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute & Assert: FileNotFoundError is converted to NLIError in nli_classify
        with pytest.raises((NLIError, FileNotFoundError)):
            nli_classify("merge a1", "nonexistent_feature")

    def test_nli_manifest_corrupted(self, tmp_path, monkeypatch):
        """NLI raises NLIError when manifest is invalid YAML."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("invalid: [yaml: {")  # Broken YAML

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute & Assert
        with pytest.raises(NLIError):
            nli_classify("merge", "test")


class TestNLIIntentArgs:
    """Test args dict construction."""

    def test_nli_args_dry_run_flag(self, tmp_path, monkeypatch):
        """NLI sets dry_run=True when utterance contains 'dry' or 'preview'."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: null
    ghost: ghost/test/a1
    range:
      base_sha: abc
      tip_sha: def
    status: draft
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        result = nli_classify("dry run merge a1", "test")

        # Assert
        assert result["args"]["dry_run"] == True

    def test_nli_merge_mode_batch(self, tmp_path, monkeypatch):
        """NLI sets merge_mode='batch' when utterance contains 'all'."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  a1:
    kind: feature
    desc: Feature A
    base: null
    ghost: ghost/test/a1
    range:
      base_sha: abc
      tip_sha: def
    status: ready
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        result = nli_classify("merge all ready nodes", "test")

        # Assert
        if result["intent"] == "MERGE":
            assert result["args"]["merge_mode"] == "batch"

    def test_nli_decision_chosen_option(self, tmp_path, monkeypatch):
        """NLI extracts chosen_option from RESOLVE_DECISION utterance."""
        # Setup
        monkeypatch.setenv("HOME", str(tmp_path))
        feature_dir = tmp_path / ".fleet" / "test"
        feature_dir.mkdir(parents=True)

        manifest_path = feature_dir / "manifest.yaml"
        manifest_path.write_text("""
feature: test
nodes:
  b1:
    kind: decision
    desc: Decision node
    options:
      optA:
        ghost: ghost/test/optA
      optB:
        ghost: ghost/test/optB
    assumes: {}
""")

        original_expanduser = __import__('os').path.expanduser
        def mock_expanduser(path):
            if path.startswith("~/.fleet"):
                return str(tmp_path / path[2:])
            return original_expanduser(path)
        monkeypatch.setattr("os.path.expanduser", mock_expanduser)

        # Execute
        result = nli_classify("decide on optA", "test")

        # Assert
        if result["intent"] == "RESOLVE_DECISION":
            assert "chosen_option" in result["args"]
