#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys


def _usage(progname=sys.argv[0]):
    return """\
Usage: %(progname)s [OPTIONS] SOURCE-TRACENV [DEST-TRACENV] DBURI

  Migrate to new environment with another database or another
  database without creating a new environment.

Options:
  -i, --in-place    migrate without creating a new environment

Examples:
  $ %(progname)s /path/to/source /path/to/dest sqlite:db/trac.db
  $ %(progname)s /path/to/source /path/to/dest postgres://tracuser:password@localhost/trac?schema=destination
  $ %(progname)s /path/to/source /path/to/dest mysql://tracuser:password@localhost/trac
  $ %(progname)s --in-place /path/to/source sqlite:db/trac.db
  $ %(progname)s --in-place /path/to/source postgres://tracuser:password@localhost/trac?schema=destination
  $ %(progname)s --in-place /path/to/source mysql://tracuser:password@localhost/trac
""" % {'progname': progname}


def main(args):
    if len(args) != 3:
        print >>sys.stderr, _usage()
        return 127
    if args[0] in ('-i', '--in-place'):
        dest, source, dburi = args
    else:
        source, dest, dburi = args

    try:
        from trac.env import Environment
        from tracmigrate.admin import TracMigrationCommand
    except ImportError, e:
        print >>sys.stderr, 'Requires trac and tracmigrateplugin: %s' % \
                            unicode(e)
        return 126

    env = Environment(source)
    if env.needs_upgrade():
        print >>sys.stderr, '''\
The Trac Environment needs to be upgraded.

Run "trac-admin %s upgrade"''' % source
        return 2
    return TracMigrationCommand(env)._do_migrate(dest, dburi)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
