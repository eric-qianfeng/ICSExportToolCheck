import argparse
import html
import json
import math
import shutil
import sqlite3
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import zip_longest
from numbers import Integral, Real
from pathlib import Path
from urllib.parse import quote

import numpy as np


@dataclass(frozen=True)
class Tolerances:
    time_absolute: float = 0.0
    time_relative: float = 0.0
    value_absolute: float = 0.0
    value_relative: float = 0.0

    @classmethod
    def from_config(cls, config):
        options = config.get("comparison", {})
        if not isinstance(options, dict):
            raise ValueError("comparison must be an object")
        names = (
            "timeAbsoluteTolerance", "timeRelativeTolerance",
            "valueAbsoluteTolerance", "valueRelativeTolerance",
        )
        values = []
        for name in names:
            value = options.get(name, 0.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite nonnegative number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
            values.append(float(value))
        return cls(*values)


def values_equal(old, new, absolute=0.0, relative=0.0):
    if old is None or new is None:
        return old is None and new is None
    if isinstance(old, Real) and isinstance(new, Real):
        if old == new:
            return True
        if isinstance(old, Integral) and isinstance(new, Integral):
            if absolute == 0 and relative == 0:
                return False
            return abs(int(old) - int(new)) <= max(
                absolute, relative * max(abs(int(old)), abs(int(new)))
            )
        if math.isnan(old) or math.isnan(new):
            return math.isnan(old) and math.isnan(new)
        if not math.isfinite(old) or not math.isfinite(new):
            return False
        if absolute == 0 and relative == 0:
            return False
        return abs(old - new) <= max(absolute, relative * max(abs(old), abs(new)))
    return type(old) is type(new) and old == new


def metadata_differences(old, new, tolerances):
    failures = {"sample_count", "start_time_ns"}
    ignored = {
        "bit_length", "data_type", "signal_type", "slope", "offset", "states", "conversion",
        "StartByte", "StartBit", "BigEndian", "MUXValue", "Formula", "Sequence",
    }
    for field in sorted((old.keys() | new.keys()) - ignored):
        left, right = old.get(field), new.get(field)
        if field == "start_time_ns":
            equal = values_equal(left, right, tolerances.time_absolute * 1e9, tolerances.time_relative)
        else:
            equal = values_equal(left, right)
        if not equal:
            yield ("FAIL" if field in failures else "WARNING", field, left, right)


@dataclass
class Channel:
    key: tuple
    metadata: dict
    location: tuple
    internal_id: int | tuple | None = None


def match_channels(old_channels, new_channels):
    for key in sorted(old_channels.keys() | new_channels.keys()):
        left, right = old_channels.get(key, []), new_channels.get(key, [])
        if len(left) > 1 and len(left) == len(right):
            old_by_id = {channel.internal_id: channel for channel in left}
            new_by_id = {channel.internal_id: channel for channel in right}
            if (
                None not in old_by_id and None not in new_by_id
                and len(old_by_id) == len(left) and len(new_by_id) == len(right)
            ):
                for internal_id in sorted(old_by_id.keys() | new_by_id.keys()):
                    yield (
                        key,
                        [old_by_id[internal_id]] if internal_id in old_by_id else [],
                        [new_by_id[internal_id]] if internal_id in new_by_id else [],
                        internal_id,
                    )
                continue
        yield key, left, right, None


@dataclass
class Batch:
    times: np.ndarray
    raw: np.ndarray
    physical: np.ndarray
    valid: np.ndarray


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


class DBReader:
    def __init__(self, path):
        self.connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.metadata = {"format": "SQLite"}
        self.channels = {}
        try:
            query = """SELECT c.*, m.ID AS matched_message_id, m.Name AS message_name, m.Network AS message_network,
                m.DataTableName AS data_table, m.ArbId AS arb_id, m.MessageCount AS message_count,
                m.Transmitters AS transmitters FROM Channels c LEFT JOIN Messages m ON c.MessageId=m.ID"""
            counts = {}
            states = {}
            if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ChannelStates'"
            ).fetchone():
                for row in self.connection.execute("SELECT ChannelId, NumVal, StateText FROM ChannelStates"):
                    states.setdefault(row[0], []).append((row[1], row[2]))
            for record in self.connection.execute(query):
                row = dict(record)
                if row["matched_message_id"] is None:
                    if row["ID"] == 0 and row["Name"] == "Collection Key" and row["MessageId"] is None and row["MessageChannel"] is None:
                        self.metadata["collection_key"] = tuple(
                            (name, value) for name, value in row.items()
                            if name not in {"ID", "matched_message_id", "message_name", "message_network", "data_table", "arb_id", "message_count", "transmitters"}
                        )
                        continue
                    raise ValueError(f"Channel has no matching message: {row['ID']} / {row['Name']}")
                number = int(row["MessageChannel"])
                table = row["data_table"] or f"MessageData{row['MessageId']:04d}"
                if number > 500:
                    table += f"_{number // 500 + 1:04d}"
                if table not in counts:
                    counts[table] = self.connection.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                    ).fetchone()[0]
                metadata = {
                    "unit": row.get("Units"), "signal_type": row.get("SignalType"),
                    "bit_length": row.get("BitLength"), "slope": row.get("Slope"),
                    "offset": row.get("Offset"), "sample_count": counts[table],
                    "data_type": row.get("DataType"), "arb_id": row["arb_id"],
                    "states": tuple(sorted(states.get(row["ID"], []), key=repr)),
                }
                excluded = {
                    "ID", "MessageId", "MessageChannel", "Name", "Units", "SignalType",
                    "BitLength", "Slope", "Offset", "DataType", "data_table", "message_name",
                    "message_network", "arb_id",
                    "matched_message_id",
                }
                metadata.update({name: value for name, value in row.items() if name not in excluded})
                key = (row["message_network"] or "", row["message_name"] or "", row["Name"] or "")
                channel = Channel(key, metadata, (table, f"Signal{number}"), int(row["ID"]))
                self.channels.setdefault(key, []).append(channel)
        except Exception:
            self.close()
            raise

    def batches(self, channel, size=8192):
        table, column = channel.location
        query = (
            f"SELECT TIME_VAL, {quote_identifier(column)} FROM {quote_identifier(table)} "
            "ORDER BY TIME_VAL, rowid"
        )
        cursor = self.connection.execute(query)
        try:
            while rows := cursor.fetchmany(size):
                times = np.asarray([row[0] for row in rows])
                raw = np.asarray([row[1] for row in rows])
                valid = np.asarray([row[1] is not None for row in rows], dtype=bool)
                slope, offset = channel.metadata["slope"], channel.metadata["offset"]
                if slope is None or offset is None:
                    raise ValueError(f"Missing scaling parameters: {channel.key}")
                if raw.dtype.kind == "O":
                    physical = np.asarray([
                        None if value is None else float(value) * slope + offset for value in raw
                    ], dtype=object)
                else:
                    physical = raw.astype(np.float64) * slope + offset
                yield Batch(times, raw, physical, valid)
        finally:
            cursor.close()

    def close(self):
        self.connection.close()


