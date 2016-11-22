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

import pyes
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

def bug_create(config, team, teamcfg, title, body, attachments):
    '''This will create a Bugzilla bug using whatever settings you have for a team in 'teamsetup' '''
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
        a, b, c, d, e = self.process_vuln_flatmode(config['teamsetup'][team], teamvulns.assets,
            teamvulns.vulnerabilities_per_asset, teamvulns.services_per_asset)
        self.full_text_output = a
        self.short_csv = b
        self.affected_packages_list = c
        self.oldest = d
        self.withservices_csv = e

    def summarize(self, data, dlen=64):
        '''summarize any string longer than dlen to dlen+ (truncated)'''
        if len(data) > dlen:
            return data[:dlen]+' (truncated)'
        return data

    def get_full_text_output(self):
        return self.full_text_output

    def get_short_csv(self):
        return self.short_csv

    def get_withservices_csv(self):
        return self.withservices_csv

    def get_affected_packages_list(self):
        return self.affected_packages_list

    def get_oldest(self):
        return self.oldest

    def process_vuln_flatmode(self, teamcfg, assets, vulns, services):
        '''Preparser that could use some refactoring.'''
        textdata = ''
        short_list = ''
        withservices_list = ''
        pkg_affected = dict()
        oldest_all = 0
        oldest = 0

        # See if we want to incorporate service information
        includeservices = False
        if 'includeservices' in teamcfg and teamcfg['includeservices']:
            includeservices = True

        # Include a header with the services list
        withservices_list += '# hostname,ip,techowner,requirestcw,packages...\n'

        # Unroll all vulns
        for a in assets:
            risks = list()
            proofs = list()
            titles = list()
            ages = list()
            patch_in = list()
            cves = list()
            requirestcw = None
            techowner = None
            for v in vulns[a.assetid]:
                risks       += [v.impact_label.upper()]
                proofs      += [v.proof]
                titles      += [v.title]
                ages        += [v.age_days]
                cves        += v.cves
                patch_in    += [v.patch_in]
                # If includeservices is set, if any service information is found in the vulnerability
                # list we will use that data in the service output attachment
                if includeservices:
                    requirestcw = 'na'
                    techowner = 'na'
                    serviceent = services[a.assetid]
                    if serviceent != None:
                        if serviceent.techowner != None:
                            techowner = serviceent.techowner
                        if serviceent.tcw != None:
                            requirestcw = serviceent.tcw

            #pkg_vuln = Counter(proofs).most_common()
            pkgs = list()
            pkg_parsed = True
            pkg_ver = dict()
            for i in proofs:
                p = self.parse_proof(i)
                pname = p['pkg']
                pver = p['version']
                if p == None:
                    pkg_parsed = False
                    pkgs += [i]
                    pkg_affected[i] = 'Unknown'
                else:
                    pkg_ver[pname] = pver
                    pkgs += [pname]
                    try:
                        pkg_affected[pname] += [pver]
                        pkg_affected[pname] = list(set(pkg_affected[pname]))
                    except KeyError:
                        pkg_affected[pname] = [pver]

            # Uniquify
            pkgs    = sorted(set(pkgs))
            risks   = sorted(set(risks))
            cves    = sorted(set(cves))

            if pkg_parsed:
                pkgs_pretty = list()
                for i in pkgs:
                    pkgs_pretty += ['{} (affected version {})'.format(i, self.summarize(pkg_ver[i]))]
            else:
                pkgs_pretty = pkgs

            # What's the oldest vuln found in this asset?
            oldest = 0

            for i in ages:
                if i > oldest:
                    oldest = i

            data = """{nr_vulns} vulnerabilities for {hostname} {ipv4}

Risk: {risk} - oldest vulnerability has been seen on these systems {age} day(s) ago at the time of report generation.
CVES: {cve}.
OS: {osname}
Packages to upgrade: {packages}
-------------------------------------------------------------------------------------

""".format(hostname     = a.hostname,
                ipv4        = a.ipv4address,
                nr_vulns    = len(vulns[a.assetid]),
                risk        = str.join(',', risks),
                age         = oldest,
                cve         = self.summarize(str.join(',', cves)),
                osname      = a.os,
                packages    = str.join(',', pkgs_pretty),
                )

            short_list += "{hostname},{ip},{pkg}\n".format(hostname=a.hostname, ip=a.ipv4address, pkg=str.join(' ', pkgs))
            # If includeservices is set, build an additional attachment that includes this information
            if includeservices:
                withservices_list += "{hostname},{ip},{techowner},{requirestcw},{pkg}\n".format(hostname=a.hostname, ip=a.ipv4address, techowner=techowner, requirestcw=requirestcw, pkg=str.join(' ',pkgs))
            textdata += data
            if oldest > oldest_all:
                oldest_all = oldest

        return (textdata, short_list, pkg_affected, oldest_all, withservices_list)

    def parse_proof(self, proof):
        '''Attempt to detect the way the proof field has been formatted, and
        hand the data off to a suitable parser
        '''
        # Just ignore the Windows proofs
        if re.match('.*Vulnerable OS: Microsoft Windows.*', proof) or re.match('.*HKEY_LOCAL_MACHINE.*', proof):
            return self.parse_proof_method_windows(proof)
        if re.match('.+\d+Vulnerable software installed.+', proof):
            return self.parse_proof_method_usn(proof)
        elif re.match('^Vulnerable software installed:.+', proof):
            return self.parse_proof_method_swonly(proof)
        # Fallback to the most common method
        return self.parse_proof_method_rhsa(proof)

    def parse_proof_method_windows(self, proof):
        '''Attempt to parse proofs as are returned from Windows systems. These can vary
        a great deal, so best effort here.
        '''
        ret = {'pkg': 'No package name provided', 'os': 'No OS name provided', 'version': 'No version provided'}
        # First try to extract the impacted software
        mtchresw = re.compile('.*Vulnerable software installed: (.+?)(Vulnerable OS|\*|Based|$)')
        results = mtchresw.search(proof)
        if results != None:
            ret['pkg'] = results.group(1).strip()
        # Next try to get the OS
        mtchreos = re.compile('.*Vulnerable OS: (.+?)(Vulnerable software|Based|$)')
        results = mtchreos.search(proof)
        if results != None:
            ret['os'] = results.group(1).strip()
        # Lastly try to get the version
        mtchrever = re.compile('affected version - (.+?)\*')
        results = mtchrever.search(proof)
        if results != None:
            ret['version'] = results.group(1).strip()
        else:
            # It's possible the version is tacked onto the extracted package name, if we have something
            # that looks like a version string here use that
            results = re.search('(\d+\.[\d.]+)', ret['pkg'])
            if results != None:
                ret['version'] = results.group(1)
        return ret

    def parse_proof_method_swonly(self, proof):
        '''Finds a package name, os, etc. in a proof-style (nexpose) string, such as:
        Vulnerable software installed: HP Device Control 09.10.00.00

        Returns a dict = {'pkg': 'package name', 'os': 'os name', 'version': 'installed version'}
        or None if parsing failed.
        '''
        osname = ''
        pkg = ''
        version = ''

        try:
            tmp = proof.split('Vulnerable software installed: ')[1].split()
            version = tmp[-1]
            pkg = ' '.join(tmp[:-1])
            os = 'Undefined OS'
        except:
            return {'pkg': 'No package name provided', 'os': 'No OS name provided', 'version': 'No version provided'}

        return {'pkg': pkg, 'os': osname, 'version': version}

    def parse_proof_method_usn(self, proof):
        '''Finds a package name, os, etc. in a proof-style (nexpose) string, such as:
        Vulnerable OS: Ubuntu Linux 12.04Vulnerable software installed: Ubuntu tcpdump 4.2.1-1ubuntu2

        Returns a dict = {'pkg': 'package name', 'os': 'os name', 'version': 'installed version'}
        or None if parsing failed.
        '''
        osname = ''
        pkg = ''
        version = ''

        try:
            tmp = proof.split('Vulnerable software installed: ')
            os = tmp[0].split('Vulnerable OS: ')[1]
            tmp = tmp[1].split(' ')
            pkg = tmp[-2]
            version = tmp[-1]
        except:
            return {'pkg': 'No package name provided', 'os': 'No OS name provided', 'version': 'No version provided'}

        return {'pkg': pkg, 'os': osname, 'version': version}
        
    def parse_proof_method_rhsa(self, proof):
        '''Finds a package name, os, etc. in a proof-style (nexpose) string, such as:
        Vulnerable OS: Red Hat Enterprise Linux 5.5 * krb5-libs - version 1.6.1-55.el5_6.1 is installed

        Returns a dict = {'pkg': 'package name', 'os': 'os name', 'version': 'installed version'}
        or None if parsing failed.
        '''
        osname = ''
        pkg = ''
        version = ''

        try:
            tmp = proof.split('Vulnerable OS: ')[1]
            tmp = tmp.split('*')
            osname = tmp[0].strip()
            # spaces matter in the split - as package names never contain spaces, but may contain dashes
            tmp = tmp[1].split(' - ')
            pkg = tmp[0].lstrip().strip()
            tmp = str.join('', tmp[1:]).split('version ')[1]
            version = tmp.split(' is installed')[0]
        except:
            return {'pkg': 'No package name provided', 'os': 'No OS name provided', 'version': 'No version provided'}

        return {'pkg': pkg, 'os': osname, 'version': version}

