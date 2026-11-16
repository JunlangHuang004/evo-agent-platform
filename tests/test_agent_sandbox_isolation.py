"""Opt-in, actual Docker isolation checks; no model or generated product code.

RUN_AGENT_SANDBOX_DOCKER_TESTS=1 enables the pinned local Python image. Only
fixed test scripts execute, inside Docker. Optional evidence is fixture-only.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from backend.artifacts import ArtifactStore
from backend.config import Config
from backend.memory import MemoryService
from backend.persistence import Repository
from backend.sandbox import ToolError
from backend.tools import ToolGateway


@unittest.skipUnless(os.environ.get('RUN_AGENT_SANDBOX_DOCKER_TESTS') == '1',
                     'Set RUN_AGENT_SANDBOX_DOCKER_TESTS=1 for real local Docker isolation tests.')
class AgentSandboxIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / 'team-a', {'LLM_API_KEY': 'fixture-only-isolation-key',
                                                    'GIT_TOKEN': 'fixture-only-git-token'})
        self.gateway = ToolGateway(self.config)
        self.runs = set()
        caps = self.gateway.capabilities()
        self.assertTrue(caps['dockerAvailable'], 'The opt-in test requires the existing local Docker engine.')
        self.assertTrue(caps['dockerImageReady'], 'No image pull or host fallback is permitted.')

    def tearDown(self):
        for run in self.runs:
            self.gateway.cancel(run)
        self.temp.cleanup()

    def write(self, actor, run, path, content, gateway=None):
        self.runs.add(run)
        result = (gateway or self.gateway).execute(actor, run, 'file.write', {'path': path, 'content': content})
        self.assertTrue(result['ok'])

    def python(self, actor, run, code, args=(), timeout=12, gateway=None):
        self.runs.add(run)
        return (gateway or self.gateway).execute(actor, run, 'code.test',
            {'language': 'python', 'code': code, 'args': list(args), 'timeout': timeout})

    def record(self, value):
        folder = os.environ.get('AGENT_ISOLATION_EVIDENCE_DIR')
        if folder:
            destination = Path(folder); destination.mkdir(parents=True, exist_ok=True)
            value = {'test': self.id(), 'observedAtUnix': time.time(),
                     'scope': 'Independent fixed-fixture real Docker isolation, no model/product success claim.', **value}
            (destination / (self._testMethodName + '.json')).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

    def inspect_running(self, run, actor=None, timeout=7):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.gateway.sandbox._lock:
                names = [name for name, owner_run in self.gateway.sandbox._containers.items() if owner_run == run]
            for name in names:
                result = subprocess.run([self.gateway.sandbox.docker, 'inspect', name],
                    env=self.gateway.sandbox._environment(), capture_output=True, text=True, timeout=2)
                if result.returncode:
                    continue
                value = json.loads(result.stdout)[0]
                if value['State']['Running'] and (actor is None or value['Config']['Labels'].get('evo.agent') == actor):
                    return value
            time.sleep(.03)
        self.fail('The exact owned container did not become inspectably running.')

    def test_owners_runs_and_teams_cannot_see_each_others_private_files(self):
        run = 'same-run'
        self.write('executor', run, 'private.txt', 'executor-private')
        self.write('reviewer', run, 'private.txt', 'reviewer-private')
        self.write('executor', 'other-run', 'other-run-only.txt', 'other-run-private')
        other = ToolGateway(Config(self.root / 'team-b', {}))
        self.write('executor', run, 'other-team-only.txt', 'other-team-private', gateway=other)
        self.write('executor', run, 'private.txt', 'other-team-version', gateway=other)
        script = ('import json,pathlib,sys\n'
                  'own=pathlib.Path("/workspace/private.txt").read_text()\n'
                  'assert own==sys.argv[1]\n'
                  'assert not pathlib.Path("/workspace/other-run-only.txt").exists()\n'
                  'assert not pathlib.Path("/workspace/other-team-only.txt").exists()\n'
                  'assert all(not pathlib.Path(p).exists() for p in sys.argv[2:])\n'
                  'print(json.dumps({"own":own,"foreignPathsVisible":False}))\n')
        foreign = [str(self.gateway.workspace('reviewer', run)),
                   str(self.gateway.workspace('executor', 'other-run')), str(other.workspace('executor', run))]
        results = []
        for actor, expected in [('executor', 'executor-private'), ('reviewer', 'reviewer-private')]:
            result = self.python(actor, run, script, [expected, *foreign])
            self.assertTrue(result['ok'], result.get('stderr'))
            self.assertEqual(json.loads(result['stdout'])['own'], expected)
            self.assertEqual(result['isolation']['ownerAgentId'], actor)
            self.assertEqual(result['isolation']['runId'], run)
            results.append(result)
        different_team = self.python('executor', run, 'print("other-team")', gateway=other)
        self.assertTrue(different_team['ok'])
        self.assertNotEqual(results[0]['isolation']['namespaceId'], different_team['isolation']['namespaceId'])
        self.record({'results': results, 'otherTeamIsolation': different_team['isolation']})

    def test_api_traversal_symlink_and_memory_database_are_not_exposed_to_code(self):
        run = 'private-data-run';self.write('reviewer', run, 'private.txt', 'reviewer source')
        repo = Repository(self.config.data_dir)
        try:
            memory = MemoryService(repo)
            private = memory.propose('reviewer', 'PRIVATE_MEMORY_FIXTURE', 'private fixture', 'source-task', run, [])
            self.assertEqual(memory.list('executor'), [])
            self.assertEqual(memory.list('reviewer')[0]['id'], private['id'])
            host_file = self.config.root / 'host.env';host_file.write_text('PRIVATE_HOST_CONFIGURATION_FIXTURE')
            for path in ('../reviewer/private-data-run/private.txt', '../../platform.sqlite', '.env', str(repo.path)):
                with self.subTest(path=path), self.assertRaises(ToolError):
                    self.gateway.execute('executor', run, 'file.read', {'path': path})
            link = self.gateway.workspace('executor', run) / 'peer-link.txt'
            link.symlink_to(self.gateway.workspace('reviewer', run) / 'private.txt')
            with self.assertRaises(ToolError):
                self.gateway.execute('executor', run, 'file.read', {'path': 'peer-link.txt'})
            script = ('import json,os,pathlib,sys\n'
                      'assert all(not pathlib.Path(p).exists() for p in sys.argv[1:])\n'
                      'assert not pathlib.Path("/var/run/docker.sock").exists()\n'
                      'assert not any("API_KEY" in k or "GIT_TOKEN" in k for k in os.environ)\n'
                      'assert "fixture-only-isolation-key" not in " ".join(os.environ.values())\n'
                      'print(json.dumps({"privateDatabaseVisible":False,"hostConfigVisible":False,"dockerSocketVisible":False,"secretsVisible":False}))\n')
            result = self.python('executor', run, script, [str(repo.path), str(host_file)])
            self.assertTrue(result['ok'], result.get('stderr'))
            self.record({'result': result})
        finally:
            repo.close()

    def test_explicit_artifact_grant_is_readable_and_immutable_through_api_and_docker(self):
        run = 'grant-run';self.write('executor', run, 'shared.md', 'Exact authorized immutable snapshot.')
        store = ArtifactStore(self.config.data_dir)
        artifact = store.register('executor', run, 'shared.md', 'producer-task')
        imported = 'imports/' + artifact['id'] + '/' + artifact['name']
        with self.assertRaises(ToolError):
            self.gateway.execute('reviewer', run, 'file.read', {'path': imported})
        grant = store.grant(artifact['id'], 'reviewer', run, 'orchestrator:review-task')
        self.assertEqual(self.gateway.execute('reviewer', run, 'file.read', {'path': imported})['content'], artifact['content'])
        for name, args in [('file.write', {'path': imported, 'content': 'forbidden'}),
                           ('file.delete', {'path': imported})]:
            with self.subTest(tool=name), self.assertRaises(ToolError):
                self.gateway.execute('reviewer', run, name, args, approved=True)
        script = ('import json,pathlib,sys,hashlib\n'
                  'p=pathlib.Path(sys.argv[1]);body=p.read_bytes();blocked=[]\n'
                  'for action in (lambda:p.write_text("forbidden"),lambda:p.unlink()):\n'
                  ' try:action();blocked.append(False)\n'
                  ' except OSError:blocked.append(True)\n'
                  'assert all(blocked)\n'
                  'assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]\n'
                  'print(json.dumps({"readable":True,"writeAndDeleteBlocked":blocked}))\n')
        result = self.python('reviewer', run, script, ['/imports/' + grant['path'], artifact['sha256']])
        self.assertTrue(result['ok'], result.get('stderr'))
        self.assertEqual(self.gateway.execute('executor', run, 'file.read', {'path': 'shared.md'})['sha256'], artifact['sha256'])
        self.record({'artifactId': artifact['id'], 'sha256': artifact['sha256'], 'grantReadOnly': grant['readOnly'], 'result': result})

    def test_new_invocation_has_a_new_container_but_only_the_owner_workspace_persists(self):
        run = 'fresh-run'
        first = self.python('executor', run, 'from pathlib import Path\nPath("created.txt").write_text("persistent owner state")\nprint("created")')
        second = self.python('executor', run, 'from pathlib import Path\nassert Path("created.txt").read_text()=="persistent owner state"\nprint("retained")')
        self.assertTrue(first['ok']);self.assertTrue(second['ok'])
        self.assertNotEqual(first['sandboxId'], second['sandboxId'])
        self.assertNotEqual(first['isolation']['containerId'], second['isolation']['containerId'])
        self.assertEqual(first['isolation']['namespaceId'], second['isolation']['namespaceId'])
        for actor, other_run in [('reviewer', run), ('executor', 'another-run')]:
            with self.subTest(actor=actor, run=other_run), self.assertRaises(ToolError):
                self.gateway.execute(actor, other_run, 'file.read', {'path': 'created.txt'})
        self.record({'first': first, 'second': second})

    def test_finishing_one_agent_does_not_stop_the_other_and_real_container_limits_match(self):
        run = 'parallel-agents'
        with ThreadPoolExecutor(max_workers=2) as pool:
            long = pool.submit(self.python, 'reviewer', run, 'import time\ntime.sleep(4)\nprint("reviewer survived")', (), 10)
            live = self.inspect_running(run, 'reviewer')
            short = pool.submit(self.python, 'executor', run, 'print("executor finished")')
            self.assertTrue(short.result(timeout=7)['ok'])
            self.assertFalse(long.done(), 'The other Agent must remain independently running.')
            still_running = self.inspect_running(run, 'reviewer', timeout=2)
            self.assertEqual(still_running['Id'], live['Id'])
            result = long.result(timeout=7);self.assertTrue(result['ok'], result.get('stderr'))
        host, config = live['HostConfig'], live['Config']
        self.assertEqual(host['NetworkMode'], 'none');self.assertTrue(host['ReadonlyRootfs'])
        self.assertIn('ALL', host['CapDrop']);self.assertTrue(any('no-new-privileges' in x for x in host['SecurityOpt']))
        self.assertEqual(host['Memory'], 256 * 1024 * 1024);self.assertEqual(host['MemorySwap'], 256 * 1024 * 1024)
        self.assertEqual(host['NanoCpus'], 1_000_000_000);self.assertEqual(host['PidsLimit'], 64)
        self.assertEqual(config['User'], f'{os.getuid()}:{os.getgid()}');self.assertNotEqual(config['User'].split(':')[0], '0')
        self.assertEqual(config['Labels']['evo.agent'], 'reviewer');self.assertEqual(config['Labels']['evo.run'], run)
        self.assertEqual(config['Labels']['evo.namespace'], result['isolation']['namespaceId'])
        mounts = {mount['Destination']: mount for mount in live['Mounts']}
        self.assertEqual(set(mounts), {'/workspace', '/imports'})
        self.assertEqual(Path(mounts['/workspace']['Source']), self.gateway.workspace('reviewer', run).resolve())
        self.assertTrue(mounts['/workspace']['RW']);self.assertFalse(mounts['/imports']['RW'])
        self.assertNotIn(self.config.api_key, ' '.join(config['Env']))
        self.assertNotIn(self.config.git_token, ' '.join(config['Env']))
        self.record({'containerId': live['Id'], 'imageId': live['Image'], 'isolation': result['isolation'],
                     'hostConfig': {k: host[k] for k in ('NetworkMode','ReadonlyRootfs','CapDrop','SecurityOpt','Memory','MemorySwap','NanoCpus','PidsLimit','Ulimits')},
                     'mounts': live['Mounts'], 'labels': config['Labels'], 'result': result})

    def test_cancel_is_bound_to_one_run_and_does_not_cancel_another_container(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled = pool.submit(self.python, 'executor', 'cancel-target', 'import time\ntime.sleep(30)\nprint("must be cancelled")', (), 40)
            target = self.inspect_running('cancel-target', 'executor')
            survivor = pool.submit(self.python, 'reviewer', 'keep-running', 'import time\ntime.sleep(3)\nprint("other run survived")', (), 9)
            other = self.inspect_running('keep-running', 'reviewer')
            receipt = self.gateway.cancel('cancel-target')
            self.assertEqual(receipt['cancelledContainers'], 1)
            stopped = cancelled.result(timeout=5)
            self.assertFalse(stopped['ok']);self.assertTrue(stopped['cancelled'])
            self.assertEqual(self.inspect_running('keep-running', 'reviewer', timeout=2)['Id'], other['Id'])
            result = survivor.result(timeout=7);self.assertTrue(result['ok'], result.get('stderr'))
        with self.assertRaises(ToolError):
            self.python('executor', 'cancel-target', 'print("not restarted")')
        self.record({'cancelledContainerId': target['Id'], 'survivorContainerId': other['Id'],
                     'cancellationReceipt': receipt, 'cancelledResult': stopped, 'survivorResult': result})


if __name__ == '__main__':
    unittest.main()
