import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def load_config(config_path: Path) -> dict[str, Path]:
    config_path = config_path.resolve()
    with config_path.open(encoding="utf-8-sig") as config_file:
        raw_config = json.load(config_file)
    if not isinstance(raw_config, dict):
        raise ValueError("Config must be a JSON object.")
    paths = {}
    for key in ("OldExportDir", "NewExportDir", "vsbDir", "exportDir"):
        value = raw_config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Config requires a non-empty string: {key}")
        paths[key] = (config_path.parent / value).resolve()
    return paths


def find_inputs(input_dir: Path) -> list[tuple[Path, Path]]:
    if not input_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {input_dir}")
    groups = []
    pending = [input_dir]
    while pending:
        folder = pending.pop()
        entries = sorted(folder.iterdir(), key=lambda path: path.name.lower())
        pending.extend(reversed([
            path for path in entries if path.is_dir() and not path.is_symlink()
        ]))
        files = [path for path in entries if path.is_file()]
        databases = [path for path in files if path.suffix.lower() == ".vsdb"]
        recordings = [path for path in files if path.suffix.lower() == ".vsb"]
        if not databases or not recordings:
            continue
        if len(databases) != 1:
            raise ValueError(
                f"Expected exactly one VSDB and at least one VSB in: {folder}"
            )
        groups.append((folder, databases[0]))
    if not groups:
        raise ValueError(f"No directories containing both VSDB and VSB files found in: {input_dir}")
    return groups


def run_tool(arguments: list[str]) -> None:
    print(f"\n> {subprocess.list2cmdline(arguments)}", flush=True)
    subprocess.run(arguments, cwd=Path(arguments[0]).parent, check=True)


def has_output(directory: Path, extensions: set[str]) -> bool:
    return any(
        path.is_file() and path.suffix.lower() in extensions
        for path in directory.rglob("*")
    )


def export_version(
    tool_dir: Path, input_dir: Path, database: Path, output_dir: Path, version: str
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".export-{version}-", dir=output_dir) as temporary:
        stage = Path(temporary)
        run_tool([
            str(tool_dir / "vsbExport-bin.exe"),
            "--csv", "-p", "-o", str(stage),
            "-d", str(database), "-b", str(input_dir),
        ])
        if not has_output(stage, {".db"}):
            raise RuntimeError(f"{version}: VSB export produced no DB files.")
        run_tool([str(tool_dir / "dbExport-bin.exe"), "-ap", str(stage), "256"])
        if not has_output(stage, {".mf4", ".mdf", ".dat"}):
            raise RuntimeError(f"{version}: DB export produced no MDF files.")
        files = sorted(path for path in stage.rglob("*") if path.is_file())
        for source in files:
            relative = source.relative_to(stage)
            target = output_dir / relative.with_name(
                f"{source.stem}-{version}{source.suffix}"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        return len(files)


def export_all(config_path: Path) -> int:
    paths = load_config(config_path)
    groups = find_inputs(paths["vsbDir"])
    versions = (("old", paths["OldExportDir"]), ("new", paths["NewExportDir"]))
    for _, tool_dir in versions:
        for name in ("vsbExport-bin.exe", "dbExport-bin.exe"):
            if not (tool_dir / name).is_file():
                raise ValueError(f"Tool does not exist: {tool_dir / name}")
    failures = 0
    for folder, database in groups:
        relative_folder = folder.relative_to(paths["vsbDir"])
        for version, tool_dir in versions:
            print(f"\n[{relative_folder}] {version}", flush=True)
            try:
                count = export_version(
                    tool_dir, folder, database, paths["exportDir"] / relative_folder, version
                )
                print(f"Completed: {count} files", flush=True)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
                failures += 1
                print(f"FAILED [{relative_folder}/{version}]: {error}", file=sys.stderr)
    print(f"\nFinished: {len(groups) * 2 - failures} succeeded, {failures} failed.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export old/new VSB recordings to DB, CSV and MDF.")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.json"),
        help="Configuration file (default: config.json next to this script).",
    )
    arguments = parser.parse_args()
    try:
        return export_all(arguments.config)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())