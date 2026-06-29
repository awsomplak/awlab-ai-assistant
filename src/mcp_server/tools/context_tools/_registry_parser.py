"""
Registry parsing helpers — extract structured data from registry.md.

Extracted from context.py for Task 30 refactoring.
"""

import re


def get_current_phase_from_tasks(content: str) -> int | None:
    """
    Extract the current (highest) phase number from tasks.md content.

    Returns:
        Phase number (e.g., 4) or None if no phase headers found.
    """
    phase_pattern = re.compile(r"^##\s+Phase\s+(\d+)", re.IGNORECASE)
    current_phase = None
    for line in content.splitlines():
        phase_match = phase_pattern.match(line)
        if phase_match:
            current_phase = int(phase_match.group(1))
    return current_phase