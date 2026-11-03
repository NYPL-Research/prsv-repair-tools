import argparse
import datetime
import logging
import subprocess
import re
import os
import filecmp
import concurrent.futures
from pathlib import Path
from repair_tools.utils.cli import Parser
from repair_tools.utils.logger_setup import setup_logging
import repair_tools.download_sc_mp4 as dscmp4
from repair_tools.utils.format_utils import print_standard_summary
from repair_tools.utils.file_utils import find_package_files

def parse_args() -> argparse.Namespace:
    parser = Parser()
    parser.add_package()
    parser.add_packagedirectory()
    parser.add_argument(
        "--logpath",
        "-l",
        type=Path,
        default=Path.home() / "Downloads/verify_transcode",
        help="Directory to store logs"
    )
    parser.add_argument(
        "--fix-sympath",
        "-s",
        action="store_true",
        help="Modify symlink target paths starting with /source/repo/ to start with /Volumes/repo/ instead."
    )
    parser.add_argument(
        "--margin",
        "-m",
        type=float,
        default=0.5,
        help="Allowed duration difference in seconds between PM and SC (default: 0.5s)."
    )
    parser.add_argument(
        "--check-framemd5",
        "-fmd5",
        action="store_true",
        help="Generate and check framemd5 for video files. (Note: for lossless-to-lossless transcodes)."
    )
    parser.add_argument(
        "--delete-invalid",
        "-di",
        action="store_true",
        help="Delete invalid files"
    )
    return parser.parse_args()

def get_base_name(filename: str) -> str: # made a function fo future reuse
    return re.sub(r'_(pm|sc|em)$', '', filename, flags=re.IGNORECASE)

