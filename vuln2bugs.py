#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Copyright (c) 2015 Mozilla Corporation
# Author: Guillaume Destuynder <gdestuynder@mozilla.com>

# Previous versions of this program handled an older vulnerability
# format (version 1) which was generated by vmintgr. This version has
# been adjusted to handle version 2 only (generated by scanapi).
#
# Sample MozDef vuln data format (version 2) for reference.
#  {
#    "endpoint": "vulnerability",
#    "utctimestamp": "2016-11-22T19:28:17.329005+00:00",
#    "description": "scanapi runscan mozdef emitter",
#    "zone": "scl3",
#    "sourcename": "scanapi",
#    "vulnerabilities": [
#      {
#        "name": "CentOS 7 : kernel (CESA-2016:2098) (Dirty COW)",
#        "vulnerable_packages": [
#          "kernel-3.10.0-327.36.2.el7"
#        ],
#        "output": "\nRemote package installed : kernel-3.10.0-327.36.2.el7\nShould be"
#                      " : kernel-3.10.0-327.36.3.el7\n"
#        "cve": "CVE-2016-5195",
#        "cvss": "7.2",
#        "risk": "high"
#      }
#    ],
#    "version": 2,
#    "scan_start": "2016-11-21T20:26:44+00:00",
#    "asset": {
#      "owner": {
#        "operator": "it",
#        "v2bkey": "it-opsec",
#        "team": "opsec"
#      },
#      "os": "Linux Kernel 3.10.0-327.36.2.el7.x86_64 on CentOS Linux release 7.2.1511 (Core)",
#      "hostname": "hostname.mozilla.com",
#      "ipaddress": "1.2.3.4"
#    },
#    "customendpoint": true,
#    "scan_end": "2016-11-21T20:41:11+00:00"
#  }

from elasticsearch import Elasticsearch
from elasticsearch_dsl import Search, Q
import sys, os
from pyes.es import ES
import pytz
from datetime import datetime
from dateutil.parser import parse
from datetime import timedelta
import hjson as json
from io import StringIO
from collections import Counter
import re
import hashlib
import base64
import socket
import getopt

from bugzilla import *

DEBUG = True

SLADAYS = 90

def debug(msg):
    if DEBUG:
        sys.stderr.write('+++ {}\n'.format(msg))

def toUTC(suspectedDate, localTimeZone=None):
    '''Anything => UTC date. Magic.'''
    if (localTimeZone == None):
        try:
            localTimeZone = '/'.join(os.path.realpath('/etc/localtime').split('/')[-2:])
        except:
            localTimeZone = 'UTC'
    utc = pytz.UTC
    objDate = None
    if (type(suspectedDate) == str):
        objDate = parse(suspectedDate, fuzzy=True)
    elif (type(suspectedDate) == datetime):
        objDate=suspectedDate

    if (objDate.tzinfo is None):
        try:
            objDate=pytz.timezone(localTimeZone).localize(objDate)
        except pytz.exceptions.UnknownTimeZoneError:
            #Meh if all fails, I decide you're UTC!
            objDate=pytz.timezone('UTC').localize(objDate)
        objDate=utc.normalize(objDate)
    else:
        objDate=utc.normalize(objDate)
    if (objDate is not None):
        objDate=utc.normalize(objDate)

    return objDate

def bug_create(config, team, teamcfg, title, body, attachments, whiteboard=True):
    '''This will create a Bugzilla bug using whatever settings you have for a team in 'teamsetup',
    you will pretty much always want whiteboard set to True unless this is a bug that should not
    be managed by vuln2bugs (e.g., filter report bugs)'''
    url = config['bugzilla']['host']
    b = bugzilla.Bugzilla(url=url+'/rest/', api_key=config['bugzilla']['api_key'])

    bug = bugzilla.DotDict()
    bug.component = teamcfg['component']
    bug.product = teamcfg['product']
    bug.version = teamcfg['version']
    bug.status = teamcfg['status']
    bug.summary = title
    bug.groups = teamcfg['groups']
    bug.description = body
    today = toUTC(datetime.now())
    sla = today + timedelta(days=SLADAYS)
    if whiteboard:
        bug.whiteboard = 'autoentry v2b-autoclose v2b-autoremind v2b-duedate={} v2b-key={}'.format(sla.strftime('%Y-%m-%d'), team)
    bug.priority = teamcfg['priority']
    bug.severity = teamcfg['severity']
    bug = b.post_bug(bug)

    for i in attachments:
        b.post_attachment(bug.id, i)

    debug('Created bug {}/{}'.format(url, bug.id))