def conversion_signature(conversion):
    if conversion is None:
        return None
    fields = []
    for key in sorted(dir(conversion)):
        if key in {"conversion_type", "formula"} or key.startswith(
            ("val_", "raw_", "phys_", "lower_", "upper_", "P")
        ) or key in {"a", "b"}:
            if not key.endswith("_addr") and key not in {"val_param_nr"}:
                try:
                    value = getattr(conversion, key)
                except AttributeError:
                    continue
                fields.append((key, value))
    for key, value in sorted(conversion.referenced_blocks.items()):
        fields.append((key, conversion_signature(value) if hasattr(value, "conversion_type") else value))
    return tuple(fields)


def mf4_physical_values(samples, conversion):
    values = np.asarray(samples)
    if conversion is None:
        return values
    physical = np.asarray(conversion.convert(values, as_object=True))
    if conversion.conversion_type != 7 or values.ndim != 1 or values.dtype.kind not in "biuf":
        return physical
    physical = physical.astype(object)
    unmatched = np.ones(values.shape, dtype=bool)
    for index in range(conversion.val_param_nr):
        matched = unmatched & (values == getattr(conversion, f"val_{index}"))
        reference = conversion.referenced_blocks[f"text_{index}"]
        if matched.any():
            if hasattr(reference, "conversion_type"):
                physical[matched] = mf4_physical_values(values[matched], reference)
            else:
                physical[matched] = reference
        unmatched &= ~matched
    default = conversion.referenced_blocks.get("default_addr")
    if unmatched.any():
        if hasattr(default, "conversion_type"):
            physical[unmatched] = mf4_physical_values(values[unmatched], default)
        elif isinstance(default, bytes) and default:
            physical[unmatched] = default
        elif default is None or default == b"":
            physical[unmatched] = values[unmatched]
    return physical


