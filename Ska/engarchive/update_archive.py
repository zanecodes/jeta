#!/usr/bin/env python
# Licensed under a 3-clause BSD style license - see LICENSE.rst

from __future__ import print_function, division, absolute_import

import re
import os
import glob
import time
from six.moves import cPickle as pickle
from six.moves import zip
import argparse
import shutil
import itertools
from collections import OrderedDict, defaultdict

from astropy.time import Time
from Chandra.Time import DateTime
import Ska.File
import Ska.DBI
import Ska.Numpy
import pyyaks.logger
import pyyaks.context
import astropy.io.fits as pyfits
import tables
import tables3_api
import numpy as np
import scipy.stats.mstats

import Ska.engarchive.fetch as fetch
import Ska.engarchive.converters as converters
import Ska.engarchive.file_defs as file_defs
import Ska.engarchive.derived as derived
#import Ska.arc5gl

from jSka.ingest import process
from jSka.ingest.archive import DataProduct

working_filename = None

def get_options(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",
                        action="store_true",
                        help="Dry run (no actual file or database updatees)")
    parser.add_argument("--no-full",
                        action="store_false",
                        dest="update_full",
                        default=True,
                        help=("Do not fetch files from archive and update "
                              "full-resolution MSID archive"))
    parser.add_argument("--no-stats",
                        action="store_false",
                        dest="update_stats",
                        default=True,
                        help="Do not update 5 minute and daily stats archive")
    parser.add_argument("--create",
                        action="store_true",
                        help="Create the MSID H5 files from scratch")
    parser.add_argument("--truncate",
                        help="Truncate archive after <date> (CAUTION!!)")
    parser.add_argument("--max-lookback-time",
                        type=float,
                        default=60,
                        help="Maximum look back time for updating statistics (days)")
    parser.add_argument("--date-now",
                        default=DateTime().date,
                        help="Set effective processing date for testing (default=NOW)")
    parser.add_argument("--date-start",
                        default=None,
                        help=("Processing start date (loops by max-lookback-time "
                              "until date-now if set)"))
    parser.add_argument("--max-gap",
                        type=float,
                        help="Maximum time gap between archive files")
    parser.add_argument("--allow-gap-after-days",
                        type=float,
                        default=4,
                        help="Allow archive file gap when file is this old (days, default=4)")
    parser.add_argument("--max-arch-files",
                        type=int,
                        default=500,
                        help="Maximum number of archive files to ingest at once")
    parser.add_argument("--data-root",
                        default=".",
                        help="Engineering archive root directory for MSID and arch files")
    parser.add_argument("--occ",
                        action="store_true",
                        help="Running on the OCC GRETA network (no arc5gl)")
    parser.add_argument("--content",
                        action='append',
                        help="Content type to process [match regex] (default = all)")
    parser.add_argument("--log-level",
                        help="Logging level")
    return parser.parse_args(args)

# Configure fetch.MSID to cache recent results for performance in
# derived parameter updates.
fetch.CACHE = True

opt = get_options()
if opt.create:
    opt.update_stats = False

ft = fetch.ft
msid_files = pyyaks.context.ContextDict('update_archive.msid_files',
                                        basedir=opt.data_root)
msid_files.update(file_defs.msid_files)
arch_files = pyyaks.context.ContextDict('update_archive.arch_files',
                                        basedir=opt.data_root)
arch_files.update(file_defs.arch_files)

# Set up fetch so it will first try to read from opt.data_root if that is
# provided as an option and exists, and if not fall back to the default of
# fetch.ENG_ARCHIVE.  Fetch is a read-only process so this is safe when testing.
if opt.data_root:
    fetch.msid_files.basedir = ':'.join([opt.data_root, fetch.ENG_ARCHIVE])

# Set up logging
loglevel = pyyaks.logger.VERBOSE if opt.log_level is None else int(opt.log_level)
logger = pyyaks.logger.get_logger(name='engarchive', level=loglevel,
                                  format="%(asctime)s %(message)s")

# Also adjust fetch logging if non-default log-level supplied (mostly for debug)
if opt.log_level is not None:
    fetch.add_logging_handler(level=int(opt.log_level))

archfiles_hdr_cols = ('tstart', 'tstop', 'startmjf', 'startmnf', 'stopmjf', 'stopmnf',
                      'tlmver', 'ascdsver', 'revision', 'date')


def get_colnames():
    """Get column names for the current content type (defined by ft['content'])"""
    colnames = [x for x in pickle.load(open(msid_files['colnames'].abs, 'rb'))
                if x not in fetch.IGNORE_COLNAMES]
    return colnames


