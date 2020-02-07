'''The default mapper, responsible for parsing MARC21 records acording to the
FOLIO community specifications'''
import json
import os.path
from textwrap import wrap
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from io import StringIO

import requests
from pymarc import Field, JSONWriter
from marc_to_folio.conditions import Conditions
from marc_to_folio.default_mapper import DefaultMapper


class RulesMapper(DefaultMapper):
    '''Maps a MARC record to inventory instance format according to
    the FOLIO community convention'''
    # Bootstrapping (loads data needed later in the script.)

    def __init__(self, folio, results_path):
        self.filter_chars = r'[.,\/#!$%\^&\*;:{}=\-_`~()]'
        self.filter_chars_dop = r'[.,\/#!$%\^&\*;:{}=\_`~()]'
        self.filter_last_chars = r',$'
        self.folio = folio
        self.conditions = Conditions(folio)
        self.migration_user_id = 'd916e883-f8f1-4188-bc1d-f0dce1511b50'
        self.srs_recs = []
        instance_url = 'https://raw.githubusercontent.com/folio-org/mod-inventory-storage/master/ramls/instance.json'
        schema_request = requests.get(instance_url)
        # schema_request = requests.get('https://raw.githubusercontent.com/folio-org/mod-source-record-manager/master/ramls/instance.json')
        schema_text = schema_request.text
        # schema_text = schema_text.replace('raml-util/schemas/tags.schema', 'https://raw.githubusercontent.com/folio-org/raml/master/schemas/tags.schema')
        # schema_text = schema_text.replace('raml-util/schemas/metadata.schema', 'https://raw.githubusercontent.com/folio-org/raml/master/schemas/metadata.schema')

        self.instance_schema = json.loads(schema_text)
        # self.instance_schema['properties'].pop('tags')
        # self.instance_schema['title'] = 'Instance'
        # self.instance_schema['$id'] = instance_url
        self.holdings_map = {}
        self.results_path = results_path
        self.srs_records_file = open(os.path.join(
            self.results_path, 'srs.json'), "w+")
        self.srs_raw_records_file = open(os.path.join(
            self.results_path, 'srs_raw_records.json'), "w+")
        self.srs_marc_records_file = open(os.path.join(
            self.results_path, 'srs_marc_records.json'), "w+")
        self.id_map = {}
        print("Fetching valid language codes...")
        self.language_codes = list(self.fetch_language_codes())
        self.contrib_name_types = {}
        self.mapped_folio_fields = {}
        self.alt_title_map = {}
        self.identifier_types = []
        # self.mappings = self.folio.folio_get_single_object('/mapping-rules')
        with open('/mnt/c/code/folio-fse/MARC21-To-FOLIO/maps/mapping_rules_default.json') as map_f:
            self.mappings = json.load(map_f)
        self.unmapped_tags = {}
        self.unmapped_conditions = {}

    def parse_bib(self, marc_record, record_source):
        ''' Parses a bib recod into a FOLIO Inventory instance object
            Community mapping suggestion: https://bit.ly/2S7Gyp3
             This is the main function'''
        folio_instance = {
            'id': str(uuid.uuid4()),
            'metadata': super().get_metadata_construct(self.migration_user_id)
        }
        for marc_field in marc_record:
            if marc_field.tag not in self.mappings:
                add_stats(self.unmapped_tags, marc_field.tag)
            else:
                mappings = self.mappings[marc_field.tag]
                self.map_field_according_to_mapping(
                    marc_field, mappings, folio_instance)
        # Do stuff not easily captured by the mapping rules
        folio_instance['modeOfIssuanceId'] = self.get_mode_of_issuance_id(
            marc_record)
        folio_instance['languages'].extend(self.get_languages(marc_record))
        folio_instance['languages'] = list(self.filter_langs(
            folio_instance['languages'], marc_record['001'].format_field()))
        folio_instance['natureOfContentTermIds'] = self.get_nature_of_content(
            marc_record)
        self.validate(folio_instance)
        self.dedupe_rec(folio_instance)
        return folio_instance

    def wrap_up(self):
        self.flush_srs_recs()
        self.srs_records_file.close()
        self.srs_marc_records_file.close()
        self.srs_raw_records_file.close()
        print(self.unmapped_tags)
        print(self.unmapped_conditions)

    def map_field_according_to_mapping(self, marc_field, mappings, rec):
        for mapping in mappings:
            if 'entity' not in mapping:
                if 'rules' in mapping and any(mapping['rules']) and any(mapping['rules'][0]['conditions']):
                    self.add_value_to_target(
                        rec, mapping['target'], self.apply_rules(marc_field, mapping))
                else:
                    self.add_value_to_target(
                        rec, mapping['target'], [marc_field.format_field()])
            else:
                e_per_subfield = mapping.get(
                    'entityPerRepeatedSubfield', False)
                self.handle_entity_mapping(
                    marc_field, mapping['entity'], rec, e_per_subfield)

    def handle_entity_mapping(self, marc_field, entity_mapping, rec, e_per_subfield):
        e_parent = entity_mapping[0]['target'].split('.')[0]
        if e_per_subfield:
            for sf_tuple in grouped(marc_field.subfields, 2):
                temp_field = Field(tag=marc_field.tag,
                                   indicators=marc_field.indicators,
                                   subfields=[sf_tuple[0], sf_tuple[1]])
                entity = self.create_entity(
                    entity_mapping, temp_field, e_parent)
                self.add_entity_to_record(entity, e_parent, rec)
        else:
            entity = self.create_entity(entity_mapping, marc_field, e_parent)
            self.add_entity_to_record(entity, e_parent, rec)

    def create_entity(self, entity_mapping, marc_field, entity_parent_key):
        entity = {}
        for em in entity_mapping:
            k = em['target'].split('.')[-1]
            rv = self.apply_rules(marc_field, em)
            if rv:
                v = rv[0]
                if entity_parent_key == k:
                    entity = v
                else:
                    entity[k] = v
        return entity

    def add_entity_to_record(self, entity, entity_parent_key, rec):
        sch = self.instance_schema['properties']
        if sch[entity_parent_key]['type'] == 'array':
            if entity_parent_key not in rec:
                rec[entity_parent_key] = [entity]
            else:
                rec[entity_parent_key].append(entity)
        else:
            rec[entity_parent_key] = entity

    def apply_rules(self, marc_field, mapping):
        values = []
        value = ''
        if mapping.get('rules', []) and mapping['rules'][0].get('conditions', []):
            c_type_def = mapping['rules'][0]['conditions'][0]['type'].split(
                ',')
            condition_types = [x.strip() for x in c_type_def]
            parameter = mapping['rules'][0]['conditions'][0].get(
                'parameter', {})
            # print(f'conditions {condition_types}')
            if mapping.get('applyRulesOnConcatenatedData', ''):
                value = ' '.join(
                    marc_field.get_subfields(*mapping['subfield']))
                value = self.apply_rule(
                    value, condition_types, marc_field, parameter)
            else:
                if mapping.get('subfield', []):
                    value = ' '.join([self.apply_rule(x, condition_types, marc_field, parameter)
                                      for x in marc_field.get_subfields(*mapping['subfield'])])
                else:
                    value = self.apply_rule(
                        marc_field.format_field(), condition_types, marc_field, parameter)
        elif not mapping.get('rules', []) or not mapping['rules'][0].get('conditions', []):
            value = ' '.join(marc_field.get_subfields(*mapping['subfield']))
            # print(f"no rules {mapping}")
        if mapping.get('subFieldSplit', ''):
            values = wrap(value, 3)
        else:
            values = [value]
        return values

    def apply_rule(self, value, condition_types, marc_field, parameter):
        v = value
        for condition_type in condition_types:
            v = self.conditions.get_condition(
                condition_type, v, parameter, marc_field)
        return v

    def add_value_to_target(self, rec, target_string, value):
        targets = target_string.split('.')
        sch = self.instance_schema['properties']
        prop = rec
        sc_prop = sch
        sc_parent = None
        parent = None
        if len(targets) == 1:
            # print(f"{target_string} {value} {rec}")
            if sch[target_string]['type'] == 'array' and sch[target_string]['items']['type'] == 'string':
                if target_string not in rec:
                    rec[target_string] = value
                else:
                    rec[target_string].extend(value)
            elif sch[target_string]['type'] == 'string':
                rec[target_string] = value[0]
            else:
                raise Exception(
                    f"Edge! {target_string} {sch[target_string]['type']}")
        else:
            for target in targets:
                if target in sc_prop:
                    sc_prop = sc_prop[target]
                else:
                    sc_prop = sc_parent['items']['properties'][target]
                if target not in rec:
                    sc_prop_type = sc_prop.get('type', 'string')
                    if sc_prop_type == 'array':
                        prop[target] = []
                        break
                        # prop[target].append({})
                    elif sc_parent['type'] == 'array' and sc_prop_type == 'string':
                        print(f"break! {target} {sc_prop['type']} {prop}")
                        break
                    else:
                        if (sc_parent['type'] == 'array'):
                            prop[target] = {}
                            parent.append(prop[target])
                        else:
                            raise Exception(
                                f"Edge! {target_string} {sch[target_string]}")
                if target == targets[-1]:
                    prop[target] = value[0]
                prop = prop[target]
                sc_parent = sc_prop
                parent = target

    def validate(self, folio_rec):
        if folio_rec["title"].strip() == "":
            print(f"No title for {folio_rec['hrid']}")
        for key, value in folio_rec.items():
            if isinstance(value, str) and len(value) > 0:
                self.mapped_folio_fields['key]'] = self.mapped_folio_fields.get(
                    key, 0) + 1
            if isinstance(value, list) and len(value) > 0:
                self.mapped_folio_fields['key]'] = self.mapped_folio_fields.get(
                    key, 0) + 1

    def save_source_record(self, marc_record, instance_id):
        '''Saves the source Marc_record to the Source record Storage module'''
        marc_record.add_field(Field(tag='999',
                                    indicators=['f', 'f'],
                                    subfields=['i', instance_id]))
        self.srs_recs.append((marc_record, instance_id))
        if len(self.srs_recs) > 1000:
            self.flush_srs_recs()
            self.srs_recs = []

    def flush_srs_recs(self):
        pool = ProcessPoolExecutor(max_workers=4)
        results = list(pool.map(get_srs_strings, self.srs_recs))
        self.srs_records_file.write("".join(r[0]for r in results))
        self.srs_marc_records_file.write(
            "".join(r[2] for r in results))
        self.srs_raw_records_file.write("".join(r[1] for r in results))

    def post_new_source_storage_record(self, loan):
        okapi_headers = self.folio.okapi_headers
        host = self.folio.okapi_url
        path = ("{}/source-storage/records".format(host))
        response = requests.post(path,
                                 data=loan,
                                 headers=okapi_headers)
        if response.status_code != 201:
            print("Something went wrong. HTTP {}\nMessage:\t{}"
                  .format(response.status_code, response.text))

    def get_nature_of_content(self, marc_record):
        return ["81a3a0e2-b8e5-4a7a-875d-343035b4e4d7"]

    def get_mode_of_issuance_id(self, marc_record):
        mode_of_issuance = marc_record.leader[7]
        table = {'m': 'Monograph', 's': 'Serial'}
        name = table.get(mode_of_issuance, 'Other')
        return next(i['id'] for i in self.folio.modes_of_issuance
                    if name == i['name'])

    def get_languages(self, marc_record):
        '''Get languages and tranforms them to correct codes'''
        languages = set()
        lang_fields = marc_record.get_fields('041')
        if any(lang_fields):
            subfields = 'abdefghjkmn'
            for lang_tag in lang_fields:
                lang_codes = lang_tag.get_subfields(*list(subfields))
                for lang_code in lang_codes:
                    lang_code = str(lang_code).lower()
                    langlength = len(lang_code.replace(" ", ""))
                    if langlength == 3:
                        languages.add(lang_code.replace(" ", ""))
                    elif langlength > 3 and langlength % 3 == 0:
                        lc = lang_code.replace(" ", "")
                        new_codes = [lc[i:i + 3]
                                     for i in range(0, len(lc), 3)]
                        languages.update(new_codes)
                        languages.discard(lang_code)

                languages.update()
            languages = set(self.filter_langs(filter(None, languages),
                                              marc_record['001'].format_field()))
        elif '008' in marc_record and len(marc_record['008'].data) > 38:
            from_008 = ''.join((marc_record['008'].data[35:38]))
            if from_008:
                languages.add(from_008.lower())
        # TODO: test agianist valide language codes
        return list(languages)

    def fetch_language_codes(self):
        '''fetches the list of standardized language codes from LoC'''
        url = "https://www.loc.gov/standards/codelists/languages.xml"
        tree = ET.fromstring(requests.get(url).content)
        name_space = "{info:lc/xmlns/codelist-v1}"
        xpath_expr = "{0}languages/{0}language/{0}code".format(name_space)
        for code in tree.findall(xpath_expr):
            yield code.text

    def filter_langs(self, language_values, legacyid):
        forbidden_values = ['###', 'zxx']
        for language_value in language_values:
            if language_value in self.language_codes and language_value not in forbidden_values:
                yield language_value
            else:
                if language_value == 'jap':
                    yield 'jpn'
                elif language_value == 'fra':
                    yield 'fre'
                elif language_value == 'sve':
                    yield 'swe'
                elif language_value == 'tys':
                    yield 'ger'
                else:
                    print('Illegal language code: {} for {}'
                          .format(language_value, legacyid))

    def dedupe_rec(self, rec):
        # remove duplicates
        for key, value in rec.items():
            if isinstance(value, list):
                res = []
                for v in value:
                    if v not in res:
                        res.append(v)
                rec[key] = res


