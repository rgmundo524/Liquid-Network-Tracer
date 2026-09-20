"""Select supported CSV columns without importing spreadsheet helper fields."""

import csv
import io

from .common import TraceError


def csv_rows(text, *, fields, required, normalize, missing_message):
    """Yield numbered records, retaining malformed-row detection for callers.

    Read positionally so repeated or blank ignored headers cannot hide missing
    cells. Duplicate supported headers remain ambiguous, including aliases.
    As with DictReader, a None key marks a row whose width differs from its header.
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    headers = next(reader, [])
    columns = [(index, normalize(header)) for index, header in enumerate(headers)]
    columns = [(index, field) for index, field in columns if field in fields]
    names = [field for _, field in columns]
    if len(names) != len(set(names)):
        raise TraceError("CSV requires unique supported column names")
    if not required <= set(names):
        raise TraceError(missing_message)
    for values in reader:
        if not values:
            continue
        if len(values) != len(headers):
            yield reader.line_num, {None: None}
        else:
            yield reader.line_num, {field: values[index] for index, field in columns}
