import csv, sys
import itertools
from os import environ
from os.path import join
import boto
from boto.route53.record import Record, ResourceRecordSets

class ComparableRecord(object):
    def __init__(self, obj):
        for k,v in obj.__dict__.items():
            self.__dict__[k] = v

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __hash__(self):
        it = (self.name, self.type, self.alias_hosted_zone_id,
              self.alias_dns_name, tuple(sorted(self.resource_records)),
              self.ttl, self.region, self.weight, self.identifier)
        return it.__hash__()

    def to_change_dict(self):
        data = {}
        for k,v in self.__dict__.items():
            if k == 'resource_records':
                continue
            else:
                data[k] = v
        return data

def exit_with_error(error):
    sys.stderr.write(error)
    sys.exit(1)


def get_zone(con, zone_name):
    zone_response = con.get_hosted_zone_by_name(zone_name)

    if zone_response:
        data = {}
        data['id'] = zone_response['GetHostedZoneResponse']['HostedZone']['Id'].replace('/hostedzone/','')
        data['name'] = zone_response['GetHostedZoneResponse']['HostedZone']['Name']
        if 'DelegationSet' in zone_response['GetHostedZoneResponse']:
            data['ns'] = zone_response['GetHostedZoneResponse']['DelegationSet']['NameServers']
        return data
    else:
        return None

def get_or_create_zone(con, zone_name):
    zone = get_zone(con, zone_name)
    if not zone:
        con.create_hosted_zone(zone_name)
    return get_zone(con, zone_name)

def group_values(lines):
    records = []
    for _, records in itertools.groupby(lines, lambda row: row[0:2]):
        for __, by_value in itertools.groupby(records, lambda row: row[-3:]):
            recs = list(by_value) # consume the iterator so we can grab positionally
            first = recs[0]

            record = Record()
            record.name = first[0]
            record.type = first[1]
            if first[2].startswith('ALIAS'):
                _, alias_hosted_zone_id, alias_dns_name = first[2].split(':')
                record.alias_hosted_zone_id = alias_hosted_zone_id
                record.alias_dns_name = alias_dns_name
            else:
                record.resource_records = [r[2] for r in recs]
                record.ttl = first[3]
            record.region = first[4] or None
            record.weight = first[5] or None
            record.identifier = first[6] or None

            yield record

def read_lines(file_in):
    reader = csv.reader(file_in)
    lines = list(reader)
    if lines[0][0] == 'NAME':
        lines = lines[1:]
    return lines

def read_records(file_in):
    return list(group_values(read_lines(file_in)))

def skip_apex_soa_ns(zone, records):
    for record in records:
        if record.name == zone['name'] and record.type in ['SOA', 'NS']:
            continue
        else:
            yield record

def comparable(records):
    return {ComparableRecord(record) for record in records}

def get_file(filename, mode):
    ''' Get a file-like object for a filename and mode.
    
        If filename is "-" return one of stdin or stdout.
    '''
    if filename == '-':
        if mode.startswith('r'):
            return sys.stdin
        elif mode.startswith('w'):
            return sys.stdout
        else:
            raise ValueError('Unknown mode "{}"'.format(mode))
    else:
        return open(filename, mode)

def load(con, zone_name, file_in):
    ''' Send DNS records from input file to Route 53.
    
        Arguments are Route53 connection, zone name, and file open for reading.
    '''
    zone = get_or_create_zone(con, zone_name)

    existing_records = comparable(skip_apex_soa_ns(zone, con.get_all_rrsets(zone['id'])))
    desired_records = comparable(skip_apex_soa_ns(zone, read_records(file_in)))

    to_delete = existing_records.difference(desired_records)
    to_add = desired_records.difference(existing_records)

    if to_add or to_delete:
        changes = ResourceRecordSets(con, zone['id'])
        for record in to_delete:
            change = changes.add_change('DELETE', **record.to_change_dict())
            print "DELETE", record.name, record.type
            for value in record.resource_records:
                change.add_value(value)
        for record in to_add:
            change = changes.add_change('CREATE', **record.to_change_dict())
            print "CREATE", record.name, record.type
            for value in record.resource_records:
                change.add_value(value)

        print "Applying changes..."
        changes.commit()
        print "Done."
    else:
        print "No changes."


def dump(con, zone_name, fout):
    ''' Receive DNS records from Route 53 to output file.
    
        Arguments are Route53 connection, zone name, and file open for writing.
    '''
    zone = get_zone(con, zone_name)
    if not zone:
        exit_with_error("ERROR: Zone <" + zone_name + "> not found!")

    out = csv.writer(fout)
    out.writerow(['NAME','TYPE','VALUE','TTL','REGION','WEIGHT','SETID'])

    records = list(con.get_all_rrsets(zone['id']))
    for r in records:
        if r.alias_dns_name:
            vals = [':'.join(['ALIAS', r.alias_hosted_zone_id, r.alias_dns_name])]
        else:
            vals = r.resource_records
        for val in vals:
            out.writerow([r.name, r.type, val, r.ttl, r.region, r.weight, r.identifier])
    fout.flush()

def run(params):
    con = boto.connect_route53()
    zone_name = params['<zone>']
    filename = params['<file>']

    if params['dump']:
        dump(con, zone_name, get_file(filename, 'w'))
    elif params['load']:
        load(con, zone_name, get_file(filename, 'r'))
    else:
        return 1
