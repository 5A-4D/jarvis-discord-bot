from __future__ import annotations

import re
from enum import Enum

from .public_images import is_public_image_request


class PublicDecision(Enum):
    ALLOW = "allow"
    DENY_ACTION = "deny_action"

_LOCAL_DATA_ACCESS_PATTERN = re.compile(
    r"\b(?:search|read(?:ing)?|access|inspect|list|show)\b.{0,80}\b(?:files?|folders?|"
    r"directories|directory|desktops?|laptops?|computers?|hosts?|machines?|"
    r"databases?|processes|process|"
    r"local\s+(?:disk|drive|storage)|document)\b",
    re.IGNORECASE,
)

_DIRECT_ACTION_PATTERN = re.compile(
    r"^\s*(?:(?:please\s+)|(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)|"
    r"(?:i\s+(?:need|want|would\s+like)\s+you\s+to\s+)|"
    r"(?:go\s+ahead\s+and\s+))?(?:create|write|edit|modify|delete|remove|move|copy|"
    r"rename|save|download|upload|install|uninstall|run|execute|launch|restart|"
    r"send|post|dm|email|play)\b",
    re.IGNORECASE,
)

_CONTENT_REQUEST_PATTERN = re.compile(
    r"^\s*(?:(?:please\s+)|(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)|"
    r"(?:i\s+(?:need|want|would\s+like)\s+you\s+to\s+))?"
    r"(?:create|write|edit|modify|review|debug|explain)\b.{0,80}\b(?:code|"
    r"script|program|function|class|regex|query|example|answer|summary|table|"
    r"guide|instructions?)\b",
    re.IGNORECASE,
)

_PRIVATE_NETWORK_URL_PATTERN = re.compile(
    r"https?://(?:localhost|127(?:\.\d{1,3}){3}|10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
    r"\[?::1\]?|169\.254(?:\.\d{1,3}){2})(?::\d+)?(?:[/\s]|$)",
    re.IGNORECASE,
)

def classify_public_request(prompt: str) -> PublicDecision:
    """Allow read-only inquiries while rejecting host and account operations."""
    cleaned = prompt.strip()
    if not cleaned:
        return PublicDecision.ALLOW
    if _PRIVATE_NETWORK_URL_PATTERN.search(cleaned):
        return PublicDecision.DENY_ACTION
    if _LOCAL_DATA_ACCESS_PATTERN.search(cleaned):
        return PublicDecision.DENY_ACTION
    if is_public_image_request(cleaned):
        return PublicDecision.ALLOW
    if _CONTENT_REQUEST_PATTERN.search(cleaned):
        return PublicDecision.ALLOW
    if _DIRECT_ACTION_PATTERN.search(cleaned):
        return PublicDecision.DENY_ACTION
    return PublicDecision.ALLOW