class MF4Reader:
    def __init__(self, path):
        from asammdf import MDF

        self.measurement = MDF(path)
        self.channels = {}
        self.metadata = {
            "start_time_ns": int(self.measurement.header.abs_time),
            "header_comment": self.measurement.header.comment,
        }
        try:
            for group_index, group in enumerate(self.measurement.groups):
                message = group.channel_group.acq_name or ""
                source = group.channel_group.acq_source
                source_path = source.path if source else ""
                suffix = "." + message
                network = source_path[:-len(suffix)] if message and source_path.endswith(suffix) else source_path
                for channel_index, channel in enumerate(group.channels):
                    if channel.channel_type in {2, 3}:
                        continue
                    conversion = channel.conversion
                    key = (network, message, channel.name)
                    metadata = {
                        "unit": channel.unit or (conversion.unit if conversion else ""),
                        "data_type": channel.data_type, "bit_length": channel.bit_count,
                        "sample_count": int(group.channel_group.cycles_nr),
                        "conversion": conversion_signature(conversion),
                        "description": channel.comment,
                        "transmitter": source.name if source else "",
                        "source_comment": source.comment if source else "",
                        "channel_type": channel.channel_type,
                    }
                    self.channels.setdefault(key, []).append(Channel(
                        key, metadata, (group_index, channel_index), (group_index, channel_index),
                    ))
        except Exception:
            self.close()
            raise

    def batches(self, channel, size=8192):
        group, index = channel.location
        count = channel.metadata["sample_count"]
        for offset in range(0, count, size):
            signal = self.measurement.get(
                group=group, index=index, raw=True, record_offset=offset,
                record_count=min(size, count - offset), ignore_invalidation_bits=True,
            )
            if len(signal.samples) != min(size, count - offset):
                raise ValueError(f"Unexpected sample count: {channel.key}")
            physical = mf4_physical_values(signal.samples, signal.conversion)
            valid = np.ones(len(signal.samples), dtype=bool) if signal.invalidation_bits is None else ~signal.invalidation_bits
            yield Batch(signal.timestamps, signal.samples, np.asarray(physical), valid)

    def close(self):
        self.measurement.close()


def equal_mask(old, new, absolute=0.0, relative=0.0):
    old, new = np.asarray(old), np.asarray(new)
    if old.shape != new.shape:
        return np.zeros(len(old), dtype=bool)
    if old.ndim != 1 or old.dtype.kind not in "biuf" or new.dtype.kind not in "biuf":
        result = []
        for left, right in zip(old, new):
            if np.ndim(left) or np.ndim(right):
                result.append(bool(np.all(equal_mask(np.ravel(left), np.ravel(right), absolute, relative))))
            else:
                left = left.item() if isinstance(left, np.generic) else left
                right = right.item() if isinstance(right, np.generic) else right
                result.append(values_equal(left, right, absolute, relative))
        return np.asarray(result, dtype=bool)
    integer_pair = old.dtype.kind in "biu" and new.dtype.kind in "biu"
    mixed_integer = (old.dtype.kind in "biu") != (new.dtype.kind in "biu")
    if mixed_integer or (integer_pair and (old.dtype != new.dtype or absolute or relative)):
        return equal_mask(old.astype(object), new.astype(object), absolute, relative)
    with np.errstate(invalid="ignore", over="ignore"):
        equal = old == new
        if not integer_pair:
            equal |= np.isnan(old) & np.isnan(new)
            if absolute or relative:
                equal |= np.isfinite(old) & np.isfinite(new) & (
                    np.abs(old - new) <= np.maximum(absolute, relative * np.maximum(np.abs(old), np.abs(new)))
                )
    return equal


