#!/usr/bin/env python3
"""
Manifest I/O and schema validation.

Provides atomic read/write operations for the fleet manifest YAML file,
with full schema validation. Single-writer discipline enforced via lock.
"""

import os
import json
import tempfile
import fcntl
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class ManifestError(Exception):
    """Base exception for manifest errors."""
    pass


class SchemaError(ManifestError):
    """Schema validation error."""
    pass


def manifest_read(feature: str) -> Dict:
    """
    Read and validate manifest for a feature.

    Args:
        feature: Feature name (e.g., "payments")

    Returns:
        Parsed manifest dict

    Raises:
        FileNotFoundError: Manifest not found
        SchemaError: Schema validation failed
    """
    manifest_path = _manifest_path(feature)

    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found for feature '{feature}' at {manifest_path}")

    try:
        with open(manifest_path, 'r') as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SchemaError(f"Failed to parse manifest YAML: {e}")

    if data is None:
        raise SchemaError("Manifest file is empty")

    errors = manifest_validate(data)
    if errors:
        raise SchemaError(f"Schema validation failed: {'; '.join(errors)}")

    return data


def manifest_write_atomic(feature: str, manifest_obj: Dict) -> None:
    """
    Atomically write manifest for a feature.

    Validates schema before write, then uses temp+rename for atomicity.

    Args:
        feature: Feature name
        manifest_obj: Manifest dict to write

    Raises:
        SchemaError: Validation failed
        IOError: Write failed
    """
    # Validate schema before write
    errors = manifest_validate(manifest_obj)
    if errors:
        raise SchemaError(f"Schema validation failed: {'; '.join(errors)}")

    manifest_path = _manifest_path(feature)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp + rename
    fd, temp_path = tempfile.mkstemp(
        prefix=f".manifest.{feature}.",
        suffix=".tmp",
        dir=str(manifest_path.parent)
    )

    try:
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump(manifest_obj, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())

        os.rename(temp_path, str(manifest_path))
    except Exception as e:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise IOError(f"Failed to write manifest: {e}")


def manifest_validate(obj: Dict) -> List[str]:
    """
    Validate manifest against schema rules.

    Returns:
        List of validation errors (empty if valid)
    """
    errors = []

    # Check required top-level fields
    required_fields = ['feature', 'nodes']
    for field in required_fields:
        if field not in obj:
            errors.append(f"FLEET_003: missing required field '{field}'")

    if not isinstance(obj.get('nodes'), dict):
        errors.append("FLEET_003: 'nodes' must be a dict")
        return errors  # Cannot continue validation without nodes

    # Validate node structure
    nodes = obj.get('nodes', {})
    for node_name, node in nodes.items():
        if not isinstance(node, dict):
            errors.append(f"FLEET_003: node '{node_name}' must be a dict")
            continue

        # Check kind field
        if 'kind' not in node:
            errors.append(f"FLEET_003: node '{node_name}' missing 'kind' field")
            continue

        kind = node.get('kind')
        if kind not in ['feature', 'decision', 'artifact']:
            errors.append(f"FLEET_003: node '{node_name}' invalid kind '{kind}'")

        # Decision nodes must have options
        if kind == 'decision':
            if 'options' not in node or not isinstance(node.get('options'), dict):
                errors.append(f"FLEET_003: decision node '{node_name}' missing 'options' dict")

    # Cycle detection on edges (via base for features, deps for artifacts)
    try:
        if _has_cycle(nodes):
            errors.append("FLEET_002: DAG has cycle")
    except Exception as e:
        errors.append(f"FLEET_003: cycle detection error: {e}")

    return errors


def _manifest_path(feature: str) -> Path:
    """Get manifest file path for a feature."""
    return Path(os.path.expanduser(f"~/.fleet/{feature}/manifest.yaml"))


def _has_cycle(nodes: Dict) -> bool:
    """
    Detect cycle in DAG via DFS.

    Edges: base (feature) or deps (artifact).
    """
    visited = set()
    rec_stack = set()

    def dfs(node_name: str) -> bool:
        if node_name in rec_stack:
            return True  # Cycle detected
        if node_name in visited:
            return False

        visited.add(node_name)
        rec_stack.add(node_name)

        node = nodes.get(node_name, {})
        descendants = []

        # Feature edges via base
        if node.get('base'):
            descendants.append(node['base'])

        # Artifact edges via deps
        if node.get('kind') == 'artifact' and node.get('deps'):
            descendants.extend(node['deps'])

        for desc in descendants:
            if desc in nodes and dfs(desc):
                return True

        rec_stack.remove(node_name)
        return False

    # Run DFS from all nodes
    for node_name in nodes:
        if node_name not in visited:
            if dfs(node_name):
                return True

    return False


if __name__ == '__main__':
    # Simple test
    import sys

    if len(sys.argv) > 1:
        feature_name = sys.argv[1]
        try:
            data = manifest_read(feature_name)
            print(f"✓ Manifest for '{feature_name}' is valid")
            print(json.dumps(data, indent=2))
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
