# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import StringIO
import sys

from nova import twistd
from nova import exception
from nova import flags
from nova import test


FLAGS = flags.FLAGS


class TwistdTestCase(test.TrialTestCase):
    def setUp(self):
        super(TwistdTestCase, self).setUp()
        self.Options = twistd.WrapTwistedOptions(twistd.TwistdServerOptions)
        sys.stdout = StringIO.StringIO()

    def tearDown(self):
        super(TwistdTestCase, self).tearDown()
        sys.stdout = sys.__stdout__

    def test_basic(self):
        options = self.Options()
        argv = options.parseOptions()

    def test_logfile(self):
        options = self.Options()
        argv = options.parseOptions(['--logfile=foo'])
        self.assertEqual(FLAGS.logfile, 'foo')

    def test_help(self):
        options = self.Options()
        self.assertRaises(SystemExit, options.parseOptions, ['--help'])
        self.assert_('pidfile' in sys.stdout.getvalue())
