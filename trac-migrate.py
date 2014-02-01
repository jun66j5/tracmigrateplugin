#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys


def _usage(progname=sys.argv[0]):
    return """\
Usage: %(progname)s SOURCE-TRACENV DEST-TRACENV DBURI

Examples:
  $ %(progname)s /path/to/source /path/to/dest sqlite:db/trac.db
  $ %(progname)s /path/to/source /path/to/dest postgres://tracuser:password@localhost/trac?schema=destination
  $ %(progname)s /path/to/source /path/to/dest mysql://tracuser:password@localhost/trac
""" % {'progname': progname}


def main(args):
    try:
        from trac.env import Environment
        from tracmigrate.admin import TracMigrationCommand
    except ImportError, e:
        print >>sys.stderr, 'Requires trac and tracmigrateplugin: %s' % \
                            unicode(e)
        return 127

    if len(args) != 3:
        print >>sys.stderr, _usage()
        return 126
    source, dest, dburi = args

    env = Environment(source)
    if env.needs_upgrade():
        print >>sys.stderr, '''\
The Trac Environment needs to be upgraded.

Run "trac-admin %s upgrade"''' % source
        return 2
    return TracMigrationCommand(env)._do_migrate(dest, dburi)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