class TeamVulns():
    '''TeamVulns extract the vulnerability data from MozDef and sorts it into clear structures'''
    def __init__(self, config, team):
        self.team = team
        self.config = config
        # Get all entries/data from ES/MozDef
        self.raw = self.get_entries()
        # Build a dict with our assets
        self.assets = self.get_assets()

    def get_assets(self):
        '''Returns dict containing each asset and vulns, using ipaddress as key'''
        assets = dict()
        for i in self.raw:
            if i.asset.ipaddress in assets:
                raise Exception('duplicate ipaddress value in asset results')
            assets[i.asset.ipaddress] = i

        return assets

    def get_entries(self):
        '''Get all entries for a team + their filter from ES/MozDef'''
        teamfilter = self.config['teamsetup'][self.team]['filter']
        es = ES((self.config['mozdef']['proto'], self.config['mozdef']['host'], self.config['mozdef']['port']))

        # Default filter - time period
        try:
            td = self.config['es'][teamfilter]['_time_period']
        except KeyError:
            debug('No _time_period defined, defaulting to 24h')
            td = 24
        begindateUTC = toUTC(datetime.now() - timedelta(hours=td))
        enddateUTC= toUTC(datetime.now())
        print begindateUTC, enddateUTC
        fDate = pyes.RangeQuery(qrange=pyes.ESRange('utctimestamp', from_value=begindateUTC, to_value=enddateUTC))

        # Setup team query based on our JSON configuration
        query = pyes.query.BoolQuery()
        query.add_must(pyes.QueryStringQuery('asset.owner.v2bkey: "{}"'.format(self.team)))
        # sourcename is a required field
        if 'sourcename' not in self.config['es'][teamfilter]:
            raise Exception('sourcename not present in filter')
        query.add_must(pyes.MatchQuery('sourcename', self.config['es'][teamfilter]['sourcename']))

        q = pyes.ConstantScoreQuery(query)
        q = pyes.FilteredQuery(q, pyes.BoolFilter(must=[fDate]))

        results = es.search(query=q, indices=self.config['es']['index'])

        raw = results._search_raw(0, results.count())
        # This doesn't do much, but pyes has no "close()" or similar functionality.
        es.force_bulk()

        if (raw._shards.failed != 0):
            raise Exception("Some shards failed! {0}".format(raw._shards.__str__()))

        # Nobody cares for the metadata past this point (all the goodies are in '_source')
        data = []
        for i in raw.hits.hits:
            data += [i._source]
        return data