def sample_differences(old_batches, new_batches, tolerances):
    position = 0
    for old, new in zip_longest(old_batches, new_batches):
        old_count = len(old.times) if old is not None else 0
        new_count = len(new.times) if new is not None else 0
        common = min(old_count, new_count)
        if common:
            fields = (
                ("time", old.times[:common], new.times[:common], tolerances.time_absolute, tolerances.time_relative),
                ("valid", old.valid[:common], new.valid[:common], 0, 0),
                ("physical", old.physical[:common], new.physical[:common], tolerances.value_absolute, tolerances.value_relative),
            )
            for name, left, right, absolute, relative in fields:
                matching = equal_mask(left, right, absolute, relative)
                if name == "physical":
                    matching |= ~(old.valid[:common] & new.valid[:common])
                for index in np.flatnonzero(~matching):
                    yield position + int(index), name, old.times[index], new.times[index], left[index], right[index]
        for index in range(common, max(old_count, new_count)):
            left = (old.times[index], old.physical[index], old.valid[index]) if index < old_count else None
            right = (new.times[index], new.physical[index], new.valid[index]) if index < new_count else None
            yield position + index, "sample_missing", left[0] if left else None, right[0] if right else None, left, right
        position += max(old_count, new_count)


STYLE = """
:root { color-scheme: light; --ink:#202623; --muted:#58635e; --line:#d6ded9;
--green:#14663f; --red:#ac2430; --amber:#845b00; }
* { box-sizing:border-box; letter-spacing:0; }
body { margin:0; color:var(--ink); background:#f7f9f8; font:14px 'Segoe UI',sans-serif; }
header { padding:28px 4%; background:#e9f1ed; border-bottom:3px solid var(--green); }
main { padding:24px 4%; }
h1,h2,h3 { font-family:Georgia,serif; overflow-wrap:anywhere; }
h1 { font-size:28px; margin:12px 0; } h2 { font-size:22px; } h3 { font-size:18px; }
a { color:#075d88; overflow-wrap:anywhere; }
p { line-height:1.6; } code,td { overflow-wrap:anywhere; white-space:pre-wrap; }
.table-scroll { overflow:auto; max-width:100%; }
table { border-collapse:collapse; width:100%; margin:12px 0 24px; table-layout:fixed; }
th,td { text-align:left; vertical-align:top; border:1px solid var(--line); padding:8px; }
th { background:#eaf0ed; } td { background:white; }
.PASS { color:var(--green); } .FAIL,.ERROR { color:var(--red); }
.WARNING { color:var(--amber); } .status { font-weight:700; }
.toolbar { display:flex; flex-wrap:wrap; gap:16px; align-items:center; padding:16px 0; }
input,select { font:inherit; padding:8px; border:1px solid #87978e; border-radius:4px; max-width:100%; }
input { width:320px; } label { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
button { font:inherit; min-width:36px; min-height:36px; cursor:pointer; }
#index-pages { display:flex; gap:12px; align-items:center; margin:12px 0; }
section.channel { border-top:2px solid var(--line); padding:12px 0; content-visibility:auto;
contain-intrinsic-size:auto 240px; }
details>summary { cursor:pointer; padding:10px 0; font-weight:600; }
small,.muted { color:var(--muted); } .metrics { display:flex; gap:24px; flex-wrap:wrap; }
@media(max-width:640px) { header,main { padding:18px 12px; } th,td { padding:5px; font-size:12px; }
.details-table { min-width:850px; } h1 { font-size:24px; } }
"""

