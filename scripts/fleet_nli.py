#!/usr/bin/env python3
"""
Natural Language Intent (NLI) classifier for fleet operations.

Classifies user utterances into one of 11 intents (PLAN, MATERIALIZE, WORK, COMMIT,
PUBLISH, REVIEW_FIX, RESOLVE_DECISION, ARTIFACT_REFRESH, MERGE, STATUS, DRYRUN).
Performs fuzzy node resolution and confidence gating for destructive operations.
"""

import os
import re
from typing import Dict, List, Optional
from difflib import SequenceMatcher
from fleet_manifest import manifest_read, ManifestError


class NLIError(Exception):
    """Base exception for NLI errors."""
    pass


# Intent definitions with keyword patterns and confidence baselines
INTENTS = {
    "PLAN": {
        "keywords": ["plan", "spec", "design", "outline"],
        "base_confidence": 0.9,
        "destructive": False
    },
    "MATERIALIZE": {
        "keywords": ["materialize", "create", "init", "setup", "build"],
        "base_confidence": 0.85,
        "destructive": False
    },
    "WORK": {
        "keywords": ["work", "start", "begin", "allocate", "checkout"],
        "base_confidence": 0.8,
        "destructive": False
    },
    "COMMIT": {
        "keywords": ["commit", "done", "finish", "complete", "mark"],
        "base_confidence": 0.8,
        "destructive": True
    },
    "PUBLISH": {
        "keywords": ["publish", "pr", "present", "push", "generate"],
        "base_confidence": 0.85,
        "destructive": False
    },
    "REVIEW_FIX": {
        "keywords": ["fix", "review", "resolve", "address"],
        "base_confidence": 0.75,
        "destructive": True
    },
    "RESOLVE_DECISION": {
        "keywords": ["decide", "resolve", "choose", "pick", "select"],
        "base_confidence": 0.8,
        "destructive": True
    },
    "ARTIFACT_REFRESH": {
        "keywords": ["refresh", "regenerate", "update", "artifact"],
        "base_confidence": 0.8,
        "destructive": False
    },
    "MERGE": {
        "keywords": ["merge", "cleanup", "squash", "integrate"],
        "base_confidence": 0.85,
        "destructive": True
    },
    "STATUS": {
        "keywords": ["status", "show", "list", "blocked", "state"],
        "base_confidence": 0.85,
        "destructive": False
    },
    "DRYRUN": {
        "keywords": ["dry", "preview", "simulate", "dry-run"],
        "base_confidence": 0.8,
        "destructive": False
    }
}

CONFIDENCE_THRESHOLD = 0.7


def nli_classify(utterance: str, feature: Optional[str] = None) -> Dict:
    """
    Classify user utterance into an intent and extract node references.

    Args:
        utterance: User utterance (e.g., "merge the auth node")
        feature: Optional feature name context

    Returns:
        Dict with {intent, feature, nodes, args, confidence, requires_confirmation?}
        - intent: One of the 11 intent types
        - feature: Feature name (inferred or provided)
        - nodes: List of matched node dicts
        - args: Dict of operation arguments (dry_run, merge_mode, etc.)
        - confidence: Float [0..1]
        - requires_confirmation: Bool (if True for destructive op, user must confirm)
    """
    # Step 1: Infer feature context
    if not feature:
        feature = _infer_feature_context()

    # Step 2: Read manifest to get available nodes
    try:
        manifest = manifest_read(feature)
    except ManifestError as e:
        raise NLIError(f"Failed to read manifest for feature '{feature}': {e}")

    nodes = manifest.get("nodes", {})
    utterance_lower = utterance.lower()

    # Step 3: Detect intent via keyword matching and heuristic scoring.
    # An intent only scores if at least one of its keywords is present in the
    # utterance; otherwise every intent would inherit its base_confidence and
    # the highest-base intent (PLAN) would always win regardless of wording.
    intent_scores = {}
    for intent_name, intent_config in INTENTS.items():
        matched = False
        for keyword in intent_config["keywords"]:
            if keyword in utterance_lower:
                matched = True
                break
        if matched:
            # Base confidence with a small boost for a positive keyword hit
            score = min(1.0, intent_config["base_confidence"] + 0.05)
            intent_scores[intent_name] = score

    if not intent_scores:
        # No clear intent; default to STATUS
        intent = "STATUS"
        confidence = 0.5
    else:
        intent = max(intent_scores, key=intent_scores.get)
        confidence = intent_scores[intent]

    # Step 4: Fuzzy node resolution
    node_refs = []
    keywords = _extract_keywords(utterance)
    for keyword in keywords:
        matches = fuzzy_match_nodes(keyword, nodes)
        for match in matches:
            if match not in node_refs:
                node_refs.append(match)

    # Step 5: Resolve ambiguity
    if len(node_refs) > 1:
        # Multiple matches; surface to user for disambiguation
        pass  # User will pick via interactive prompt (handled by caller)
    elif len(node_refs) == 0 and intent in ["COMMIT", "MERGE", "PUBLISH", "REVIEW_FIX"]:
        # Destructive op but no nodes found; cannot proceed
        pass

    # Step 6: Build args dict
    args = {
        "feature": feature,
        "nodes": node_refs,
        "dry_run": "dry" in utterance_lower or "preview" in utterance_lower
    }

    if intent == "MERGE":
        args["merge_mode"] = "batch" if "all" in utterance_lower else "individual"

    if intent == "RESOLVE_DECISION":
        # Extract chosen option (e.g., "decide on optA")
        option_match = re.search(r"\b(opt\w+|option\w+)\b", utterance_lower)
        if option_match:
            args["chosen_option"] = option_match.group(1)

    # Step 7: Confidence check for destructive ops
    intent_config = INTENTS.get(intent, {})
    requires_confirmation = False
    if intent_config.get("destructive", False):
        if confidence < CONFIDENCE_THRESHOLD or len(node_refs) != 1:
            requires_confirmation = True

    return {
        "intent": intent,
        "feature": feature,
        "nodes": node_refs,
        "args": args,
        "confidence": confidence,
        "requires_confirmation": requires_confirmation
    }


