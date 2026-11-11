"""Validation for names entering the model catalogue."""

import logging

logger = logging.getLogger(__name__)


def is_safe_model_name(name: str) -> bool:
    """Reject C0/C1 control characters and log skipped names without raw controls."""
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name):
        logger.warning("Skipping model with control characters in its name: %r", name)
        return False
    return True
