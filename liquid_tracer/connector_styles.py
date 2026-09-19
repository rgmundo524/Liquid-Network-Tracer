"""Shared visual hierarchy without changing an edge's evidential role."""


def stroke_width(role):
    """Make the traced flow easier to follow while keeping context visible."""
    return 1 if role.startswith("context") else 3
