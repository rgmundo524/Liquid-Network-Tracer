"""Shared visual hierarchy without changing an edge's evidential role."""


def routed_shape(style, routing_exception):
    """Keep curved routes curved; only straight connectors need an elbow fallback.

    A routing exception describes the path, not its appearance. Return links,
    fee links and obstacle detours still retain their routes and attachments.
    """
    return "elbowed" if style == "straight" and routing_exception else style


def stroke_width(role):
    """Make the traced flow easier to follow while keeping context visible."""
    return 1 if role.startswith("context") else 3
