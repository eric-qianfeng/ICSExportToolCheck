# DB/MDF Export Check Tool

Project version: `1.0.0` (tracked in `VERSION`; check with `python .\main.py --version`).

Stage 1 converts the same VSB recordings with the old and new Windows export
tools. Stage 2 compares old/new DB and MDF files independently and generates
offline HTML reports with a run summary and channel indexes.

## Requirements

- Windows and Python 3.11 or newer for both stages. Stage 1 alone also works
  with Python 3.9+ and requires no third-party packages.
- Both tool directories must contain `vsbExport-bin.exe` and `dbExport-bin.exe`.
- `vsbDir` and all descendant directories are scanned recursively. A directory
  is exported only when it directly contains one `.vsdb` file and at least one
  `.vsb` file. Incomplete directories are skipped, but their children are still
  checked. Files from different levels are never combined. Multiple VSDBs in a
  directory containing VSBs are an error. Input files are not modified.
- If no eligible directory is found, the workflow stops rather than comparing
  potentially stale outputs. Directory symlinks are not followed.

## Export Tools

Each version's tool directory, configured by `OldExportDir` or `NewExportDir`,
must contain both executables:

- `vsbExport-bin.exe` reads a `.vsdb` database definition and one or more
  directories containing `.vsb` recordings. It creates the SQLite `.db` signal
  database and can also export CSV and other message formats.
- `dbExport-bin.exe` reads the generated DB files and exports channel data to
  formats such as MDF. This project uses MDF 4.1 (`.mf4`).

The commands used by this project are:

```powershell
vsbExport-bin.exe --csv -p -o <output_dir> -d <vsdb_path> -b <vsb_dir>[,<vsb_dir>...]
dbExport-bin.exe -ap <directory_containing_db_files> 256
```

For `vsbExport-bin.exe`, `--csv` also creates CSV output, `-p` displays
progress, `-o` selects the output directory, `-d` selects the VSDB file, and
`-b` selects the VSB directory or comma-separated VSB directories. The exporter
also creates the DB needed by the next step. Avoid `-m` for this workflow:
that option exports messages only and does not create a DB.

For `dbExport-bin.exe`, `-a` selects all channels and `-p` displays progress;
`-ap` combines these options. Format `256` means MDF 4.1 (`.mf4`). Other format
codes include CSV `1`, MDF `.dat` `2`, MATLAB `.mat` `4`, and all formats `263`.
The executables are launched from their own installation directories, once for
the old tools and once for the new tools for every eligible input directory.

## Run

From the project directory, with your Python interpreter available as `python`:

```powershell
python -m pip install -r requirements.txt
python .\main.py
python .\main.py --config .\config.json
```

`main.py` runs export first, then comparison and HTML report generation. If any
export fails, comparison is skipped to avoid comparing stale outputs. Exit codes
are 0 for equal data (including metadata-only warnings), 1 for detected data
differences, and 2 for export, configuration or comparison execution errors.

The stages can still be run separately with `export_files.py` and `compare_files.py`.

If `python` opens the Microsoft Store, use the interpreter selected in VS Code.
The project virtual environment on this machine can run the complete workflow:

```powershell
& '.\.venv\Scripts\python.exe' .\main.py
```

Configuration paths are resolved relative to the configuration file, not the
terminal's current directory. The default configuration is next to the script.

## Output

Each eligible input directory keeps its relative hierarchy under `exportDir`.
For example, `vsbs/group/TestFile` maps to `db_mdf/group/TestFile`, avoiding
collisions between same-named folders in different groups. Files directly in
`vsbDir` are also eligible and map directly to `exportDir`:

```text
db_mdf/TestFile/
  recording-old.db
  recording-old.csv
  recording-old.mf4
  recording-new.db
  recording-new.csv
  recording-new.mf4
```

For each version, the script runs:

```text
vsbExport-bin.exe --csv -p -o <temporary_output> -d <vsdb_file> -b <input_subdirectory>
dbExport-bin.exe -ap <temporary_output> 256
```

The tools run from their own installation directories. Each version uses a
fresh temporary output directory so existing DBs cannot be reused accidentally.
All generated files, including CSVs, receive `-old` or `-new` before their last
extension; nested output directories are preserved. Format 256 requests MF4;
the script also accepts `.mdf` and `.dat` MDF output extensions.

Matching destination files are overwritten after both commands succeed and DB
and MDF outputs are present. Unrelated or obsolete destination files are not
deleted. A failed conversion does not publish its temporary files, and its
temporary directory is cleaned up. Publishing multiple files is not atomic.

Tool output is shown directly in the terminal. Failure of one version does not
prevent other versions or input groups from running. Exit code 0 means every
conversion succeeded; 1 means validation or conversion failed.

## Tests

```powershell
python -m unittest -v
```

Tests simulate export tool execution and use tiny SQLite and real MF4 fixtures.
They do not convert or modify the real recordings.

## Compare And Report

Install the MF4 reader and numerical dependencies into your selected environment:

```powershell
python -m pip install -r requirements.txt
python .\compare_files.py
python .\compare_files.py --config .\config.json
```

On this machine, the project virtual environment is available as
`.\.venv\Scripts\python.exe`. The comparison is independent of stage 1: it reads
existing files in `exportDir` without rerunning exporters or modifying inputs.

