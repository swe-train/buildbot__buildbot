# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from __future__ import absolute_import
from __future__ import print_function
from future.builtins import range

import os

from twisted.internet import defer
from twisted.python import log
from twisted.python import threadpool
from twisted.python.failure import Failure
from twisted.trial.unittest import SynchronousTestCase

from buildbot.config import BuilderConfig
from buildbot.interfaces import LatentWorkerCannotSubstantiate
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.interfaces import LatentWorkerSubstantiatiationCancelled
from buildbot.process.buildstep import BuildStep
from buildbot.process.factory import BuildFactory
from buildbot.process.properties import Interpolate
from buildbot.process.properties import Properties
from buildbot.process.results import CANCELLED
from buildbot.process.results import EXCEPTION
from buildbot.process.results import RETRY
from buildbot.process.results import SUCCESS
from buildbot.test.fake.latent import LatentController
from buildbot.test.fake.reactor import NonThreadPool
from buildbot.test.fake.reactor import TestReactor
from buildbot.test.fake.step import BuildStepController
from buildbot.test.util.integration import getMaster
from buildbot.test.util.misc import enable_trace
from buildbot.util.eventual import _setReactor


class TestException(Exception):

    """
    An exception thrown in tests.
    """


class Tests(SynchronousTestCase):

    def setUp(self):
        self.patch(threadpool, 'ThreadPool', NonThreadPool)
        self.reactor = TestReactor()
        self.addCleanup(self.reactor.stop)
        _setReactor(self.reactor)
        self.addCleanup(_setReactor, None)

        # to ease debugging we display the error logs in the test log
        origAddCompleteLog = BuildStep.addCompleteLog

        def addCompleteLog(self, name, _log):
            if name.endswith("err.text"):
                log.msg("got error log!", name, _log)
            return origAddCompleteLog(self, name, _log)
        self.patch(BuildStep, "addCompleteLog", addCompleteLog)

        if 'BBTRACE' in os.environ:
            enable_trace(self, ["twisted", "worker_transition.py", "util/tu", "util/path",
                                "log.py", "/mq/", "/db/", "buildbot/data/", "fake/reactor.py"])

    def tearDown(self):
        # Flush the errors logged by the master stop cancelling the builds.
        self.flushLoggedErrors(LatentWorkerSubstantiatiationCancelled)
        self.assertFalse(self.master.running, "master is still running!")

    def getMaster(self, config_dict):
        self.master = master = self.successResultOf(
            getMaster(self, self.reactor, config_dict))
        return master

    def createBuildrequest(self, master, builder_ids, properties=None):
        properties = properties.asDict() if properties is not None else None
        return self.successResultOf(
            master.data.updates.addBuildset(
                waited_for=False,
                builderids=builder_ids,
                sourcestamps=[
                    {'codebase': '',
                     'repository': '',
                     'branch': None,
                     'revision': None,
                     'project': ''},
                ],
                properties=properties,
            )
        )

    def test_latent_workers_start_in_parallel(self):
        """
        If there are two latent workers configured, and two build
        requests for them, both workers will start substantiating
        concurrently.
        """
        controllers = [
            LatentController(self, 'local1'),
            LatentController(self, 'local2'),
        ]
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local1", "local2"],
                              factory=BuildFactory()),
            ],
            'workers': [controller.worker for controller in controllers],
            'protocols': {'null': {}},
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Request two builds.
        for i in range(2):
            self.createBuildrequest(master, [builder_id])

        # Check that both workers were requested to start.
        self.assertEqual(controllers[0].starting, True)
        self.assertEqual(controllers[1].starting, True)
        for controller in controllers:
            controller.start_instance(True)
            controller.auto_stop(True)

    def test_refused_substantiations_get_requeued(self):
        """
        If a latent worker refuses to substantiate, the build request becomes unclaimed.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))

        # Indicate that the worker can't start an instance.
        controller.start_instance(False)

        # When the substantiation fails, the buildrequest becomes unclaimed.
        self.assertEqual(
            set(brids),
            {req['buildrequestid'] for req in unclaimed_build_requests}
        )
        controller.auto_stop(True)
        self.flushLoggedErrors(LatentWorkerFailedToSubstantiate)

    def test_failed_substantiations_get_requeued(self):
        """
        If a latent worker fails to substantiate, the build request becomes unclaimed.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))

        # The worker fails to substantiate.
        controller.start_instance(
            Failure(TestException("substantiation failed")))
        # Flush the errors logged by the failure.
        self.flushLoggedErrors(TestException)

        # When the substantiation fails, the buildrequest becomes unclaimed.
        self.assertEqual(
            set(brids),
            {req['buildrequestid'] for req in unclaimed_build_requests}
        )
        controller.auto_stop(True)

    def test_failed_substantiations_get_exception(self):
        """
        If a latent worker fails to substantiate, the result is an exception.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Trigger a buildrequest
        self.createBuildrequest(master, [builder_id])

        # The worker fails to substantiate.
        controller.start_instance(
            Failure(LatentWorkerCannotSubstantiate("substantiation failed")))
        # Flush the errors logged by the failure.
        self.flushLoggedErrors(LatentWorkerCannotSubstantiate)

        dbdict = self.successResultOf(
            master.db.builds.getBuildByNumber(builder_id, 1))

        # When the substantiation fails, the result is an exception.
        self.assertEqual(EXCEPTION, dbdict['results'])
        controller.auto_stop(True)

    def test_worker_accepts_builds_after_failure(self):
        """
        If a latent worker fails to substantiate, the worker is still able to accept jobs.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        controller.auto_stop(True)
        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))
        # The worker fails to substantiate.
        controller.start_instance(
            Failure(TestException("substantiation failed")))
        # Flush the errors logged by the failure.
        self.flushLoggedErrors(TestException)

        # The retry logic should only trigger after a exponential backoff
        self.assertEqual(controller.starting, False)

        # advance the time to the point where we should retry
        master.reactor.advance(controller.worker.quarantine_initial_timeout)

        # If the worker started again after the failure, then the retry logic will have
        # already kicked in to start a new build on this (the only) worker. We check that
        # a new instance was requested, which indicates that the worker
        # accepted the build.
        self.assertEqual(controller.starting, True)

        # The worker fails to substantiate(again).
        controller.start_instance(
            Failure(TestException("substantiation failed")))
        # Flush the errors logged by the failure.
        self.flushLoggedErrors(TestException)

        # advance the time to the point where we should not retry
        master.reactor.advance(controller.worker.quarantine_initial_timeout)
        self.assertEqual(controller.starting, False)
        # advance the time to the point where we should retry
        master.reactor.advance(controller.worker.quarantine_initial_timeout)
        self.assertEqual(controller.starting, True)

    def test_worker_multiple_substantiations_succeed(self):
        """
        If multiple builders trigger try to substantiate a worker at
        the same time, if the substantiation succeeds then all of
        the builds proceed.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy-1",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
                BuilderConfig(name="testy-2",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_ids = [
            self.successResultOf(master.data.updates.findBuilderId('testy-1')),
            self.successResultOf(master.data.updates.findBuilderId('testy-2')),
        ]

        finished_builds = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, build: finished_builds.append(build),
            ('builds', None, 'finished')))

        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, builder_ids)

        # The worker succeeds to substantiate.
        controller.start_instance(True)

        controller.connect_worker()

        # We check that there were two builds that finished, and
        # that they both finished with success
        self.assertEqual([build['results']
                          for build in finished_builds], [SUCCESS] * 2)
        controller.auto_stop(True)

    def test_stalled_substantiation_then_timeout_get_requeued(self):
        """
        If a latent worker substantiate, but not connect, and then be unsubstantiated,
        the build request becomes unclaimed.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))

        # We never start the worker, rather timeout it.
        master.reactor.advance(controller.worker.missing_timeout)
        # Flush the errors logged by the failure.
        self.flushLoggedErrors(defer.TimeoutError)

        # When the substantiation fails, the buildrequest becomes unclaimed.
        self.assertEqual(
            set(brids),
            {req['buildrequestid'] for req in unclaimed_build_requests}
        )
        controller.auto_stop(True)

    def test_failed_sendBuilderList_get_requeued(self):
        """
        sendBuilderList can fail due to missing permissions on the workdir,
        the build request becomes unclaimed
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))
        logs = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, log: logs.append(log),
            ('logs', None, 'new')))

        # The worker succeed to substantiate
        def remote_setBuilderList(self, dirs):
            raise TestException("can't create dir")
        controller.patchBot(self, 'remote_setBuilderList',
                            remote_setBuilderList)
        controller.start_instance(True)
        controller.connect_worker()

        # Flush the errors logged by the failure.
        self.flushLoggedErrors(TestException)

        # When the substantiation fails, the buildrequest becomes unclaimed.
        self.assertEqual(
            set(brids),
            {req['buildrequestid'] for req in unclaimed_build_requests}
        )
        # should get 2 logs (html and txt) with proper information in there
        self.assertEqual(len(logs), 2)
        logs_by_name = {}
        for _log in logs:
            fulllog = self.successResultOf(
                master.data.get(("logs", str(_log['logid']), "raw")))
            logs_by_name[fulllog['filename']] = fulllog['raw']

        for i in ["err_text", "err_html"]:
            self.assertIn("can't create dir", logs_by_name[i])
            # make sure stacktrace is present in html
            self.assertIn("buildbot.test.integration.test_latent.TestException",
                logs_by_name[i])
        controller.auto_stop(True)

    def test_failed_ping_get_requeued(self):
        """
        sendBuilderList can fail due to missing permissions on the workdir,
        the build request becomes unclaimed
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Trigger a buildrequest
        bsid, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))
        logs = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, log: logs.append(log),
            ('logs', None, 'new')))

        # The worker succeed to substantiate
        def remote_print(self, msg):
            if msg == "ping":
                raise TestException("can't ping")
        controller.patchBot(self, 'remote_print', remote_print)
        controller.start_instance(True)
        controller.connect_worker()

        # Flush the errors logged by the failure.
        self.flushLoggedErrors(TestException)

        # When the substantiation fails, the buildrequest becomes unclaimed.
        self.assertEqual(
            set(brids),
            {req['buildrequestid'] for req in unclaimed_build_requests}
        )
        # should get 2 logs (html and txt) with proper information in there
        self.assertEqual(len(logs), 2)
        logs_by_name = {}
        for _log in logs:
            fulllog = self.successResultOf(
                master.data.get(("logs", str(_log['logid']), "raw")))
            logs_by_name[fulllog['filename']] = fulllog['raw']

        for i in ["err_text", "err_html"]:
            self.assertIn("can't ping", logs_by_name[i])
            # make sure stacktrace is present in html
            self.assertIn("buildbot.test.integration.test_latent.TestException",
                logs_by_name[i])
        controller.auto_stop(True)

    def test_worker_close_connection_while_building(self):
        """
        If the worker close connection in the middle of the build, the next build can start correctly
        """
        controller = LatentController(self, 'local', build_wait_timeout=0)
        # a step that we can finish when we want
        stepcontroller = BuildStepController()
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory([stepcontroller.step]),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # Request two builds.
        for i in range(2):
            self.createBuildrequest(master, [builder_id])
        controller.auto_stop(True)

        self.assertTrue(controller.starting)
        controller.start_instance(True)
        controller.connect_worker()

        builds = self.successResultOf(
            master.data.get(("builds",)))
        self.assertEqual(builds[0]['results'], None)
        controller.disconnect_worker()
        builds = self.successResultOf(
            master.data.get(("builds",)))
        self.assertEqual(builds[0]['results'], RETRY)

        # Request one build.
        self.createBuildrequest(master, [builder_id])
        controller.start_instance(True)
        controller.connect_worker()
        builds = self.successResultOf(
            master.data.get(("builds",)))
        self.assertEqual(builds[1]['results'], None)
        stepcontroller.finish_step(SUCCESS)
        builds = self.successResultOf(
            master.data.get(("builds",)))
        self.assertEqual(builds[1]['results'], SUCCESS)

    def test_build_stop_with_cancelled_during_substantiation(self):
        """
        If a build is stopping during latent worker substantiating, the build becomes cancelled
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder = master.botmaster.builders['testy']
        builder_id = self.successResultOf(builder.getBuilderId())

        # Trigger a buildrequest
        self.createBuildrequest(master, [builder_id])

        # Stop the build
        build = builder.getBuild(0)
        build.stopBuild('no reason', results=CANCELLED)

        # Indicate that the worker can't start an instance.
        controller.start_instance(False)

        dbdict = self.successResultOf(
            master.db.builds.getBuildByNumber(builder_id, 1))
        self.assertEqual(CANCELLED, dbdict['results'])
        controller.auto_stop(True)
        self.flushLoggedErrors(LatentWorkerFailedToSubstantiate)

    def test_build_stop_with_retry_during_substantiation(self):
        """
        If master is shutting down during latent worker substantiating, the build becomes retry.
        """
        controller = LatentController(self, 'local')
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory(),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }
        master = self.getMaster(config_dict)
        builder = master.botmaster.builders['testy']
        builder_id = self.successResultOf(builder.getBuilderId())

        # Trigger a buildrequest
        _, brids = self.createBuildrequest(master, [builder_id])

        unclaimed_build_requests = []
        self.successResultOf(master.mq.startConsuming(
            lambda key, request: unclaimed_build_requests.append(request),
            ('buildrequests', None, 'unclaimed')))

        # Stop the build
        build = builder.getBuild(0)
        build.stopBuild('no reason', results=RETRY)

        # Indicate that the worker can't start an instance.
        controller.start_instance(False)

        dbdict = self.successResultOf(
            master.db.builds.getBuildByNumber(builder_id, 1))

        self.assertEqual(RETRY, dbdict['results'])
        self.assertEqual(
            set(brids),
            {req['buildrequestid'] for req in unclaimed_build_requests}
        )
        controller.auto_stop(True)
        self.flushLoggedErrors(LatentWorkerFailedToSubstantiate)

    def test_rejects_build_on_instance_with_different_type_timeout_zero(self):
        """
        If latent worker supports getting its instance type from properties that
        are rendered from build then the buildrequestdistributor must not
        schedule any builds on workers that are running different instance type
        than what these builds will require.
        """
        controller = LatentController(self, 'local',
                                      kind=Interpolate('%(prop:worker_kind)s'),
                                      build_wait_timeout=0)

        # a step that we can finish when we want
        stepcontroller = BuildStepController()
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory([stepcontroller.step]),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            'multiMaster': True,
        }

        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # create build request
        self.createBuildrequest(master, [builder_id],
                                properties=Properties(worker_kind='a'))

        # start the build and verify the kind of the worker. Note that the
        # buildmaster needs to restart the worker in order to change the worker
        # kind, so we allow it both to auto start and stop
        self.assertEqual(True, controller.starting)

        controller.auto_connect_worker = True
        controller.auto_disconnect_worker = True
        controller.auto_start(True)
        controller.auto_stop(True)
        controller.connect_worker()
        self.assertEqual(self.successResultOf(controller.get_started_kind()),
                         'a')

        # before the other build finished, create another build request
        self.createBuildrequest(master, [builder_id],
                                properties=Properties(worker_kind='b'))
        stepcontroller.finish_step(SUCCESS)

        # give the botmaster chance to insubstantiate the worker and
        # maybe substantiate it for the pending build the builds on worker
        self.reactor.advance(0.1)

        # verify that the second build restarted with the expected instance
        # kind
        self.assertEqual(self.successResultOf(controller.get_started_kind()),
                         'b')
        stepcontroller.finish_step(SUCCESS)

        dbdict = self.successResultOf(master.db.builds.getBuild(1))
        self.assertEqual(SUCCESS, dbdict['results'])
        dbdict = self.successResultOf(master.db.builds.getBuild(2))
        self.assertEqual(SUCCESS, dbdict['results'])

    def test_rejects_build_on_instance_with_different_type_timeout_nonzero(self):
        """
        If latent worker supports getting its instance type from properties that
        are rendered from build then the buildrequestdistributor must not
        schedule any builds on workers that are running different instance type
        than what these builds will require.
        """
        controller = LatentController(self, 'local',
                                      kind=Interpolate('%(prop:worker_kind)s'),
                                      build_wait_timeout=5)

        # a step that we can finish when we want
        stepcontroller = BuildStepController()
        config_dict = {
            'builders': [
                BuilderConfig(name="testy",
                              workernames=["local"],
                              factory=BuildFactory([stepcontroller.step]),
                              ),
            ],
            'workers': [controller.worker],
            'protocols': {'null': {}},
            'multiMaster': True,
        }

        master = self.getMaster(config_dict)
        builder_id = self.successResultOf(
            master.data.updates.findBuilderId('testy'))

        # create build request
        self.createBuildrequest(master, [builder_id],
                                properties=Properties(worker_kind='a'))

        # start the build and verify the kind of the worker. Note that the
        # buildmaster needs to restart the worker in order to change the worker
        # kind, so we allow it both to auto start and stop
        self.assertEqual(True, controller.starting)

        controller.auto_connect_worker = True
        controller.auto_disconnect_worker = True
        controller.auto_start(True)
        controller.auto_stop(True)
        controller.connect_worker()
        self.assertEqual(self.successResultOf(controller.get_started_kind()),
                         'a')

        # before the other build finished, create another build request
        self.createBuildrequest(master, [builder_id],
                                properties=Properties(worker_kind='b'))
        stepcontroller.finish_step(SUCCESS)

        # give the botmaster chance to insubstantiate the worker and
        # maybe substantiate it for the pending build the builds on worker
        self.reactor.advance(0.1)

        # verify build has not started, even though the worker is waiting
        # for one
        self.assertIsNone(self.successResultOf(master.db.builds.getBuild(2)))
        self.assertTrue(controller.started)

        # wait until the latent worker times out, is insubstantiated,
        # is substantiated because of pending buildrequest and starts the build
        self.reactor.advance(6)
        self.assertIsNotNone(self.successResultOf(master.db.builds.getBuild(2)))

        # verify that the second build restarted with the expected instance
        # kind
        self.assertEqual(self.successResultOf(controller.get_started_kind()),
                         'b')
        stepcontroller.finish_step(SUCCESS)

        dbdict = self.successResultOf(master.db.builds.getBuild(1))
        self.assertEqual(SUCCESS, dbdict['results'])
        dbdict = self.successResultOf(master.db.builds.getBuild(2))
        self.assertEqual(SUCCESS, dbdict['results'])
