"""Response-only navigation for saved plots; archived HTML is never rewritten."""

import html
import re
from urllib.parse import quote


def add_preview_toolbar(document, case_id, preview_id):
    """Insert static navigation into already validated saved-plot HTML bytes."""
    case = quote(case_id, safe="")
    preview = quote(preview_id, safe="")
    destination = html.escape(f"/#case/{case}/preview/{preview}/miro", quote=True)
    toolbar = ("<style>.liquid-preview-tools{position:sticky;top:0;z-index:10;display:flex;"
               "gap:20px;flex-wrap:wrap;margin:0;padding:12px 24px;background:#fff;"
               "border-bottom:1px solid #d5dbe3;font:14px system-ui,sans-serif}"
               ".liquid-preview-tools a{padding:8px 12px;border-radius:6px;background:#155e75;"
               "color:#fff;text-decoration:none;font-weight:600}"
               "@media print{.liquid-preview-tools{display:none}}</style>"
               '<nav class="liquid-preview-tools" aria-label="Saved preview actions">'
               f'<a href="{destination}" target="_blank" rel="noopener noreferrer">'
               "Use this preview in Miro</a>"
               "</nav>").encode("utf-8")
    body = re.search(br"<body\b[^>]*>", document, re.IGNORECASE)
    position = body.end() if body else 0
    return document[:position] + toolbar + document[position:]