SCRIPT = """
const search = document.getElementById('search');
const statusFilter = document.getElementById('status-filter');
const indexSource = document.getElementById('channel-index-data');
const channelData = indexSource ? JSON.parse(indexSource.textContent) : null;
let indexPage = 0;
let filteredChannels = channelData;
const pageSize = 200;
function renderChannelIndex() {
    const body = document.getElementById('channel-index-body');
    body.replaceChildren();
    const pageCount = Math.max(1, Math.ceil(filteredChannels.length / pageSize));
    indexPage = Math.max(0, Math.min(indexPage, pageCount - 1));
    for (const channel of filteredChannels.slice(indexPage * pageSize, (indexPage + 1) * pageSize)) {
        const row = document.createElement('tr');
        row.dataset.label = channel.label;
        row.dataset.status = channel.status;
        const nameCell = document.createElement('td');
        const link = document.createElement('a');
        link.href = '#' + channel.anchor;
        link.textContent = channel.label;
        nameCell.append(link);
        row.append(nameCell);
        for (const key of ['status','old_samples','new_samples','sample_differences','warnings','errors']) {
            const cell = document.createElement('td');
            cell.textContent = channel[key];
            if (key === 'status') cell.className = 'status ' + channel.status;
            row.append(cell);
        }
        body.append(row);
    }
    document.getElementById('page-state').textContent =
        (indexPage + 1) + ' / ' + pageCount + ' | ' + filteredChannels.length + ' channels';
    document.getElementById('previous-page').disabled = indexPage === 0;
    document.getElementById('next-page').disabled = indexPage + 1 >= pageCount;
}
function filterIndex() {
  const query = search.value.toLowerCase();
    if (channelData) {
        filteredChannels = channelData.filter(channel => channel.label.toLowerCase().includes(query) &&
            (!statusFilter.value || channel.status === statusFilter.value));
        indexPage = 0;
        renderChannelIndex();
        return;
    }
  document.querySelectorAll('tr[data-label]').forEach(row => {
    row.hidden = !row.dataset.label.toLowerCase().includes(query) ||
      (statusFilter.value && row.dataset.status !== statusFilter.value);
  });
}
search.addEventListener('input', filterIndex);
statusFilter.addEventListener('change', filterIndex);
if (channelData) {
    document.getElementById('previous-page').addEventListener('click', () => { indexPage--; renderChannelIndex(); });
    document.getElementById('next-page').addEventListener('click', () => { indexPage++; renderChannelIndex(); });
    renderChannelIndex();
}
function revealChannel() {
    const identifier = decodeURIComponent(location.hash.slice(1));
    const payload = document.getElementById('payload-' + identifier);
    if (payload) document.getElementById('channel-details').innerHTML = JSON.parse(payload.textContent);
    const section = document.getElementById(identifier);
  if (section) {
    section.querySelectorAll('details').forEach(detail => detail.open = true);
    section.scrollIntoView();
  }
}
window.addEventListener('hashchange', revealChannel);
revealChannel();
"""


def display(value):
    if isinstance(value, np.ndarray):
        return np.array2string(value, threshold=sys.maxsize)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(display(item) for item in value) + "]"
    return repr(value)


def escape(value):
    return html.escape(str(value), quote=True)


def cells(values):
    return "".join(f"<td>{escape(value)}</td>" for value in values)


def status_of(failures=0, warnings=0, errors=0):
    return "ERROR" if errors else "FAIL" if failures else "WARNING" if warnings else "PASS"


def page_start(stream, title, back_link=None):
    stream.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1">')
    stream.write(f"<title>{escape(title)}</title><style>{STYLE}</style></head><body><header>")
    if back_link:
        stream.write(f'<a href="{escape(back_link)}">All results</a>')
    stream.write(f'<h1>{escape(title)}</h1><p>Generated {escape(datetime.now(timezone.utc).isoformat())}</p></header><main>')


def toolbar(stream, include_pass=True):
    stream.write('<div class="toolbar"><label>Search <input id="search" type="search"></label>'
                 '<label>Status <select id="status-filter"><option value="">All</option>')
    if include_pass:
        stream.write('<option>PASS</option>')
    stream.write('<option>WARNING</option><option>FAIL</option>'
                 '<option>ERROR</option></select></label></div>')


def write_table_header(stream, headings, css_class=""):
    stream.write(f'<div class="table-scroll"><table class="{css_class}"><thead><tr>')
    stream.write("".join(f"<th>{escape(heading)}</th>" for heading in headings))
    stream.write('</tr></thead><tbody>')


def table_end(stream):
    stream.write('</tbody></table></div>')


class DetailStream:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(json.dumps(text, ensure_ascii=True)[1:-1].replace("<", "\\u003c"))