def bug_type_flat(config, team, teamvulns, processor):
    teamcfg = config['teamsetup'][team]

    full_text = processor.get_full_text_output()
    short_csv = processor.get_short_csv()
    withservices_csv = processor.get_withservices_csv()
    pkgs = processor.get_affected_packages_list()
    oldest = processor.get_oldest()
    vulns_len = len(teamvulns.assets)

    # Attachments
    ba = [bugzilla.DotDict(), bugzilla.DotDict()]
    ba[0].file_name = 'short_list.csv'
    ba[0].summary = 'CSV list of affected ip,hostname,package(s)'
    ba[0].data = short_csv
    ba[1].file_name = 'detailled_list.txt'
    ba[1].summary = 'Details including CVEs, OS, etc. affected'
    ba[1].data = full_text
    if 'includeservices' in teamcfg and teamcfg['includeservices']:
        ba.append(bugzilla.DotDict())
        # Include the services related attachment
        ba[2].file_name = 'extended_list.txt'
        ba[2].summary = 'CSV list using service information'
        ba[2].data = withservices_csv

    today = toUTC(datetime.now())
    sla = today + timedelta(days=SLADAYS)

    bug_body = "{} hosts affected by filter {}\n".format(vulns_len, teamcfg['filter'])
    bug_body += "At time of report, the oldest vulnerability is {age} day(s) old.\n".format(age=oldest)
    bug_body += "Expected time to patch: {} days, before {sla}.\n\n".format(SLADAYS, sla=sla.strftime('%Y-%m-%d'))
    bug_body += "({}) Packages affected:\n".format(len(pkgs))
    for i in pkgs:
        bug_body += "{name}: {version}\n".format(name=i, version=','.join(pkgs[i]))
    bug_body += "\n\nFor additional details, queries, graphs, etc. see also {}".format(config['mozdef']['dashboard_url'])
    bug_body += "\n\nCurrent ownership mapping for all known hosts can be obtained from {}".format(config['eisowners'])
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

    # Note that the pyes library returns DotDicts which are addressable like mydict['hi'] and mydict.hi
    for team in teams:
        if singleteam != None and team != singleteam:
            continue
        if 'name' not in teams[team]:
            teams[team]['name'] = team
        debug('Processing team: {} using filter {}'.format(team, teams[team]['filter']))
        teamvulns = TeamVulns(config, team)
        processor = VulnProcessor(config, teamvulns, team)
        debug('{} assets affected by vulnerabilities with the selected filter.'.format(len(teamvulns.assets)))
        bug_type_flat(config, team, teamvulns, processor)

if __name__ == "__main__":
    main()
