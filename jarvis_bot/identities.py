from __future__ import annotations

import json
import logging
from pathlib import Path


LOGGER = logging.getLogger(__name__)


def load_identity_reference(path: Path | None) -> str:
    """Load durable name-equivalence groups for prompt reference."""
    groups: list[list[str]] = []
    if path is None or not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_groups = data.get("identity_groups", [])
        if not isinstance(raw_groups, list):
            raise ValueError("identity_groups must be a list")

        for raw_group in raw_groups:
            if not isinstance(raw_group, list):
                raise ValueError("each identity group must be a list")
            names: list[str] = []
            seen: set[str] = set()
            for raw_name in raw_group:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    raise ValueError("identity names must be non-empty strings")
                name = raw_name.strip()
                folded = name.casefold()
                if folded not in seen:
                    names.append(name)
                    seen.add(folded)
            if len(names) >= 2:
                groups.append(names)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        LOGGER.warning("Could not load server identity aliases from %s: %s", path, error)
        groups = []

    return "\n".join(" = ".join(names) for names in groups)
