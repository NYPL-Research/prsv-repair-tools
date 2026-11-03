import argparse
import logging
import shutil
import json
import re
import os
import concurrent.futures
# import warnings
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

from repair_tools.create_bag_structure import create_dir_structure
from repair_tools.utils.format_utils import print_standard_summary
from repair_tools.utils.cli import Parser
from repair_tools.get_md5 import calculate_md5

# warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
# warnings.simplefilter(action='ignore', category=FutureWarning)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def parse_args() -> argparse.Namespace:
    parser = Parser()
    parser.add_package()
    parser.add_argument(
        "--logpath",
        type=Path,
        default=Path("logs"),
        help="Directory for log files."
    )
    parser.add_argument(
        "--debug",
        action="store_true"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent threading workers."
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Moves files to the new package structure. If omitted, files will be copied safely."
    )
    return parser.parse_args()

def find_column_by_keywords(df_columns, keywords: list[str]):
    for col in df_columns:
        # normalize potential headers
        normalized_col = re.sub(r'[\W_]+', '', str(col).lower())
        for kw in keywords:
            normalized_kw = re.sub(r'[\W_]+', '', kw.lower())
            if normalized_col == normalized_kw:
                return col
                
    # ignoring short strings to avoid false positives
    for col in df_columns:
        normalized_col = re.sub(r'[\W_]+', '', str(col).lower())
        for kw in keywords:
            normalized_kw = re.sub(r'[\W_]+', '', kw.lower())
            if len(normalized_kw) > 2 and normalized_kw in normalized_col:
                return col
    return None

def get_tab_priority(sheet_name: str) -> int:
    """
    Returns a priority weight for sorting. Higher number = Higher priority.
    """
    normalized_name = re.sub(r'[\W_]+', '', sheet_name.lower())
    
    if "preservationmaster" in normalized_name or "archiveoriginal" in normalized_name:
        return 4
    elif "editmaster" in normalized_name:
        return 3
    elif "servicecopy" in normalized_name:
        return 2
    return 1  

def get_val(row_data: pd.Series, col, default="unknown"):
    """Safely extracts a value from a pandas series, avoiding NaNs."""
    if col and pd.notna(row_data.get(col)):
        val = str(row_data.get(col)).strip()
        if val and val.lower() != 'nan':
            return val
    return default

def parse_duration_to_ms(duration_str) -> int:
    """Calculates milliseconds from standard HH:MM:SS.mmm timecodes."""
    try:
        parts = str(duration_str).strip().split(':')
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
            return int((h * 3600 + m * 60 + s) * 1000)
    except Exception:
        pass
    return 0

def extract_fallback_id(filename: str) -> str:
    """Strips common suffixes and division prefixes to isolate a clean ID from a filename."""
    # strip suffixes like _ao, _em, _v01
    stripped = re.sub(r'(_(ao|em|sc|pm|v\d+))+(\.[a-zA-Z0-9]+)?$', '', filename)
    # strip division prefixes ie myd_, mym_, dan_ 
    stripped = re.sub(r'^[a-z]{3}_', '', stripped, flags=re.IGNORECASE)
    return stripped