class VulnProcessor():
    '''The VulnProcessor takes a teamvuln object and extra prettyfi-ed data as strings, lists, etc'''
    def __init__(self, config, teamvulns, team):
        self.teamvulns = teamvulns
        self.config = config
        self.filtered_text_output = ''
        a, b, c, d = self.process_vuln_flatmode(config['teamsetup'][team], teamvulns.assets)
        self.full_text_output = a
        self.short_csv = b
        self.total_affected_hosts = c
        self.filtered_asset_vulns = d
        # Populate filtered_text_output using the data in filtered_asset_vulns
        self.proc_filtered_vulns(config['teamsetup'][team])

    def summarize(self, data, dlen=64):
        '''summarize any string longer than dlen to dlen+ (truncated)'''
        if len(data) > dlen:
            return data[:dlen]+' (truncated)'
        return data

    def get_total_affected_hosts(self):
        return self.total_affected_hosts

    def get_full_text_output(self):
        return self.full_text_output

    def get_short_csv(self):
        return self.short_csv

    def get_filtered_vulns_segment(self):
        return self.filtered_text_output

    def shorten_package(self, pkgname):
        '''Attempt to shorten a package to just the name; uses patterns of some known
        package formats, if pkgname does not match any known formats it will just return
        pkgname'''
        mg = re.match('^(.+?)[.\-_]\d+?[.\-_]?', pkgname)
        if mg == None or len(mg.groups()) != 1:
            return pkgname
        return mg.group(1)

    def filter_exception(self, vulnname, teamname):
        try:
            with open(self.config['filteredreport']['exceptions']) as fd:
                rules = fd.readlines()
                for x in rules:
                    if x[0] == '#':
                        continue
                    args = x.strip().split()
                    if args[0] != teamname and args[0] != '*':
                        continue
                    if re.match(args[1], vulnname) != None:
                        return True
        except IOError:
            pass
        return False

    def proc_filtered_vulns(self, teamconfig):
        '''Create a segment of text suitable to detail the list of vulnerabilities filtered
        from the bug for this team. The format here could be improved. The granularity of
        function input is asset based, but we will collapse all of the data into a unique
        list of vulnerability titles'''
        vlist = []
        self.filtered_text_output += '########## Filtered for {}\n'.format(teamconfig['name'])
        for x in self.filtered_asset_vulns:
            vlist += self.filtered_asset_vulns[x].keys()
        if len(vlist) == 0:
            self.filtered_text_output += 'None\n\n'
        else:
            self.filtered_text_output += '\n'.join(sorted(set(vlist))) + '\n'

    def process_vuln_flatmode(self, teamcfg, assets):
        '''Preparser that could use some refactoring.'''
        textdata = ''
        short_list = ''
        pkg_affected = dict()
        total_affected_hosts = 0
        filtered_asset_vulns = {}

        try:
            mincvss = float(self.config['es'][teamcfg['filter']]['mincvss'])
        except KeyError:
            mincvss = None
        try:
            risklabels = self.config['es'][teamcfg['filter']]['risklabels']
        except KeyError:
            risklabels = None
        # Unroll all vulns
        for assetkey in sorted(assets.keys()):
            assetdata = assets[assetkey]
            impacts = list()
            pkgs = list()
            titles = list()
            cves = list()
            titlelinkmap = dict()

            # vulns_filtered will track any vulnerabilities that are filtered from the
            # output
            vulns_filtered = {}
            # Apply any CVSS filters
            if mincvss != None:
                buf = []
                for x in assetdata['vulnerabilities']:
                    if self.filter_exception(x['name'], teamcfg['name']) or \
                            ('cvss' in x and x['cvss'] != '' and float(x['cvss']) >= mincvss):
                        buf.append(x)
                    else:
                        vulns_filtered[x['name']] = x
                assetdata['vulnerabilities'] = buf
            # Apply any label filters
            if risklabels != None:
                buf = []
                for x in assetdata['vulnerabilities']:
                    if self.filter_exception(x['name'], teamcfg['name']) or \
                            ('risk' in x and x['risk'] in risklabels):
                        buf.append(x)
                    else:
                        vulns_filtered[x['name']] = x
                assetdata['vulnerabilities'] = buf
            filtered_asset_vulns[assetdata['asset']['hostname']] = vulns_filtered

            if len(assetdata['vulnerabilities']) == 0:
                continue
            total_affected_hosts += 1

            for v in assetdata['vulnerabilities']:
                impacts     += [v.risk.upper()]
                titles      += [v.name]
                if len(v.vulnerable_packages) == 0:
                    pkgs += ['some_unknown_packages_see_details']
                else:
                    pkgs += [self.shorten_package(x) for x in v.vulnerable_packages]
                if v.cve != None:
                    cves += [v.cve]
                else:
                    cves += ['CVE-NOTAVAILABLE']
                try:
                    titlelinkmap[v.name] = v.link
                except AttributeError:
                    pass

            # Uniquify
            pkgs    = sorted(set(pkgs))
            impacts = sorted(set(impacts))
            cves    = sorted(set(cves))
            titles  = sorted(set(titles))

            data = """{nr_vulns} vulnerabilities for {hostname} {ipv4}

Impact: {impact}
CVES: {cve}
OS: {osname}
Packages to upgrade: {packages}
Summary:
""".format(hostname     = assetdata.asset.hostname,
                ipv4        = assetdata.asset.ipaddress,
                nr_vulns    = len(assetdata.vulnerabilities),
                impact      = str.join(',', impacts),
                cve         = self.summarize(str.join(',', cves)),
                osname      = assetdata.asset.os,
                packages    = str.join(',', pkgs),
                )
            for v in titles:
                data += '{title}'.format(title=v)
                if v in titlelinkmap:
                    data += ' ({link})\n'.format(link=titlelinkmap[v])
                else:
                    data += '\n'
            data += '\n-----------------------------------------------------\n\n'

            short_list += "{hostname},{ip},{pkg}\n".format(hostname=assetdata.asset.hostname, \
                    ip=assetdata.asset.ipaddress, pkg=str.join(' ', pkgs))
            textdata += data

        return (textdata, short_list, total_affected_hosts, filtered_asset_vulns)

