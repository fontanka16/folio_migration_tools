'''Main "script."'''
import csv
import logging
from typing import Annotated
from typing import List
from typing import Optional

from folio_uuid.folio_namespaces import FOLIONamespaces
from pydantic import Field

from folio_migration_tools.custom_exceptions import TransformationProcessError
from folio_migration_tools.helper import Helper
from folio_migration_tools.library_configuration import FileDefinition
from folio_migration_tools.library_configuration import HridHandling
from folio_migration_tools.library_configuration import LibraryConfiguration
from folio_migration_tools.marc_rules_transformation.rules_mapper_holdings import (
    RulesMapperHoldings,
)
from folio_migration_tools.migration_tasks.migration_task_base import MigrationTaskBase
from folio_migration_tools.task_configuration import AbstractTaskConfiguration


class HoldingsMarcTransformer(MigrationTaskBase):
    class TaskConfiguration(AbstractTaskConfiguration):
        name: str
        legacy_id_marc_path: str
        deduplicate_holdings_statements: Optional[bool] = True
        migration_task_type: str
        hrid_handling: Optional[HridHandling] = HridHandling.default
        deactivate035_from001: Optional[bool] = False
        files: List[FileDefinition]
        location_map_file_name: str
        default_call_number_type_name: str
        fallback_holdings_type_id: str
        holdings_type_uuid_for_boundwiths: Annotated[
            str,
            Field(
                title="Holdings Type for Boundwith Holdings",
                description=(
                    "UUID for a Holdings type (set in Settings->Inventory) "
                    "for Bound-with Holdings)"
                ),
            ),
        ] = ""
        boundwith_relationship_file_path: Annotated[
            str,
            Field(
                title="Boundwith relationship file path",
                description=(
                    "Path to a file outlining Boundwith relationships, in the style of Voyager."
                    " A TSV file with MFHD_ID and BIB_ID headers and values"
                ),
            ),
        ] = ""
        create_source_records: Annotated[
            bool, Field(description="Controls wheter or not to retain the MARC records in SRS.")
        ] = True
        reset_hrid_settings: Optional[bool] = False
        update_hrid_settings: Annotated[
            bool,
            Field(
                title="Update HRID settings",
                description="At the end of the run, update FOLIO with the HRID settings",
            ),
        ] = True

    @staticmethod
    def get_object_type() -> FOLIONamespaces:
        return FOLIONamespaces.holdings

    def __init__(
        self,
        task_config: TaskConfiguration,
        library_config: LibraryConfiguration,
        use_logging: bool = True,
    ):
        csv.register_dialect("tsv", delimiter="\t")
        super().__init__(library_config, task_config, use_logging)
        self.task_config = task_config
        self.holdings_types = list(
            self.folio_client.folio_get_all("/holdings-types", "holdingsTypes")
        )
        self.default_holdings_type = next(
            (
                h
                for h in self.holdings_types
                if h["id"] == self.task_config.fallback_holdings_type_id
            ),
            {"name": ""},
        )
        if not self.default_holdings_type:
            raise TransformationProcessError(
                "",
                (
                    f"Holdings type with ID {self.task_config.fallback_holdings_type_id}"
                    " not found in FOLIO."
                ),
            )
        logging.info(
            "%s will be used as default holdings type",
            self.default_holdings_type.get("name", ""),
        )

        # Load Boundwith relationship map
        self.boundwith_relationship_map = []
        if self.task_config.boundwith_relationship_file_path:
            with open(
                self.folder_structure.legacy_records_folder
                / self.task_config.boundwith_relationship_file_path
            ) as boundwith_relationship_file:
                self.boundwith_relationship_map = list(
                    csv.DictReader(boundwith_relationship_file, dialect="tsv")
                )
            logging.info(
                "Rows in Bound with relationship map: %s", len(self.boundwith_relationship_map)
            )

        location_map_path = (
            self.folder_structure.mapping_files_folder / self.task_config.location_map_file_name
        )
        with open(location_map_path) as location_map_file:
            self.location_map = list(csv.DictReader(location_map_file, dialect="tsv"))
            logging.info("Locations in map: %s", len(self.location_map))

        self.check_source_files(
            self.folder_structure.legacy_records_folder, self.task_config.files
        )
        self.instance_id_map = self.load_id_map(self.folder_structure.instance_id_map_path, True)
        self.mapper = RulesMapperHoldings(
            self.folio_client,
            self.location_map,
            self.task_config,
            self.library_configuration,
            self.instance_id_map,
            self.boundwith_relationship_map,
        )
        if (
            self.task_configuration.reset_hrid_settings
            and self.task_configuration.update_hrid_settings
        ):
            self.mapper.hrid_handler.reset_holdings_hrid_counter()
        logging.info("%s Instance ids in map", len(self.instance_id_map))
        logging.info("Init done")

    def do_work(self):
        self.do_work_marc_transformer()

    def wrap_up(self):
        logging.info("Done. Transformer Wrapping up...")
        self.extradata_writer.flush()
        self.processor.wrap_up()
        with open(self.folder_structure.migration_reports_file, "w+") as report_file:
            self.mapper.migration_report.write_migration_report(
                "Bibliographic records transformation report",
                report_file,
                self.start_datetime,
            )
            Helper.print_mapping_report(
                report_file,
                self.mapper.parsed_records,
                self.mapper.mapped_folio_fields,
                self.mapper.mapped_legacy_fields,
            )

        logging.info(
            "Done. Transformation report written to %s",
            self.folder_structure.migration_reports_file.name,
        )

        self.clean_out_empty_logs()
