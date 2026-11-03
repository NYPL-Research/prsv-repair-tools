import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path

import soundfile as sf

VALID_MEDIA_EXTS = {".mp4", ".wav", ".flac"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inspect, merge, and validate part JSON metadata across bags."
    )
    parser.add_argument(
        "--package",
        "-p",
        type=Path,
        nargs="+",
        help="Path to one or more individual package/bag directories.",
    )
    parser.add_argument(
        "--directory",
        "-d",
        type=Path,
        nargs="+",
        help="Path to directory containing package bags.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=Path.home() / "Downloads",
        help="Output destination directory (default: Downloads [**EB use, remove when pushed!!]).",
    )
    return parser.parse_args()


def format_duration(total_seconds):
    # HH:MM:SS.mmm
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    millis = int(round((total_seconds - int(total_seconds)) * 1000))
    if millis >= 1000:
        millis -= 1000
        seconds += 1
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def get_media_measurements(media_path):
    path_obj = Path(media_path)
    if not path_obj.exists():
        return None, None

    file_size_bytes = path_obj.stat().st_size
    duration_seconds = None

    ext = path_obj.suffix.lower()

    if ext in [".wav", ".flac"]:
        try:
            info = sf.info(str(path_obj))
            duration_seconds = float(info.duration)
        except Exception:
            pass

    # ffprobe for mp4 containers or fallback
    if duration_seconds is None:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path_obj),
        ]
        try:
            res = subprocess.check_output(cmd).decode().strip()
            duration_seconds = float(res)
        except Exception as e:
            print(f"Warning: Failed to get duration for {path_obj.name}: {e}")

    return file_size_bytes, duration_seconds


def merge_signal_notes(notes_list):
    combined_tokens = []
    seen = set()

    for note in notes_list:
        if not note:
            continue
        tokens = [t.strip() for t in note.split(";") if t.strip()]
        for token in tokens:
            if token not in seen:
                seen.add(token)
                combined_tokens.append(token)

    return "; ".join(combined_tokens)


def merge_part_jsons(part_json_files, joined_media_path=None, output_path=None):
    if not part_json_files:
        return None

    with open(part_json_files[0], "r", encoding="utf-8") as f:
        merged_data = copy.deepcopy(json.load(f))

    all_signal_notes = []
    if "technical" in merged_data and "signalNotes" in merged_data["technical"]:
        all_signal_notes.append(merged_data["technical"]["signalNotes"])

    for pf in part_json_files[1:]:
        with open(pf, "r", encoding="utf-8") as f:
            next_json = json.load(f)
            if "technical" in next_json and "signalNotes" in next_json["technical"]:
                all_signal_notes.append(next_json["technical"]["signalNotes"])

    if "technical" in merged_data:
        merged_data["technical"]["signalNotes"] = merge_signal_notes(all_signal_notes)

    if "source" in merged_data and "subObject" in merged_data["source"]:
        merged_data["source"]["subObject"].pop("partNumber", None)

    first_part_stem = Path(part_json_files[0]).stem
    target_base_stem = re.sub(r"p\d\d", "", first_part_stem)

    if joined_media_path and Path(joined_media_path).exists():
        media_file = Path(joined_media_path)
        media_size, media_dur_sec = get_media_measurements(media_file)

        if "asset" in merged_data:
            merged_data["asset"]["referenceFilename"] = media_file.name

        if "technical" in merged_data:
            merged_data["technical"]["filename"] = media_file.stem
            merged_data["technical"]["extension"] = media_file.suffix.lstrip(".").lower()

            if media_size is not None:
                if "fileSize" not in merged_data["technical"]:
                    merged_data["technical"]["fileSize"] = {"unit": "B"}
                merged_data["technical"]["fileSize"]["measure"] = media_size

            if media_dur_sec is not None:
                duration_ms = int(round(media_dur_sec * 1000))
                if "durationMilli" not in merged_data["technical"]:
                    merged_data["technical"]["durationMilli"] = {"unit": "ms"}
                merged_data["technical"]["durationMilli"]["measure"] = duration_ms
                merged_data["technical"]["durationHuman"] = format_duration(media_dur_sec)

    else:
        print(f"Notice: Companion media file not found for {target_base_stem}. Using summed JSON estimates.")
        
        if "asset" in merged_data and "referenceFilename" in merged_data["asset"]:
            merged_data["asset"]["referenceFilename"] = re.sub(
                r"p\d\d", "", merged_data["asset"]["referenceFilename"]
            )
        if "technical" in merged_data and "filename" in merged_data["technical"]:
            merged_data["technical"]["filename"] = target_base_stem

        total_bytes = 0
        total_ms = 0
        for pf in part_json_files:
            with open(pf, "r", encoding="utf-8") as f:
                p_data = json.load(f)
                tech = p_data.get("technical", {})
                total_bytes += tech.get("fileSize", {}).get("measure", 0)
                total_ms += tech.get("durationMilli", {}).get("measure", 0)

        if "technical" in merged_data:
            if "fileSize" in merged_data["technical"]:
                merged_data["technical"]["fileSize"]["measure"] = total_bytes
            if "durationMilli" in merged_data["technical"]:
                merged_data["technical"]["durationMilli"]["measure"] = total_ms
            merged_data["technical"]["durationHuman"] = format_duration(total_ms / 1000.0)

    if output_path:
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(merged_data, f, indent=4)
        print(f"Exported merged JSON: {out_file.name}")

    return merged_data


