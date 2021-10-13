import logging
import collections
import sys

from migration_tools.custom_exceptions import (
    TransformationProcessError,
    TransformationRecordFailedError,
)
from migration_tools.migration_report import MigrationReport
from migration_tools.report_blurbs import Blurbs


class MapperBase:
    def __init__(self):
        logging.info("MapperBase initiating")
        self.mapped_folio_fields = {}
        self.migration_report = MigrationReport()
        self.num_criticalerrors = 0
        self.num_exeptions = 0
        self.mapped_legacy_fields = {}
        self.stats = {}
        self.schema_properties = None

    def report_legacy_mapping(self, field_name, present, mapped):
        if field_name not in self.mapped_legacy_fields:
            self.mapped_legacy_fields[field_name] = [int(present), int(mapped)]
        else:
            self.mapped_legacy_fields[field_name][0] += int(present)
            self.mapped_legacy_fields[field_name][1] += int(mapped)

    def report_folio_mapping(self, folio_record, schema):
        try:
            flattened = flatten(folio_record)
            for field_name, v in flattened.items():
                mapped = 0
                if isinstance(v, str) and v.strip():
                    mapped = 1
                elif isinstance(v, list) and any(v):
                    l = len([a for a in v if a])
                    mapped = l
                if field_name not in self.mapped_folio_fields:
                    self.mapped_folio_fields[field_name] = [mapped]
                else:
                    self.mapped_folio_fields[field_name][0] += mapped
            if not self.schema_properties:
                self.schema_properties = schema["properties"].keys()
            unmatched_properties = (
                p for p in self.schema_properties if p not in folio_record.keys()
            )
            for p in unmatched_properties:
                self.mapped_folio_fields[p] = [0]
        except Exception as ee:
            logging.error(ee)

    def handle_transformation_field_mapping_error(self, index_or_id, error):
        self.migration_report.add(Blurbs.FieldMappingErrors, error)
        error.id = error.id or index_or_id
        error.log_it()
        self.migration_report.add_general_statistics("Field Mapping Errors found")

    def handle_transformation_process_error(
        self, idx, error: TransformationProcessError
    ):
        self.migration_report.add_general_statistics("Transformation process error")
        logging.critical("%s\t%s", idx, error)
        sys.exit()

    def handle_transformation_record_failed_error(
        self, records_processed: int, error: TransformationRecordFailedError
    ):
        self.migration_report.add(
            Blurbs.GeneralStatistics, "Records failed due to an error"
        )
        logging.error(error.message)
        error.id = error.id or records_processed
        error.log_it()
        self.num_criticalerrors += 1
        if (
            self.num_criticalerrors / (records_processed + 1) > 0.2
            and self.num_criticalerrors > 5000
        ):
            logging.fatal(
                "Stopping. More than %s critical data errors", self.num_criticalerrors
            )
            logging.error(
                "Errors: %s\terrors/records: %s",
                self.num_criticalerrors,
                (self.num_criticalerrors / (records_processed + 1)),
            )
            sys.exit()

    def handle_generic_exception(self, idx, excepion: Exception):
        self.num_exeptions += 1
        print("\n=======ERROR===========")
        print(
            f"Row {idx:,} failed with the following unhandled Exception: {excepion}  "
            f"of type {type(excepion).__name__}"
        )
        if self.num_exeptions > 500:
            logging.fatal(
                "Stopping. More than %s unhandled exceptions. Code needs fixing",
                self.num_exeptions,
            )
            sys.exit()


def flatten(d, parent_key="", sep="."):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)