def create_content_dir():
    """
    Make empty files for colnames.pkl and archfiles.db3
    for the current content type ft['content'].
    This only works within the development (git) directory in conjunction
    with the --create option.
    """
    dirname = msid_files['contentdir'].abs
    if not os.path.exists(dirname):
        logger.info('Making directory {}'.format(dirname))
        os.makedirs(dirname)

    empty = set()
    if not os.path.exists(msid_files['colnames'].abs):
        with open(msid_files['colnames'].abs, 'wb') as f:
            pickle.dump(empty, f)

    if not os.path.exists(msid_files['archfiles'].abs):
        archfiles_def = open('archfiles_def.sql').read()
        filename = msid_files['archfiles'].abs
        logger.info('Creating db {}'.format(filename))
        db = Ska.DBI.DBI(dbi='sqlite', server=filename, autocommit=False)
        db.execute(archfiles_def)
        db.commit()


_fix_state_code_cache = {}


def fix_state_code(state_code):
    """
    Return a version of ``state_code`` that has only alphanumeric chars.  This
    can be used as a column name, unlike e.g. "n_+1/2".  Since this gets called
    in an inner loop cache the result.
    """
    try:
        out = _fix_state_code_cache[state_code]
    except KeyError:
        out = state_code
        for sub_in, sub_out in ((r'\+', 'PLUS_'),
                                (r'\-', 'MINUS_'),
                                (r'>', '_GREATER_'),
                                (r'/', '_DIV_')):
            out = re.sub(sub_in, sub_out, out)
        _fix_state_code_cache[state_code] = out

    return out


def main():
    """
    Perform one full update of the eng archive based on opt parameters.
    This may be called in a loop by the program-level main().
    """
    logger.info('Run time options: \n{}'.format(opt))
    logger.info('Update_archive file: {}'.format(os.path.abspath(__file__)))
    logger.info('Fetch module file: {}'.format(os.path.abspath(fetch.__file__)))
    logger.info('')

    # Get the archive content filetypes
    filetypes = fetch.filetypes
    if opt.content:
        contents = [x.upper() for x in opt.content]
        filetypes = [x for x in filetypes
                     if any(re.match(y, x.content) for y in contents)]

    for filetype in filetypes:
        # Update attributes of global ContextValue "ft".  This is needed for
        # rendering of "files" ContextValue.
        ft['content'] = filetype.content.lower()

        if opt.create:
            create_content_dir()

        colnames = [x for x in pickle.load(open(msid_files['colnames'].abs, 'rb'))
                    if x not in fetch.IGNORE_COLNAMES]

        if not os.path.exists(fetch.msid_files['archfiles'].abs):
            logger.info('No archfiles.db3 for %s - skipping' % ft['content'])
            continue

        logger.info('Processing %s content type', ft['content'])

        if opt.truncate:
            truncate_archive(filetype, opt.truncate)
            continue

        if opt.update_full:
            if filetype['instrum'] == 'DERIVED':
                update_derived(filetype)
            else:
                update_archive(filetype)

        if opt.update_stats:
            for colname in colnames:
                if opt.state_codes_only:
                    # Check if colname has a state code in the TDB or if it is in the
                    # special-case fetch.STATE_CODES dict (e.g. simdiag or simmrg telem).
                    try:
                        Ska.tdb.msids[colname].Tsc['STATE_CODE']
                    except Exception:
                        if not colname.upper() in fetch.STATE_CODES:
                            continue

                msid = update_stats(colname, 'daily')
                update_stats(colname, '5min', msid)


