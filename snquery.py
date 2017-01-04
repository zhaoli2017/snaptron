#!/usr/bin/env python2.7
import sys
import re
import subprocess
import shlex
import snapconf
import snapconfshared 
import snaputil

#for speedy multi-sample ID searching per jx
import ahocorasick

RegionArgs = snapconfshared.RegionArgs
default_region_args = snapconfshared.default_region_args

EITHER_START=snapconfshared.EITHER_START
EITHER_END=snapconfshared.EITHER_END
return_formats = snaputil.return_formats

#return formats:
TSV=snapconfshared.TSV
UCSC_BED=snapconfshared.UCSC_BED
UCSC_URL=snapconfshared.UCSC_URL

def build_sid_ahoc_queries(sample_ids):
    acs = ahocorasick.Automaton()
    #need to add "," and ":" to make sure we dont match coverage counts or within other IDs
    [acs.add_word(","+sid+":", sid) for sid in sample_ids]
    acs.make_automaton()
    return acs

class RunExternalQueryEngine:

    def __init__(self,cmd,qargs,rangeq,snaptron_ids,region_args=default_region_args,run_in_shell=False):
        self.cmd = cmd
        self.qargs = qargs
        self.ra = region_args
        self.snaptron_ids = snaptron_ids
        #this trumps whatever stream_back instructions we were given
        if self.ra.result_count:
            self.ra = ra._replace(stream_back=False)
            self.ra = ra._replace(save_introns=True)

        self.filter_by_introns = (self.ra.intron_filter != None and len(self.ra.intron_filter) > 0)
        self.filter_by_samples = (self.ra.sample_filter != None and len(self.ra.sample_filter) > 0)

        (header_method,streamer_method) = return_formats[self.ra.return_format]
        self.header = header_method(sys.stdout,region_args=self.ra,interval=self.qargs)
        self.streamer_method = streamer_method
        #exit early as we only want the ucsc_url
        if self.ra.return_format == UCSC_URL:
            return
            
        self.range_filters = self.ra.range_filters if self.ra.range_filters is not None and len(self.ra.range_filters) > 0 else None
        if cmd == snapconf.SQLITE:
            self.delim = '\t'
            arguments = []
            where = []
            (chrom,start,end) = snaputil.sqlite3_interval_query_parse(self.qargs,where,arguments,self.ra)
            self.chrom = chrom
            self.start = start
            self.end = end
            snaputil.sqlite3_range_query_parse(rangeq,where,arguments)
            fields_to_select = self.ra.sqlite_db_fields
            #force sqlite3 to 3 decimal places
            if fields_to_select is None:
                select_fields = snapconf.INTRON_HEADER_FIELDS
                select_fields[snapconf.CHROM_COL]='chrom'
                select_fields[snapconf.DONOR_COL]='donor'
                select_fields[snapconf.ACCEPTOR_COL]='acceptor'
                select_fields[snapconf.COV_AVG_COL]="printf('%.3f',coverage_avg)"
                select_fields[snapconf.COV_MED_COL]="printf('%.3f',coverage_median)"
                fields_to_select = ",".join(select_fields)
            select = "SELECT %s from %s WHERE %s" % (fields_to_select, self.ra.sqlite_db_table, ' AND '.join(where))
            if self.ra.debug:
                sys.stderr.write("%s\t%s\n" % (select,arguments))
            query_ = select
            chr_patt = re.compile('(chr)|[+-]')
            for (i,arg_) in enumerate(arguments):
                arg_ = str(arg_)
                if chr_patt.search(arg_):
                    query_ = re.sub('\?',"\'%s\'" % arg_,query_,count=1)
                else:
                    query_ = re.sub('\?',arg_,query_,count=1)
            additional_cmd = ""
            if len(self.ra.additional_cmd) > 0:
                additional_cmd = " | %s" % (self.ra.additional_cmd)
            full_cmd_args = [self.cmd, '-separator \'	\'', self.ra.sqlite_db_file, '"%s"' % query_, additional_cmd]
            self.full_cmd = " ".join(full_cmd_args)
            cmd_to_run = shlex.split(self.full_cmd)
            #it's an error to submit an additional command but not to set run_in_shell=True
            if run_in_shell:
                cmd_to_run = self.full_cmd
            sys.stderr.write("%s\n" % (self.full_cmd))
            self.extern_proc = subprocess.Popen(cmd_to_run, stdout=subprocess.PIPE, shell=run_in_shell, bufsize=-1)


    def run_query(self):
        ids_found = set()
        sample_set = set()
        #exit early as we only want the ucsc_url
        if self.ra.return_format == UCSC_URL:
            return (ids_found,sample_set)
        for line in self.extern_proc.stdout:
            fields = line.rstrip().split(self.delim)
            snaptron_id = str(fields[snapconf.INTRON_ID_COL])
            lstart = int(fields[self.ra.region_start_col])
            lend = int(fields[self.ra.region_end_col])
            #first attempt to filter by violation of containment (if in effect)
            if self.ra.exact and (lstart != self.start or lend != self.end):
                continue
            #2nd attempt to filter by violation of containment (if in effect)
            if self.ra.contains and (lstart < self.start or lend > self.end):
                continue
            #third attempt to filter by violation of within one end or the other (if in effect)
            if (self.ra.either == EITHER_START and lstart < self.start) or (self.ra.either == EITHER_END and lend > self.end):
                continue
            #now filter, this order is important (filter first, than save ids/print)
            if self.filter_by_introns and snaptron_id not in self.ra.intron_filter and snaptron_id not in self.snaptron_ids:
                continue
            #filter by M (sample IDs), recalculate summaries from subset of samples, and update fields
            samples_found_iter = None
            if self.ra.sid_search_object is not None:
                samples_found_iter = self.ra.sid_search_object.iter(fields[snapconf.SAMPLE_IDS_COL])
                #check to see if this jx has one or more of the sample IDs
                (found_np, fields) = snaputil.extract_sids_and_covs_from_search_iter(samples_found_iter, fields)
                if fields is None:
                    continue
            #print fields
            #not used unless testing Tabix or doing a F + M query
            if (self.cmd == snapconf.TABIX or samples_found_iter is not None) and self.range_filters and snaputil.filter_by_ranges(fields,self.range_filters):
                continue
            #combine these two so we only have to split sample <= 1 times
            if self.ra.save_samples:
                samples = set(fields[snapconf.SAMPLE_IDS_COL].split(","))
                #due to start prefixed "," delete empty string in set
                if '' in samples:
                    samples.remove('')
                sample_set.update(samples)
            #TODO: use samples_found_iter to get a projection of just the samples
            #searched for and then recalculate summary stats
            #filter return stream based on range queries (if any)
            if self.ra.stream_back:
                 if samples_found_iter is not None:
                    self.streamer_method(sys.stdout,None,fields,region_args=self.ra)
                 else:
                    self.streamer_method(sys.stdout,line,fields,region_args=self.ra)
            if self.ra.save_introns:
                ids_found.add(snaptron_id)
        exitc=self.extern_proc.wait() 
        if exitc != 0:
            raise RuntimeError("%s returned non-0 exit code\n" % (self.full_cmd))
        return (ids_found, sample_set)
