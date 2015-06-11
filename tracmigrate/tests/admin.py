# -*- coding: utf-8 -*-

from cStringIO import StringIO
from pkg_resources import resource_filename
import inspect
import os
import shutil
import sys
import tempfile
import unittest

from trac import db_default
from trac.attachment import Attachment
from trac.db.api import DatabaseManager, get_column_names
from trac.env import Environment
from trac.test import EnvironmentStub, get_dburi
from trac.util import read_file
from trac.wiki.admin import WikiAdmin

from tracmigrate.admin import TracMigrationCommand


class DummyOut(object):

    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False


class MigrationTestCase(unittest.TestCase):

    def setUp(self):
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        sys.stdout = sys.stderr = DummyOut()
        self.path = tempfile.mkdtemp(prefix='trac-migrate-')
        self.src_path = os.path.join(self.path, 'src')
        self.dst_path = os.path.join(self.path, 'dst')
        self.src_env = None
        self.dst_env = None
        self._destroy_db()

    def tearDown(self):
        sys.stdout = self.stdout
        sys.stderr = self.stderr
        if self.src_env:
            self.src_env.shutdown()
        if self.dst_env:
            self.dst_env.shutdown()
        shutil.rmtree(self.path)

    def _create_env(self, path, dburi):
        env = Environment(path, True, [('trac', 'database', dburi)])
        @env.with_transaction()
        def fn(db):
            cursor = db.cursor()
            cursor.execute("UPDATE system SET value='21' "
                           "WHERE name='initial_database_version'")
        pages_dir = resource_filename('trac.wiki', 'default-pages')
        WikiAdmin(env).load_pages(pages_dir)
        att = Attachment(env, 'wiki', 'WikiStart')
        att.insert('filename.txt', StringIO('test'), 4)
        env.shutdown()

    if 'destroying' in inspect.getargspec(EnvironmentStub.__init__)[0]:
        def _destroy_db(self):
            EnvironmentStub(destroying=True)
    else:
        def _destroy_db(self):
            EnvironmentStub().destroy_db()

    def _get_all_records(self, env):
        def primary(row, columns):
            if len(columns) == 1:
                return row[columns[0]]
            else:
                return tuple(row[col] for col in columns)

        db = env.get_read_db()
        cursor = db.cursor()
        records = {}
        for table in db_default.schema:
            primary_cols = ','.join(db.quote(col) for col in table.key)
            query = "SELECT * FROM %s ORDER BY %s" % (db.quote(table.name),
                                                      primary_cols)
            cursor.execute(query)
            columns = get_column_names(cursor)
            rows = {}
            for row in cursor:
                row = dict(zip(columns, row))
                rows[primary(row, table.key)] = row
            records[table.name] = rows
        return records

    def _migrate(self, env, path, dburi):
        TracMigrationCommand(env)._do_migrate(path, dburi)

    def _migrate_inplace(self, env, dburi):
        self._migrate(env, '--in-place', dburi)

    def _compare_records(self, expected, actual):
        self.assertEqual(expected.keys(), actual.keys())
        for table in db_default.schema:
            name = table.name
            if name == 'report':
                self.assertEqual(expected[name].keys(), actual[name].keys())
            else:
                self.assertEqual(expected[name], actual[name])

    def test_migrate_from_sqlite_to_env(self):
        self._create_env(self.src_path, 'sqlite:db/trac.db')
        dburi = get_dburi()
        if dburi == 'sqlite::memory:':
            dburi = 'sqlite:db/trac.db'

        self.src_env = Environment(self.src_path)
        src_records = self._get_all_records(self.src_env)
        self._migrate(self.src_env, self.dst_path, dburi)
        self.dst_env = Environment(self.dst_path)
        dst_records = self._get_all_records(self.dst_env)
        self.assertEqual({'name': 'initial_database_version', 'value': '21'},
                         dst_records['system']['initial_database_version'])
        self._compare_records(src_records, dst_records)
        att = Attachment(self.dst_env, 'wiki', 'WikiStart', 'filename.txt')
        self.assertEqual('test', read_file(att.path))

    def test_migrate_from_sqlite_inplace(self):
        self._create_env(self.src_path, 'sqlite:db/trac.db')
        dburi = get_dburi()
        if dburi in ('sqlite::memory:', 'sqlite:db/trac.db'):
            dburi = 'sqlite:db/trac-migrate.db'

        self.src_env = Environment(self.src_path)
        src_records = self._get_all_records(self.src_env)
        self._migrate_inplace(self.src_env, dburi)
        self.src_env.shutdown()
        self.src_env = Environment(self.src_path)
        dst_records = self._get_all_records(self.src_env)
        self.assertEqual({'name': 'initial_database_version', 'value': '21'},
                         dst_records['system']['initial_database_version'])
        self._compare_records(src_records, dst_records)

    def test_migrate_to_sqlite_env(self):
        dburi = get_dburi()
        if dburi == 'sqlite::memory:':
            dburi = 'sqlite:db/trac.db'
        self._create_env(self.src_path, dburi)

        self.src_env = Environment(self.src_path)
        src_records = self._get_all_records(self.src_env)
        self._migrate(self.src_env, self.dst_path, 'sqlite:db/trac.db')
        self.dst_env = Environment(self.dst_path)
        dst_records = self._get_all_records(self.dst_env)
        self.assertEqual({'name': 'initial_database_version', 'value': '21'},
                         dst_records['system']['initial_database_version'])
        self._compare_records(src_records, dst_records)
        att = Attachment(self.dst_env, 'wiki', 'WikiStart', 'filename.txt')
        self.assertEqual('test', read_file(att.path))

    def test_migrate_to_sqlite_inplace(self):
        dburi = get_dburi()
        if dburi in ('sqlite::memory:', 'sqlite:db/trac.db'):
            dburi = 'sqlite:db/trac-migrate.db'
        self._create_env(self.src_path, dburi)

        self.src_env = Environment(self.src_path)
        src_records = self._get_all_records(self.src_env)
        self._migrate_inplace(self.src_env, 'sqlite:db/trac.db')
        self.src_env.shutdown()
        self.src_env = Environment(self.src_path)
        dst_records = self._get_all_records(self.src_env)
        self.assertEqual({'name': 'initial_database_version', 'value': '21'},
                         dst_records['system']['initial_database_version'])
        self._compare_records(src_records, dst_records)


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(MigrationTestCase))
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
