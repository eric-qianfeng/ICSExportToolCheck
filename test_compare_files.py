import unittest
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import numpy as np

from compare_files import (
    Batch, Channel, DBReader, MF4Reader, Tolerances, compare_pair, discover_pairs, equal_mask,
    match_channels, metadata_differences, mf4_physical_values, sample_differences, values_equal, write_index,
)
from unittest.mock import patch


class IndexDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.collecting = False
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.collecting = dict(attrs).get("id") == "channel-index-data"

    def handle_endtag(self, tag):
        if tag == "script":
            self.collecting = False

    def handle_data(self, data):
        if self.collecting:
            self.fragments.append(data)

    def channels(self):
        return json.loads("".join(self.fragments))


class ComparisonTests(unittest.TestCase):
    def test_strict_and_tolerant_values(self):
        self.assertFalse(values_equal(1.0, 1.00000001))
        self.assertTrue(values_equal(1.0, 1.00000001, absolute=0.000001))
        self.assertTrue(values_equal(100.0, 101.0, relative=0.01))
        self.assertFalse(values_equal(2**63, 2**63 + 1))
        self.assertFalse(values_equal(None, 0))
        self.assertTrue(values_equal(float("nan"), float("nan")))
        self.assertFalse(values_equal(float("inf"), -float("inf")))

    def test_invalid_tolerances(self):
        self.assertEqual(Tolerances.from_config({}), Tolerances())
        for value in (-1, float("nan"), float("inf"), True, "0"):
            with self.assertRaises(ValueError):
                Tolerances.from_config({"comparison": {"timeAbsoluteTolerance": value}})

    def test_descriptions_and_units_only_warn(self):
        differences = list(metadata_differences(
            {"description": "old", "unit": "m"},
            {"description": "new", "unit": "km"}, Tolerances(),
        ))
        self.assertEqual([row[:2] for row in differences], [
            ("WARNING", "description"), ("WARNING", "unit"),
        ])

    def test_storage_and_conversion_definitions_are_not_compared(self):
        fields = (
            "bit_length", "data_type", "signal_type", "slope", "offset", "states", "conversion",
            "StartByte", "StartBit", "BigEndian", "MUXValue", "Formula", "Sequence",
        )
        old = dict.fromkeys(fields, 1)
        new = dict.fromkeys(fields, 2)
        self.assertEqual(list(metadata_differences(old, new, Tolerances())), [])

    def test_array_comparison_keeps_large_integers_exact(self):
        self.assertEqual(equal_mask(np.array([2**63], dtype=np.uint64), np.array([2**63 + 1], dtype=np.uint64)).tolist(), [False])
        self.assertEqual(equal_mask(np.array([2**53 + 1]), np.array([float(2**53)] )).tolist(), [False])
        self.assertEqual(equal_mask(np.array([float("nan"), 1.0]), np.array([float("nan"), 1.001]), 0.01).tolist(), [True, True])

    def test_samples_compare_time_physical_validity_and_missing(self):
        old = Batch(np.array([1., 2.]), np.array([1, 2]), np.array([2., 4.]), np.array([True, True]))
        new = Batch(np.array([1.01]), np.array([3]), np.array([6.]), np.array([True]))
        differences = list(sample_differences([old], [new], Tolerances()))
        self.assertEqual([row[1] for row in differences], ["time", "physical", "sample_missing"])
        tolerant = list(sample_differences([old], [new], Tolerances(time_absolute=0.02)))
        self.assertNotIn("time", [row[1] for row in tolerant])
        invalid = Batch(np.array([1.]), np.array([999]), np.array([999.]), np.array([False]))
        valid = Batch(np.array([1.]), np.array([0]), np.array([0.]), np.array([True]))
        self.assertEqual([row[1] for row in sample_differences([invalid], [valid], Tolerances())], ["valid"])

    def test_sqlite_split_table_scaling_and_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript('''
                    CREATE TABLE Messages(ID, Name, Network, DataTableName, ArbId, MessageCount, Transmitters);
                    CREATE TABLE Channels(ID, MessageId, MessageChannel, Name, Units, SignalType, BitLength, Slope, Offset, DataType);
                    CREATE TABLE MessageData0001_0002(TIME_VAL, Signal501);
                    INSERT INTO Messages VALUES(1, 'message', 'network', 'MessageData0001', '123', 2, '');
                    INSERT INTO Channels VALUES(10, 1, 501, 'channel', 'm', 'RawValue', 16, 2., 1., 0);
                    INSERT INTO Channels(ID, Name) VALUES(0, 'Collection Key');
                    INSERT INTO MessageData0001_0002 VALUES(2., NULL), (1., 3);
                ''')
            reader = DBReader(path)
            try:
                channel = reader.channels[("network", "message", "channel")][0]
                self.assertEqual(channel.internal_id, 10)
                self.assertIn("collection_key", reader.metadata)
                batch = next(reader.batches(channel))
                self.assertEqual(batch.times.tolist(), [1., 2.])
                self.assertEqual(batch.physical.tolist(), [7., None])
                self.assertEqual(batch.valid.tolist(), [True, False])
            finally:
                reader.close()

    def test_sqlite_different_storage_and_scaling_pass_when_physical_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path, new_path = root / "old.db", root / "new.db"
            for path, raw, slope, bit_length, data_type in (
                (old_path, 476., 1., 64, 4), (new_path, 119, 4., 16, 0),
            ):
                with closing(sqlite3.connect(path)) as connection:
                    connection.executescript('''
                        CREATE TABLE Messages(ID, Name, Network, DataTableName, ArbId, MessageCount, Transmitters);
                        CREATE TABLE Channels(ID, MessageId, MessageChannel, Name, Units, SignalType, BitLength, Slope, Offset, DataType);
                        CREATE TABLE MessageData0001(TIME_VAL, Signal1);
                        INSERT INTO Messages VALUES(1, 'message', 'network', 'MessageData0001', '123', 1, '');
                    ''')
                    connection.execute("INSERT INTO Channels VALUES(1, 1, 1, 'torque', 'Nm', 'RawValue', ?, ?, 0., ?)", (bit_length, slope, data_type))
                    connection.execute("INSERT INTO MessageData0001 VALUES(0., ?)", (raw,))
                    connection.commit()
            result = compare_pair(old_path, new_path, root / "report.html", "db", Tolerances(), "index.html")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["failures"], 0)
            self.assertEqual(result["warnings"], 0)

    def test_html_report_contains_full_differences_safe_names_and_index(self):
        class Reader:
            def __init__(self, path):
                self.metadata = {}
                self.old = path.name == "old.db"
                key = ("network", "message", "<script>alert(1)</script>")
                self.channels = {key: [Channel(key, {
                    "sample_count": 2, "description": "old" if self.old else "new",
                }, ())]}

            def batches(self, channel):
                values = np.array([1., 2.]) if self.old else np.array([3., 4.])
                yield Batch(np.array([1., 2.]), values, values, np.array([True, True]))

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / "old.db", root / "new.db"
            old.touch()
            new.touch()
            output = root / "reports" / "folder" / "a-db.html"
            with patch("compare_files.DBReader", Reader):
                result = compare_pair(old, new, output, "db", Tolerances(), "../index.html")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["warnings"], 1)
            self.assertEqual(result["channels"][0]["sample_differences"], 2)
            document = output.read_text(encoding="utf-8")
            self.assertIn('"anchor": "channel-1"', document)
            self.assertIn('id="payload-channel-1"', document)
            self.assertIn("&lt;script&gt;", document)
            self.assertNotIn("<script>alert(1)</script>", document)
            result["folder"] = "folder"
            self.assertEqual(write_index(root / "reports", [result], Tolerances()), "FAIL")
            self.assertIn('href="folder/a-db.html"', (root / "reports" / "index.html").read_text())

    def test_pair_discovery_includes_missing_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a-old.db").touch()
            pairs = discover_pairs(root)
            self.assertIn((Path("."), "a", "db"), pairs)
            self.assertIn((Path("."), "a", "mf4"), pairs)
            result = compare_pair(root / "a-old.db", None, root / "report.html", "db", Tolerances(), "index.html")
            self.assertEqual(result["status"], "FAIL")

    def test_channel_index_omits_pass_and_keeps_other_statuses(self):
        class Reader:
            def __init__(self, path):
                self.old = path.name == "old.db"
                self.metadata = {}
                self.channels = {}
                for name in ("pass", "warning", "fail", "error"):
                    key = ("network", "message", name)
                    description = "changed" if name == "warning" and not self.old else "same"
                    self.channels[key] = [Channel(key, {"sample_count": 1, "description": description}, ())]

            def batches(self, channel):
                if channel.key[-1] == "error":
                    raise ValueError("unreadable channel")
                value = 2 if channel.key[-1] == "fail" and not self.old else 1
                yield Batch(np.array([0.]), np.array([value]), np.array([value]), np.array([True]))

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / "old.db", root / "new.db"
            old.touch()
            new.touch()
            output = root / "report.html"
            with patch("compare_files.DBReader", Reader):
                result = compare_pair(old, new, output, "db", Tolerances(), "index.html")
            parser = IndexDataParser()
            document = output.read_text(encoding="utf-8")
            parser.feed(document)
            self.assertEqual({row["status"] for row in parser.channels()}, {"WARNING", "FAIL", "ERROR"})
            self.assertEqual(len(result["channels"]), 4)
            self.assertEqual(result["old_channels"], 4)
            self.assertNotIn("<option>PASS</option>", document)

    def test_equal_count_duplicates_compare_samples_by_id_not_order(self):
        changed_ids = set()
        new_ids = [20, 10]

        class Reader:
            def __init__(self, path):
                self.old = path.name == "old.db"
                self.metadata = {}
                key = ("network", "message", "duplicate")
                identifiers = [10, 20] if self.old else new_ids
                self.channels = {key: [
                    Channel(key, {"sample_count": 1}, (), identifier)
                    for identifier in identifiers
                ]}

            def batches(self, channel):
                value = channel.internal_id
                if not self.old and channel.internal_id in changed_ids:
                    value += 1
                yield Batch(np.array([0.]), np.array([value]), np.array([value]), np.array([True]))

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / "old.db", root / "new.db"
            old.touch()
            new.touch()
            output = root / "report.html"
            with patch("compare_files.DBReader", Reader):
                result = compare_pair(old, new, output, "db", Tolerances(), "index.html")
                self.assertEqual(result["status"], "PASS")
                self.assertEqual(len(result["channels"]), 2)
                self.assertEqual(result["old_channels"], 2)
                changed_ids.add(20)
                result = compare_pair(old, new, output, "db", Tolerances(), "index.html")
            self.assertEqual([row["status"] for row in result["channels"]], ["PASS", "FAIL"])
            self.assertEqual(result["channels"][1]["sample_differences"], 1)
            parser = IndexDataParser()
            parser.feed(output.read_text(encoding="utf-8"))
            self.assertEqual(len(parser.channels()), 1)
            self.assertIn("internal ID: 20", parser.channels()[0]["label"])
            changed_ids.clear()
            new_ids[:] = [20, 30]
            with patch("compare_files.DBReader", Reader):
                result = compare_pair(old, new, output, "db", Tolerances(), "index.html")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["failures"], 2)
            self.assertEqual(result["errors"], 0)
            self.assertIn("missing_internal_id", output.read_text(encoding="utf-8"))

    def test_duplicate_matching_reports_unmatched_ids_and_unequal_counts(self):
        key = ("network", "message", "duplicate")

        def channels(identifiers):
            return {key: [Channel(key, {}, (), identifier) for identifier in identifiers]}

        matches = list(match_channels(channels([10, 20]), channels([20, 30])))
        self.assertEqual([(len(left), len(right), identifier) for _, left, right, identifier in matches], [
            (1, 0, 10), (1, 1, 20), (0, 1, 30),
        ])
        matches = list(match_channels(channels([10, 20]), channels([10])))
        self.assertEqual([(len(left), len(right), identifier) for _, left, right, identifier in matches], [(2, 1, None)])
        matches = list(match_channels(channels([10]), channels([20])))
        self.assertEqual([(len(left), len(right), identifier) for _, left, right, identifier in matches], [(1, 1, None)])
        matches = list(match_channels(channels([10, 10]), channels([10, 20])))
        self.assertEqual([(len(left), len(right), identifier) for _, left, right, identifier in matches], [(2, 2, None)])

    def test_real_mf4_group_reordering_metadata_warning_and_values(self):
        from asammdf import MDF, Signal
        from asammdf.blocks.source_utils import Source

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path, new_path = root / "old.mf4", root / "new.mf4"

            def save(path, messages, changed=False):
                with MDF(version="4.10") as measurement:
                    measurement.header.start_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
                    for message in messages:
                        source = Source("sender", "network." + message, "", 2, 2)
                        samples = np.array([1, 9 if changed else 2, 3], dtype=np.int16)
                        signal = Signal(
                            samples=samples, timestamps=np.array([0., 1., 2.]),
                            name="same-name", unit="m",
                            conversion={"conversion_type": 1, "a": 2., "b": 1.},
                            comment="old description" if path == old_path else "new description",
                        )
                        measurement.append([signal], acq_name=message, acq_source=source)
                    measurement.save(path, overwrite=True)

            save(old_path, ["first", "second"])
            save(new_path, ["second", "first"])
            passed = compare_pair(old_path, old_path, root / "passed.html", "mf4", Tolerances(), "index.html")
            self.assertEqual(passed["status"], "PASS")
            self.assertEqual(len(passed["channels"]), 2)
            parser = IndexDataParser()
            parser.feed((root / "passed.html").read_text(encoding="utf-8"))
            self.assertEqual(parser.channels(), [])
            result = compare_pair(old_path, new_path, root / "warning.html", "mf4", Tolerances(), "index.html")
            self.assertEqual(result["status"], "WARNING")
            self.assertEqual(result["old_channels"], 2)
            self.assertEqual(result["new_channels"], 2)
            self.assertEqual(result["warnings"], 2)
            save(new_path, ["second", "first"], changed=True)
            result = compare_pair(old_path, new_path, root / "failure.html", "mf4", Tolerances(), "index.html")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(sum(row["sample_differences"] for row in result["channels"]), 2)
            save(old_path, ["first", "first"])
            save(new_path, ["first", "first"])
            result = compare_pair(old_path, new_path, root / "duplicates.html", "mf4", Tolerances(), "index.html")
            self.assertEqual(result["status"], "WARNING")
            self.assertEqual(len(result["channels"]), 2)
            self.assertTrue(all("internal ID:" in row["label"] for row in result["channels"]))

    def test_mf4_type7_nested_linear_default_preserves_mixed_physical_values(self):
        from asammdf import MDF, Signal
        from asammdf.blocks.v4_blocks import ChannelConversion

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested-default.mf4"
            linear = ChannelConversion(conversion_type=1, a=2.0, b=1.0)
            conversion = ChannelConversion(
                conversion_type=7, ref_param_nr=2, val_0=255, text_0=b"Invalid",
            )
            conversion.referenced_blocks["default_addr"] = linear
            with MDF(version="4.10") as measurement:
                measurement.append([Signal(
                    samples=np.tile(np.array([1, 255, 2], dtype=np.uint16), 51),
                    timestamps=np.arange(153, dtype=float), name="nested",
                    conversion=conversion,
                )])
                measurement.save(path)
            reader = MF4Reader(path)
            try:
                channel = next(iter(reader.channels.values()))[0]
                parsed = reader.measurement.groups[0].channels[channel.location[1]].conversion
                self.assertEqual(parsed.referenced_blocks["default_addr"].conversion_type, 1)
                for size in (1, 3, 153):
                    batches = list(reader.batches(channel, size=size))
                    physical = [value for batch in batches for value in batch.physical.tolist()]
                    raw = [value for batch in batches for value in batch.raw.tolist()]
                    self.assertEqual(raw, [1, 255, 2] * 51)
                    self.assertEqual(physical, [3.0, b"Invalid", 5.0] * 51)
            finally:
                reader.close()

    def test_mf4_empty_state_default_preserves_stored_physical_values(self):
        from asammdf import MDF, Signal
        from asammdf.blocks.v4_blocks import ChannelConversion

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path, new_path = root / "old.mf4", root / "new.mf4"
            old_conversion = ChannelConversion(
                conversion_type=7, ref_param_nr=2, val_0=32764.0, text_0=b"Fault",
            )
            new_conversion = ChannelConversion(
                conversion_type=7, ref_param_nr=2, val_0=8191.0, text_0=b"Fault",
            )
            new_conversion.referenced_blocks["default_addr"] = ChannelConversion(
                conversion_type=1, a=4.0, b=0.0,
            )
            for path, samples, conversion in (
                (old_path, np.array([476., 524., 32764., 0.]), old_conversion),
                (new_path, np.array([119, 131, 8191, 0], dtype=np.uint16), new_conversion),
            ):
                with MDF(version="4.10") as measurement:
                    measurement.header.start_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
                    measurement.append([Signal(
                        samples=samples, timestamps=np.arange(4, dtype=float),
                        name="brake-torque", conversion=conversion,
                    )])
                    measurement.save(path)
            readers = [MF4Reader(old_path), MF4Reader(new_path)]
            try:
                batches = [next(reader.batches(next(iter(reader.channels.values()))[0])) for reader in readers]
                for batch in batches:
                    self.assertEqual(batch.physical.tolist(), [476., 524., b"Fault", 0.])
                fields = [row[1] for row in sample_differences([batches[0]], [batches[1]], Tolerances())]
                self.assertEqual(fields, [])
            finally:
                for reader in readers:
                    reader.close()
            result = compare_pair(old_path, new_path, root / "physical-only.html", "mf4", Tolerances(), "index.html")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["failures"], 0)
            parser = IndexDataParser()
            parser.feed((root / "physical-only.html").read_text(encoding="utf-8"))
            self.assertEqual(parser.channels(), [])

    def test_mf4_empty_default_does_not_replace_explicit_state_text(self):
        from asammdf.blocks.v4_blocks import ChannelConversion

        conversion = ChannelConversion(
            conversion_type=7, ref_param_nr=2, val_0=255, text_0=b"",
        )
        self.assertEqual(mf4_physical_values(np.array([1, 255]), conversion).tolist(), [1, b""])
        conversion.referenced_blocks["default_addr"] = b"Unknown"
        self.assertEqual(mf4_physical_values(np.array([1, 255]), conversion).tolist(), [b"Unknown", b""])

    def test_mf4_large_float_state_table_preserves_explicit_text_mappings(self):
        from asammdf import MDF, Signal
        from asammdf.blocks.v4_blocks import ChannelConversion

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large-float-states.mf4"
            conversion = ChannelConversion(
                conversion_type=7, ref_param_nr=3,
                val_0=74.96322, text_0=b"NoDataExists",
                val_1=74.99985000000001, text_1=b"Fault",
            )
            samples = np.tile(np.array([74.96322, 74.99985000000001, 1.25]), 60)
            with MDF(version="4.10") as measurement:
                measurement.header.start_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
                measurement.append([Signal(
                    samples=samples, timestamps=np.arange(len(samples), dtype=float),
                    name="float-states", conversion=conversion,
                )])
                measurement.save(path)
            reader = MF4Reader(path)
            try:
                channel = next(iter(reader.channels.values()))[0]
                batches = reader.batches(channel)
                try:
                    batch = next(batches)
                finally:
                    batches.close()
                self.assertEqual(batch.raw.tolist(), samples.tolist())
                self.assertEqual(batch.physical.tolist(), [b"NoDataExists", b"Fault", 1.25] * 60)
            finally:
                reader.close()

    def test_mf4_recurses_through_multiple_default_conversion_blocks(self):
        from asammdf.blocks.v4_blocks import ChannelConversion

        inner = ChannelConversion(conversion_type=7, ref_param_nr=2, val_0=255, text_0=b"Fault")
        inner.referenced_blocks["default_addr"] = ChannelConversion(conversion_type=1, a=2., b=1.)
        outer = ChannelConversion(conversion_type=7, ref_param_nr=2, val_0=999, text_0=b"Reserved")
        outer.referenced_blocks["default_addr"] = inner
        values = np.tile(np.array([1, 255, 999, 2]), 40)
        self.assertEqual(mf4_physical_values(values, outer).tolist(), [3., b"Fault", b"Reserved", 5.] * 40)

    def test_corrupt_file_and_ambiguous_pair_are_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "broken.db"
            path.write_bytes(b"not SQLite")
            result = compare_pair(path, path, root / "broken.html", "db", Tolerances(), "index.html")
            self.assertEqual(result["status"], "ERROR")
            result = compare_pair(None, None, root / "ambiguous.html", "mf4", Tolerances(), "index.html", input_error="Ambiguous files")
            self.assertEqual(result["status"], "ERROR")
            self.assertIn('class="status ERROR"', (root / "ambiguous.html").read_text())


if __name__ == "__main__":
    unittest.main()