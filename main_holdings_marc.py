'''Main "script."'''
import csv
import json
import logging
import os
import sys
from os import listdir
from os.path import isfile

import requests
from argparse_prompt import PromptParser
from folioclient.FolioClient import FolioClient
from pymarc.reader import MARCReader

from migration_tools.custom_exceptions import (
    TransformationProcessError,
    TransformationRecordFailedError,
)
from migration_tools.folder_structure import FolderStructure
from migration_tools.main_base import MainBase
from migration_tools.marc_rules_transformation.holdings_processor import (
    HoldingsProcessor,
)
from migration_tools.marc_rules_transformation.rules_mapper_holdings import (
    RulesMapperHoldings,
)


def parse_args():
    """Parse CLI Arguments"""
    parser = PromptParser()
    parser.add_argument("base_folder", help="Base folder of the client.")
    parser.add_argument("okapi_url", help=("OKAPI base url"))
    parser.add_argument("tenant_id", help=("id of the FOLIO tenant."))
    parser.add_argument("username", help=("the api user"))
    parser.add_argument("--password", help="the api users password", secure=True)
    parser.add_argument(
        "--default_call_number_type_id",
        help="UUID of the default callnumber type",
        default="95467209-6d7b-468b-94df-0f5d7ad2747d",
    )
    parser.add_argument(
        "--suppress",
        "-ds",
        help="This batch of records are to be suppressed in FOLIO.",
        default=False,
        type=bool,
    )
    parser.add_argument(
        "--time_stamp",
        "-ts",
        help="Time Stamp String (YYYYMMDD-HHMMSS) from Instance transformation. Required",
    )
    args = parser.parse_args()
    print(args.time_stamp)
    if len(args.time_stamp) != 15:
        print(f"Time stamp ({args.time_stamp}) is not set properly")
        sys.exit()
    print(f"\tOkapi URL:\t{args.okapi_url}")
    print(f"\tTenanti Id:\t{args.tenant_id}")
    print(f"\tUsername:\t{args.username}")
    print("\tPassword:\tSecret")
    return args


def main():
    """Main method. Magic starts here."""
    args = parse_args()
    folder_structure = FolderStructure(args.base_folder, args.time_stamp)
    folder_structure.setup_migration_file_structure("holdingsrecord")
    MainBase.setup_logging(folder_structure)
    folder_structure.log_folder_structure()

    try:
        folio_client = FolioClient(
            args.okapi_url, args.tenant_id, args.username, args.password
        )
    except requests.exceptions.SSLError:
        logging.critical("SSL error. Check your VPN or Internet connection. Exiting")
        sys.exit()

    csv.register_dialect("tsv", delimiter="\t")
    files = [
        os.path.join(folder_structure.legacy_records_folder, f)
        for f in listdir(folder_structure.legacy_records_folder)
        if isfile(os.path.join(folder_structure.legacy_records_folder, f))
    ]
    with open(folder_structure.instance_id_map_path) as instance_id_map_file, open(
        folder_structure.locations_map_path
    ) as location_map_f, open(folder_structure.mfhd_rules_path) as mapping_rules_file:
        instance_id_map = {}
        for index, json_string in enumerate(instance_id_map_file):
            # {"legacy_id", "folio_id","instanceLevelCallNumber", "suppressed"}
            map_object = json.loads(json_string)
            if index % 50000 == 0:
                print(
                    f"{index} instance ids loaded to map {map_object['legacy_id']}",
                    end="\r",
                )
            instance_id_map[map_object["legacy_id"]] = map_object
        logging.info(f"loaded {index} migrated instance IDs")

        location_map = list(csv.DictReader(location_map_f, dialect="tsv"))
        rules_file = json.load(mapping_rules_file)

        logging.info(f"Locations in map: {len(location_map)}")
        logging.info(any(location_map))
        logging.info(f'Default location code {rules_file["defaultLocationCode"]}')
        logging.info(f"{len(instance_id_map)} Instance ids in map")
        mapper = RulesMapperHoldings(
            folio_client,
            instance_id_map,
            location_map,
            rules_file["defaultLocationCode"],
            args.default_call_number_type_id,
        )
        mapper.mappings = rules_file["rules"]

        processor = HoldingsProcessor(
            mapper, folio_client, folder_structure, args.suppress
        )
        for records_file in files:
            try:
                with open(records_file, "rb") as marc_file:
                    reader = MARCReader(marc_file, to_unicode=True, permissive=True)
                    reader.hide_utf8_warnings = True
                    reader.force_utf8 = True
                    logging.info(f"Running {records_file}")
                    read_records(reader, processor)
            except TransformationProcessError as tpe:
                logging.critical(tpe)
                sys.exit()
            except Exception:
                logging.exception(f"Failure in Main: {records_file}", stack_info=True)
        processor.wrap_up()


def read_records(reader, processor: HoldingsProcessor):
    for idx, record in enumerate(reader):
        try:
            if record is None:
                raise TransformationRecordFailedError(
                    f"Index in file:{idx}",
                    f"MARC parsing error: {reader.current_exception}",
                    f"{reader.current_chunk}",
                )
            else:
                processor.process_record(record)
        except TransformationRecordFailedError as error:
            error.log_it()
        except ValueError as error:
            logging.error(error)


if __name__ == "__main__":
    main()
