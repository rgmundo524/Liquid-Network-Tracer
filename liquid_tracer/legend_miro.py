"""Readable, bounded color-key shapes for Miro without adding graph nodes."""

import html
import math

from .legend import legend_notes, legend_rows
from .name_colors import color_value
from .presentation_items import proof

WIDTH = 1300
FONT_SIZE = 16
PAGE_GAP = 80
CONTENT_LIMIT = 5500  # Below the documented 6000-character shape-content limit.
ROWS_PER_PAGE = 20
WRAP_COLUMNS = 100


def _wrapped(value):
    """Escape after wrapping; never cut an HTML entity or discard name text."""
    lines = []
    for line in str(value).split("\n"):
        lines.extend(line[start:start + WRAP_COLUMNS]
                     for start in range(0, max(1, len(line)), WRAP_COLUMNS))
    return "<br>".join(html.escape(line) for line in lines), len(lines)


def _row(label, description, color):
    name, name_lines = _wrapped(label)
    detail, detail_lines = _wrapped(description)
    content = ('<p><span style="color: ' + color + '">●</span> <strong>'
               + name + '</strong>' + (' · ' + detail if description else '') + '</p>')
    # Allow wrapping where a label and its description share their first line.
    lines = max(1, name_lines + detail_lines - 1,
                math.ceil((len(label) + len(description) + 6) / WRAP_COLUMNS))
    return content, 30 * lines + 12


def _row_blocks(row):
    color = color_value(row["color"])
    label, description = str(row["label"]), str(row["description"])
    content, height = _row(label, description, color)
    if len(content) <= 4400:
        return [(content, height)]
    # Historical imported labels need not obey today's short-name validator.
    # Preserve every character across pages, even for entity-heavy text.
    chunks = [label[start:start + 600] for start in range(0, len(label), 600)] or [""]
    result = [_row(chunk, "", color) for chunk in chunks]
    result.extend(_row("", description[start:start + 600], color)
                  for start in range(0, len(description), 600))
    return result


def _note_blocks(value):
    text = str(value)
    blocks = []
    for start in range(0, max(1, len(text)), 600):
        content, lines = _wrapped(text[start:start + 600])
        blocks.append(("<p>" + content + "</p>", 27 * lines + 10))
    return blocks


def _pages(graph):
    title = ("SYNTHETIC DATA · " if graph.get("simulated") else "") + "Liquid UTXO trace · Color key"
    blocks = [block for row in legend_rows(graph) for block in _row_blocks(row)]
    blocks.extend(block for note in legend_notes(graph) for block in _note_blocks(note))
    pages, page, height = [], [], 90

    def heading(number):
        suffix = " · continued " + str(number + 1) if number else ""
        return "<p><strong>" + html.escape(title + suffix) + "</strong></p>"

    for content, block_height in blocks:
        header = heading(len(pages))
        if page and (len(page) >= ROWS_PER_PAGE
                     or len(header) + sum(len(part) for part in page) + len(content) > CONTENT_LIMIT):
            pages.append((header + "".join(page), height))
            page, height = [], 90
        page.append(content)
        height += block_height
    pages.append((heading(len(pages)) + "".join(page), height))
    return pages


def make_items(graph):
    """Primary legend plus stable, proven overflow pages, outside trace geometry."""
    top = min((float(node["y"]) - float(node["height"]) / 2
               for node in graph.get("nodes", [])), default=0)
    annotation = graph.get("layout", {}).get("annotations", {}).get("legend", {})
    x = float(annotation.get("x", 700))
    # Old layouts saved a 260-high legend's center. Preserve its bottom anchor
    # when it is safe; growth goes upward, away from transactions and fee rows.
    bottom = min(top - 100, float(annotation.get("y", top - 230)) + 130)
    shapes, catalog = [], {}
    for page, (content, height) in enumerate(_pages(graph)):
        key = "legend"
        if page:
            marker = proof("legend", "legend", page)
            key = marker["key"]
            catalog[key] = marker
        shapes.append({"key": key, "body": {
            "data": {"shape": "rectangle", "content": content},
            "position": {"x": x + page * (WIDTH + PAGE_GAP), "y": bottom - height / 2,
                         "origin": "center"},
            "geometry": {"width": WIDTH, "height": height},
            "style": {"fillColor": "#f8fafc", "borderColor": "#cbd5e1", "borderWidth": "1",
                      "fontSize": str(FONT_SIZE), "color": "#172033", "textAlign": "left",
                      "textAlignVertical": "top"}}})
    return shapes, catalog


def bounds(graph):
    """Center coordinates and dimensions, shared by planning and compaction."""
    shapes, _ = make_items(graph)
    return [(item["body"]["position"]["x"], item["body"]["position"]["y"],
             item["body"]["geometry"]["width"], item["body"]["geometry"]["height"])
            for item in shapes]