def fix_misorders(filetype):
    """Fix problems in the eng archive where archive files were ingested out of
    time order.  This results in a non-monotonic times in the MSID hdf5 files
    and subsequently corrupts the stats files.  This routine looks for
    discontinuities in rowstart assuming filename ordering and swaps neighbors.
    One needs to verify in advance (--dry-run --fix-misorders --content ...)
    that this will be an adequate fix.
    Example::
      update_archive.py --dry-run --fix-misorders --content misc3eng
      update_archive.py --fix-misorders --content misc3eng >& fix_misc3.log
      update_archive.py --content misc3eng --max-lookback-time 100 >>& fix_misc3.log
    In the --dry-run it is important to verify that the gap is really just from
    two mis-ordered files that can be swapped.  Look at the rowstart,rowstop values
    in the filename-ordered list.
    :param filetype: filetype
    :returns: minimum time for all misorders found
    """
    colnames = pickle.load(open(msid_files['colnames'].abs, 'rb'))

    # Setup db handle with autocommit=False so that error along the way aborts insert transactions
    db = Ska.DBI.DBI(dbi='sqlite', server=msid_files['archfiles'].abs, autocommit=False)

    # Get misordered archive files
    archfiles = db.fetchall('SELECT * FROM archfiles order by filename')
    bads = archfiles['rowstart'][1:] - archfiles['rowstart'][:-1] < 0

    if not np.any(bads):
        logger.info('No misorders')
        return

    for bad in np.flatnonzero(bads):
        i2_0, i1_0 = archfiles['rowstart'][bad:bad + 2]
        i2_1, i1_1 = archfiles['rowstop'][bad:bad + 2]

        # Update hdf5 file for each column (MSIDs + TIME, MJF, etc)
        for colname in colnames:
            ft['msid'] = colname
            logger.info('Fixing %s', msid_files['msid'].abs)
            if not opt.dry_run:
                filepath = DataProduct.get_file_write_path(msid_files['msid'].abs, colname, h5type='values')
                h5 = tables.open_file(filepath, mode='a')
                #h5 = tables.open_file(msid_files['msid'].abs, mode='a')
                hrd = h5.root.data
                hrq = h5.root.quality

                hrd1 = hrd[i1_0:i1_1]
                hrd2 = hrd[i2_0:i2_1]
                hrd[i1_0:i1_0 + len(hrd2)] = hrd2
                hrd[i1_0 + len(hrd2): i2_1] = hrd1

                hrq1 = hrq[i1_0:i1_1]
                hrq2 = hrq[i2_0:i2_1]
                hrq[i1_0:i1_0 + len(hrq2)] = hrq2
                hrq[i1_0 + len(hrq2): i2_1] = hrq1

                h5.close()

        # Update the archfiles table
        cmd = 'UPDATE archfiles SET '
        cols = ['rowstart', 'rowstop']
        cmd += ', '.join(['%s=?' % x for x in cols])
        cmd += ' WHERE filename=?'
        rowstart1 = i1_0
        rowstop1 = rowstart1 + i2_1 - i2_0
        rowstart2 = rowstop1 + 1
        rowstop2 = i2_1
        vals1 = [rowstart1, rowstop1, archfiles['filename'][bad]]
        vals2 = [rowstart2, rowstop2, archfiles['filename'][bad + 1]]
        logger.info('Running %s %s', cmd, vals1)
        logger.info('Running %s %s', cmd, vals2)

        logger.info('Swapping rows %s for %s', [i1_0, i1_1, i2_0, i2_1], filetype.content)
        logger.info('%s', archfiles[bad - 3:bad + 5])
        logger.info('')

        if not opt.dry_run:
            db.execute(cmd, [x.tolist() for x in vals1])
            db.execute(cmd, [x.tolist() for x in vals2])
            db.commit()

    return np.min(archfiles['tstart'][bads])


def del_stats(colname, time0, interval):
    """Delete all rows in ``interval`` stats file for column ``colname`` that
    occur after time ``time0`` - ``interval``.  This is used to fix problems
    that result from a file misorder.  Subsequent runs of update_stats will
    refresh the values correctly.
    """
    dt = {'5min': 328,
          'daily': 86400}[interval]

    ft['msid'] = colname
    ft['interval'] = interval
    stats_file = msid_files['stats'].abs
    if not os.path.exists(stats_file):
        raise IOError('Stats file {} not found'.format(stats_file))

    logger.info('Fixing stats file %s after time %s', stats_file, DateTime(time0).date)

    stats = tables.open_file(stats_file, mode='a',
                            filters=tables.Filters(complevel=5, complib='zlib'))
    index0 = time0 // dt - 1
    indexes = stats.root.data.col('index')[:]
    row0 = np.searchsorted(indexes, [index0])[0] - 1
    if opt.dry_run:
        n_del = len(stats.root.data) - row0
    else:
        n_del = stats.root.data.removeRows(row0, len(stats.root.data))
    logger.info('Deleted %d rows from row %s (%s) to end', n_del, row0,
                DateTime(indexes[row0] * dt).date)
    stats.close()


