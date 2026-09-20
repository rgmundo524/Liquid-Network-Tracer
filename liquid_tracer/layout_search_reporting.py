"""Bounded, credential-free summaries of an ELK search's outcomes."""

from .layout_search import MAX_LAYOUT_ATTEMPTS


def public_search_counts(value, *, allow_no_success=False):
    """Expose a consistent numeric summary without copying worker diagnostics."""
    if not isinstance(value, dict):
        return {}
    fields = ("attempt_count", "attempted_count", "successful_count", "failed_count")
    counts = {key: value.get(key) for key in fields}
    if any(type(number) is not int for number in counts.values()):
        return {}
    requested, attempted, successful, failed = (counts[key] for key in fields)
    minimum_success = 0 if allow_no_success else 1
    if (not 1 <= attempted <= requested <= MAX_LAYOUT_ATTEMPTS
            or not minimum_success <= successful <= attempted
            or not 0 <= failed <= attempted or successful + failed != attempted):
        return {}
    return counts


def layout_search_warning(value):
    counts = public_search_counts(value)
    if not counts or not counts["failed_count"]:
        return ""
    return (f"{counts['successful_count']} of {counts['attempt_count']} layout attempts succeeded; "
            f"{counts['failed_count']} failed. Best completed layout retained.")