Configuration includes `reportDir` and optional numeric tolerances:

```json
"comparison": {
  "timeAbsoluteTolerance": 0.0,
  "timeRelativeTolerance": 0.0,
  "valueAbsoluteTolerance": 0.0,
  "valueRelativeTolerance": 0.0
}
```

All four default to zero (strict comparison), and must be finite nonnegative
numbers. Time absolute tolerance is in seconds; value absolute tolerance is in
the corresponding raw/physical units. Relative tolerances are dimensionless.
The numeric rule is `abs(old-new) <= max(abs_tol, rel_tol * max(abs(old), abs(new)))`.
Large integers remain exact in strict mode. Two NaNs compare equal; infinity
must have the same sign. Missing values are not treated as zeros.

Files are paired recursively by relative directory, base name and format,
such as `a-old.db` / `a-new.db` and `a-old.mf4` / `a-new.mf4`. The MDF reader also
accepts MDF4 content named `.mdf` or `.dat`; different extensions can pair within
the MDF family. Multiple candidates for one version are an error. Missing
versions or a missing DB/MDF counterpart are failures, not silently skipped.
CSV files and unfinished `.export-*` directories are excluded.

Channels match by network, message and channel name. A unique match does not
depend on internal IDs or group order. When both versions contain the same
number of duplicate names, each duplicate is paired by its exact internal ID:
SQLite `Channels.ID`, or the MF4 `(group index, channel index)` tuple. Duplicate
counts that differ, missing internal IDs, or non-unique internal IDs still fail
explicitly. Different internal IDs are never paired just by order. Reports show
the internal ID used for each duplicate match. MF4 uses acquisition source path
and acquisition name to establish the network/message.
No fuzzy naming, interpolation or resampling is performed. Samples are compared
in timestamp order by sequence position, so an inserted/deleted sample can also
produce downstream timestamp/value differences. Every sample of each unambiguous
matched channel is read in chunks, and every detected difference is retained.

Only converted physical sample values are compared, together with timestamps,
validity and sample counts. Raw storage values, bit lengths, storage data types,
signal types, scaling/conversion definitions, state-table definitions and bit-layout
metadata are not compared. The definitions are used only to calculate physical
values; a different representation does not cause a failure or warning.
SQLite physical values follow `double(raw) * Slope + Offset`; SQLite NULL is
missing. MF4 physical values use its stored conversion (including text states).
Type 7 state tables recursively evaluate referenced conversion blocks, including
linear default CCs and multiple levels of default conversions. For compatibility
with the old exporter, an empty text default means numeric pass-through for values
not explicitly listed in the state table: those files already store physical
values. Explicit state entries, including empty state text, are preserved. Numeric
and textual results are kept together rather than replacing text with NaN.
Equal physical values pass when timestamps, validity, sample counts and channel
matching also agree. For example, an old stored value of 476 and a new stored
value of 119 with a scale of 4 compare as equal physical values of 476.
Payload values are not compared when either sample is invalid; differing validity
still fails. MF4 master channels are represented by each signal's timestamps,
not duplicated as standalone channels. The absolute MDF recording start is also
checked, using time tolerances.

Descriptions, units, transmitters, stored statistics, arbitration IDs and other
descriptive metadata differences are warnings, not failures. Warnings count as a
successful comparison. Converted text states are compared as physical values;
their underlying state-table definitions are not compared separately.
Reserved SQLite `Collection Key` records are
included in file metadata rather than counted as signal channels; other channels
with missing message references produce an error.

Reports are written to:

```text
reports/index.html
reports/TestFile/a-db.html
reports/TestFile/a-mf4.html
```

Open `reports/index.html` directly in a browser; no server or network is needed.
The index contains this run's overall result, folder statuses and report links.
Each report includes a searchable/status-filtered paginated channel index, stable
channel anchors, full differences, warnings and read errors. All detail data is
embedded in the HTML; only the selected channel is rendered, keeping the initial
DOM small without truncating differences. JavaScript must be enabled. Channel
details expand when their index link is followed. Missing/duplicate channels show their full metadata,
locations and sample counts instead of redundantly listing every unmatched sample.
Failed-check totals count individual fields, not distinct channels or samples.

- `PASS`: equal under the configured tolerances.
- `WARNING`: data checks pass; descriptive metadata differs. This is successful.
- `FAIL`: data differences, missing files/channels/IDs or unresolved duplicate identities.
- `ERROR`: reading failed or comparison was incomplete. Never treated as equal.

Comparison exit codes: 0 for PASS/WARNING, 1 for FAIL, 2 for ERROR/configuration
errors. Reports with matching names are overwritten; the index lists only reports
from this run, although obsolete report files are not deleted.

## Sample Tool Warnings

The bundled new exporter warns that `ProtocolSignalDatabaseV2.vsdb` is missing
and Ethernet packets may not be generated properly. Follow the tool's guidance
and place the appropriate database next to its executable if required.

For the bundled sample, the old exporter logged 3,454,674 messages and the new
exporter logged 1,000,000 messages. Both returned success and produced files.
The cause of this difference has not been investigated; successful conversion
does not establish data completeness or equality.