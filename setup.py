#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup, find_packages

extra = {}

setup(
    name = 'TracMigratePlugin',
    version = '0.12.0.4',
    description = '',
    license = 'BSD', # the same as Trac
    url = 'http://trac-hacks.org/wiki/TracMigratePlugin',
    author = 'Jun Omae',
    author_email = 'jun66j5@gmail.com',
    install_requires = ['Trac'],
    packages = find_packages(exclude=['*.tests*']),
    test_suite = 'tracmigrate.tests.suite',
    entry_points = {
        'trac.plugins': [
            'tracmigrate.admin = tracmigrate.admin',
        ],
    },
    **extra)
