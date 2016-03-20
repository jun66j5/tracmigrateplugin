# -*- coding: utf-8 -*-

import os
import re
import shutil
import sys
import time
from ConfigParser import RawConfigParser
from subprocess import PIPE, Popen
from tempfile import mkdtemp

from trac.core import Component, implements, TracError
from trac.admin.api import IAdminCommandProvider, get_dir_list
from trac.db import sqlite_backend
from trac.db.api import DatabaseManager, get_column_names, _parse_db_str
from trac.env import Environment
from trac.util.compat import any, close_fds
from trac.util.text import printerr, printout


def get_connection(env):
    return DatabaseManager(env).get_connection()


class MigrateEnvironment(Environment):

    abstract = True  # avoid showing in plugins admin page
    required = False

    def is_component_enabled(self, cls):
        name = self._component_name(cls)
        if not any(name.startswith(mod) for mod in ('trac.', 'tracopt.')):
            return False
        return Environment.is_component_enabled(self, cls)


class TracMigrationCommand(Component):

    implements(IAdminCommandProvider)

    _help = """\
    Migrate to another database

    This command migrates to another database in new Trac Environment or this
    Trac Environment in-place. The new Trac Environment is specified in the
    <tracenv>. If -i/--in-place option is specified, in-place migration.
    Another database is specified in the <dburi> and must be empty."""

    def get_admin_commands(self):
        yield ('migrate', '<tracenv|-i|--in-place> <dburi>',
               self._help, self._complete_migrate, self._do_migrate)

    def _do_migrate(self, env_path, dburi):
        if env_path in ('-i', '--in-place'):
            return self._do_migrate_inplace(dburi)
        else:
            return self._do_migrate_to_env(env_path, dburi)

    def _do_migrate_to_env(self, env_path, dburi):
        try:
            os.rmdir(env_path)  # remove directory if it's empty
        except OSError:
            pass
        if os.path.exists(env_path) or os.path.lexists(env_path):
            self._printerr('Cannot create Trac environment: %s: File exists',
                           env_path)
            return 1

        dst_env = self._create_env(env_path, dburi)
        src_dburi = self.config.get('trac', 'database')
        src_db = get_connection(self.env)
        dst_db = get_connection(dst_env)
        self._copy_tables(src_db, dst_db, src_dburi, dburi)
        self._copy_directories(src_db, dst_env)

    def _do_migrate_inplace(self, dburi):
        src_dburi = self.config.get('trac', 'database')
        if src_dburi == dburi:
            self._printerr('Source database and destination database are '
                           'same: %s', dburi)
            return 1

        env_path = mkdtemp(prefix='migrate-',
                           dir=os.path.dirname(self.env.path))
        try:
            dst_env = self._create_env(env_path, dburi)
            src_db = get_connection(self.env)
            dst_db = get_connection(dst_env)
            self._copy_tables(src_db, dst_db, src_dburi, dburi, inplace=True)
            del src_db
            del dst_db
            dst_env.shutdown()
            dst_env = None
            if dburi.startswith('sqlite:'):
                schema, params = _parse_db_str(dburi)
                dbpath = os.path.join(self.env.path, params['path'])
                dbdir = os.path.dirname(dbpath)
                if not os.path.isdir(dbdir):
                    os.makedirs(dbdir)
                shutil.copy(os.path.join(env_path, params['path']), dbpath)
        finally:
            shutil.rmtree(env_path)

        self._backup_tracini(self.env)
        self.config.set('trac', 'database', dburi)
        self.config.save()

    def _backup_tracini(self, env):
        dir = env.path
        src = env.config.filename
        basename = os.path.basename
        dst = src + '.migrate-%d' % int(time.time())
        shutil.copyfile(src, dst)
        self._printout('Back up conf/%s to conf/%s in %s.', basename(src),
                       basename(dst), dir)

    def _create_env(self, env_path, dburi):
        parser = RawConfigParser()
        parser.read([os.path.join(self.env.path, 'conf', 'trac.ini')])
        options = dict(((section, name), value)
                       for section in parser.sections()
                       for name, value in parser.items(section))
        options[('trac', 'database')] = dburi
        options = sorted((section, name, value) for (section, name), value
                                                in options.iteritems())

        # create an environment without plugins
        env = MigrateEnvironment(env_path, create=True, options=options)
        env.shutdown()
        # copy plugins directory
        os.rmdir(os.path.join(env_path, 'plugins'))
        shutil.copytree(os.path.join(self.env.path, 'plugins'),
                        os.path.join(env_path, 'plugins'))
        # create tables for plugins to upgrade in out-process
        # (if Python is 2.5+, it can use "-m trac.admin.console" simply)
        tracadmin = """\
import sys; \
from pkg_resources import load_entry_point; \
sys.exit(load_entry_point('Trac', 'console_scripts', 'trac-admin')())"""
        proc = Popen((sys.executable, '-c', tracadmin, env_path, 'upgrade'),
                     stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=close_fds)
        stdout, stderr = proc.communicate(input='')
        for f in (proc.stdin, proc.stdout, proc.stderr):
            f.close()
        if proc.returncode != 0:
            raise TracError("upgrade command failed (stdout %r, stderr %r)" %
                            (stdout, stderr))
        return Environment(env_path)

    def _copy_tables(self, src_db, dst_db, src_dburi, dburi, inplace=False):
        self._printout('Copying tables:')

        if src_dburi.startswith('sqlite:'):
            src_db.cnx._eager = False  # avoid uses of eagar cursor
        src_cursor = src_db.cursor()
        if src_dburi.startswith('sqlite:'):
            if type(src_cursor.cursor) is not sqlite_backend.PyFormatCursor:
                raise AssertionError('src_cursor.cursor is %r' %
                                     src_cursor.cursor)
        src_tables = set(self._get_tables(src_dburi, src_cursor))
        cursor = dst_db.cursor()
        tables = set(self._get_tables(dburi, cursor)) & src_tables
        sequences = set(self._get_sequences(dburi, cursor, tables))
        progress = self._isatty()
        replace_cast = self._get_replace_cast(src_db, dst_db, src_dburi, dburi)

        # speed-up copying data with SQLite database
        if dburi.startswith('sqlite:'):
            cursor.execute('PRAGMA synchronous = OFF')
            multirows_insert = sqlite_backend.sqlite_version >= (3, 7, 11)
            max_paramters = 999
        else:
            multirows_insert = True
            max_paramters = None

        def copy_table(db, cursor, table):
            src_cursor.execute('SELECT * FROM ' + src_db.quote(table))
            columns = get_column_names(src_cursor)
            n_rows = 100
            if multirows_insert and max_paramters:
                n_rows = min(n_rows, int(max_paramters // len(columns)))
            quoted_table = db.quote(table)
            holders = '(%s)' % ','.join(['%s'] * len(columns))
            count = 0

            cursor.execute('DELETE FROM ' + quoted_table)
            while True:
                rows = src_cursor.fetchmany(n_rows)
                if not rows:
                    break
                count += len(rows)
                if progress:
                    self._printout('%d records\r  %s table... ',
                                   count, table, newline=False)
                if replace_cast is not None and table == 'report':
                    rows = self._replace_report_query(rows, columns,
                                                      replace_cast)
                query = 'INSERT INTO %s (%s) VALUES ' % \
                        (quoted_table, ','.join(map(db.quote, columns)))
                if multirows_insert:
                    cursor.execute(query + ','.join([holders] * len(rows)),
                                   sum(rows, ()))
                else:
                    cursor.executemany(query + holders, rows)

            return count

        try:
            cursor = dst_db.cursor()
            for table in sorted(tables):
                self._printout('  %s table... ', table, newline=False)
                count = copy_table(dst_db, cursor, table)
                self._printout('%d records.', count)
            for table in tables & sequences:
                dst_db.update_sequence(cursor, table)
            dst_db.commit()
        except:
            dst_db.rollback()
            raise

    def _get_replace_cast(self, src_db, dst_db, src_dburi, dst_dburi):
        if src_dburi.split(':', 1) == dst_dburi.split(':', 1):
            return None

        type_re = re.compile(r' AS ([^)]+)')
        def cast_type(db, type):
            match = type_re.search(db.cast('name', type))
            return match.group(1)

        type_maps = dict(filter(lambda (src, dst): src != dst.lower(),
                                ((cast_type(src_db, t).lower(),
                                  cast_type(dst_db, t))
                                 for t in ('text', 'int', 'int64'))))
        if not type_maps:
            return None

        cast_re = re.compile(r'\bCAST\(\s*([^\s)]+)\s+AS\s+(%s)\s*\)' %
                             '|'.join(type_maps), re.IGNORECASE)
        def replace(match):
            name, type = match.groups()
            return 'CAST(%s AS %s)' % (name, type_maps.get(type.lower(), type))
        def replace_cast(text):
            return cast_re.sub(replace, text)
        return replace_cast

    def _copy_directories(self, src_db, env):
        self._printout('Copying directories:')
        directories = self._get_directories(src_db)
        for name in directories:
            self._printout('  %s directory... ', name, newline=False)
            src = os.path.join(self.env.path, name)
            dst = os.path.join(env.path, name)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            self._printout('done.')

    def _replace_report_query(self, rows, columns, replace_cast):
        idx = columns.index('query')
        def replace(row):
            row = list(row)
            row[idx] = replace_cast(row[idx])
            return tuple(row)
        return [replace(row) for row in rows]

    def _complete_migrate(self, args):
        if len(args) == 1:
            if args[0].startswith('-'):
                return ('-i', '--in-place')
            else:
                return get_dir_list(args[0])

    def _get_tables(self, dburi, cursor):
        if dburi.startswith('sqlite:'):
            query = "SELECT name FROM sqlite_master" \
                    " WHERE type='table' AND NOT name='sqlite_sequence'"
        elif dburi.startswith('postgres:'):
            query = "SELECT tablename FROM pg_tables" \
                    " WHERE schemaname = ANY (current_schemas(false))"
        elif dburi.startswith('mysql:'):
            query = "SHOW TABLES"
        else:
            raise TracError('Unsupported %s database' % dburi.split(':')[0])
        cursor.execute(query)
        return sorted([row[0] for row in cursor])

    def _get_sequences(self, dburi, cursor, tables):
        if dburi.startswith('postgres:'):
            tables = set(tables)
            cursor.execute("""\
                SELECT c.relname
                FROM pg_class c
                INNER JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = ANY (current_schemas(false))
                AND c.relkind='S' AND c.relname LIKE %s ESCAPE '!'
                """, ('%!_id!_seq',))
            seqs = [name[:-len('_id_seq')] for name, in cursor]
            return sorted(name for name in seqs if name in tables)
        return []

    def _get_directories(self, db):
        version = self.env.get_version()
        path = ('attachments', 'files')[version >= 28]
        return (path, 'htdocs', 'templates', 'plugins')

    def _printout(self, message, *args, **kwargs):
        if args:
            message %= args
        printout(message, **kwargs)
        sys.stdout.flush()

    def _printerr(self, message, *args, **kwargs):
        if args:
            message %= args
        printerr(message, **kwargs)
        sys.stderr.flush()

    def _isatty(self):
        return sys.stdout.isatty() and sys.stderr.isatty()
