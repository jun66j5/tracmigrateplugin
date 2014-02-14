# -*- coding: utf-8 -*-

import os
import shutil
import sys
import time

from trac.core import Component, implements, TracError
from trac.admin.api import IAdminCommandProvider, get_dir_list
from trac.db.api import DatabaseManager, get_column_names
from trac.env import Environment
from trac.util.text import printerr, printout


class TracMigrationCommand(Component):

    implements(IAdminCommandProvider)

    def get_admin_commands(self):
        yield ('migrate', '<tracenv|-i|--in-place> <dburi>',
               'Migrate to new environment with another database or another '
               'database without creating a new environment.',
               self._complete_migrate, self._do_migrate)

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

        options = [('trac', 'database', dburi)]
        options.extend((section, name, value)
                       for section in self.config.sections()
                       for name, value in self.config.options(section)
                       if section != 'trac' or name != 'database')
        env = Environment(env_path, create=True, options=options)
        env.upgrade()
        env.config.save()  # remove comments

        src_db = self.env.get_read_db()
        dst_db = env.get_db_cnx()
        self._copy_tables(src_db, dst_db, dburi)
        self._copy_directories(src_db, env)

    def _do_migrate_inplace(self, dburi):
        if self.config.get('trac', 'database') == dburi:
            self._printerr('Source database and destination database are '
                           'same: %s', dburi)
            return 1

        self._backup_file(self.config.filename)
        src_db = self.env.get_read_db()

        self.config.set('trac', 'database', dburi)
        dbmgr = DatabaseManager(self.env)
        connector, args = dbmgr.get_connector()
        if dburi.startswith('sqlite:'):
            connector.init_db(**args)  # create sqlite database and tables
        dst_db = connector.get_connection(**args)
        self._copy_tables(src_db, dst_db, dburi)
        self.config.save()

    def _backup_file(self, src):
        basename = os.path.basename
        dst = src + '.migrate-%d' % int(time.time())
        shutil.copyfile(src, dst)
        self._printout('Back up %s to %s in %s.', basename(src),
                       basename(dst), os.path.dirname(dst))

    def _copy_tables(self, src_db, dst_db, dburi):
        self._printout('Copying tables:')

        src_cursor = src_db.cursor()
        src_tables = set(self._get_tables(self.config.get('trac', 'database'),
                                          src_cursor))
        cursor = dst_db.cursor()
        tables = set(self._get_tables(dburi, cursor))
        tables = sorted(tables & src_tables)
        sequences = set(self._get_sequences(dburi, cursor, tables))
        progress = self._isatty()

        for table in tables:
            @self._with_transaction(dst_db)
            def copy(db):
                if db is src_db:
                    raise AssertionError('db is src_db')
                if db is not dst_db:
                    raise AssertionError('db is not dst_db')
                cursor = db.cursor()
                self._printout('  %s table... ', table, newline=False)
                if table == 'system':
                    src_cursor.execute("SELECT value FROM system "
                                       "WHERE name='initial_database_version'")
                    row = src_cursor.fetchone()
                    cursor.execute("UPDATE system SET value=%s WHERE "
                                   "name='initial_database_version'", row)
                    self._printout("copied 'initial_database_version' value.")
                    return
                src_cursor.execute('SELECT * FROM ' + src_db.quote(table))
                columns = get_column_names(src_cursor)
                query = 'INSERT INTO ' + db.quote(table) + \
                        ' (' + ','.join(map(db.quote, columns)) + ')' + \
                        ' VALUES (' + ','.join(['%s'] * len(columns)) + ')'
                cursor.execute('DELETE FROM ' + db.quote(table))
                count = 0
                while True:
                    rows = src_cursor.fetchmany(100)
                    if not rows:
                        break
                    cursor.executemany(query, rows)
                    count += len(rows)
                    if progress:
                        self._printout('%d records.\r  %s table... ',
                                       count, table, newline=False)
                if table in sequences:
                    db.update_sequence(cursor, table)
                self._printout('%d records.', count)

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

    def _with_transaction(self, db):
        def wrapper(fn):
            try:
                fn(db)
                db.commit()
            except:
                db.rollback()
                raise
        return wrapper

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
            cursor.execute(
                r"SELECT relname FROM pg_class "
                r"WHERE relkind='S' AND relname LIKE '%\_id\_seq'")
            seqs = [name[:-len('_id_seq')] for name, in cursor]
            return sorted(name for name in seqs if name in tables)
        return []

    def _get_directories(self, db):
        version = self.env.get_version(db=db)
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