def get_media_duration(file_path: Path) -> float:
    cmd = [
        'ffprobe', '-v', 'error', # log level == error
        '-show_entries', 'format=duration', # pulls only duration field from format
        '-of', 'default=noprint_wrappers=1:nokey=1', # output format = default w/ no outer section tags and no key names
        str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logging.error(f"Failed to get duration for {file_path.name}: {e}")
        return -1.0

def generate_framemd5(file_path: Path) -> Path:
    """Generates a framemd5 sidecar file next to the target media."""
    output_path = file_path.parent / f"{file_path.stem}.framemd5"
    cmd = [
        'ffmpeg', '-v', 'error', '-y', 
        '-i', str(file_path), 
        '-f', 'framemd5', str(output_path)
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return output_path

def verify_transcode_success(pm_path: Path, sc_path: Path, margin: float) -> bool:
    if not sc_path.exists() or sc_path.stat().st_size == 0:
        logging.error(f"Verification failed: {sc_path.name} does not exist or is empty.")
        return False
        
    pm_duration = get_media_duration(pm_path)
    sc_duration = get_media_duration(sc_path)
    
    if pm_duration < 0 or sc_duration < 0:
        logging.error(f"Verification failed: Could not retrieve duration for {pm_path.name} or {sc_path.name}.")
        return False
        
    # 0.5s margin 
    duration_diff = abs(pm_duration - sc_duration)
    if duration_diff > margin:
        logging.error(f"Verification failed: Duration mismatch for {sc_path.name}. PM: {pm_duration}s, SC: {sc_duration}s (diff: {duration_diff:.3f}s)")
        return False
        
    logging.info(f"Duration verified successfully for {sc_path.name}. Diff: {duration_diff:.3f}s")
    return True

def process_pair(pm_path: Path, sc_path: Path, args: argparse.Namespace) -> dict:
    resolved_pm = dscmp4.resolve_path(pm_path, args.fix_sympath)
    resolved_sc = dscmp4.resolve_path(sc_path, args.fix_sympath)
    
    is_verified = verify_transcode_success(resolved_pm, resolved_sc, args.margin)
    
    if is_verified and args.check_framemd5 and resolved_pm.suffix.lower() in {'.mkv', '.mov', '.dv'}:
        try:
            logging.info(f"Generating framemd5 for {pm_path.name} and {sc_path.name}...")
            pm_md5 = generate_framemd5(resolved_pm)
            sc_md5 = generate_framemd5(resolved_sc)
            
            if not filecmp.cmp(pm_md5, sc_md5, shallow=False):
                logging.error(f"Framemd5 verification failed: Hash mismatch between {pm_path.name} and {sc_path.name}")
                is_verified = False
            else:
                logging.info(f"Framemd5 verified successfully for {sc_path.name}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to generate framemd5: {e.stderr}")
            is_verified = False

    if not is_verified and args.delete_invalid:
        try:
            sc_path.unlink()
            logging.info(f"Deleted invalid file: {sc_path.name}")
        except Exception as e:
            logging.error(f"Failed to delete {sc_path.name}: {e}")
            
    return {'status': 'verified' if is_verified else 'invalid', 'file': sc_path.name}

    if not is_verified and args.delete_invalid:
        try:
            sc_path.unlink()
            logging.info(f"Deleted invalid file: {sc_path.name}")
        except Exception as e:
            logging.error(f"Failed to delete {sc_path.name}: {e}")
            
    return {'status': 'verified' if is_verified else 'invalid', 'file': sc_path.name}


def main():
    args = parse_args()
    if not args.logpath.exists():
        args.logpath.mkdir(parents=True, exist_ok=True)
    setup_logging(args.logpath / f"verify_transcode_{datetime.datetime.now():%Y%m%d}.log", noisy_loggers=['repair_tools', 'ffmpeg'])

    if not getattr(args, 'packages', None):
        logging.error("No package or directory provided. Please use --package or --directory.")
        return

    pkg_paths = []
    dir_pattern = re.compile(r"^\d{6}$")  # change or add when including digarch
    
    for p in args.packages:
        if p.is_dir() and dir_pattern.match(p.name):
            pkg_paths.append(p)
    
    if not pkg_paths:
        logging.warning("No package directories found.")
        return

    stats = {
        'total_packages': len(pkg_paths),
        'pm_found': 0,
        'sc_found': 0,
        'verified': 0, # using verified instead of 'valid' to avoid confusion with validation script
        'invalid': 0,
    }
    
    max_workers = 1 #or os.cpu_count()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for pkg_path in pkg_paths:
            logging.info(f"Processing package {pkg_path.name}")
            pm_files = find_package_files(
                pkg_path, 
                folders=['PreservationMasters'], 
                valid_exts={'.mkv', '.mov', '.dv', '.flac', '.wav', '.iso'}
            )
            
            sc_files = find_package_files(
                pkg_path, 
                folders=['ServiceCopies'], 
                ignore_exts={'.json', '.framemd5'}
            )

            stats['pm_found'] += len(pm_files)
            stats['sc_found'] += len(sc_files)
            logging.info("Found %s PM files and %s SC files", len(pm_files), len(sc_files))

            if not pm_files:
                logging.warning(f"No PM files found for {pkg_path.name}")
                continue
            
            if not sc_files:
                logging.warning(f"No SC files found for {pkg_path.name}")
                continue
            
            pm_dict = {get_base_name(file.stem): file for file in pm_files}
            sc_dict = {get_base_name(file.stem): file for file in sc_files}
            
            missing_sc = set(pm_dict.keys()) - set(sc_dict.keys())
            missing_pm = set(sc_dict.keys()) - set(pm_dict.keys())
            common_bases = set(pm_dict.keys()) & set(sc_dict.keys())

            if missing_sc:
                logging.warning(f"Missing SC files for {pkg_path.name}: {', '.join([pm_dict[b].name for b in missing_sc])}")
            
            if missing_pm:
                logging.warning(f"Missing PM files for {pkg_path.name}: {', '.join([sc_dict[b].name for b in missing_pm])}")
            
            futures = {
                executor.submit(process_pair, pm_dict[base], sc_dict[base], args): base
                for base in common_bases
            }
            
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    if result['status'] == 'verified':
                        stats['verified'] += 1
                    else:
                        stats['invalid'] += 1
                except Exception as e:
                    logging.error(f"Unexpected error processing pair: {e}")
                    stats['invalid'] += 1

    print_standard_summary(
        title="Verification Results",
        stats=stats,
        logger=logging.getLogger(__name__),
        print_to_console=True
    )

if __name__ == "__main__":
    main()

