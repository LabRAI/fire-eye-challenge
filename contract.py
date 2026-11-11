"""Data-only prospective submissions and paired, positive-case comparison."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import re

UTC = dt.timezone.utc
SCHEMA = 'fire-eye-challenge-v1'
MAX_CELLS = 4096
MAX_BYTES = 60000
GRID = {'width': 938, 'height': 609, 'crs': 'EPSG:5070',
        'transform': [5000., 0., -2362425., 0., -5000., 3267405.]}
MARKER = '<!-- fire-eye-challenge:v1 -->'
FORBIDDEN = ''.join(map(chr, [81, 119, 101, 110])).casefold()

def stamp(value=None):
    return (value or dt.datetime.now(UTC)).astimezone(UTC).isoformat().replace('+00:00', 'Z')

def instant(value):
    try:
        out = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if out.tzinfo is None: raise ValueError
        return out.astimezone(UTC)
    except (AttributeError, ValueError, TypeError):
        raise ValueError('Use a complete UTC timestamp.') from None

def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def checksum(value):
    return hashlib.sha256(encode(value)).hexdigest()

def pairs(items):
    result = {}
    for k, v in items:
        if k in result: raise ValueError('Duplicate JSON fields are not accepted.')
        result[k] = v
    return result

def parse(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_BYTES:
        raise ValueError('Submit a JSON file smaller than 60 KB.')
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise ValueError('The prediction must be valid JSON with unique fields.') from None
    return validate(result)

def validate(data):
    fields = {'schema', 'model_name', 'source_id', 'issue_time_utc', 'target_time_utc', 'region', 'scores'}
    if not isinstance(data, dict) or set(data) != fields or data['schema'] != SCHEMA:
        raise ValueError('Use the published challenge template and its exact fields.')
    name = data['model_name']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,79}', name) or FORBIDDEN in name.casefold():
        raise ValueError('Use a neutral model label of 1–80 letters, digits or simple punctuation.')
    if not isinstance(data['source_id'], str) or not re.fullmatch(r'firewxfm_forecast_[A-Za-z0-9_]{1,160}', data['source_id']):
        raise ValueError('Select a published reference forecast.')
    issue, end = instant(data['issue_time_utc']), instant(data['target_time_utc'])
    if (end - issue).total_seconds() not in (43200, 86400, 129600, 172800):
        raise ValueError('The reference lead must be 12, 24, 36 or 48 hours.')
    region = data['region']
    if not isinstance(region, dict) or set(region) != {'row', 'column', 'width', 'height'} or any(type(v) is not int for v in region.values()):
        raise ValueError('Region indices must be integers on the published 5 km grid.')
    r, c, w, h = [region[x] for x in ('row', 'column', 'width', 'height')]
    if min(r, c) < 0 or min(w, h) < 1 or w*h > MAX_CELLS or c+w > GRID['width'] or r+h > GRID['height']:
        raise ValueError('Choose an in-grid rectangle containing 1–4096 cells.')
    values = data['scores']
    if not isinstance(values, list) or len(values) != w*h:
        raise ValueError('Supply one row-major score for every cell in the rectangle.')
    if any(v is not None and (type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1) for v in values):
        raise ValueError('Scores must be finite numbers between 0 and 1; null is reserved for reference NoData.')
    return {**data, 'issue_time_utc': stamp(issue), 'target_time_utc': stamp(end)}

def freeze(data, reference, received_at, reference_published_at):
    """The trusted collector fixes bytes and time before the ENTIRE target window."""
    data = validate(data)
    received = instant(received_at)
    start = instant(data['target_time_utc']) - dt.timedelta(hours=12)
    if received >= start:
        raise ValueError('This window has started. Submit a later forecast window.')
    if instant(reference_published_at) > received or instant(data['issue_time_utc']) > received:
        raise ValueError('Reference availability could not be verified before submission.')
    if len(reference) != len(data['scores']): raise ValueError('Reference grid mismatch.')
    valid = [i for i, x in enumerate(reference) if x is not None]
    if not valid: raise ValueError('This selection has no reference land cells.')
    if any(data['scores'][i] is None for i in valid):
        raise ValueError('All reference land cells require a prediction, including low-risk cells.')
    # Published baseline values have 0.001 precision; compare at the same precision.
    scores = [None if x is None else math.floor(x*1000 + .5)/1000 for x in data['scores']]
    return {'submission': data, 'submission_sha256': checksum(data), 'received_at_utc': stamp(received),
            'window_start_utc': stamp(start), 'baseline': reference, 'scores': scores,
            'valid_cells': valid, 'baseline_published_at_utc': reference_published_at,
            'method_version': 'positive-capture-v1', 'status': 'awaiting-observations'}

def top_capture(scores, positive_indices, fraction):
    """Fractional tie weight keeps a constant field at the area-budget baseline."""
    if not positive_indices: return None
    n = len(scores); budget = n*fraction
    ordered = sorted(scores, reverse=True)
    cutoff = ordered[max(0, math.ceil(budget)-1)]
    above = sum(x > cutoff for x in scores)
    equal = sum(x == cutoff for x in scores)
    tie = min(1., max(0., (budget-above)/equal))
    return sum(1. if scores[i] > cutoff else tie if scores[i] == cutoff else 0. for i in positive_indices)/len(positive_indices)

def compare(receipt, positive_cells, observed_at):
    valid = receipt['valid_cells']; indices = {v:i for i,v in enumerate(valid)}
    positives = sorted({indices[i] for i in positive_cells if i in indices})
    result = {'positive_cells':len(positives), 'evaluated_cells':len(valid),
              'observed_at_utc':observed_at, 'method_version':'positive-capture-v1',
              'scope':'Documented wildfire discovery locations in this submitted region and 12-hour window. No-fire cells are not verified negatives.'}
    for name, values in [('submitted',receipt['scores']),('firewxfm',receipt['baseline'])]:
        scores = [values[i] for i in valid]
        result[name] = {'capture_top_10pct':top_capture(scores,positives,.1),
                        'capture_top_20pct':top_capture(scores,positives,.2),
                        'mean_score_at_positive_cells':sum(scores[i] for i in positives)/len(positives) if positives else None}
    return result
