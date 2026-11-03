from repair_tools.utils.format_utils import print_standard_summary
import argparse
import logging
from pathlib import Path
import datetime

from repair_tools.utils.preservica_search_parse import PreservicaAPI
from repair_tools.utils.logger_setup import setup_logging


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--credentials",
        type=str,
        required=True,
        help="which set of credentials to use",
        )
    
    parser.add_argument(
        "--pkgtitle",
        "-p",
        nargs='+',
        help="One or more titles of packages to find and move, separated by a space."
    )
    parser.add_argument(
        "--pkguuid",
        "-u",
        nargs='+',
        help="One or more UUIDs of packages to move directly, separated by a space."
    )
    parser.add_argument(
        "--new-parent-ref",
        "-npf",
        required=True,
        help="The parentref of the new folder."
    )
    parser.add_argument(
        "--parent", 
        type=str,
        choices=["ingest", "digami", "digarch"],
        help="(Optional) Limit search to specific parent. If omitted, checks all."
    )
    parser.add_argument(
        "--logpath", 
        type=Path,
        help="Directory for log files."
    )
    return parser.parse_args()

def process_move_list(credentials: str, pkg_list: list = None, uuid_list: list = None, new_parent_ref: str = None, limit_parent: str = None, existing_logger=None):
    logger = existing_logger if existing_logger else logging.getLogger(__name__)
    
    api = PreservicaAPI(credentials)

    search_locations = {
        "INGEST": api.ingest_uuid,
        "DigAMI": api.ami_uuid,
        "DigArch": api.digarch_uuid
    }

    if limit_parent:
        key_map = {"ingest": "INGEST", "digami": "DigAMI", "digarch": "DigArch"}
        if limit_parent in key_map:
            search_locations = {key_map[limit_parent]: search_locations[key_map[limit_parent]]}

    failed_moves = set()
    successful_moves = set()
    deletion_exists = set()

    if not pkg_list and not uuid_list:
        logger.warning("No package titles or UUIDs provided. Nothing to do.")
        return True

    if uuid_list:
        logger.info(f"Starting move workflow for {len(uuid_list)} package UUIDs.")
        for pkg_uuid in uuid_list:
            logger.info(f"--- Processing UUID: {pkg_uuid} ---")
            success = api.move_entity(pkg_uuid, new_parent_ref)
            if success:
                logger.info(f"MOVED: Package UUID '{pkg_uuid}' moved successfully.")
                successful_moves.add(pkg_uuid)
            else:
                logger.error(f"FAILED: Could not move package UUID '{pkg_uuid}'.")
                failed_moves.add(pkg_uuid)

    if pkg_list:
        logger.info(f"Starting move workflow for {len(pkg_list)} package titles.")

        for pkg_title in pkg_list:
            logger.info(f"--- Processing: {pkg_title} ---")
            
            if api.fetch_uuid_by_title(pkg_title, new_parent_ref):
                logger.info(f"SKIPPED: '{pkg_title}' already in destination.")
                deletion_exists.add(pkg_title)
                continue

            found_uuid = None
            for name, parent_uuid in search_locations.items():
                if not parent_uuid: 
                    continue 
                
                found_uuid = api.fetch_uuid_by_title(pkg_title, parent_uuid)
                if found_uuid:
                    logger.info(f"Found '{pkg_title}' in {name} ({found_uuid})")
                    break
            
            if found_uuid:
                success = api.move_entity(found_uuid, new_parent_ref)
                if success:
                    logger.info(f"MOVED: '{pkg_title}' moved successfully.")
                    successful_moves.add(pkg_title)
                else:
                    logger.error(f"FAILED: Could not move '{pkg_title}'.")
                    failed_moves.add(pkg_title)
            else:
                logger.warning(f"NOT FOUND: '{pkg_title}' could not be found in source folders.")
                failed_moves.add(pkg_title)

    summary = {
        "Successful":      len(successful_moves),
        "Already in Dest": len(deletion_exists),
        "Failed/Not Found":len(failed_moves),
    }
    if failed_moves:
        summary["Failed Packages"] = sorted(list(failed_moves))
    print_standard_summary("Move Summary", summary, logger=logger)

    return len(failed_moves) == 0

def main():
    args = parse_args()

    log_path = args.logpath if args.logpath else Path.cwd()
    log_file = log_path / f"prsv_move_{datetime.datetime.now().strftime('%Y%m%d')}.log"
    logger, _ = setup_logging(log_file)

    success = process_move_list(
        credentials=args.credentials,
        pkg_list=args.pkgtitle,
        uuid_list=args.pkguuid,
        new_parent_ref=args.new_parent_ref,
        limit_parent=args.parent,
        existing_logger=logger
    )

    if success:
        logging.shutdown()
        log_file.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