def get_srs_strings(my_tuple):
    json_string = StringIO()
    writer = JSONWriter(json_string)
    writer.write(my_tuple[0])
    writer.close(close_fh=False)
    marc_uuid = str(uuid.uuid4())
    raw_uuid = str(uuid.uuid4())
    record = {
        "id": str(uuid.uuid4()),
        "deleted": False,
        "snapshotId": "67dfac11-1caf-4470-9ad1-d533f6360bdd",
        "matchedProfileId": str(uuid.uuid4()),
        "matchedId": str(uuid.uuid4()),
        "generation": 1,
        "recordType": "MARC",
        "rawRecordId": raw_uuid,
        "parsedRecordId": marc_uuid,
        "additionalInfo": {
            "suppressDiscovery": False
        },
        "externalIdsHolder": {
            "instanceId": my_tuple[1]
        }
    }
    raw_record = {
        "id": raw_uuid,
        "content": my_tuple[0].as_json()
    }
    marc_record = {
        "id": marc_uuid,
        "content": json.loads(my_tuple[0].as_json())
    }
    return (f"{record['id']}\t{json.dumps(record)}\n",
            f"{raw_record['id']}\t{json.dumps(raw_record)}\n",
            f"{marc_record['id']}\t{json.dumps(marc_record)}\n")


def grouped(iterable, n):
    "s -> (s0,s1,s2,...sn-1), (sn,sn+1,sn+2,...s2n-1), (s2n,s2n+1,s2n+2,...s3n-1), ..."
    return zip(*[iter(iterable)] * n)


def add_stats(stats, a):
    if a not in stats:
        stats[a] = 1
    else:
        stats[a] += 1