def fuzzy_match_nodes(keyword: str, nodes: Dict) -> List[str]:
    """
    Fuzzy-match a keyword against node descriptions, branches, and PR numbers.

    Args:
        keyword: Search keyword
        nodes: Dict of node_name -> node_dict from manifest

    Returns:
        List of matched node names (may be empty or contain duplicates removed)
    """
    matches = []
    keyword_lower = keyword.lower()

    for node_name, node in nodes.items():
        if not isinstance(node, dict):
            continue

        # Exact or substring match on description
        desc = node.get("desc", "").lower()
        if keyword_lower in desc:
            matches.append(node_name)
            continue

        # Substring match on ghost branch
        ghost = node.get("ghost", "").lower()
        if keyword_lower in ghost:
            matches.append(node_name)
            continue

        # Exact match on PR number
        present = node.get("present", {})
        if isinstance(present, dict):
            pr_num = present.get("pr")
            if pr_num and str(pr_num) == keyword:
                matches.append(node_name)
                continue

        # Fuzzy similarity match on desc (if no substring match)
        similarity = SequenceMatcher(None, keyword_lower, desc).ratio()
        if similarity > 0.6:
            matches.append(node_name)

    # Remove duplicates, preserving order
    seen = set()
    unique_matches = []
    for m in matches:
        if m not in seen:
            unique_matches.append(m)
            seen.add(m)

    return unique_matches


def _extract_keywords(utterance: str) -> List[str]:
    """
    Extract search keywords from utterance.

    Simple heuristic: words not in a stoplist.

    Args:
        utterance: User utterance

    Returns:
        List of keywords
    """
    stopwords = {
        "the", "a", "an", "is", "are", "be", "do", "did", "will", "would",
        "could", "should", "can", "may", "must", "might", "for", "of", "in",
        "on", "at", "to", "from", "by", "and", "or", "not", "no", "yes",
        "please", "thanks", "thank", "you", "i", "me", "my", "we", "our",
        "fleet", "node", "nodes", "branch", "branches", "commit", "commits",
        "git", "pr", "pr's", "feature", "features"
    }

    # Split on whitespace, remove punctuation, filter stopwords
    tokens = re.findall(r"\b\w+\b", utterance.lower())
    keywords = [t for t in tokens if t not in stopwords and len(t) > 2]

    return keywords


def _infer_feature_context() -> str:
    """
    Infer feature context from environment or user input.

    Checks: cwd git remote, last-touched feature in ~/.fleet/, or prompts user.

    Returns:
        Feature name
    """
    # Try to infer from cwd
    import subprocess
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2
        )
        remote = result.stdout.strip()
        # Extract feature name from remote (heuristic)
        if "fleet" in remote.lower():
            return "fleet"  # Default fallback
    except Exception:
        pass

    # Check ~/.fleet/ for most-recently-touched feature
    fleet_dir = os.path.expanduser("~/.fleet")
    if os.path.isdir(fleet_dir):
        features = [
            d for d in os.listdir(fleet_dir)
            if os.path.isdir(os.path.join(fleet_dir, d))
        ]
        if features:
            # Return most recent
            feature_times = [
                (d, os.path.getmtime(os.path.join(fleet_dir, d)))
                for d in features
            ]
            feature_times.sort(key=lambda x: x[1], reverse=True)
            return feature_times[0][0]

    # Prompt user
    print("No feature context detected. Available features in ~/.fleet/:")
    if os.path.isdir(fleet_dir):
        for d in os.listdir(fleet_dir):
            print(f"  - {d}")
    feature = input("Enter feature name: ").strip()
    return feature


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        utterance = " ".join(sys.argv[1:])
        feature = os.environ.get("FLEET_FEATURE")

        try:
            result = nli_classify(utterance, feature)
            print("Classification result:")
            for key, value in result.items():
                print(f"  {key}: {value}")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: fleet_nli.py '<utterance>'", file=sys.stderr)
        sys.exit(1)