class TeamVulns():
    '''TeamVulns extract the vulnerability data from MozDef and sorts it into clear structures'''
    def __init__(self, config, team):
        self.team = team
        self.config = config
        self.teamconfig = self.config['teamsetup'][team]
        # Get all entries/data from ES/MozDef
        self.raw = self.get_entries()
        # Build a dict with our assets
        self.assets = self.get_assets()

    def nodata(self):
        '''Return true if no data was found for the team (no assets located in query)'''
        if len(self.assets.keys()) == 0:
            return True
        return False

    def get_assets(self):
        '''Returns dict containing each asset and vulns, using hostname and ipaddress as key'''
        assets = dict()
        for i in self.raw:
            if 'deduphostname' in self.teamconfig and self.teamconfig['deduphostname']:
                if i.asset.hostname in [x.split('|')[1] for x in assets.keys()]:
                    continue
            key = i.asset.ipaddress + "|" + i.asset.hostname
            if key in assets:
                raise Exception('duplicate ipaddress|hostname value in asset results')
            assets[key] = i

        return assets

    def get_entries(self):
        '''Get all entries for a team + their filter from ES/MozDef'''
        teamfilter = self.config['teamsetup'][self.team]['filter']
        es = Elasticsearch([{'host': self.config['mozdef']['host'], 'port': self.config['mozdef']['port']}])

        # Default filter - time period
        try:
            td = self.config['es'][teamfilter]['_time_period']
        except KeyError:
            debug('No _time_period defined, defaulting to 24h')
            td = 24
        begindateUTC = toUTC(datetime.now() - timedelta(hours=td))
        enddateUTC= toUTC(datetime.now())
        print begindateUTC, enddateUTC
        range_query = Q('range', **{'utctimestamp': {'gte': begindateUTC, 'lte': enddateUTC}})

        # Setup team query based on our JSON configuration
        musts = []
        musts.append(range_query)
        musts.append(Q('query_string', query='asset.owner.v2bkey: "{}"'.format(self.team)))
        musts.append(Q('match', sourcename=self.config['es'][teamfilter]['sourcename']))
        must_nots = []
        shoulds = []

        query = Q('bool', must=musts, must_not=must_nots, should=shoulds)
        # XXX esdsl appears to limit queries to a maximum of 10000 results; just hardcode the max
        # here for now but this should probably be modified to use some sort of scroll cursor.
        results = Search(using=es, index=self.config['es']['index']).params(size=10000,request_timeout=180).filter(query).execute()

        if results._shards.failed != 0:
            raise Exception("Some shards failed! {0}".format(raw._shards.__str__()))

        # Nobody cares for the metadata past this point (all the goodies are in 'hits')
        return results.hits