def find_part_json_files(package_path):
    data_path = package_path / "data"
    if not data_path.exists():
        data_path = package_path

    return [
        x for x in data_path.rglob("*")
        if x.is_file() and x.suffix.lower() == ".json" and re.search(r"p\d\d", x.name)
    ]


def group_part_json_files(json_files):
    groups = {}

    for f in json_files:
        match = re.search(r"p(\d\d)", f.name)
        if not match:
            continue

        part_num = int(match.group(1))
        dest_filename = re.sub(r"p\d\d", "", f.name)
        group_key = f.parent / dest_filename

        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append((part_num, f))

    for key in groups:
        groups[key].sort(key=lambda item: item[0])
        groups[key] = [item[1] for item in groups[key]]

    return groups


def locate_media(target_json_path):
    base_stem = target_json_path.stem
    parent_dir = target_json_path.parent

    for ext in VALID_MEDIA_EXTS:
        candidate = parent_dir / f"{base_stem}{ext}"
        if candidate.exists():
            return candidate

    return None


def process_bag(package_path, output_dir):
    pkg_dir = Path(package_path)
    part_jsons = find_part_json_files(pkg_dir)

    if not part_jsons:
        print(f"Skipping {pkg_dir.name}: No part JSON files found.")
        return

    grouped = group_part_json_files(part_jsons)

    print(f"\n==========================================")
    print(f"Package: {pkg_dir.name}")
    print(f"JSON Sequences Found: {len(grouped)}")

    for dest_key_path, files in grouped.items():
        print(f"\n--- Merging JSON Chain: {dest_key_path.name} ({len(files)} parts) ---")

        companion_media = locate_media(dest_key_path)
        if companion_media:
            print(f"Matched stitched media: {companion_media.name}")
        else:
            print(f"No stitched media file found alongside JSON yet.")

        # Export to ~/Downloads for validation
        target_output = output_dir / dest_key_path.name
        merge_part_jsons(files, joined_media_path=companion_media, output_path=target_output)


def main():
    args = parse_args()

    pkg_paths = []
    dir_pattern = re.compile(r"^\d{6}$")

    if args.package:
        for pkg in args.package:
            resolved_pkg = pkg.resolve()
            if not resolved_pkg.is_dir():
                print(f"Error: Package directory not found: {resolved_pkg}")
                continue
            if not any(resolved_pkg.iterdir()):
                print(f"Warning: Skipping empty package directory: {resolved_pkg}")
                continue
            pkg_paths.append(resolved_pkg)

    elif args.directory:
        for d in args.directory:
            resolved_dir = d.resolve()
            if not resolved_dir.is_dir():
                print(f"Error: Directory not found: {resolved_dir}")
                continue
            if not any(resolved_dir.iterdir()):
                print(f"Warning: Skipping empty directory: {resolved_dir}")
                continue

            if dir_pattern.match(resolved_dir.name):
                pkg_paths.append(resolved_dir)
            else:
                sub_dirs = [
                    sub_d.resolve()
                    for sub_d in resolved_dir.iterdir()
                    if sub_d.is_dir() and any(sub_d.iterdir())
                ]
                pkg_paths.extend(sub_dirs)
    else:
        print("Error: Either --package (-p) or --directory (-d) must be specified.")
        return

    if not pkg_paths:
        print("No valid packages found to process.")
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(pkg_paths)} package(s) to process.")
    print(f"Export Destination: {args.output_dir}")

    for pkg_path in pkg_paths:
        process_bag(package_path=pkg_path, output_dir=args.output_dir)


if __name__ == "__main__":
    main()