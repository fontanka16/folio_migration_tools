import json
import logging
import sys
from pathlib import Path
from os import environ

import httpx
import humps
from argparse_prompt import PromptParser
from pydantic import ValidationError

from folio_migration_tools.custom_exceptions import TransformationProcessError
from folio_migration_tools.library_configuration import LibraryConfiguration
from folio_migration_tools.migration_tasks import *  # noqa: F403, F401
from folio_migration_tools.migration_tasks import migration_task_base

import i18n

i18n.load_config(Path(__file__).parents[2] / "i18n_config.py")


def parse_args(args):
    task_classes = iter(inheritors(migration_task_base.MigrationTaskBase))
    parser = PromptParser()
    parser.add_argument(
        "configuration_path",
        help="Path to configuration file",
        nargs="?",
        prompt="FOLIO_MIGRATION_TOOLS_CONFIGURATION_PATH" not in environ,
        default=environ.get("FOLIO_MIGRATION_TOOLS_CONFIGURATION_PATH"),
    )
    tasks_string = ", ".join(sorted(tc.__name__ for tc in task_classes))

    parser.add_argument(
        "task_name",
        help=("Task name. Use one of: " f"{tasks_string}"),
        nargs="?",
        prompt="FOLIO_MIGRATION_TOOLS_TASK_NAME" not in environ,
        default=environ.get("FOLIO_MIGRATION_TOOLS_TASK_NAME"),
    )
    parser.add_argument(
        "--okapi_password",
        help="pasword for the tenant in the configuration file",
        prompt="FOLIO_MIGRATION_TOOLS_OKAPI_PASSWORD" not in environ,
        default=environ.get("FOLIO_MIGRATION_TOOLS_OKAPI_PASSWORD"),
        secure=True,
    )
    parser.add_argument(
        "--base_folder_path",
        help=("path to the base folder for this library. Built on migration_repo_template"),
        prompt="FOLIO_MIGRATION_TOOLS_BASE_FOLDER_PATH" not in environ,
        default=environ.get("FOLIO_MIGRATION_TOOLS_BASE_FOLDER_PATH"),
    )
    parser.add_argument(
        "--report_language",
        help=(
            "Language to write the reports. Defaults english for untranslated languages/strings."
        ),
        default=environ.get("FOLIO_MIGRATION_TOOLS_REPORT_LANGUAGE", "en"),
        prompt=False,
    )
    return parser.parse_args(args)


def main():
    try:
        task_classes = list(inheritors(migration_task_base.MigrationTaskBase))

        args = parse_args(sys.argv[1:])
        i18n.set("locale", args.report_language)
        with open(args.configuration_path) as config_file_path:
            try:
                config_file_humped = json.load(config_file_path)
                config_file_humped["libraryInformation"]["okapiPassword"] = args.okapi_password
                config_file_humped["libraryInformation"]["baseFolder"] = args.base_folder_path
                config_file = humps.decamelize(config_file_humped)
                library_config = LibraryConfiguration(**config_file["library_information"])
                try:
                    migration_task_config = next(
                        t for t in config_file["migration_tasks"] if t["name"] == args.task_name
                    )
                except StopIteration:
                    task_names = [t.get("name", "") for t in config_file["migration_tasks"]]
                    print(
                        f"Referenced task name {args.task_name} not found in the "
                        f'configuration file. Use one of {", ".join(task_names)}'
                        "\nHalting..."
                    )
                    sys.exit(2)
                try:
                    task_class = next(
                        tc
                        for tc in task_classes
                        if tc.__name__ == migration_task_config["migration_task_type"]
                    )
                    task_config = task_class.TaskConfiguration(**migration_task_config)
                    task_obj = task_class(task_config, library_config)
                    task_obj.do_work()
                    task_obj.wrap_up()
                except StopIteration:
                    print(
                        f'Referenced task {migration_task_config["migration_task_type"]} '
                        "is not a valid option. Update your task to incorporate "
                        f"one of {json.dumps([tc.__name__ for tc in task_classes], indent=4)}"
                    )
                    sys.exit(2)
                except TransformationProcessError as tpe:
                    logging.critical(tpe.message)
                    print(f"\n{tpe.message}: {tpe.data_value}")
                    sys.exit(1)
            except json.decoder.JSONDecodeError as json_error:
                logging.critical(json_error)
                print(json_error.doc)
                print(
                    f"\n{json_error}"
                    f"\nError parsing the above JSON mapping or configruation file. Halting."
                )
                sys.exit(2)
            except ValidationError as e:
                print(e.json())
                print("Validation errors in configuration file:")
                print("==========================================")

                for validation_message in json.loads(e.json()):
                    print(
                        f"{validation_message['msg']}\t"
                        f"{', '.join(humps.camelize(str(x)) for x in validation_message['loc'])}"
                    )
                print("Halting")

            # task_obj.do_work()
            logging.info("Work done. Shutting down")
            sys.exit(0)
            # task_obj.wrap_up()
    except httpx.HTTPError as connection_error:
        print(
            f"\nConnection Error when connecting to {connection_error.request.url}. "
            "Are you connectet to the Internet/VPN? Do you need to update DNS settings?"
        )
        sys.exit(1)
    except FileNotFoundError as fnf_error:
        print(f"\n{fnf_error.strerror}: {fnf_error.filename}")
        sys.exit(1)
    except Exception as ee:
        logging.exception("Unhandled exception")
        print(f"\n{ee}")
        sys.exit(1)


def inheritors(base_class):
    subclasses = set()
    work = [base_class]
    while work:
        parent = work.pop()
        for child in parent.__subclasses__():
            if child not in subclasses:
                subclasses.add(child)
                work.append(child)
    return subclasses


if __name__ == "__main__":
    main()