def compare_pair(old_path, new_path, output_path, kind, tolerances, back_link, input_error=None):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "report": output_path, "status": "PASS", "failures": 0, "warnings": 0,
        "errors": 0, "old_channels": 0, "new_channels": 0, "channels": [],
    }
    file_rows = []
    file_metadata = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as body, ExitStack() as stack:
        try:
            if input_error:
                raise ValueError(input_error)
            for version, path in (("old", old_path), ("new", new_path)):
                if path is None or not path.is_file():
                    result["failures"] += 1
                    file_rows.append(("FAIL", "missing_file", version, str(path)))
            if not result["failures"]:
                reader_type = DBReader if kind == "db" else MF4Reader
                old_reader = reader_type(old_path)
                stack.callback(old_reader.close)
                new_reader = reader_type(new_path)
                stack.callback(new_reader.close)
                file_metadata = [("old", old_reader.metadata), ("new", new_reader.metadata)]
                result["old_channels"] = sum(map(len, old_reader.channels.values()))
                result["new_channels"] = sum(map(len, new_reader.channels.values()))
                for severity, field, left, right in metadata_differences(
                    old_reader.metadata, new_reader.metadata, tolerances
                ):
                    result["failures" if severity == "FAIL" else "warnings"] += 1
                    file_rows.append((severity, field, display(left), display(right)))
                if not old_reader.channels and not new_reader.channels:
                    raise ValueError("Both files contain no comparable channels")
                matches = list(match_channels(old_reader.channels, new_reader.channels))
                for number, (key, left, right, internal_id) in enumerate(matches, 1):
                    label = " / ".join(key)
                    if internal_id is not None:
                        label += f" [internal ID: {display(internal_id)}]"
                    channel_result = {
                        "anchor": f"channel-{number}", "label": label,
                        "failures": 0, "warnings": 0, "errors": 0, "sample_differences": 0,
                        "old_samples": left[0].metadata["sample_count"] if len(left) == 1 else "-",
                        "new_samples": right[0].metadata["sample_count"] if len(right) == 1 else "-",
                    }
                    body.write(f'<script type="application/json" id="payload-channel-{number}">"')
                    detail = DetailStream(body)
                    detail.write(f'<section class="channel" id="channel-{number}"><a href="#channel-index">Channel index</a>')
                    detail.write(f'<h3>{escape(channel_result["label"])}</h3><details><summary>All differences</summary>')
                    write_table_header(detail, ("Status", "Sample (0-based)", "Field", "Old time", "New time", "Old", "New"), "details-table")
                    try:
                        if len(left) != 1 or len(right) != 1:
                            field = "missing_internal_id" if internal_id is not None else (
                                "duplicate_identity" if len(left) > 1 or len(right) > 1 else "missing_channel"
                            )
                            channel_result["failures"] += 1
                            detail.write('<tr>' + cells(("FAIL", "-", field, "-", "-", len(left), len(right))) + '</tr>')
                            for version, channels in (("old", left), ("new", right)):
                                for channel in channels:
                                    detail.write('<tr>' + cells(("INFO", "-", f"{version} channel metadata (ID: {display(channel.internal_id)})", "-", "-", display(channel.metadata), display(channel.location))) + '</tr>')
                        else:
                            if internal_id is not None:
                                detail.write('<tr>' + cells(("INFO", "-", "matched_internal_id", "-", "-", display(left[0].internal_id), display(right[0].internal_id))) + '</tr>')
                            for severity, field, old_value, new_value in metadata_differences(left[0].metadata, right[0].metadata, tolerances):
                                channel_result["failures" if severity == "FAIL" else "warnings"] += 1
                                detail.write('<tr>' + cells((severity, "-", field, "-", "-", display(old_value), display(new_value))) + '</tr>')
                            with ExitStack() as generators:
                                old_batches = old_reader.batches(left[0])
                                new_batches = new_reader.batches(right[0])
                                generators.callback(old_batches.close)
                                generators.callback(new_batches.close)
                                for position, field, old_time, new_time, old_value, new_value in sample_differences(old_batches, new_batches, tolerances):
                                    channel_result["failures"] += 1
                                    channel_result["sample_differences"] += 1
                                    detail.write('<tr>' + cells(("FAIL", position, field, display(old_time), display(new_time), display(old_value), display(new_value))) + '</tr>')
                    except Exception as error:
                        channel_result["errors"] += 1
                        detail.write('<tr>' + cells(("ERROR", "-", "read_error", "-", "-", type(error).__name__, str(error))) + '</tr>')
                    if not any(channel_result[field] for field in ("failures", "warnings", "errors")):
                        detail.write('<tr>' + cells(("PASS", "-", "No differences", "-", "-", "-", "-")) + '</tr>')
                    table_end(detail)
                    detail.write('</details></section>')
                    body.write('"</script>')
                    channel_result["status"] = status_of(channel_result["failures"], channel_result["warnings"], channel_result["errors"])
                    for field in ("failures", "warnings", "errors"):
                        result[field] += channel_result[field]
                    result["channels"].append(channel_result)
                    if number % 500 == 0:
                        print(f"  {kind}: {number}/{len(matches)} channels", flush=True)
        except Exception as error:
            result["errors"] += 1
            file_rows.append(("ERROR", "file_read_error", type(error).__name__, str(error)))
        result["status"] = status_of(result["failures"], result["warnings"], result["errors"])
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent, suffix=".tmp", delete=False) as report:
            temporary_path = Path(report.name)
            page_start(report, output_path.stem, back_link)
            report.write(f'<h2 class="status {result["status"]}">{result["status"]}</h2>')
            report.write(f'<p>Old: {escape(old_path)}<br>New: {escape(new_path)}</p>')
            report.write(f'<p>Channels: {result["old_channels"]} old / {result["new_channels"]} new. '
                         f'Failed checks: {result["failures"]}; warnings: {result["warnings"]}; errors: {result["errors"]}.</p>')
            report.write('<p>Matched by network / message / channel name. Equal-count duplicate names are paired by internal ID '
                         '(SQLite Channels.ID; MDF group/channel indexes). Missing IDs or unequal duplicate counts fail. '
                         'Sample values are compared only after physical conversion. Raw storage, bit lengths, data types '
                         'and conversion definitions are not compared. '
                         'Samples are compared in timestamp order '
                         'by sequence position, without interpolation or resampling. Both invalid values compare as missing; '
                         'two NaNs compare equal. Missing channels are listed with metadata and sample counts.</p>')
            report.write(f'<p>Tolerances: {escape(tolerances)}. Numeric rule: |old-new| &lt;= max(absolute, relative * max(|old|, |new|)). '
                         'All differences are retained. Field differences, not distinct samples, are counted.</p>')
            if file_metadata:
                report.write('<details><summary>File metadata (including reserved non-signal records)</summary>')
                write_table_header(report, ("Version", "Metadata"))
                for version, metadata in file_metadata:
                    report.write('<tr>' + cells((version, display(metadata))) + '</tr>')
                table_end(report)
                report.write('</details>')
            if file_rows:
                write_table_header(report, ("Status", "File field", "Old / version", "New / detail"))
                for row in file_rows:
                    report.write('<tr>' + cells(row) + '</tr>')
                table_end(report)
            report.write('<h2 id="channel-index">Channel index</h2>')
            toolbar(report, include_pass=False)
            report.write('<nav id="index-pages"><button type="button" id="previous-page" title="Previous page" aria-label="Previous page">&larr;</button>'
                         '<span id="page-state"></span><button type="button" id="next-page" title="Next page" aria-label="Next page">&rarr;</button></nav>')
            write_table_header(report, ("Channel", "Status", "Old samples", "New samples", "Sample field differences", "Warnings", "Errors"))
            report.write('</tbody><tbody id="channel-index-body">')
            table_end(report)
            report.write('<noscript>Enable JavaScript to navigate this complete offline report.</noscript>')
            report.write('<div id="channel-details"></div><script type="application/json" id="channel-index-data">')
            indexed_channels = [channel for channel in result["channels"] if channel["status"] != "PASS"]
            report.write(json.dumps(indexed_channels, ensure_ascii=True).replace("<", "\\u003c"))
            report.write('</script>')
            body.seek(0)
            shutil.copyfileobj(body, report)
            report.write(f'<script>{SCRIPT}</script></main></body></html>')
        temporary_path.replace(output_path)
    return result