def create_filtered_bug(config, filterconfig, attach):
    '''Creates a bug listing all vulnerabilities filtered from vuln2bugs reports for any
    teams with filter reporting enabled. Note that vuln2bugs does not manage this bug and
    it needs to be closed manually once reviewed.'''
    if filterconfig['weeklyrun'] != toUTC(datetime.now()).weekday():
        return
    debug('Creating filter report bug')
    ba = [bugzilla.DotDict()]
    ba[0].file_name = 'filtered_vulns.txt'
    ba[0].summary = 'List of vulnerabilities filtered for teams with filter reporting enabled'
    ba[0].data = '\n'.join(attach)
    today = toUTC(datetime.now())

    bug_body = 'Infosec vuln2bugs filtered vulnerabilities report\n\n'
    bug_body += 'Vulnerabilities filtered by auto-triage policies are detailed in the attachment\n'
    bug_body += 'for review. An exception should be included in vuln2bugs configuration for any\n'
    bug_body += 'vulnerabilities listed here that should be auto-triaged.\n\n'
    bug_body += 'Note this bug only contains filtered issues for teams that have filter reporting\n'
    bug_body += 'enabled. This bug can be resolved once review is complete.\n'

    bug_title = 'vuln2bugs auto-triage filter report'
    bug_create(config, None, filterconfig, bug_title, bug_body, ba, whiteboard=False)

def bug_type_flat(config, team, teamvulns, processor):
    teamcfg = config['teamsetup'][team]

    full_text = processor.get_full_text_output()
    short_csv = processor.get_short_csv()
    vulns_len = processor.get_total_affected_hosts()

    # Attachments
    ba = [bugzilla.DotDict(), bugzilla.DotDict()]
    ba[0].file_name = 'short_list.csv'
    ba[0].summary = 'CSV list of affected ip,hostname,package(s)'
    ba[0].data = short_csv
    ba[1].file_name = 'detailled_list.txt'
    ba[1].summary = 'Details including CVEs, OS, etc. affected'
    ba[1].data = full_text

    today = toUTC(datetime.now())
    sla = today + timedelta(days=SLADAYS)

    bug_body = 'Infosec vuln2bugs auto-triage for {}\n\n'.format(team)
    bug_body += 'A number of hosts belonging to {} have been identified as requiring patches.\n'.format(team)
    bug_body += 'Expected time to patch is within 90 days unless otherwise indicated by other\n'
    bug_body += 'bugs. See the attachments for details, attachments are updated based on current\n'
    bug_body += 'state each time vuln2bugs runs.\n'
    bug_body += "\nFor additional details, queries, etc. see also {}".format(config['mozdef']['dashboard_url'])
    #bug_body += "\n\nCurrent ownership mapping for all known hosts can be obtained from {}".format(config['eisowners'])    #commented out because this function is deprecated
    bug_body += "\n\nEscalation process details can be obtained from {}".format(config['doclink'])

    # Only make a new bug if no old one exists
    bug_title = "[{} hosts] Bulk vulnerability report for {} using filter: {}".format(
                vulns_len, teamcfg['name'], teamcfg['filter'])
    bug = find_latest_open_bug(config, team)
    if ((bug == None) and (vulns_len > 0)):
        bug_create(config, team, teamcfg, bug_title, bug_body, ba)
    else:
        #No more vulnerablities? Woot! Close the bug!
        if (vulns_len == 0):
            close = True
            if (bug == None or len(bug) == 0):
                debug('No vulnerabilities found for {}, no previous bug found, nothing to do!'.format(team))
                return
        else:
            close = False
        update_bug(config, teamcfg, bug_title, bug_body, ba, bug, close)

