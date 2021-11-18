import json
import logging
import os
from argparse_prompt import PromptParser
import time
import traceback
from abc import abstractmethod
from folio_uuid import FOLIONamespaces
from datetime import datetime

import requests
from folioclient import FolioClient

from migration_tasks.migration_task_base import MigrationTaskBase
from migration_tools.migration_configuration import MigrationConfiguration


def write_failed_batch_to_file(batch, file):
    for record in batch:
        file.write(f"{json.dumps(record)}\n")


class BatchPoster(MigrationTaskBase):
    def __init__(self, configuration: MigrationConfiguration):
        super().__init__(configuration)
        self.failed_ids = []
        self.first_batch = True
        self.object_name = configuration.object_name
        self.api_path = list_objects()[configuration.object_name]
        self.failed_objects = []
        object_name_formatted = configuration.object_name.replace(" ", "").lower()
        time_stamp = time.strftime("%Y%m%d-%H%M%S")
        self.failed_recs_path = os.path.join(
            configuration.results_folder,
            f"failed_{object_name_formatted}_records_{time_stamp}.json",
        )
        self.batch_size = configuration.batch_size
        self.processed = 0
        self.failed_batches = 0
        self.failed_records = 0
        self.processed_rows = 0
        self.users_created = 0
        self.users_updated = 0
        self.objects_file = configuration.objects_file
        self.users_per_group = {}
        self.failed_fields = set()
        self.num_failures = 0
        self.start = 0  # TODO: add this as an argument

    def do_work(self):
        logging.info("Starting....")
        batch = []

        with open(self.objects_file) as rows, open(
            self.failed_recs_path, "w"
        ) as failed_recs_file:
            last_row = ""
            for idx, row in enumerate(rows):
                last_row = row
                if self.processed_rows < self.start:
                    continue
                if row.strip():
                    try:
                        self.processed_rows += 1
                        json_rec = json.loads(row.split("\t")[-1])
                        if idx == 1:
                            print(json.dumps(json_rec, indent=True))
                        batch.append(json_rec)
                        if len(batch) == int(self.batch_size):
                            self.post_batch(batch, failed_recs_file)
                            batch = []
                    except UnicodeDecodeError as unicode_error:
                        print("=========ERROR==============")
                        print(
                            f"{unicode_error} Posting failed. Encoding error reading file"
                        )
                        print(
                            f"Failing row, either the one shown here or the next row in {self.objects_file}"
                        )
                        print(last_row)
                        print("=========Stack trace==============")
                        traceback.print_exc()
                        print("=======================", flush=True)
                    except Exception as exception:
                        logging.exception(f"{exception}")
                        logging.error(f"Failed row: {last_row}")
                        self.failed_batches += 1
                        self.failed_records += len(batch)
                        write_failed_batch_to_file(batch, failed_recs_file)
                        batch = []
                        self.num_failures += 0
                        if self.num_failures > 50:
                            logging.error(f"Exceeded number of failures at row {idx}")
                            raise exception
                            # Last batch
            self.post_batch(batch, failed_recs_file)
        logging.info(f"Done posting {idx} records. ")
        logging.info(
            f"Failed records: {self.failed_records} failed records in {self.failed_batches} "
            f"failed batches. Failed records saved to {self.failed_recs_path}"
        )

    def post_batch(self, batch, failed_recs_file):
        response = self.do_post(batch)
        if response.status_code == 201:
            logging.info(
                f"Posting successful! Total rows: {self.processed_rows} Total failed: {self.failed_records} "
                f"in {response.elapsed.total_seconds()}s "
                f"Batch Size: {len(batch)} Request size: {get_req_size(response)} "
            )
        elif response.status_code == 200:
            json_report = json.loads(response.text)
            self.users_created += json_report.get("createdRecords", 0)
            self.users_updated += json_report.get("updatedRecords", 0)
            self.failed_records += json_report.get("failedRecords", 0)
            if json_report.get("failedRecords", 0) > 0:
                failed_recs_file.write(response.text)
            logging.info(
                f"Posting successful! Total rows: {self.processed_rows} Total failed: {self.failed_records} "
                f"created: {self.users_created} updated: {self.users_updated} "
                f"in {response.elapsed.total_seconds()}s "
                f"Batch Size: {len(batch)} Request size: {get_req_size(response)} "
            )
        elif response.status_code == 422:
            resp = json.loads(response.text)
            raise Exception(
                f"HTTP {response.status_code}\t"
                f"Request size: {get_req_size(response)}"
                f"{datetime.utcnow().isoformat()} UTC\n"
                f"{json.dumps(resp, indent=4)}"
            )
        else:
            raise Exception(
                f"HTTP {response.status_code}\t"
                f"Request size: {get_req_size(response)}"
                f"{datetime.utcnow().isoformat()} UTC\n"
                f"{response.text}"
            )

    def do_post(self, batch):
        kind = list_objects()[self.object_name]
        path = kind["api_endpoint"]
        url = self.folio_client.okapi_url + path
        if kind["object_name"] == "users":
            payload = {kind["object_name"]: list(batch), "totalRecords": len(batch)}
        elif kind["total_records"]:
            payload = {"records": list(batch), "totalRecords": len(batch)}
        else:
            payload = {kind["object_name"]: batch}
        return requests.post(
            url, data=json.dumps(payload), headers=self.folio_client.okapi_headers
        )

    def wrap_up(self):
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def add_arguments(parser: PromptParser):
        MigrationTaskBase.add_common_arguments(parser)
        MigrationTaskBase.add_argument(parser, "objects_file", "path data file")
        MigrationTaskBase.add_argument(parser, "batch_size", "batch size")
        MigrationTaskBase.add_argument(
            parser,
            "results_folder",
            "Folder where failing records and logs will be stored",
        )
        MigrationTaskBase.add_argument(
            parser,
            "object_name",
            "What objects to batch post",
            choices=list(list_objects().keys()),
        )


def list_objects():
    return {
        "Items": {
            "object_name": "items",
            "api_endpoint": "/item-storage/batch/synchronous",
            "total_records": False,
        },
        "Holdings": {
            "object_name": "holdingsRecords",
            "api_endpoint": "/holdings-storage/batch/synchronous",
            "total_records": False,
        },
        "Instances": {
            "object_name": "instances",
            "api_endpoint": "/instance-storage/batch/synchronous",
            "total_records": False,
        },
        "SRS": {
            "object_name": "records",
            "api_endpoint": "/source-storage/batch/records",
            "total_records": True,
        },
        "Users": {
            "object_name": "users",
            "api_endpoint": "/user-import",
            "total_records": True,
        },
    }


def chunks(records, number_of_chunks):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(records), number_of_chunks):
        yield records[i : i + number_of_chunks]


def get_human_readable(size, precision=2):
    suffixes = ["B", "KB", "MB", "GB", "TB"]
    suffix_index = 0
    while size > 1024 and suffix_index < 4:
        suffix_index += 1  # increment the index of the suffix
        size = size / 1024.0  # apply the division
    return "%.*f%s" % (precision, size, suffixes[suffix_index])


def get_req_size(response):
    size = response.request.method
    size += response.request.url
    size += "\r\n".join(
        "{}{}".format(k, v) for k, v in response.request.headers.items()
    )
    size += response.request.body or []
    return get_human_readable(len(size.encode("utf-8")))