def discover_pairs(export_dir):
    pairs = {}
    bases = set()
    for path in sorted(export_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".db", ".mf4", ".mdf", ".dat"}:
            continue
        relative = path.relative_to(export_dir)
        if any(part.startswith(".export-") for part in relative.parts):
            continue
        for version in ("old", "new"):
            suffix = "-" + version
            if path.stem.endswith(suffix):
                base = path.stem[:-len(suffix)]
                kind = "db" if path.suffix.lower() == ".db" else "mf4"
                bases.add((relative.parent, base))
                pairs.setdefault((relative.parent, base, kind), {}).setdefault(version, []).append(path)
                break
    for folder, base in bases:
        for kind in ("db", "mf4"):
            pairs.setdefault((folder, base, kind), {})
    return pairs


def write_index(report_dir, results, tolerances):
    output = report_dir / "index.html"
    report_dir.mkdir(parents=True, exist_ok=True)
    counts = {status: sum(row["status"] == status for row in results) for status in ("PASS", "WARNING", "FAIL", "ERROR")}
    overall = status_of(counts["FAIL"], counts["WARNING"], counts["ERROR"])
    if not results:
        overall = "ERROR"
    with output.open("w", encoding="utf-8") as stream:
        page_start(stream, "Export comparison results")
        stream.write(f'<h2 class="status {overall}">{overall}</h2><div class="metrics">')
        for status, count in counts.items():
            stream.write(f'<p class="status {status}">{status}: {count}</p>')
        stream.write('</div><p>This run compares old/new DB and MDF files independently. '
                     'FAIL indicates data differences or missing files; WARNING indicates non-failing metadata differences; '
                     'ERROR indicates incomplete comparison. PASS and WARNING are successful comparisons. CSV files are not compared.</p>')
        stream.write(f'<p>{escape(tolerances)}</p>')
        if not results:
            stream.write('<p class="ERROR">No comparable exported files were found.</p>')
        folders = sorted({row["folder"] for row in results})
        stream.write('<h2>Folder index</h2><ul>')
        for number, folder in enumerate(folders):
            group = [row for row in results if row["folder"] == folder]
            status = status_of(sum(row["status"] == "FAIL" for row in group), sum(row["status"] == "WARNING" for row in group), sum(row["status"] == "ERROR" for row in group))
            stream.write(f'<li><a href="#folder-{number}">{escape(folder)}</a> <strong class="{status}">{status}</strong></li>')
        stream.write('</ul>')
        toolbar(stream)
        for number, folder in enumerate(folders):
            stream.write(f'<h2 id="folder-{number}">{escape(folder)}</h2>')
            write_table_header(stream, ("Report", "Status", "Old channels", "New channels", "Failed checks", "Warnings", "Errors"))
            for row in results:
                if row["folder"] != folder:
                    continue
                relative = row["report"].relative_to(report_dir).as_posix()
                stream.write(f'<tr data-label="{escape(relative)}" data-status="{row["status"]}">'
                             f'<td><a href="{quote(relative, safe="/")}">{escape(row["report"].name)}</a></td>'
                             f'<td class="status {row["status"]}">{row["status"]}</td>')
                stream.write(cells(row[field] for field in ("old_channels", "new_channels", "failures", "warnings", "errors")) + '</tr>')
            table_end(stream)
        stream.write(f'<script>{SCRIPT}</script></main></body></html>')
    return overall


