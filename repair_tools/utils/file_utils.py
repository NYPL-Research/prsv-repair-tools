# file_utils.py
import logging
import json
import os
import shutil
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--source",
        "-s",
        required=True,
        help="""Path of volume to be indexed.""",
        )
    parser.add_argument(
        "--cache",
        "-c",
        required=True,
        help="""Path of index cache (or directory where one will be created).""",
        )
    return parser.parse_args()

def _is_six_digit_dir(name: str) -> bool:
    """Checks if a string is a 6-digit number."""
    return len(name) == 6 and name.isdigit()

def find_package_files(pkg_path: Path, folders: list, valid_exts: set = None, ignore_exts: set = None) -> list:
    """Finds files in specified subfolders, with optional extension filtering."""
    files = []
    for folder in folders:
        target_dir = pkg_path / 'data' / folder
        if target_dir.exists() and target_dir.is_dir():
            for f in target_dir.rglob("*"):
                if not (f.is_file() or f.is_symlink()) or f.name.startswith("._"):
                    continue
                
                suffix = f.suffix.lower()
                if valid_exts and suffix not in valid_exts:
                    continue
                if ignore_exts and suffix in ignore_exts:
                    continue
                    
                files.append(f)
        else:
            logger.warning(f"Directory not found: {target_dir}")
    return files

def _find_matching_dirs(root: str, dirs: List[str]) -> Dict[str, List[str]]:
    """Finds 6-digit directories in a list and returns a dict."""
    matches = {}
    for d in dirs:
        if _is_six_digit_dir(d):
            full_path = os.path.join(root, d)
            if d not in matches:
                matches[d] = []
            matches[d].append(full_path)
    return matches

def load_or_create_target_index(target_dir: Path, cache_path: Path, num_threads: int) -> Dict[str, List[str]]:
    """Loads the target index from cache or creates a new one by scanning."""
    if cache_path.exists():
        logger.info(f"Loading target volume index from: {cache_path}")
        with open(cache_path, "r") as f:
            return json.load(f)
    
    logger.info(f"Indexing target volume (this may take a while): {target_dir}")
    index = {}
    tasks = []

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        for root, dirs, _ in os.walk(target_dir):
            if any(_is_six_digit_dir(d) for d in dirs):
                logger.info(f"Scanning: {root}")
                tasks.append(executor.submit(_find_matching_dirs, root, dirs))

        for future in as_completed(tasks):
            for name, paths in future.result().items():
                if name not in index:
                    index[name] = []
                index[name].extend(paths)

    logger.info(f"Saving target volume index to: {cache_path}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(index, f, indent=2)
    return index

# def load_or_create_source_index(source_dir: Path, cache_path: Path) -> Dict[str, Path]:
#     """Loads the source index from cache or creates a new one."""
#     cache_path = Path(str(cache_path).replace("_reingest", f"_{source_dir.name}_reingest"))
#     if cache_path.exists():
#         logger.info(f"Loading source index from {cache_path}")
#         with open(cache_path, "r") as f:
#             return {key: Path(value) for key, value in json.load(f).items()}

#     logger.info(f"Creating new source index for: {source_dir}")
#     index = {
#         dir.name: dir for dir in source_dir.rglob("*")
#     }
#         # if dir.is_dir() and _is_six_digit_dir(dir.name)
#     logger.info(f"Source index created with {len(index)} items")
    
#     cache_path.parent.mkdir(parents=True, exist_ok=True)
#     with open(cache_path, "w") as f:
#         json.dump({key: str(value) for key, value in index.items()}, f, indent=2)
#     return index

# testing alt os.walk speed
def load_or_create_source_index(source_dir: Path, cache_path: Path, dir_options: bool = None) -> Dict[str, Path]:
    """Loads the source index from cache or creates a new one"""
    cache_path = Path(str(cache_path).replace("_reingest", f"_{source_dir.name}_reingest"))

    if cache_path.exists():
        logger.info(f"Loading source index from {cache_path}")
        with open(cache_path, "r") as f:
            return {key: Path(value) for key, value in json.load(f).items()}

    logger.info(f"Creating new source index for: {source_dir}")
    
    index = {} 
    
    root_str = str(source_dir)
  
    for root, dirs, _ in os.walk(root_str):
        for d in dirs:
            if dir_options:
                if len(d) == 6 and d.isdigit():
                    index[d] = os.path.join(root, d)
                    # print(os.path.join(root, d))
            else:
                d_len = os.listdir(os.path.join(root, d))
                if len(d_len) == 0:
                    logging.warning(f"Empty dir found: {os.path.join(root, d)}")
                index[d] = os.path.join(root, d)

    logger.info(f"Source index created with {len(index)} items")
    
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(cache_path, "w") as f:
        json.dump(index, f, indent=2)
        
    return {k: Path(v) for k, v in index.items()}

def build_destination_path(dir_name: str, source_path: Path, base_dest_dir: Path) -> Path:
    """
    Constructs the correct destination path, preserving the parent
    directory if the name contains 'Audio', 'Film', or 'Video' (HDDs).
    """
    # to be built out
    if any(x in source_path.name for x in ("Audio", "Film", "Video")):
        return base_dest_dir / dir_name
    else:
        return base_dest_dir / dir_name

def copy_package(source_path: Path, dest_path: Path) -> Tuple[str, str]:
    try:
        logger.info(f"Copying {source_path} to {dest_path} ...")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        rsync_cmd = ["rsync", "-aP", f"{str(source_path)}/", f"{str(dest_path)}/"]
        subprocess.run(rsync_cmd, check=True, text=True, capture_output=True)
        return "copied", str(source_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Error copying {source_path.name}: {e.stderr}")
        return "failed", e.stderr
    except Exception as e:
        logger.error(f"An unexpected error occurred copying {source_path.name}: {e}")
        return "failed", str(e)

def move_package(source_path: Path, dest_path: Path, use_rsync: bool) -> Tuple[str, str]:
    try:
        logger.info(f"Moving {source_path} to {dest_path}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if use_rsync:
            rsync_cmd = [
                "rsync", "-aP", "--remove-source-files",
                f"{str(source_path)}/", f"{str(dest_path)}/"
            ]
            subprocess.run(rsync_cmd, check=True, text=True, capture_output=True)
            shutil.rmtree(source_path) 
        else:
            shutil.move(str(source_path), str(dest_path))
        
        return "moved", str(source_path)
    
    except FileExistsError:
        logger.warning(f"Package '{source_path.name}' already exists at {dest_path}, skipping.")
        return "skipped", "Directory already exists in target"
    except FileNotFoundError:
        logger.warning(f"Directory already moved: {source_path}, skipping.")
        return "skipped", "Directory already moved"
    except OSError as e:
        logger.error(f"Could not remove {source_path}, dir may not be empty: {e}")
        return "failed", str(e)
    except subprocess.CalledProcessError as e:
        logger.error(f"Error moving {source_path.name}: {e.stderr}")
        return "failed", e.stderr
    except Exception as e:
        logger.error(f"An unexpected error occurred moving {source_path.name}: {e}")
        return "failed", str(e)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    args = parse_args()
    source_dir = Path(args.source)
    cache_dir = Path(args.cache)
    print(f"Source directory is: {source_dir}")

    source_index = load_or_create_source_index(source_dir, cache_dir / f"{source_dir.name}_index.json", False)
    
if __name__ == "__main__":
    main()