"""Local CSV export feedback shared by the terminal input editors."""

from .common import TraceError


def export_saved_inputs(case, kind):
    """Return feedback without touching the calling screen's draft or review."""
    from .input_export import save_input_export

    try:
        result = save_input_export(case, kind=kind)
        return (f"Exported saved inputs: {result['path']}\n"
                "Only saved values are included. Unsaved edits and unapproved imports are not included.")
    except (TraceError, OSError, ValueError, TypeError, KeyError) as exc:
        return f"Export failed: {exc}\nYour current edits and import review are unchanged."
