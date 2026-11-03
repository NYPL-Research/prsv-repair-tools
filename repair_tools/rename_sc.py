import argparse
import hashlib
import logging
from pathlib import Path
import re
import sys

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory",
        "-d",
        type=Path,
        nargs="+",
        required=True,
        help="Path(s) to parent directory containing package directories or individual packages.",
    )
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Test mode.",
    )
    return parser.parse_args()


def calculate_file_hash(filepath: Path, algorithm: str = "md5") -> str:
    """Compute the cryptographic hash of a file."""
    hasher = hashlib.new(algorithm)
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def update_manifest_file(manifest_path: Path, rename_map: dict):
    text = manifest_path.read_text(encoding="utf-8")
    for old_name, new_name in rename_map.items():
        text = text.replace(f"data/ServiceCopies/{old_name}", f"data/ServiceCopies/{new_name}")
        text = text.replace(f"data\\ServiceCopies\\{old_name}", f"data\\ServiceCopies\\{new_name}")
    manifest_path.write_text(text, encoding="utf-8")


def update_tagmanifest_file(tagmanifest_path: Path, target_filename: str, new_hash: str):
    lines = tagmanifest_path.read_text(encoding="utf-8").splitlines()
    updated_lines = []

    for line in lines:
        if not line.strip():
            updated_lines.append(line)
            continue

        parts = re.split(r"(\s+)", line.strip(), maxsplit=1)
        if len(parts) >= 3 and parts[2].lstrip("*") == target_filename:
            updated_lines.append(f"{new_hash}{parts[1]}{parts[2]}")
        else:
            updated_lines.append(line)

    tagmanifest_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def fix_service_copy_names(pkg_path: Path, test_mode: bool = False) -> dict:
    """
    Renames any *_em.mp4 files in data/ServiceCopies to *_sc.mp4 and updates BagIt manifests.
    Returns a dictionary of {old_name: new_name} for the files renamed.
    """
    sc_dir = pkg_path / "data" / "ServiceCopies"
    if not sc_dir.is_dir():
        return {}

    rename_map = {}
    for src in sc_dir.glob("*_em.mp4"):
        if not src.is_file():
            continue

        new_name = re.sub(r"_em\.mp4$", "_sc.mp4", src.name)
        dst = src.with_name(new_name)

        if dst.exists():
            logging.warning(f"[{pkg_path.name}] Destination exists, skipping: {dst.name}")
            if "_em" in src.name:
                try:
                    src.unlink()
                    logging.info(f"[{pkg_path.name}] Deleting {src.name} as '_sc' version exists.")
                except Exception as e: 
                    logging.error(f"[{pkg_path.name}] Error deleting {src.name}: {e}")
            continue

        if test_mode:
            logging.info(f"[{pkg_path.name}] [TEST] Would rename {src.name} -> {dst.name}")
            rename_map[src.name] = dst.name
        else:
            try:
                src.rename(dst)
                rename_map[src.name] = dst.name
                logging.info(f"[{pkg_path.name}] Renamed: {src.name} -> {dst.name}")
            except OSError as e:
                logging.error(f"[{pkg_path.name}] Failed to rename {src.name}: {e}")

    if rename_map and not test_mode:
        manifest_updated = False
        for manifest in pkg_path.glob("manifest-*.txt"):
            update_manifest_file(manifest, rename_map)

            algo_match = re.match(r"manifest-(.+)\.txt$", manifest.name)
            algo = algo_match.group(1) if algo_match else "md5"

            tagmanifest = pkg_path / f"tagmanifest-{algo}.txt"
            if tagmanifest.is_file():
                new_manifest_hash = calculate_file_hash(manifest, algorithm=algo)
                update_tagmanifest_file(tagmanifest, manifest.name, new_manifest_hash)
            manifest_updated = True

        if manifest_updated:
            logging.info(f"[{pkg_path.name}] Updated BagIt manifests successfully.")

    return rename_map



def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    for base in args.directory:
        if (base / "data" / "ServiceCopies").is_dir():
            packages = [base]
        else:
            packages = [p for p in base.iterdir() if p.is_dir() and (p / "data" / "ServiceCopies").is_dir()]

        for pkg in packages:
            fix_service_copy_names(pkg, test_mode=args.dryrun)


if __name__ == "__main__":
    main()