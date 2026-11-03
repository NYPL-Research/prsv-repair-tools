import argparse
import shutil 
import sys
from pathlib import Path

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--collection", "-c",
        type=Path,
        required=True,
        help="Path to collection, ie. M###_XX##",
    )

    return parser.parse_args()

def get_er(collection: Path) -> list: 
    """Retrieving all subdirs following naming convention of 'ER #+'"""
    er_dirs = []
    
    all_found = list(collection.rglob("ER [0-9]*"))
    
    for er_dir in all_found:
        if not er_dir.is_dir():
            continue
            
        if er_dir.parent == collection:
            continue
            
        duplicate_name = any(existing.name == er_dir.name for existing in er_dirs)
        
        if duplicate_name:
            in_list = input(f"\n[Warning] {er_dir.name} (parent: {er_dir.parent.name}) shares a name with a folder already in queue. Add anyway? (y/n): ").strip().lower()
            if in_list in {'y', 'yes'}:
                er_dirs.append(er_dir)
            elif in_list in {'n', 'no'}:
                print(f"Skipping queue addition for: {er_dir}")
                continue
            else:
                print(f"Input not accepted, skipping {er_dir}")
                continue
        else:
            er_dirs.append(er_dir)
            
    return er_dirs

def move_dirs(er_dirs: list, collection: Path):
    for er_dir in er_dirs:
        original_parent = er_dir.parent
        target_dir = collection / er_dir.name

        if target_dir.exists():
            print(f"[Error] Skipping or renaming needed: {target_dir} already exists at root.")
            continue

        try:
            shutil.move(er_dir, target_dir)
            print(f"Moved: {er_dir} -> {target_dir}")

            required_folders = ['objects', 'metadata']
            for folder_name in required_folders:
                subfolder_path = target_dir / folder_name
                
                if not subfolder_path.exists():
                    subfolder_path.mkdir(parents=True, exist_ok=True)
                    print(f"  Created missing required folder [empty]: {subfolder_path.relative_to(collection)}")

            if original_parent != collection and original_parent.exists():
                remaining_items = list(original_parent.iterdir())
                
                is_empty_or_ds_store = all(item.name == '.DS_Store' for item in remaining_items)
                
                if is_empty_or_ds_store:
                    for item in remaining_items:
                        item.unlink()
                        
                    original_parent.rmdir()
                    print(f"  Removed empty parent folder: {original_parent.name}")

        except FileNotFoundError:
            print(f"[Error] Source folder does not exist or was already moved: {er_dir}")


def main():
    args = parse_args()
    
    if not args.collection.exists() or not args.collection.is_dir():
        print(f"Error: Collection path '{args.collection}' does not exist or is not a directory.")
        sys.exit(1)
        
    er_dirs = get_er(args.collection)
    
    if not er_dirs:
        print("No nested 'ER #' directories found to move.")
        return

    move_dirs(er_dirs, args.collection)
    

if __name__ == "__main__":
    main()