def calc_stats_vals(msid, rows, indexes, interval):
    """
    Compute statistics values for ``msid`` over specified intervals.
    :param msid: Msid object (filter_bad=True)
    :param rows: Msid row indices corresponding to stat boundaries
    :param indexes: Universal index values for stat (row times // dt)
    :param interval: interval name (5min or daily)
    """
    quantiles = (1, 5, 16, 50, 84, 95, 99)
    n_out = len(rows) - 1

    # Check if data type is "numeric".  Boolean values count as numeric,
    # partly for historical reasons, in that they support funcs like
    # mean (with implicit conversion to float).
    msid_dtype = msid.vals.dtype
    msid_is_numeric = issubclass(msid_dtype.type, (np.number, np.bool_))

    # Predeclare numpy arrays of correct type and sufficient size for accumulating results.
    out = OrderedDict()
    out['index'] = np.ndarray((n_out,), dtype=np.int32)
    out['n'] = np.ndarray((n_out,), dtype=np.int32)
    out['val'] = np.ndarray((n_out,), dtype=msid_dtype)

    if msid_is_numeric:
        out['min'] = np.ndarray((n_out,), dtype=msid_dtype)
        out['max'] = np.ndarray((n_out,), dtype=msid_dtype)
        out['mean'] = np.ndarray((n_out,), dtype=np.float32)

        if interval == 'daily':
            out['std'] = np.ndarray((n_out,), dtype=msid_dtype)
            for quantile in quantiles:
                out['p{:02d}'.format(quantile)] = np.ndarray((n_out,), dtype=msid_dtype)

    # MSID may have state codes
    if msid.state_codes:
        for raw_count, state_code in msid.state_codes:
            out['n_' + fix_state_code(state_code)] = np.zeros(n_out, dtype=np.int32)

    i = 0
    for row0, row1, index in zip(rows[:-1], rows[1:], indexes[:-1]):
        vals = msid.vals[row0:row1]
        times = msid.times[row0:row1]

        n_vals = len(vals)
        if n_vals > 0:
            out['index'][i] = index
            out['n'][i] = n_vals
            out['val'][i] = vals[n_vals // 2]
            if msid_is_numeric:
                if n_vals <= 2:
                    dts = np.ones(n_vals, dtype=np.float64)
                else:
                    dts = np.empty(n_vals, dtype=np.float64)
                    dts[0] = times[1] - times[0]
                    dts[-1] = times[-1] - times[-2]
                    dts[1:-1] = ((times[1:-1] - times[:-2]) +
                                 (times[2:] - times[1:-1])) / 2.0
                    negs = dts < 0.0
                    if np.any(negs):
                        times_dts = [(DateTime(t).date, dt)
                                     for t, dt in zip(times[negs], dts[negs])]
                        logger.warning('WARNING - negative dts in {} at {}'
                                       .format(msid.MSID, times_dts))

                    # Clip to range 0.001 to 300.0.  The low bound is just there
                    # for data with identical time stamps.  This shouldn't happen
                    # but in practice might.  The 300.0 represents 5 minutes and
                    # is the largest normal time interval.  Data near large gaps
                    # will get a weight of 5 mins.
                    dts.clip(0.001, 300.0, out=dts)
                sum_dts = np.sum(dts)

                out['min'][i] = np.min(vals)
                out['max'][i] = np.max(vals)
                out['mean'][i] = np.sum(dts * vals) / sum_dts
                if interval == 'daily':
                    # biased weighted estimator of variance (N should be big enough)
                    # http://en.wikipedia.org/wiki/Mean_square_weighted_deviation
                    sigma_sq = np.sum(dts * (vals - out['mean'][i]) ** 2) / sum_dts
                    out['std'][i] = np.sqrt(sigma_sq)
                    quant_vals = scipy.stats.mstats.mquantiles(vals, np.array(quantiles) / 100.0)
                    for quant_val, quantile in zip(quant_vals, quantiles):
                        out['p%02d' % quantile][i] = quant_val

            if msid.state_codes:
                # If MSID has state codes then count the number of values in each state
                # and store.  The MSID values can have trailing spaces to fill out to a
                # uniform length, so state_code is right padded accordingly.
                max_len = max(len(state_code) for raw_count, state_code in msid.state_codes)
                fmtstr = '{:' + str(max_len) + 's}'
                for raw_count, state_code in msid.state_codes:
                    state_count = np.count_nonzero(vals == fmtstr.format(state_code))
                    out['n_' + fix_state_code(state_code)][i] = state_count

            i += 1

    return np.rec.fromarrays([x[:i] for x in out.values()], names=list(out.keys()))


def update_stats(colname, interval, msid=None):
    dt = {'5min': 328,
          'daily': 86400}[interval]

    ft['msid'] = colname
    ft['interval'] = interval
    stats_file = msid_files['stats'].abs
    logger.info('Updating stats file %s', stats_file)

    if not os.path.exists(msid_files['statsdir'].abs):
        logger.info('Making stats dir {}'.format(msid_files['statsdir'].abs))
        os.makedirs(msid_files['statsdir'].abs)

    stats = tables.open_file(stats_file, mode='a',
                            filters=tables.Filters(complevel=5, complib='zlib'))

    # INDEX0 is somewhat before any CXC archive data (which starts around 1999:205)
    INDEX0 = DateTime('1999:200:00:00:00').secs // dt
    try:
        index0 = stats.root.data.cols.index[-1] + 1
    except tables.NoSuchNodeError:
        index0 = INDEX0

    # Get all new data. time0 is the fetch start time which nominally starts at
    # 500 sec before the last available record.  However some MSIDs may not
    # be sampled for years at a time so once the archive is built and kept
    # up to date then do not look back beyond a certain point.
    if msid is None:
        # fetch telemetry plus a little extra
        time0 = max(DateTime(opt.date_now).secs - opt.max_lookback_time * 86400,
                    index0 * dt - 500)
        time1 = DateTime(opt.date_now).secs
        msid = fetch.MSID(colname, time0, time1, filter_bad=True)

    if len(msid.times) > 0:
        if index0 == INDEX0:
            # Must be creating the file, so back up a bit from earliest MSID data
            index0 = msid.times[0] // dt - 2

        indexes = np.arange(index0, msid.times[-1] / dt, dtype=np.int32)
        times = indexes * dt

        if len(times) > 2:
            rows = np.searchsorted(msid.times, times)
            vals_stats = calc_stats_vals(msid, rows, indexes, interval)
            if len(vals_stats) > 0:
                # Don't change the following logic in order to add stats data
                # on the same pass as creating the table.  Tried it and
                # something got broken so that there was a single bad record
                # after the first bunch.
                if not opt.dry_run:
                    try:
                        stats.root.data.append(vals_stats)
                        logger.info('  Adding %d records', len(vals_stats))
                    except tables.NoSuchNodeError:
                        logger.info('  Creating table with %d records ...', len(vals_stats))
                        stats.createTable(stats.root, 'data', vals_stats,
                                          "{} sampling".format(interval), expectedrows=2e7)
                    stats.root.data.flush()
            else:
                logger.info('  No stat records within available fetched values')
        else:
            logger.info('  No full stat intervals within fetched values')
    else:
        logger.info('  No MSID data found within {} to {}'
                    .format(msid.datestart, msid.datestop))

    stats.close()

    return msid


def update_derived(filetype):
    """Update full resolution MSID archive files for derived parameters with ``filetype``
    """
    # Get the last H5 table row from archfiles table for this content type
    db = Ska.DBI.DBI(dbi='sqlite', server=msid_files['archfiles'].abs)
    last_row = db.fetchone('SELECT * FROM archfiles ORDER BY filetime DESC')

    # Set the starting index from the last row in archfiles.  This
    # uses Python slicing conventions so that the previous "end"
    # value is exactly the next "start" values, e.g. [index0:index1]
    # For derived parameters we have stopmjf <==> index1
    index0 = last_row['stopmjf']

    # Get the full set of rootparams for all colnames
    colnames = pickle.load(open(msid_files['colnames'].abs, 'rb'))
    colnames = [x for x in colnames if x.startswith('DP_')]
    msids = set()
    for colname in colnames:
        dp_class = getattr(derived, colname)
        dp = dp_class()
        msids = msids.union([x.upper() for x in dp.rootparams])
        time_step = dp.time_step  # will be the same for every DP

    # Find the last time in archive for each of the content types
    # occuring in the list of rootparam MSIDs.
    # fetch.content is a mapping from MSID to content type
    last_times = {}
    ft_content = ft['content'].val
    for msid in msids:
        ft['msid'] = 'TIME'
        content = ft['content'] = fetch.content[msid]
        if content not in last_times:
            h5 = tables.open_file(fetch.msid_files['msid'].abs, mode='r')
            last_times[content] = h5.root.data[-1]
            h5.close()
    last_time = min(last_times.values()) - 1000
    ft['content'] = ft_content

    # Make a list of indexes that will correspond to the index/time ranges
    # for each pseudo-"archfile".  In this context an archfile just specifies
    # the time range covered by an ingest, but is needed by fetch to roughly
    # locate rows in the H5 file for fast queries.  Each archfile is 10000 sec
    # long, and when updating the database no more than 1000000 seconds of
    # telemetry will be read at one time.
    archfile_time_step = 10000.0
    max_archfiles = int(1000000.0 / archfile_time_step)

    # Read data out to either date_now or the last available time in telemetry.
    # opt.date_now could be set in the past for testing.
    index_step = int(round(archfile_time_step / time_step))
    time1 = min(DateTime(opt.date_now).secs, last_time)
    index1 = int(time1 / time_step)
    indexes = np.arange(index0, index1, index_step)

    archfiles = []
    for index0, index1 in zip(indexes[:-1], indexes[1:]):
        archfiles.append('{}:{}:{}'.format(filetype['content'], index0, index1))
        if len(archfiles) == max_archfiles or index1 == indexes[-1]:
            update_msid_files(filetype, archfiles)
            logger.verbose('update_msid_files(filetype={}, archfiles={})'
                           .format(str(filetype), archfiles))
            archfiles = []


def update_archive(filetype):
    """Get new CXC archive files for ``filetype`` and update the full-resolution MSID
    archive files.
    """
    archfiles = get_archive_files(filetype)
    if archfiles:
        update_msid_files(filetype, archfiles)


def make_h5_col_file_derived(dats, colname):
    """Make a new h5 table to hold column from ``dat``."""
    filename = msid_files['msid'].abs
    filedir = os.path.dirname(filename)
    if not os.path.exists(filedir):
        os.makedirs(filedir)

    # Estimate the number of rows for 20 years based on available data
    times = np.hstack([x['TIME'] for x in dats])
    dt = np.median(times[1:] - times[:-1])
    n_rows = int(86400 * 365 * 20 / dt)

    filters = tables.Filters(complevel=5, complib='zlib')
    h5 = tables.open_file(filename, mode='w', filters=filters)

    col = dats[-1][colname]
    h5shape = (0,) + col.shape[1:]
    h5type = tables.Atom.from_dtype(col.dtype)
    h5.create_earray(h5.root, 'data', h5type, h5shape, title=colname,
                    expectedrows=n_rows)
    h5.create_earray(h5.root, 'quality', tables.BoolAtom(), (0,), title='Quality',
                    expectedrows=n_rows)
    logger.verbose('WARNING: made new file {} for column {!r} shape={} with n_rows(1e6)={}'
                   .format(filename, colname, h5shape, n_rows / 1.0e6))
    h5.close()


def append_h5_col_derived(dats, colname):
    """Append new values to an HDF5 MSID data table.
    :param dats: List of pyfits HDU data objects
    :param colname: column name
    """
    def i_colname(dat):
        """Return the index for `colname` in `dat`"""
        return list(dat.dtype.names).index(colname)

    h5 = tables.open_file(msid_files['msid'].abs, mode='a')
    stacked_data = np.hstack([x[colname] for x in dats])
    stacked_quality = np.hstack([x['QUALITY'][:, i_colname(x)] for x in dats])
    logger.verbose('Appending %d items to %s' % (len(stacked_data), msid_files['msid'].abs))

    if not opt.dry_run:
        h5.root.data.append(stacked_data)
        h5.root.quality.append(stacked_quality)

    data_len = len(h5.root.data)
    h5.close()

    return data_len


def make_h5_col_file_tlm(dat, colname):
    """Make a new h5 table to hold column from ``dat``."""
    
    DataProduct.create_archive_directory(msid_files['msid'].abs, colname)
    
    DataProduct.create_values_hdf5(colname, dat, msid_files['msid'].abs)
    DataProduct.create_times_hdf5(colname, dat, msid_files['msid'].abs)  


def append_h5_col_tlm(dat, colname):
    """Append new values to an HDF5 MSID data table.
    :param dats: List of pyfits HDU data objects
    :param colname: column name
    """

    values_filepath = DataProduct.get_file_write_path(msid_files['msid'].abs, colname, h5type='values')
    times_filepath = DataProduct.get_file_write_path(msid_files['msid'].abs, colname, h5type='times')
    # print("SHHHHHAKAAAA BOOOM!!!!!")
    print(values_filepath)
    #raise ValueError('BA BOOM!!!!!!')
  
    times = dat[colname]['times']
    values = dat[colname]['values']

    h5_values_file = tables.open_file(values_filepath, mode='a')
    logger.verbose('Appending %d items to %s' % (len(values), values_filepath))

    h5_times_file = tables.open_file(times_filepath, mode='a')
    logger.verbose('Appending %d items to %s' % (len(times), times_filepath))

    if not opt.dry_run:
        h5_times_file.root.time.append(times)
        h5_values_file.root.data.append(values)

    data_len = len(h5_values_file.root.data)
    h5_times_file.close()
    h5_values_file.close()

    return data_len


def truncate_archive(filetype, date):
    """Truncate msid and statfiles for every archive file after date (to nearest
    year:doy)
    """
    colnames = pickle.load(open(msid_files['colnames'].abs, 'rb'))

    date = DateTime(date).date
    year, doy = date[0:4], date[5:8]

    # Setup db handle with autocommit=False so that error along the way aborts insert transactions
    db = Ska.DBI.DBI(dbi='sqlite', server=msid_files['archfiles'].abs, autocommit=False)

    # Get the earliest row number from the archfiles table where year>=year and doy=>doy
    out = db.fetchall('SELECT rowstart FROM archfiles '
                      'WHERE year>={0} AND doy>={1}'.format(year, doy))
    if len(out) == 0:
        return
    rowstart = out['rowstart'].min()
    time0 = DateTime("{0}:{1}:00:00:00".format(year, doy)).secs

    for colname in colnames:
        ft['msid'] = colname
        filename = DataProduct.get_file_write_path(msid_files['msid'].abs, colname, h5type='values') # msid_files['msid'].abs
        if not os.path.exists(filename):
            raise IOError('MSID file {} not found'.format(filename))
        if not opt.dry_run:
            h5 = tables.open_file(filename, mode='a')
            h5.root.data.truncate(rowstart)
            h5.root.quality.truncate(rowstart)
            h5.close()
        logger.verbose('Removed rows from {0} for filetype {1}:{2}'.format(
            rowstart, filetype['content'], colname))

        # Delete the 5min and daily stats, with a little extra margin
        if colname not in fetch.IGNORE_COLNAMES:
            del_stats(colname, time0, '5min')
            del_stats(colname, time0, 'daily')

    cmd = 'DELETE FROM archfiles WHERE (year>={0} AND doy>={1}) OR year>{0}'.format(year, doy, year)
    if not opt.dry_run:
        db.execute(cmd)
        db.commit()
    logger.verbose(cmd)


def read_archfile(i, f, filetype, row, colnames, archfiles, db):
    """Read filename ``f`` with index ``i`` (position within list of filenames).  The
    file has type ``filetype`` and will be added to MSID file at row index ``row``.
    ``colnames`` is the list of column names for the content type (not used here).
    """
    # Check if filename is already in archfiles.  If so then abort further processing.
    filename = os.path.basename(f)
    if db.fetchall('SELECT filename FROM archfiles WHERE filename=?', (filename,)):
        logger.verbose('File %s already in archfiles - unlinking and skipping' % f)
        os.unlink(f)
        return None, None

    # Read archive file and accumulate data into dats list and header into headers dict
    logger.info('Reading (%d / %d) %s' % (i, len(archfiles), filename))

    ingest = process.Ingest(f).start()

    # dates = defaultdict(list)
    # values = defaultdict(list)
    # tstart = None

    # for line in open(f, 'r'):
    #     msid, date, value = line.split()
    #     if tstart is None:
    #         tstart = Time(date).jd
    #     dates[msid].append(date)
    #     values[msid].append(date)
    # tstop = Time(date).jd  # last date in file

    # dat = {}
    # for msid in dates:
    #     dat[msid] = {'times': Time(dates[msid], format='isot', in_subfmt='date_hms').jd,
    #                  'values': np.array(values[msid])}

    # print(ingest.data)
    # print(ingest.tstart)
    # print(ingest.tstop)

    archfiles_row = dict(filename=f,
                         tstart=ingest.tstart,
                         tstop=ingest.tstop,
                         rowstart=row,
                         rowstop=row + 1,
                         date=Time.now().iso)

    
    return ingest.data, archfiles_row


def read_derived(i, filename, filetype, row, colnames, archfiles, db):
    """Read derived data using eng_archive and derived computation classes.
    ``filename`` has format <content>_<index0>_<index1> where <content>
    is the content type (e.g. "dp_thermal128"), <index0> is the start index for
    the new data and index1 is the end index (using Python slicing convention
    index0:index1).  Args ``i``, ``filetype``, and ``row`` are as in
    read_archive().  ``row`` must equal <index0>.  ``colnames`` is the list of
    column names for the content type.
    """
    # Check if filename is already in archfiles.  If so then abort further processing.

    if db.fetchall('SELECT filename FROM archfiles WHERE filename=?', (filename,)):
        logger.verbose('File %s already in archfiles - skipping' % filename)
        return None, None

    # f has format <content>_<index0>_<index1>
    # <content> has format dp_<content><mnf_step> e.g. dp_thermal128
    content, index0, index1 = filename.split(':')
    index0 = int(index0)
    index1 = int(index1)
    mnf_step = int(re.search(r'(\d+)$', content).group(1))
    time_step = mnf_step * derived.MNF_TIME
    times = time_step * np.arange(index0, index1)

    logger.info('Reading (%d / %d) %s' % (i, len(archfiles), filename))
    vals = {}
    bads = np.zeros((len(times), len(colnames)), dtype=np.bool)
    for i, colname in enumerate(colnames):
        if colname == 'TIME':
            vals[colname] = times
            bads[:, i] = False
        else:
            dp_class = getattr(Ska.engarchive.derived, colname.upper())
            dp = dp_class()
            dataset = dp.fetch(times[0] - 1000, times[-1] + 1000)
            ok = (index0 <= dataset.indexes) & (dataset.indexes < index1)
            vals[colname] = dp.calc(dataset)[ok]
            bads[:, i] = dataset.bads[ok]

    vals['QUALITY'] = bads
    dat = Ska.Numpy.structured_array(vals, list(colnames) + ['QUALITY'])

    # Accumlate relevant info about archfile that will be ingested into
    # MSID h5 files.  Commit info before h5 ingest so if there is a failure
    # the needed info will be available to do the repair.
    date = DateTime(times[0]).date
    year, doy = date[0:4], date[5:8]
    archfiles_row = dict(filename=filename,
                         filetime=int(index0 * time_step),
                         year=year,
                         doy=doy,
                         tstart=times[0],
                         tstop=times[-1],
                         rowstart=row,
                         rowstop=row + len(dat),
                         startmjf=index0,
                         stopmjf=index1,
                         date=date)

    return dat, archfiles_row


def get_dat_colnames(dat):
    """Iteratable over dat colnames"""
    return dat if isinstance(dat, dict) else dat.dtype.names


def update_msid_files(filetype, archfiles):

    print("Updating....")
    colnames = pickle.load(open(msid_files['colnames'].abs, 'rb'))
    old_colnames = colnames.copy()

    # Setup db handle with autocommit=False so that error along the way aborts insert transactions
    db = Ska.DBI.DBI(dbi='sqlite', server=msid_files['archfiles'].abs, autocommit=False)

    # Get the last row number from the archfiles table
    out = db.fetchone('SELECT max(rowstop) FROM archfiles')
    row = out['max(rowstop)'] or 0
    last_archfile = db.fetchone('SELECT * FROM archfiles where rowstop=?', (row,))

    archfiles_processed = []

    content_is_derived = (filetype['instrum'] == 'DERIVED')
    make_h5_col_file = make_h5_col_file_derived if content_is_derived else make_h5_col_file_tlm
    append_h5_col = append_h5_col_derived if content_is_derived else append_h5_col_tlm
    #append_h5_col = append_h5_col_derived if content_is_derived else append_h5_col_tlm
    

    for i, f in enumerate(archfiles):
        get_data = (read_derived if content_is_derived else read_archfile)
        dat, archfiles_row = get_data(i, f, filetype, row, colnames, archfiles, db)
        if dat is None:
            continue

        # If creating new content type and there are no existing colnames, then
        # define the column names now.
        if opt.create and not colnames:
            colnames = set(get_dat_colnames(dat))
 
        # Ensure that the time gap between the end of the last ingested archive
        # file and the start of this one is less than opt.max_gap (or
        # filetype-based defaults).  If this fails then break out of the
        # archfiles processing but continue on to ingest any previously
        # successful archfiles
        if last_archfile is None:
            time_gap = 0
        else:
            time_gap = archfiles_row['tstart'] - last_archfile['tstop']
        max_gap = opt.max_gap
        if max_gap is None:
            if filetype['instrum'] in ['DERIVED']:
                max_gap = 601
            else:
                max_gap = 32.9

        if time_gap > max_gap:
            logger.warning('WARNING: found gap of %.2f secs between archfiles %s and %s',
                           time_gap, last_archfile['filename'], archfiles_row['filename'])
        elif time_gap < 0:
            raise ValueError('overlapping archive files')

        # Update the last_archfile values.
        last_archfile = archfiles_row

        # Mark the archfile as ingested in the database and add to list for
        # subsequent relocation into arch_files archive.  In the case of a gap
        # where ingest is stopped before all archfiles are processed, this will
        # leave files either in a tmp dir (HEAD) or in the stage dir (OCC).
        # In the latter case this allows for successful processing later when the
        # gap gets filled.
        archfiles_processed.append(f)
        if not opt.dry_run:
            db.insert(archfiles_row, 'archfiles')

        # Update the running list of column names.
        colnames.update(name for name in get_dat_colnames(dat))

        row += len(dat) if content_is_derived else 1

        logger.verbose('Writing accumulated column data to h5 file at ' + time.ctime())
        processed_cols = set()
        for colname in colnames:
            ft['msid'] = colname
            if not os.path.exists(msid_files['msid'].abs):
                make_h5_col_file(dat, colname)
                if not opt.create:
                    # New MSID was found for this content type.  This must be associated with
                    # an update to the TDB.  Skip for the moment to ensure that other MSIDs
                    # are fully processed.
                    continue
            append_h5_col(dat, colname)
            processed_cols.add(colname)

        # Process any new MSIDs (this is extremely rare)
        for colname in colnames - processed_cols:
            ft['msid'] = colname
            append_h5_col(dat, colname)

    # Assuming everything worked now commit the db inserts that signify the
    # new archive files have been processed
    if not opt.dry_run:
        db.commit()

    # If colnames changed then give warning and update files.
    if colnames != old_colnames:
        logger.warning('WARNING: updating %s because colnames changed: %s'
                       % (msid_files['colnames'].abs, old_colnames ^ colnames))
        if not opt.dry_run:
            pickle.dump(colnames, open(msid_files['colnames'].abs, 'wb'))

    return archfiles_processed


def move_archive_files(filetype, archfiles):
    ft['content'] = filetype.content.lower()

    stagedir = arch_files['stagedir'].abs
    if not os.path.exists(stagedir):
        os.makedirs(stagedir)

    for f in archfiles:
        if not os.path.exists(f):
            continue
        ft['basename'] = os.path.basename(f)
        tstart = re.search(r'(\d+)', str(ft['basename'])).group(1)
        datestart = DateTime(tstart).date
        ft['year'], ft['doy'] = re.search(r'(\d\d\d\d):(\d\d\d)', datestart).groups()

        archdir = arch_files['archdir'].abs
        archfile = arch_files['archfile'].abs

        if not os.path.exists(archdir):
            os.makedirs(archdir)

        if not os.path.exists(archfile):
            logger.info('mv %s %s' % (os.path.abspath(f), archfile))
            if not opt.dry_run:
                if not opt.occ:
                    shutil.copy2(f, stagedir)
                shutil.move(f, archfile)

        if os.path.exists(f):
            logger.verbose('Unlinking %s' % os.path.abspath(f))
            os.unlink(f)


def get_archive_files(filetype):
    """Get telemetry files"""

    print("getting archive files")

    files = sorted(glob.glob('stage/*.CSV'))
    print(files)
    return files
