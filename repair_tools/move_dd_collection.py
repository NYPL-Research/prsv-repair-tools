import argparse
import json
import shutil
import sys
import re
import subprocess
import glob
from pathlib import Path

# FOR MOVING DATA DISC FILES TO CENTRAL LOCATION FOR DIGARCH PROCESSING

def find_collection_id(data):
    """
    Recursively search the JSON data for a collection ID key.
    Normalizes keys to match variations like 'cmsCollectionID', 'Collection ID', 'collection_id', etc.
    """
    target_keys = {'cmscollectionid', 'collectionid'}
    
    if isinstance(data, dict):
        for key, value in data.items():
            normalized_key = str(key).lower().replace(" ", "").replace("_", "").replace("-", "")
            
            if normalized_key in target_keys:
                return str(value).strip()
            
            result = find_collection_id(value)
            if result:
                return result
                
    elif isinstance(data, list):
        for item in data:
            result = find_collection_id(item)
            if result:
                return result
                
    return None

def process_bags(source_dir, move_dir, dry_run=False):
    source_path = Path(source_dir)
    move_path = Path(move_dir)
    
    if not source_path.is_dir():
        print(f"Error: Source directory '{source_path}' does not exist or is not a directory. Skipping.")
        return
        
    for bag_path in source_path.iterdir():
        if not bag_path.is_dir():
            continue
            
        if not re.match(r'^\d{6}$', bag_path.name):
            print(f"Skipping '{bag_path.name}': Directory name is not exactly six digits.")
            continue
            
        pm_dir = bag_path / "data" / "PreservationMasters"
        
        if not pm_dir.is_dir():
            print(f"Skipping '{bag_path.name}': No 'PreservationMasters' directory found.")
            continue
            
        json_files = list(pm_dir.glob("*.json"))
        
        if not json_files:
            print(f"Skipping '{bag_path.name}': No JSON file found in 'PreservationMasters'.")
            continue
            
        json_file_path = json_files[0]
        
        try:
            with open(json_file_path, 'r', encoding='utf-8-sig') as f:
                json_data = json.load(f)
        except Exception as e:
            print(f"Error reading JSON at {json_file_path}: {e}")
            continue
            
        collection_id = find_collection_id(json_data)
        
        if not collection_id:
            print(f"Skipping '{bag_path.name}': Could not find a valid Collection ID in {json_file_path.name}.")
            continue
            
        dest_dir = move_path / collection_id
        final_bag_dest = dest_dir / bag_path.name
        
        if dry_run:
            print(f"[DRY RUN] Would create directory (if needed): '{dest_dir}'")
            print(f"[DRY RUN] Would rsync '{bag_path}' -> '{dest_dir}'")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            print(f"Moving '{bag_path.name}' -> '{dest_dir}' using rsync")
            
            try:
                rsync_cmd = [
                    "rsync",
                    "-a",
                    "--remove-source-files",
                    str(bag_path),
                    str(dest_dir)
                ]
                
                result = subprocess.run(rsync_cmd, capture_output=True, text=True)
                
                if result.returncode == 0:
                    shutil.rmtree(bag_path)
                else:
                    print(f"Error moving '{bag_path.name}' with rsync: {result.stderr}")
                    
            except Exception as e:
                print(f"Exception during rsync for '{bag_path.name}': {e}")

    if dry_run:
        print(f"\n[DRY RUN] Process complete for '{source_path}'. Would check if empty and delete it if so.")
    else:
        try:
            if not any(source_path.iterdir()):
                print(f"\nSource directory '{source_path}' is empty. Deleting...")
                source_path.rmdir()
            else:
                print(f"\nSource directory '{source_path}' is not empty. Leaving intact.")
        except Exception as e:
            print(f"\nError checking or deleting source directory '{source_path}': {e}")

def main():
    parser = argparse.ArgumentParser(description="Move bag directories based on Collection ID found in PreservationMasters JSON via rsync.")
    parser.add_argument("--source", nargs='+', required=True, help="Path(s) to the source directory containing the bags. Accepts wildcards.")
    parser.add_argument("--movedir", required=True, help="Path to the base destination directory.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate the move without actually moving files or deleting directories.")
    
    args = parser.parse_args()
    
    source_dirs = []
    for src in args.source:
        expanded = glob.glob(src)
        if expanded:
            source_dirs.extend(expanded)
        else:
            source_dirs.append(src)
            
    source_dirs = list(dict.fromkeys(source_dirs))
    
    for src_dir in source_dirs:
        print(f"\n{'='*50}")
        print(f"Processing Source Directory: {src_dir}")
        print(f"{'='*50}")
        process_bags(src_dir, args.movedir, args.dry_run)

if __name__ == "__main__":
    main()