def generate_sidecar_json(row_data: pd.Series, output_path: Path, filename: str):
    if output_path.exists():
        return

    cols = row_data.index

    div_col = find_column_by_keywords(cols, ["division code", "division"])
    id_col = find_column_by_keywords(cols, ["class mark", "classmark", "id", "primary id", "identifier", "object id", "cms id"])
    barcode_col = find_column_by_keywords(cols, ["barcode", "bar code"])
    cms_col = find_column_by_keywords(cols, ["cms collection", "collection id", "cms id"])
    title_col = find_column_by_keywords(cols, ["title", "object name", "description"])
    role_col = find_column_by_keywords(cols, ["file role", "role", "asset role"])
    projcode_col = find_column_by_keywords (cols, ["project code", "project", "projectcode"])
    codec_col = find_column_by_keywords(cols, ["codec", "audio codec", "audio data encoding", "encoding"])
    file_format_col = find_column_by_keywords(cols, ["file format"]) 
    cue_col = find_column_by_keywords(cols, ["cue", "cue file", "cue filename"])
    date_col = find_column_by_keywords(cols, ["date created", "creation date", "date"])
    dur_human_col = find_column_by_keywords(cols, ["duration human", "human duration", "runtime", "duration"])
    dur_ms_col = find_column_by_keywords(cols, ["duration milli", "duration ms", "milliseconds"])
    size_col = find_column_by_keywords(cols, ["file size", "bytes", "size"])
    field_col = find_column_by_keywords(cols, ["sound field", "audio sound field", "stereo"])
    obj_format_col = find_column_by_keywords(cols, ["physical format", "object format", "format"])
    type_col = find_column_by_keywords(cols, ["object type", "type"])
    vol_col = find_column_by_keywords(cols, ["volume number", "volume", "vol"])
    diam_col = find_column_by_keywords(cols, ["diameter", "gauge"])
    dye_col = find_column_by_keywords(cols, ["dye", "dye layer"])
    reflect_col = find_column_by_keywords(cols, ["reflective", "reflective layer"])
    mfg_col = find_column_by_keywords(cols, ["stock manufacturer", "manufacturer", "mfg"])
    face_col = find_column_by_keywords(cols, ["face number", "face", "side"])
    
    # Software & Capture Hardware lookups
    soft_mfg_col = find_column_by_keywords(cols, ["capture software manufacturer", "software brand"])
    soft_plat_col = find_column_by_keywords(cols, ["capture software platform", "os", "platform"])
    soft_name_col = find_column_by_keywords(cols, ["capture software name", "product name", "software"])
    soft_ver_col = find_column_by_keywords(cols, ["capture software version", "software version", "version"])
    dev_mfg_col = find_column_by_keywords(cols, ["playback device manufacturer", "device brand"])
    dev_model_col = find_column_by_keywords(cols, ["playback device model", "device model", "model"])
    dev_serial_col = find_column_by_keywords(cols, ["playback device serial", "device serial", "serial"])
    
    # Operator & Vendor lookups
    op_first_col = find_column_by_keywords(cols, ["operator first name", "operator name first", "operator first", "first name"])
    op_last_col = find_column_by_keywords(cols, ["operator last name", "operator name last", "operator last", "last name"])
    org_city_col = find_column_by_keywords(cols, ["org city", "organization city", "city"])
    org_zip_col = find_column_by_keywords(cols, ["org postal", "org zip", "postal code", "zip"])
    org_state_col = find_column_by_keywords(cols, ["org state", "organization state", "state"])
    org_street_col = find_column_by_keywords(cols, ["org street", "organization street", "address"])
    org_name_col = find_column_by_keywords(cols, ["org name", "organization name", "vendor"])
    op_notes_col = find_column_by_keywords(cols, ["notes", "note", "operator note", "operator notes"])

    extracted_id = get_val(row_data, id_col)
    if extracted_id == "unknown":
        extracted_id = extract_fallback_id(filename)
        
    inferred_role = "pm"
    fn_lower = filename.lower()
    parent_lower = str(output_path.parent).lower()
    
    # assign specific JSON schema file roles based on BDAMI and AMI conventions
    if "_ao." in fn_lower or "archiveoriginals" in parent_lower:
        inferred_role = "ao"
    elif "_pm." in fn_lower or "preservationmasters" in parent_lower:
        inferred_role = "pm"
    elif "_em." in fn_lower or "editmasters" in parent_lower:
        inferred_role = "em"
    elif "_sc." in fn_lower or "servicecopies" in parent_lower:
        inferred_role = "sc"

    # division mapping
    div_val = get_val(row_data, div_col, "mym")
    if div_val.lower() == "myd":
        vern_val = "DAN" 
    elif div_val.lower() == "mym":
        vern_val = "MUS"
    # cont. mapping here
    else:
        vern_val = ""

    dur_human_val = get_val(row_data, dur_human_col)
    dur_ms_val = parse_duration_to_ms(dur_human_val) if dur_human_val != "unknown" else 0
    if dur_ms_col and get_val(row_data, dur_ms_col) != "unknown":
        try:
            dur_ms_val = int(float(get_val(row_data, dur_ms_col)))
        except ValueError:
            pass

    json_content = {
        "asset": {
            "fileRole": get_val(row_data, role_col, inferred_role), 
            "referenceFilename": filename,
            "schemaVersion": ""
        },
        "bibliographic": {
            "barcode": get_val(row_data, barcode_col),
            "cmsCollectionID": get_val(row_data, cms_col),
            "divisionCode": div_val,
            "primaryID": extracted_id, 
            "projectCode": get_val(row_data, projcode_col),
            "title": get_val(row_data, title_col),
            "vernacularDivisionCode": vern_val
        },
        "technical": {
            "audioCodec": get_val(row_data, codec_col, "unknown"),
            "cueFilename": get_val(row_data, cue_col),
            "dateCreated": get_val(row_data, date_col),
            "durationHuman": dur_human_val,
            "durationMilli": {
                "measure": dur_ms_val,
                "unit": "ms"
            },
            "extension": Path(filename).suffix.lstrip('.'),
            "fileFormat": get_val(row_data, file_format_col, "unknown"),
            "filename": Path(filename).stem,
            "fileSize": {
                "measure": int(float(get_val(row_data, size_col, 0))),
                "unit": "B"
            }
        },
        "source": {
            "audioRecording": {
                "audioSoundField": get_val(row_data, field_col, "unknown")
            },
            "object": {
                "format": get_val(row_data, obj_format_col),
                "type": get_val(row_data, type_col),
                "volumeNumber": int(float(get_val(row_data, vol_col, 1)))
            },
            "physicalDescription": {
                "diameter": {
                    "measure": float(get_val(row_data, diam_col, 0.0)),
                    "unit": "in"
                },
                "dyeLayer": get_val(row_data, dye_col),
                "reflectiveLayer": get_val(row_data, reflect_col),
                "stockManufacturer": get_val(row_data, mfg_col)
            },
            "subObject": {
                "faceNumber": int(float(get_val(row_data, face_col, 1)))
            },
            "notes": {
                "notes": get_val(row_data, op_notes_col, "")
            }
        },
        "digitizationProcess": {
            "captureSoftware": {
                "manufacturer": get_val(row_data, soft_mfg_col),
                "platform": get_val(row_data, soft_plat_col),
                "productName": get_val(row_data, soft_name_col),
                "version": get_val(row_data, soft_ver_col)
            },
            "playbackDevice": {
                "manufacturer": get_val(row_data, dev_mfg_col),
                "model": get_val(row_data, dev_model_col),
                "serialNumber": get_val(row_data, dev_serial_col)
            }
        },
        "digitizer": {
            "operator": {
                "firstName": get_val(row_data, op_first_col),
                "lastName": get_val(row_data, op_last_col)
            },
            "organization": {
                "address": {
                    "city": get_val(row_data, org_city_col),
                    "postalCode": get_val(row_data, org_zip_col),
                    "state": get_val(row_data, org_state_col),
                    "street1": get_val(row_data, org_street_col)
                },
                "name": get_val(row_data, org_name_col)
            }
        }
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(json_content, f, indent=2)
    logger.info(f"Created JSON: {output_path.name}")

def parse_original_manifest(base_path: Path) -> dict:
    checksums = {}
    manifest_path = base_path / "manifest-md5.txt"
    if manifest_path.exists():
        with open(manifest_path, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    file_name = Path(parts[1]).name
                    checksums[file_name] = parts[0]
    return checksums

def build_file_index(base_path: Path) -> dict:
    logger.info("Indexing source directories")
    index = {}
    for p in base_path.rglob("*"):
        if p.is_file() and p.suffix not in ['.xlsx', '.json'] and not p.name.startswith("._") and not p.name.startswith("~$"):
            index.setdefault(p.name, []).append(p)
    return index

def query_indexed_files(file_index: dict, search_token: str) -> list[Path]:
    results = []
    pattern = re.compile(rf'(?<!\w){re.escape(search_token)}(?!\w)', re.IGNORECASE)
    
    for fname, paths in file_index.items():
        if search_token.lower() in fname.lower() or pattern.search(fname):
            results.extend(paths)
    return results

def process_single_package(class_id: str, id_rows: pd.DataFrame, base_path: Path, matching_files: list[Path], is_debug: bool, is_move: bool, original_checksums: dict) -> dict:
    stats = {'total': len(matching_files), 'success': 0, 'fail': 0, 'errors': []}
    
    # merge shared metadata across all rows
    if not id_rows.empty:
        id_rows = id_rows.bfill().ffill().infer_objects(copy=False)
    
    new_bag_dir = base_path / "expanded_bags" / class_id
    
    if not is_debug and not new_bag_dir.exists():
        # print(f"Will create bag structure in '{new_bag_dir.parent}' named '{new_bag_dir.name}'.")
        with open(os.devnull, 'w') as fnull:
                with redirect_stdout(fnull), redirect_stderr(fnull):
                    create_dir_structure(new_bag_dir.parent, new_bag_dir.name)

    manifest_entries = []

    for file_path in matching_files:
        file_success = True
        
        parent_folder_name = file_path.parent.name
        target_dir = new_bag_dir / "data" / parent_folder_name
        target_file_path = target_dir / file_path.name
        
        if is_debug:
            logger.info(f"[DEBUG] {'Move' if is_move else 'Copy'} staged: {file_path.name} -> {target_dir}")
            debug_dir = base_path / "debug_jsons"
            debug_dir.mkdir(exist_ok=True)
            json_filename = debug_dir / f"DEBUG_{class_id}_{target_file_path.stem}.json"
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not target_file_path.exists():
                try:
                    if is_move:
                        shutil.move(str(file_path), str(target_file_path))
                    else:
                        shutil.copy2(str(file_path), str(target_file_path))
                except Exception as e:
                    stats['errors'].append(f"Failed to {'move' if is_move else 'copy'} {file_path.name}: {e}")
                    file_success = False
            
            json_filename = target_file_path.with_suffix('.json')

        if file_success and not json_filename.exists() and not id_rows.empty:
            # specify volume row
            row_to_use = id_rows.iloc[0]
            if len(id_rows) > 1:
                for _, r in id_rows.iterrows():
                    row_vals = r.astype(str).str.lower().tolist()
                    if any(file_path.name.lower() in val for val in row_vals):
                        row_to_use = r
                        break

            try:
                generate_sidecar_json(row_to_use, json_filename, target_file_path.name)
            except Exception as e:
                stats['errors'].append(f"Failed to generate JSON {json_filename.name}: {e}")
                file_success = False

        if file_success and not is_debug:
            rel_path = target_file_path.relative_to(new_bag_dir)
            
            if file_path.name in original_checksums:
                checksum = original_checksums[file_path.name]
            else:
                checksum = calculate_md5(target_file_path)
            
            if checksum:
                manifest_entries.append(f"{checksum}  {rel_path}")
            
            json_rel_path = json_filename.relative_to(new_bag_dir)
            json_checksum = calculate_md5(json_filename)
            if json_checksum:
                 manifest_entries.append(f"{json_checksum}  {json_rel_path}")
            
            stats['success'] += 1
        else:
            stats['fail'] += 1

    if not is_debug and stats['fail'] == 0:
        # get rid of any empty subdirectories left behind by create_dir_structure() skeleton
        data_dir = new_bag_dir / "data"
        if data_dir.exists():
            for sub_dir in data_dir.iterdir():
                if sub_dir.is_dir() and not any(sub_dir.iterdir()):
                    try:
                        sub_dir.rmdir()
                    except OSError:
                        pass

        if manifest_entries:
            try:
                manifest_path = new_bag_dir / "manifest-md5.txt"
                with open(manifest_path, 'w', encoding='utf-8') as manifest_file:
                    for entry in manifest_entries:
                        manifest_file.write(f"{entry}\n")
                logger.info(f"Manifest created for package: {new_bag_dir.name}")
            except Exception as e:
                stats['errors'].append(f"Manifest generation failed for {new_bag_dir.name}: {e}")

    return stats

def process_excel_file(excel_file: Path, base_path: Path, is_debug: bool, file_index: dict, original_checksums: dict, max_workers: int, is_move: bool) -> dict:
    logger.info(f"Opening Excel Workbook: {excel_file.name}")
    sheet_stats = {'total': 0, 'success': 0, 'fail': 0, 'errors': []}
    
    try:
        xls = pd.read_excel(excel_file, sheet_name=None, header=None)
    except Exception as e:
        sheet_stats['errors'].append(f"Failed to read {excel_file.name}: {e}")
        return sheet_stats

    consolidated_tasks = {}

    for sheet_name, raw_df in xls.items():
        df = raw_df.copy()
        id_column = None
        header_row_index = None

        current_sheet_priority = get_tab_priority(sheet_name)

        best_row_idx = 0
        max_score = 0
        header_keywords = [
            "classmark", "classmarkid", "primaryid", "referencefilename", 
            "filenameauto", "filename", "extension", "datecreated", "fileformat",
            "format", "duration", "operator"
        ]
        
        for idx, row in df.iterrows():
            if idx > 7: break # only check top 7 rows (padded from avg header row count)
            
            row_values = row.dropna().astype(str).str.strip().tolist()
            normalized_row = [re.sub(r'[\W_]+', '', val.lower()) for val in row_values]
            
            score = sum(1 for kw in header_keywords if any(kw in val for val in normalized_row))
            if score > max_score:
                max_score = score
                best_row_idx = idx

        if max_score > 0:
            header_row_index = best_row_idx
            df.columns = df.iloc[header_row_index].str.strip()
            df = df.iloc[header_row_index + 1:].reset_index(drop=True)
        else:
            logger.info(f"Skipping tab '{sheet_name}' - No valid headers found.")
            continue

        id_column = find_column_by_keywords(df.columns, ["class mark", "classmark", "identifier", "primary id", "object id", "cms id", "item id", "id"]) or \
                    find_column_by_keywords(df.columns, ["reference filename", "filename auto", "filename"])

        if not id_column:
            logger.info(f"Skipping tab '{sheet_name}' - No valid identifier column found.")
            continue
            
        logger.info(f"Searching for '{id_column}' on sheet '{sheet_name}'.")
        # non empty unique rows:
        unique_ids = df[id_column].dropna().astype(str).unique()
        
        # filename as ID?
        is_filename_col = False
        if id_column:
            is_filename_col = bool(find_column_by_keywords([id_column], ["reference filename", "filename auto", "filename"]))

        for raw_id in unique_ids:
            raw_id = str(raw_id).strip()
            if not raw_id or raw_id.lower() == 'nan':
                continue
            
            # extract the base ID
            if is_filename_col:
                class_id = extract_fallback_id(raw_id)
            else:
                class_id = raw_id
                
            search_token = class_id.replace('_', '')
                
            id_rows = df[df[id_column].astype(str) == raw_id]
            matching_files = query_indexed_files(file_index, search_token)
            
            if matching_files:
                # merge/append to retain data from all tabs
                if class_id in consolidated_tasks:
                    consolidated_tasks[class_id]['rows'] = pd.concat([consolidated_tasks[class_id]['rows'], id_rows], ignore_index=True)
                    # dedupe
                    existing_files = set(consolidated_tasks[class_id]['files'])
                    existing_files.update(matching_files)
                    consolidated_tasks[class_id]['files'] = list(existing_files)
                else:
                    consolidated_tasks[class_id] = {
                        'priority': current_sheet_priority,
                        'rows': id_rows,
                        'files': matching_files,
                        'tab': sheet_name
                    }

    if consolidated_tasks:
        # logger.info(f"Spawning concurrent thread workers Pool (Max Threads: {max_workers}) for {len(consolidated_tasks)} prioritized unique packages.")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for class_id, task_data in consolidated_tasks.items():
                futures.append(executor.submit(
                    process_single_package,
                    class_id,
                    task_data['rows'],
                    base_path,
                    task_data['files'],
                    is_debug,
                    is_move,
                    original_checksums
                ))
            
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                sheet_stats['total'] += res['total']
                sheet_stats['success'] += res['success']
                sheet_stats['fail'] += res['fail']
                sheet_stats['errors'].extend(res['errors'])

    return sheet_stats

def main():
    args = parse_args()
    overall_stats = {'total': 0, 'success': 0, 'fail': 0, 'errors': []}
    
    for package in getattr(args, 'packages', []):
        base_path = Path(package)
        if not base_path.exists():
            logger.warning(f"Path does not exist: {base_path}")
            overall_stats['errors'].append(f"Target path not found: {base_path}")
            continue
            
        file_index = build_file_index(base_path)
        original_checksums = parse_original_manifest(base_path)

        for excel_file in base_path.rglob("*.xlsx"):
            if not excel_file.name.startswith("._") and not excel_file.name.startswith("~$"):
                res = process_excel_file(excel_file, base_path, args.debug, file_index, original_checksums, args.workers, args.move)
                overall_stats['total'] += res['total']
                overall_stats['success'] += res['success']
                overall_stats['fail'] += res['fail']
                overall_stats['errors'].extend(res['errors'])

    title = f"Execution Summary{'  [DEBUG MODE]' if args.debug else ''}"
    summary = {
        "Total Files Targeted": overall_stats['total'],
        "Successful": f"{overall_stats['success']}/{overall_stats['total']}",
        "Failed": f"{overall_stats['fail']}/{overall_stats['total']}",
    }
    if overall_stats['errors']:
        summary["Errors"] = overall_stats['errors']
    else:
        summary["Status"] = "Completed w/o errors."
    print_standard_summary(title, summary, logger=logger)

if __name__ == "__main__":
    main()