def find_latest_open_bug(config, team):
    url = config['bugzilla']['host']
    b = bugzilla.Bugzilla(url=url+'/rest/', api_key=config['bugzilla']['api_key'])
    teamcfg = config['teamsetup'][team]

    terms = [{'product': teamcfg['product']}, {'component': teamcfg['component']},
            {'creator': config['bugzilla']['creator']}, {'whiteboard': 'autoentry'}, {'resolution': ''},
            {'status': 'NEW'}, {'status': 'ASSIGNED'}, {'status': 'REOPENED'}, {'status': 'UNCONFIRMED'},
            {'whiteboard': 'v2b-key={}'.format(team)}]
    bugs = b.search_bugs(terms)['bugs']
    #Newest only
    try:
        return bugzilla.DotDict(bugs[-1])
    except IndexError:
        return None

def khash(data):
    '''Single place to change hashes of attachments'''
    newdata = data
    #TODO to improve this cruft we need to store all attachment data in real structures.
    must_start_with = None
    if (data.find('Packages to upgrade') != -1):
        must_start_with = 'Packages to upgrade'

    if must_start_with != None:
        #Remove generic data that we don't care for while comparing like dates so these don't get checksummed.
        #So we just get Packages:
        datalist = newdata.split('\n')
        newdata = ''
        for i in datalist:
            if i.startswith(must_start_with):
                newdata += i+"\n"

    return hashlib.sha256(newdata.encode('ascii')).hexdigest()

def set_needinfo(b, bug, user):
    '''Check if needinfo is set for the user, and set it if not set.
    Returns True only when needinfo is actually being set by this function.'''
    for f in bug.flags:
        try:
            if (f['requestee'] == user) and (f['setter'] == bug.creator) and (f['name'] == 'needinfo'):
                debug("Bug {} already has need info set for {}".format(bug.id, user))
                return False
        #Some flags don't have these fields, we skip 'em
        except KeyError:
            continue

    bug_update = bugzilla.DotDict()
    bug_update.flags = [{'type_id': 800, 'name': 'needinfo', 'status': '?', 'new': True, 'requestee': user}]
    #Needinfo may fail if the user is set to deny them.
    try:
        b.put_bug(bug.id, bug_update)
        return True
    except Exception as e:
        debug("Exception occured while setting NEEDINFO: {}".format(str(e)))
        return False


