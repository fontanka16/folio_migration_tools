'''The default mapper, responsible for parsing MARC21 records acording to the
FOLIO community specifications'''
import uuid
import xml.etree.ElementTree as ET
from random import randint

import requests


class DefaultMapper:
    '''Maps a MARC record to inventory instance format according to
    the FOLIO community convention'''
    # Bootstrapping (loads data needed later in the script.)
    def __init__(self, folio):
        self.folio = folio
        print("Fetching valid language codes...")
        self.language_codes = self.fetch_language_codes()

    def parse_bib(self, marc_record, record_source):
        ''' Parses a bib recod into a FOLIO Inventory instance object
            Community mapping suggestion: https://bit.ly/2S7Gyp3
             This is the main function'''
        # not mapped. Randomizes an instance type:
        rec = {
            'id': str(uuid.uuid4()),
            # This should be the new Libris ID?
            'hrid': str(marc_record['001'].format_field()),
            'title': self.get_title(marc_record),
            'indexTitle': self.get_index_title(marc_record),
            'source': record_source,
            'contributors': list(self.get_contributors(marc_record)),
            'identifiers': list(self.get_identifiers(marc_record)),
            # TODO: Add instanceTypeId
            'instanceTypeId': self.folio.instance_types[0]['id'],
            # 'alternativeTitles': list(self.get_alt_titles(marc_record)),
            'series': list(set(self.get_series(marc_record))),
            'editions': [(marc_record['250'] and
                          marc_record['250']['a']) or ''],
            'subjects': list(set(self.get_subjects(marc_record))),
            # TODO: add classification
            # 	'classification': [{'classificationNumber':'a',
            # 'classificationTypeId':''}]
            'publication': list((self.get_publication(marc_record))),
            # TODO: add instanceFormatId
            'instanceFormatIds': [self.folio.instance_formats[0]['id']],
            # TODO: add physical description
            # 'physicalDescriptions': ['pd1', 'pd2'],
            'languages': self.get_languages(marc_record),
            'notes': list(self.get_notes(marc_record))}
        return rec

    def get_index_title(self, marc_record):
        '''Returns the index title according to the rules'''
        field = marc_record['245']
        title_string = " ".join(field.get_subfields('a', 'n', 'p'))
        ind2 = field.indicator2
        if ind2 == '0':
            return title_string
        elif ind2 in map(str, range(1, 9)):
            num_take = int(ind2)
            return title_string[:num_take]
        else:
            return ''

    def get_notes(self, marc_record):
        '''Collects all notes fields and stores them as generic notes.'''
        # TODO: specify note types with better accuracy.
        note_tags = ['500', '501', '502', '504', '505', '506', '507', '508',
                     '510', '511', '513', '514', '515', '516', '518', '520',
                     '521', '522', '524', '525', '526', '530', '532', '533',
                     '534', '535', '536', '538', '540', '541', '542', '544',
                     '545', '546', '547', '550', '552', '555', '556', '561',
                     '562', '563', '565', '567', '580', '581', '583', '584',
                     '585', '586', '588', '590']
        for tag in note_tags:
            for field in marc_record.get_fields(tag):
                yield field.format_field()

    def get_title(self, marc_record):
        '''Get title or raise exception.'''
        title = marc_record.title()
        if title:
            return title
        else:
            raise ValueError("No title for {}".format(marc_record['001']))

    def folio_record_template(self, identifier):
        '''Create a new folio record from template'''
        # if created from json schema validation could happen earlier...
        return {'id': str(identifier)}

    def get_contributors(self, marc_record):
        '''Collects contributors from the marc record and adds the apropriate
        Ids'''
        fields = {'100': next(f['id'] for f in self.folio.contrib_name_types
                              if f['name'] == 'Personal name'),
                  '110': next(f['id'] for f in self.folio.contrib_name_types
                              if f['name'] == 'Corporate name'),
                  '111': next(f['id'] for f in self.folio.contrib_name_types
                              if f['name'] == 'Meeting name')}
        first = 0
        for field_tag in fields:
            for field in marc_record.get_fields(field_tag):
                ctype = self.get_contrib_type_id(marc_record)
                first += 1
                yield {'name': field.format_field(),
                       'contributorNameTypeId': fields[field_tag],
                       'contributorTypeId': ctype,
                       'primary': first < 2}

    def get_contrib_type_id(self, marc_record):
        ''' Maps type of contribution to the right FOLIO Contributor types'''
        # TODO: create logic here...
        ret = '5daa3848-958c-4dd8-9724-b7ae83a99a27'
        return ret

    # No urls wanted from Chalmers?.
    # def get_urls(self, marc_record):
    #     for field in marc_record.get_fields('856'):
    #        yield field['u'] or ''

    def get_subjects(self, marc_record):
        ''' Get subject headings from the marc record.'''
        tags = ['600', '610', '611', '630', '647', '648', '650', '651'
                '653', '654', '655', '656', '657', '658', '662']
        for tag in tags:
            for field in marc_record.get_fields(tag):
                yield field.format_field()

    def get_alt_titles(self, marc_record):
        '''Finds all Alternative titles.'''
        # TODO: Currently all alt titles to one type
        tags = ['240', '246', '247']
        alt_titles = set()
        for tag in tags:
            for field in marc_record.get_fields(tag):
                alt_titles.add(field.format_field())
        for title in alt_titles:
            tid = '0fe58901-183e-4678-a3aa-0b4751174ba8'
            yield {'alternativeTitleTypeId': tid,
                   'alternativeTitle': title}

    def get_publication(self, marc_record):
        '''Publication'''
        tags = ['260', '264']
        for tag in tags:
            for field in marc_record.get_fields(tag):
                yield {'publisher': field['b'] or '',
                       'place': field['a'] or '',
                       'dateOfPublication': field['c'] or ''}

    def get_series(self, marc_record):
        '''Series'''
        tags = ['440', '490', '800', '810', '811', '830']
        for tag in tags:
            for field in marc_record.get_fields(tag):
                yield field.format_field()

    def get_languages(self, marc_record):
        '''Get languages and tranforms them to correct codes'''
        languages = (marc_record['041'] and marc_record['041']
                     .get_subfields('a', 'b', 'd', 'e', 'f', 'g', 'h',
                                    'j', 'k', 'm', 'n')) or []
        from_008 = (marc_record['008'][35:37]
                    if '008' in marc_record else '')
        if from_008 and from_008 not in ['###', 'zxx']:
            languages.append(from_008)
        languages = list(set(filter(None, languages)))
        # TODO: test agianist valide language codes
        return languages

    def fetch_language_codes(self):
        '''fetches the list of standardized language codes from LoC'''
        url = "https://www.loc.gov/standards/codelists/languages.xml"
        tree = ET.fromstring(requests.get(url).content)
        name_space = "{info:lc/xmlns/codelist-v1}"
        xpath_expr = "{0}languages/{0}language/{0}code".format(name_space)
        for code in tree.findall(xpath_expr):
            yield code.text

    def get_identifiers(self, marc_record):
        '''Collects Identifiers and adds the appropriate metadata'''
        fields = {'020': next(f['id'] for f
                              in self.folio.identifier_types
                              if f['name'] == 'ISBN'),
                  '022': next(f['id'] for f
                              in self.folio.identifier_types
                              if f['name'] == 'ISSN')}
        for field_tag in fields:
            for field in marc_record.get_fields(field_tag):
                yield {'identifierTypeId': fields[field_tag],
                       'value': field.format_field()}
