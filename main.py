import argparse
import sys
from pathlib import Path

from compare_files import compare_all
from export_files import export_all

PROJECT_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export old/new recordings, compare outputs and generate HTML reports.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROJECT_VERSION}")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.json"),
        help="Configuration file (default: config.json next to this script).",
    )
    arguments = parser.parse_args(argv)
    config_path = arguments.config.resolve()
    try:
        print("[1/2] Exporting DB, CSV and MDF files", flush=True)
        if export_all(config_path) != 0:
            print("ERROR: Export failed. Comparison skipped to avoid using stale outputs.", file=sys.stderr)
            return 2
        print("\n[2/2] Comparing outputs and generating HTML reports", flush=True)
        return compare_all(config_path)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())