def update_bug(config, teamcfg, title, body, attachments, bug, close):
    '''This will update any open bug with correct attributes.
    This check attachments instead of a control hash since it's needed for attachment obsolescence.. its also neat
    anyway.'''
    #Safety stuff - never edit bugs that aren't ours
    #These asserts should normally never trigger
    assert bug.creator == config['bugzilla']['creator']
    assert bug.whiteboard.find('autoentry') != -1

    any_update = False

    url = config['bugzilla']['host']
    b = bugzilla.Bugzilla(url=url+'/rest/', api_key=config['bugzilla']['api_key'])
    debug('Checking for updates on {}/{}'.format(url, bug.id))

    #Check if we have to close this bug first (i.e. job's done, hurrai!)
    if (bug.whiteboard.find('v2b-autoclose') != -1):
        if (close):
            bug_update = bugzilla.DotDict()
            bug_update.resolution = 'fixed'
            bug_update.status = 'resolved'
            b.put_bug(bug.id, bug_update)
            return

    # Due date checks
    today = toUTC(datetime.now())
    try:
        h = bug.whiteboard.split('v2b-duedate=')[1]
        due = h.split(' ')[0]
        debug('Bug completion is due by {}'.format(due))
    except IndexError:
        due_dt = today
        debug('No due date found in whiteboard tag, hmm, maybe someone removed it')
    else:
        try:
            due_dt = toUTC(datetime.strptime(due, "%Y-%m-%d"))
        except ValueError:
            debug('Due date found in whiteboard tag seems invalid, resetting to today')
            due_dt = today

    new_hashes = {}
    for a in attachments:
        new_hashes[khash(a.data)] = a

    old_hashes = {}
    for a in b.get_attachments(bug.id)[str(bug.id)]:
        a = bugzilla.DotDict(a)
        if a.is_obsolete: continue
        a.data = base64.standard_b64decode(a.data).decode('ascii', 'ignore')
        old_hashes[khash(a.data)] = a

    for h in new_hashes:
        if (h in old_hashes): continue
        a = new_hashes[h]
        for i in old_hashes:
            old_a = old_hashes[i]
            if (old_a.file_name == a.file_name):
                # setting obsolete attachments during the new attachment post does not actually work in the API
                # So we update the old attachment to set it obsolete meanwhile
                a.obsoletes = [old_a.id]
                tmp = bugzilla.DotDict()
                tmp.is_obsolete = True
                tmp.file_name = old_a.file_name
                b.put_attachment(old_a.id, tmp)
        b.post_attachment(bug.id, a)
        any_update = True

    if (any_update):
        #Summary/title update
        bug_update = bugzilla.DotDict()
        bug_update.summary = title
        b.put_bug(bug.id, bug_update)

        debug('Updated bug {}/{}'.format(url, bug.id))

    #Do we need to autoremind?
    elif (bug.whiteboard.find('v2b-autoremind') != -1):
        if (due_dt < today):
            if (set_needinfo(b, bug, bug.assigned_to)):
                bug_update = bugzilla.DotDict()
                b.post_comment(bug.id, 'Bug is past due date (out of SLA - was due for {due}, we are {today}).'.format(
                        due=due_dt.strftime('%Y-%m-%d'), today=today.strftime('%Y-%m-%d')))
                b.put_bug(bug.id, bug_update)

def usage():
    sys.stdout.write('usage: {} [-h] [-t team]\n'.format(sys.argv[0]))

def main():
    debug('Debug mode on')

    singleteam = None
    try:
        optlist, args = getopt.getopt(sys.argv[1:], 'ht:')
    except getopt.GetoptError as err:
        sys.stderr.write(str(err) + '\n')
        usage()
        sys.exit(1)
    for o, a in optlist:
        if o == '-h':
            usage()
            sys.exit(0)
        elif o == '-t':
            singleteam = a

    with open('vuln2bugs.json') as fd:
        config = json.load(fd)

    teams = config['teamsetup']

    try:
        filteredreport = config['filteredreport']
    except KeyError:
        filteredreport = None
    filteredattachment = []

    # Note that the pyes library returns DotDicts which are addressable like mydict['hi'] and mydict.hi
    for team in teams:
        if singleteam != None and team != singleteam:
            continue
        if 'name' not in teams[team]:
            teams[team]['name'] = team
        debug('Processing team: {} using filter {}'.format(team, teams[team]['filter']))
        teamvulns = TeamVulns(config, team)
        if teamvulns.nodata():
            debug('no asset data found! not performing any action for this team')
            continue
        processor = VulnProcessor(config, teamvulns, team)
        debug('{} assets affected by vulnerabilities with the selected filter.'.format(processor.get_total_affected_hosts()))
        if filteredreport != None:
            if 'reportfiltered' in teams[team] and teams[team]['reportfiltered']:
                filteredattachment.append(processor.get_filtered_vulns_segment())
        bug_type_flat(config, team, teamvulns, processor)
    if filteredreport != None and len(filteredattachment) != 0:
        create_filtered_bug(config, filteredreport, filteredattachment)

if __name__ == "__main__":
    main()
