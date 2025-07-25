import concurrent.futures
from datetime import datetime, timezone
import json
from multiprocessing import cpu_count
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import os
import glob
import yaml

# Copyleft Licenses to exclude
EXCLUDE_LICENSES = ['AGPL', 'EUPL', 'GPL', 'LGPL', 'OSL', 'ODbL', 'Ms-RL', 'GFDL']


POLYFILE_DIR: Path = Path(__file__).absolute().parent
COMPILE_SCRIPT: Path = POLYFILE_DIR / "polyfile" / "kaitai" / "compiler.py"
KAITAI_FORMAT_LIBRARY: Path = POLYFILE_DIR / "kaitai_struct_formats"
KAITAI_PARSERS_DIR: Path = POLYFILE_DIR / "polyfile" / "kaitai" / "parsers"
MANIFEST_PATH: Path = KAITAI_PARSERS_DIR / "manifest.json"


def find_files_with_excluded_licenses(directory, license_list) -> List[str]:
    """
    Recursively scans a directory for files and identifies any that contain
    a license from the excluded list.

    The check is performed as a substring match (e.g., 'GPL' in the list
    will match a license named 'GPL-3.0-or-later').
    """
    # Create the recursive search pattern
    search_path = os.path.join(directory, '**', f'*.ksy')
    file_paths = glob.glob(search_path, recursive=True)

    if not file_paths:
        return []

    flagged_files = []

    for file_path in file_paths:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                if data and isinstance(data, dict):
                    license_val = data.get('meta', {}).get('license')
                    if not license_val:
                        continue

                    # Check if any part of the license name is in our exclude list
                    for excluded_license in license_list:
                        if excluded_license in license_val:
                            flagged_files.append(file_path)
                            break # Found a match, no need to check other excluded licenses for this file

        except yaml.YAMLError as e:
            print(f"❌ Error parsing YAML in file '{file_path}': {e}")
        except Exception as e:
            print(f"❌ An unexpected error occurred with file '{file_path}': {e}")

    return flagged_files


# Make sure the ktaitai_struct_formats submodlue is cloned:
if not (KAITAI_FORMAT_LIBRARY / "README.md").exists():
    subprocess.check_call(["git", "submodule", "init"], cwd=str(POLYFILE_DIR))
    subprocess.check_call(["git", "submodule", "update"], cwd=str(POLYFILE_DIR))


def compile_ksy(path: Path) -> List[Tuple[str, str]]:
    output = subprocess.check_output(
        [sys.executable, str(COMPILE_SCRIPT), str(path), str(KAITAI_PARSERS_DIR)],
        cwd=str(KAITAI_FORMAT_LIBRARY)
    ).decode("utf-8")
    return [  # type: ignore
        tuple(line.split("\t")[:2])  # (class_name, python_path)
        for line in output.split("\n") if line.strip()
    ]


def mtime(path: Path) -> datetime:
    # has the file been modified?
    was_modified = subprocess.call(
        ["git", "diff", "--exit-code", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(KAITAI_FORMAT_LIBRARY)
    ) != 0
    if was_modified:
        # the file was modified since the last commit, so use its filesystem mtime
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    # the file has not been modified since the last commit, so use the last commit time
    last_commit_date = subprocess.check_output(["git", "log", "-1", "--format=\"%cd\"", str(path)],
                                               cwd=str(KAITAI_FORMAT_LIBRARY)).decode("utf-8").strip()
    return datetime.strptime(last_commit_date, "\"%a %b %d %H:%M:%S %Y %z\"")


def rebuild(force: bool = False):
    # Get the list of copyleft-licensed files to exclude

    excluded_files = find_files_with_excluded_licenses(
        KAITAI_FORMAT_LIBRARY,
        EXCLUDE_LICENSES
    )
    excluded_paths = {Path(f).absolute() for f in excluded_files}

    # Remove the manifest file to force a rebuild:
    if force or not MANIFEST_PATH.exists():
        if MANIFEST_PATH.exists():
            MANIFEST_PATH.unlink()
        needs_rebuild = True
    else:
        # see if any of the files are out of date and need to be recompiled
        newest_definition: Optional[datetime] = None
        for definition in KAITAI_FORMAT_LIBRARY.glob("**/*.ksy"):
            # Skip excluded files
            if definition.absolute() in excluded_paths:
                continue
            modtime = mtime(definition)
            if newest_definition is None or newest_definition < modtime:
                newest_definition = modtime
        needs_rebuild = newest_definition > mtime(MANIFEST_PATH)

    if needs_rebuild:
        # the definitions have been updated, so we need to recompile everything

        if subprocess.call(
            [sys.executable, str(COMPILE_SCRIPT), "--install"]
        ) != 0:
            sys.stderr.write("Error: You must have kaitai-struct-compiler installed\nSee https://kaitai.io/#download\n")
            sys.exit(1)

        # Count non-excluded files
        all_ksy_files = list(KAITAI_FORMAT_LIBRARY.glob("**/*.ksy"))
        ksy_files_to_compile = [f for f in all_ksy_files if f.absolute() not in excluded_paths]
        num_excluded = len(all_ksy_files) - len(ksy_files_to_compile)
        
        if num_excluded > 0:
            print(f"Excluding {num_excluded} copyleft-licensed KSY files from compilation")
        
        num_files = len(ksy_files_to_compile)

        try:
            from tqdm import tqdm
        except ModuleNotFoundError:
            def tqdm(*args, **kwargs):
                class TQDM:
                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc_val, exc_tb):
                        pass

                    def write(self, message, *_, **__):
                        sys.stderr.write(message)
                        sys.stderr.write("\n")
                        sys.stderr.flush()

                    def update(self, n: int):
                        pass
                return TQDM()

        ksy_manifest: Dict[str, Dict[str, Any]] = {}

        with tqdm(leave=False, desc="Compiling the Kaitai Struct Format Library", total=num_files) as t:
            with concurrent.futures.ThreadPoolExecutor(max_workers=cpu_count()) as executor:
                futures_to_path: Dict[concurrent.futures.Future, Path] = {
                    executor.submit(compile_ksy, file): file
                    for file in ksy_files_to_compile
                }
                for future in concurrent.futures.as_completed(futures_to_path):
                    t.update(1)
                    path = futures_to_path[future]
                    relative_path = str(path.relative_to(KAITAI_FORMAT_LIBRARY))
                    if relative_path in ksy_manifest:
                        raise ValueError(f"{relative_path} appears twice in the Kaitai format library!")
                    try:
                        (first_spec_class_name, first_spec_python_path), *dependencies = future.result()
                        ksy_manifest[relative_path] = {
                            "class_name": first_spec_class_name,
                            "python_path": str(Path(first_spec_python_path).relative_to(KAITAI_PARSERS_DIR)),
                            "dependencies": [
                                {
                                    "class_name": class_name,
                                    "python_path": str(Path(python_path).relative_to(KAITAI_PARSERS_DIR))
                                }
                                for class_name, python_path in dependencies
                            ]
                        }
                        t.write(f"Compiled {path.name}")
                    except Exception as e:
                        t.write(f"Warning: Failed to compile {path}: {e}\n")

        with open(MANIFEST_PATH, "w") as f:
            json.dump(ksy_manifest, f)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-all", "-a", action="store_true", help="rebuilds all parsers from scratch")

    args = parser.parse_args()

    rebuild(force=args.rebuild_all)
