"""
Vault loader utilities for Drewgent brain.

Reads Obsidian vault markdown files and extracts frontmatter + content.
Used by DrewgentBrain to load P-layer documents from the vault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class VaultDoc:
    """A loaded vault document with frontmatter parsed."""

    title: str
    content: str
    tags: list[str]
    links: list[str]
    frontmatter: dict
    file_path: Path
    space: Optional[str] = None
    type: Optional[str] = None


def load_vault_doc(path: str | Path) -> VaultDoc:
    """
    Load a vault markdown file and parse its frontmatter.

    Args:
        path: Path to the .md file

    Returns:
        VaultDoc with title, content, tags, links, frontmatter extracted
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Vault doc not found: {path}")

    raw = path.read_text(encoding="utf-8")
    return parse_vault_doc(raw, path)


def parse_vault_doc(raw: str, file_path: Optional[Path] = None) -> VaultDoc:
    """
    Parse raw markdown content into VaultDoc.

    Args:
        raw: Raw file content (with or without frontmatter)
        file_path: Path for reference (used to extract title if no H1)

    Returns:
        VaultDoc with all fields populated
    """
    frontmatter: dict = {}
    content = raw

    # Parse YAML frontmatter
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            fm_text = raw[3:end]
            content = raw[end + 4 :].lstrip("\n")
            frontmatter = _parse_frontmatter(fm_text)

    # Extract title from frontmatter or first H1
    title = frontmatter.get("title", "")
    if not title:
        h1_match = re.search(r"^#\s+(.+)\s*$", content, re.MULTILINE)
        if h1_match:
            title = h1_match.group(1)
        elif file_path:
            title = file_path.stem.replace("-", " ").replace("_", " ").title()

    # Extract tags from frontmatter
    tags_raw = frontmatter.get("tags", [])
    if isinstance(tags_raw, str):
        tags_raw = [tags_raw]
    tags = [str(t).strip() for t in tags_raw]

    # Extract wikilinks
    links = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
    links = [l.strip() for l in links]

    return VaultDoc(
        title=title,
        content=content,
        tags=tags,
        links=links,
        frontmatter=frontmatter,
        file_path=file_path or Path("."),
        space=frontmatter.get("space"),
        type=frontmatter.get("type"),
    )


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter into a dict."""
    result: dict = {}
    current_key: str | None = None
    current_list: list | None = None

    for line in text.splitlines():
        stripped = line.strip()

        # End of frontmatter
        if stripped == "---" and current_key is not None:
            break

        # Key: value
        colon_pos = stripped.find(":")
        if colon_pos > 0 and current_key is None:
            key = stripped[:colon_pos].strip()
            val = stripped[colon_pos + 1 :].strip()

            if not val or val == "":
                current_key = key
                current_list = None
                continue

            # Inline list
            if val.startswith("[") and val.endswith("]"):
                result[key] = _parse_inline_list(val)
            elif val.startswith('"') or val.startswith("'"):
                result[key] = val.strip("\"'")
            else:
                result[key] = val
            continue

        # Continuation of multiline value
        if current_key is not None:
            if stripped.startswith("- "):
                if current_list is None:
                    current_list = [result.get(current_key, [])]
                    if not isinstance(current_list[0], list):
                        current_list = [[result.pop(current_key)]]
                    result[current_key] = current_list
                current_list[0].append(stripped[2:].strip())
            else:
                current_key = None
                current_list = None

    return result


def _parse_inline_list(text: str) -> list[str]:
    """Parse an inline YAML list like [tag1, tag2, tag3]."""
    inner = text.strip("[]")
    if not inner:
        return []
    items = []
    for item in inner.split(","):
        item = item.strip().strip("'\"").strip()
        if item:
            items.append(item)
    return items


def load_brain_rules_from_vault(vault_path: str | Path) -> list[str]:
    """
    Load P0 brainstem rule tokens from vault .neuron files.

    Returns list of rule_token strings found in P0-brainstem directory.
    """
    vault = Path(vault_path).expanduser()
    p0_dir = vault / "P0-brainstem"

    if not p0_dir.exists():
        return []

    rule_tokens = []
    for neuron_file in p0_dir.rglob("*.neuron"):
        name = neuron_file.stem  # 禁rm_rf_root.neuron → 禁rm_rf_root
        if name.startswith("禁"):
            rule_tokens.append(name)

    return rule_tokens


def get_p_layer_content(vault_path: str | Path, layer: str) -> str:
    """
    Load content from a specific P-layer directory.

    Args:
        vault_path: Path to Drewgent vault (~/.drewgent)
        layer: Layer name like "P0-brainstem", "P1-limbic"

    Returns:
        Concatenated content of all .md files in the layer directory
    """
    vault = Path(vault_path).expanduser()
    layer_dir = vault / layer

    if not layer_dir.exists():
        return ""

    parts = []
    for md_file in sorted(layer_dir.rglob("*.md")):
        try:
            doc = load_vault_doc(md_file)
            parts.append(f"# {doc.title}\n\n{doc.content}")
        except Exception:
            continue

    return "\n\n".join(parts)