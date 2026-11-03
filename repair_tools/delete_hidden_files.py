import argparse
from repair_tools.utils.cli import extant_dir, list_of_paths, ExtendUnique as StoreListAction
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)

class ExtendUnique(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None)

        if items is None:
            items = set(values)
        elif isinstance(items, set):
            items = items.union(values)

        setattr(namespace, self.dest, items)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
            "--directory",
            type=Path,
            dest="directory",
            action=ExtendUnique,
            nargs='+',
            help="path to a directory of packages",
        )
    return parser.parse_args()
    



def get_hidden_files(directory: Path):
    for hidden_f in directory.rglob('.*'):
        # if hidden_f.name == ".DS_Store":
        if hidden_f.is_dir():
            hidden_f.rmdir()
            logging.info(f"Removing: {str(hidden_f.name)}")
        else:
            hidden_f.unlink()
            logging.info(f"Removing: {str(hidden_f.name)}")
        # else:
            # logging.info(f"Hidden file found: {str(hidden_f)}")
            # confirm = input("Remove hidden file? (y/n): ").strip().lower()
            # if confirm == 'y':
            #     if hidden_f.is_dir():
            #         hidden_f.rmdir()
            #     else:
            #         hidden_f.unlink()
            #     logging.info(f"Removed: {str(hidden_f.name)}")


def main():
    args = parse_args()
    
    for package_path in args.directory:
        get_hidden_files(package_path)



if __name__ == "__main__":
    main()