def compare_all(config_path):
    config_path = Path(config_path).resolve()
    with config_path.open(encoding="utf-8-sig") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    tolerances = Tolerances.from_config(config)
    paths = {}
    for key in ("exportDir", "reportDir"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Config requires a non-empty string: {key}")
        paths[key] = (config_path.parent / value).resolve()
    if not paths["exportDir"].is_dir():
        raise ValueError(f"Export directory does not exist: {paths['exportDir']}")
    results = []
    for (folder, base, kind), versions in sorted(discover_pairs(paths["exportDir"]).items()):
        output = paths["reportDir"] / folder / f"{base}-{kind}.html"
        back_link = "../" * len(folder.parts) + "index.html"
        print(f"Comparing {folder / base} ({kind})", flush=True)
        old_files, new_files = versions.get("old", []), versions.get("new", [])
        if len(old_files) > 1 or len(new_files) > 1:
            result = compare_pair(
                None, None, output, kind, tolerances, back_link,
                input_error=f"Ambiguous files: {old_files} / {new_files}",
            )
        else:
            result = compare_pair(old_files[0] if old_files else None, new_files[0] if new_files else None, output, kind, tolerances, back_link)
        result["folder"] = folder.as_posix()
        results.append(result)
        print(f"  {result['status']}: {result['failures']} failed checks, {result['warnings']} warnings, {result['errors']} errors", flush=True)
    overall = write_index(paths["reportDir"], results, tolerances)
    print(f"Overall: {overall}. Report: {paths['reportDir'] / 'index.html'}", flush=True)
    return 2 if overall == "ERROR" else 1 if overall == "FAIL" else 0


def main():
    parser = argparse.ArgumentParser(description="Compare old/new SQLite DB and MDF files and generate indexed HTML reports.")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    arguments = parser.parse_args()
    try:
        return compare_all(arguments.config)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())