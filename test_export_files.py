import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import main as workflow
from export_files import export_all, export_version, find_inputs, load_config


class ExportTests(unittest.TestCase):
    def test_config_paths_are_relative_to_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({
                "OldExportDir": "old", "NewExportDir": "new",
                "vsbDir": "input", "exportDir": "output",
            }), encoding="utf-8-sig")
            self.assertEqual(load_config(config)["vsbDir"], root.resolve() / "input")

    def test_input_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "recording with spaces"
            folder.mkdir()
            database = folder / "signals.VSDB"
            database.touch()
            (folder / "first.vsb").touch()
            (folder / "second.VSB").touch()
            self.assertEqual(find_inputs(root), [(folder, database)])
            (folder / "extra.vsdb").touch()
            with self.assertRaises(ValueError):
                find_inputs(root)

    def test_recursive_scan_checks_children_of_complete_and_incomplete_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            eligible = [
                root / "database_only" / "nested",
                root / "ready",
                root / "ready" / "nested",
                root / "recording_only" / "empty" / "nested",
            ]
            for folder in eligible:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "signals.VSDB").touch()
                (folder / "recording.VSB").touch()
            (root / "database_only" / "parent.vsdb").touch()
            (root / "database_only" / "extra.vsdb").touch()
            (root / "recording_only" / "parent.vsb").touch()
            (root / "empty").mkdir()
            self.assertEqual(find_inputs(root), [
                (folder, folder / "signals.VSDB") for folder in eligible
            ])

    def test_scan_includes_root_and_does_not_inherit_parent_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "signals.vsdb"
            database.touch()
            (root / "recording.vsb").touch()
            child = root / "child"
            child.mkdir()
            (child / "recording.vsb").touch()
            self.assertEqual(find_inputs(root), [(root, database)])

    def test_no_matching_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "signals.vsdb").touch()
            child = root / "child"
            child.mkdir()
            (child / "recording.vsb").touch()
            with self.assertRaisesRegex(ValueError, "No directories containing both"):
                find_inputs(root)

    def test_export_preserves_nested_paths_for_same_named_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            folders = [input_dir / "first" / "shared", input_dir / "second" / "shared"]
            for folder in folders:
                folder.mkdir(parents=True)
                (folder / "signals.vsdb").touch()
                (folder / "recording.vsb").touch()
            paths = {
                "vsbDir": input_dir, "exportDir": root / "output",
                "OldExportDir": root / "old", "NewExportDir": root / "new",
            }
            versions = [("old", paths["OldExportDir"]), ("new", paths["NewExportDir"])]
            for _, tool_dir in versions:
                tool_dir.mkdir()
                (tool_dir / "vsbExport-bin.exe").touch()
                (tool_dir / "dbExport-bin.exe").touch()
            with patch("export_files.load_config", return_value=paths), patch("export_files.export_version", return_value=3) as export:
                self.assertEqual(export_all(root / "config.json"), 0)
                self.assertEqual(export.call_args_list, [
                    call(tool_dir, folder, folder / "signals.vsdb", paths["exportDir"] / folder.relative_to(input_dir), version)
                    for folder in folders for version, tool_dir in versions
                ])

    def test_versions_are_isolated_and_outputs_are_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            (output / "signals-old.db").write_text("stale", encoding="utf-8")
            input_dir = root / "input with spaces"
            database = input_dir / "signals.vsdb"
            stages = []

            def fake_run(arguments, **kwargs):
                self.assertTrue(kwargs["check"])
                self.assertEqual(kwargs["cwd"], root)
                if Path(arguments[0]).name == "vsbExport-bin.exe":
                    self.assertEqual(arguments[1:3], ["--csv", "-p"])
                    self.assertEqual(arguments[-4:], ["-d", str(database), "-b", str(input_dir)])
                    stage = Path(arguments[4])
                    self.assertEqual(list(stage.iterdir()), [])
                    stages.append(stage)
                    (stage / "signals.db").write_text("fresh DB", encoding="utf-8")
                    (stage / "signals.csv").write_text("signal,value", encoding="utf-8")
                else:
                    self.assertEqual(arguments[1], "-ap")
                    self.assertEqual(arguments[3], "256")
                    stage = Path(arguments[2])
                    self.assertTrue((stage / "signals.db").is_file())
                    (stage / "signals.mf4").write_bytes(b"MDF")
                return subprocess.CompletedProcess(arguments, 0)

            with patch("export_files.subprocess.run", side_effect=fake_run):
                for version in ("old", "new"):
                    self.assertEqual(export_version(root, input_dir, database, output, version), 3)
            self.assertNotEqual(stages[0], stages[1])
            self.assertEqual(len(list(output.iterdir())), 6)
            for version in ("old", "new"):
                self.assertEqual((output / f"signals-{version}.db").read_text(), "fresh DB")
                self.assertTrue((output / f"signals-{version}.csv").is_file())
                self.assertTrue((output / f"signals-{version}.mf4").is_file())

    def test_failed_conversion_keeps_existing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "signals-old.db"
            original.write_text("previous result", encoding="utf-8")
            with patch("export_files.subprocess.run") as run:
                run.side_effect = subprocess.CalledProcessError(1, "exporter")
                with self.assertRaises(subprocess.CalledProcessError):
                    export_version(root, root, root / "signals.vsdb", root, "old")
            self.assertEqual(original.read_text(), "previous result")
            self.assertEqual(list(root.iterdir()), [original])

    def test_success_exit_without_db_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("export_files.subprocess.run"):
                with self.assertRaisesRegex(RuntimeError, "no DB"):
                    export_version(root, root, root / "signals.vsdb", root, "new")


class WorkflowTests(unittest.TestCase):
    def test_runs_export_before_comparison_and_preserves_result(self):
        config_path = Path(workflow.__file__).with_name("config.json").resolve()
        for result in (0, 1, 2):
            with self.subTest(result=result), patch("main.export_all", return_value=0) as export, patch("main.compare_all", return_value=result) as compare:
                stages = Mock()
                stages.attach_mock(export, "export")
                stages.attach_mock(compare, "compare")
                self.assertEqual(workflow.main([]), result)
                self.assertEqual(stages.mock_calls, [call.export(config_path), call.compare(config_path)])

    def test_custom_config_is_used_by_both_stages(self):
        config_path = Path("custom config.json").resolve()
        with patch("main.export_all", return_value=0) as export, patch("main.compare_all", return_value=0) as compare:
            self.assertEqual(workflow.main(["--config", str(config_path)]), 0)
            export.assert_called_once_with(config_path)
            compare.assert_called_once_with(config_path)

    def test_export_failure_skips_comparison(self):
        with patch("main.export_all", return_value=1), patch("main.compare_all") as compare:
            self.assertEqual(workflow.main([]), 2)
            compare.assert_not_called()

    def test_export_exception_skips_comparison(self):
        with patch("main.export_all", side_effect=ValueError("Invalid config")), patch("main.compare_all") as compare:
            self.assertEqual(workflow.main([]), 2)
            compare.assert_not_called()

    def test_comparison_exception_returns_error(self):
        with patch("main.export_all", return_value=0), patch("main.compare_all", side_effect=OSError("Cannot write report")):
            self.assertEqual(workflow.main([]), 2)


if __name__ == "__main__":
    unittest.main()