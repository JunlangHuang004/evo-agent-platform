"""Four-role orchestration with real file/artifact/SQLite handoffs.

The model and public web are deterministic fixtures. Docker execution is replaced
at the Tool Gateway boundary by explicit test-double receipts; generated code is
never executed on the host. Live-model/live-Docker E2E is a separate validation.
"""
import ast
from collections import defaultdict
import copy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest

from backend.agents import AGENTS
from backend.artifacts import ArtifactStore
from backend.config import Config
from backend.engine import Platform
from backend.tools import ToolGateway, ToolError


PROGRAM = 'def total(values):\n    return sum(values)\n'
EXECUTOR_TEST = 'from solution import total\nassert total([2, 3, 5]) == 10\nassert total([]) == 0\n'
SOURCE_URL = 'https://docs.python.org/3/library/functions.html#sum'
SOURCE_TEXT = 'sum(iterable, start=0) returns the sum of a start value and an iterable of numbers.'


class BoundaryFixtureGateway(ToolGateway):
    """Real confined files; fixed web and Docker-boundary test doubles only."""
    def __init__(self, config):
        super().__init__(config)
        self.receipts = []

    def capabilities(self):
        return {'dockerAvailable': False, 'dockerImageReady': False,
                'hostFallback': False, 'testDoubleCodeExecution': True}

    def _search(self, args):
        return {'ok': True, 'query': args['query'], 'fixture': True,
                'results': [{'url': SOURCE_URL, 'title': 'Python built-in sum'}]}

    def _web_fetch(self, url):
        if url != SOURCE_URL:
            raise ToolError('permission', 'The test web fixture only allows its fixed public source.')
        return {'ok': True, 'url': url, 'title': 'Python built-in sum', 'content': SOURCE_TEXT, 'fixture': True}

    def execute(self, agent_id, run_id, name, args, approved=False):
        if not name.startswith('code.'):
            return super().execute(agent_id, run_id, name, args, approved)
        if name not in AGENTS[agent_id]['tools']:
            raise ToolError('permission', 'Code fixture preserves the real actor allowlist.')
        self._validate_args(name, args)
        source = super().execute(agent_id, run_id, 'file.read', {'path': args['path']})['content']
        ast.parse(source)  # Parsing is safe; do not exec generated Python on the host.
        checked = []
        if agent_id == 'executor':
            if args['path'] not in ('solution.py', 'test_solution.py'):
                raise ToolError('parameter', 'Unexpected executor fixture script.')
            program = super().execute(agent_id, run_id, 'file.read', {'path': 'solution.py'})['content']
            if program != PROGRAM:
                raise ToolError('parameter', 'Executor fixture received unexpected program text.')
            if name == 'code.test' and source != EXECUTOR_TEST:
                raise ToolError('parameter', 'Executor fixture received unexpected test text.')
            checked.append({'path': 'solution.py', 'sha256': hashlib.sha256(program.encode()).hexdigest()})
        elif agent_id == 'reviewer':
            if name != 'code.test' or args['path'] != 'independent_check.py':
                raise ToolError('parameter', 'Reviewer must use its own independent check.')
            tree = ast.parse(source)
            imported = next((node.value.value for node in tree.body if isinstance(node, ast.Assign)
                             and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                             and any(isinstance(target, ast.Name) and target.id == 'SOURCE' for target in node.targets)), None)
            if not imported or not imported.startswith('imports/'):
                raise ToolError('permission', 'Reviewer check must refer to an explicitly granted read-only artifact.')
            program = super().execute(agent_id, run_id, 'file.read', {'path': imported})['content']
            if program != PROGRAM:
                raise ToolError('parameter', 'Independent fixture input differs from the registered program.')
            checked.append({'path': imported, 'sha256': hashlib.sha256(program.encode()).hexdigest()})
        else:
            raise ToolError('permission', 'Only executor/reviewer use the code boundary fixture.')
        receipt = {'ok': True, 'exitCode': 0, 'stdout': 'Deterministic Docker-boundary test double; no host execution.',
                   'stderr': '', 'testDouble': True, 'checkedInputs': checked,
                   'checkerSha256': hashlib.sha256(source.encode()).hexdigest()}
        self.receipts.append({'actor': agent_id, 'tool': name, 'path': args['path'], **copy.deepcopy(receipt)})
        return receipt

    def cancel(self, run_id):
        # No containers or external requests are started by this test gateway.
        return None


def step(identifier, role, dependencies=(), capability=None):
    return {'id': identifier, 'title': identifier, 'goal': identifier,
            'agentId': role, 'dependsOn': list(dependencies),
            'requiredCapabilities': [capability] if capability else [],
            'acceptanceCriteria': ['Publish the specified artifact and cite only recorded evidence.']}


def tool(name, **args):
    return {'type': 'tool', 'name': name, 'args': args}


def finish(text):
    return {'type': 'final', 'status': 'done', 'text': text,
            'reflection': {'trigger': 'artifact collaboration', 'lesson': 'Read explicitly granted artifact snapshots before using them.',
                           'conditions': 'cross-agent handoff', 'kind': 'procedural'}}


def independent_check(import_path):
    """A real check script, executed only by Docker in the optional integration run."""
    return (f'SOURCE = {import_path!r}\n'
            'from pathlib import Path\nimport importlib.util\nimport os\n'
            "target = Path('/imports') / Path(SOURCE).relative_to('imports')\n"
            "spec = importlib.util.spec_from_file_location('review_target', target)\n"
            'module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n'
            'for values, expected in [([], 0), ([4], 4), ([-2, 5], 3), ([1, 2, 3], 6)]:\n'
            '    assert module.total(values) == expected\n'
            'try:\n    with target.open("a") as stream:\n        stream.write("unexpected write")\n'
            'except OSError:\n    pass\nelse:\n    raise AssertionError("Artifact import was writable")\n'
            'assert "LLM_API_KEY" not in os.environ\n'
            'print("Reviewer independently checked all cases and read-only import.")\n')


class FourRoleModel:
    def __init__(self, evidence='own'):
        self.evidence = evidence
        self.counts = defaultdict(int)
        self.contexts = {}
        self.executor_test_call = None
        self.executor_projected_test_ids = []
        self.lock = threading.Lock()
        self.plan = {'summary': 'Coordinator scope -> Research sources -> Executor implementation -> Reviewer evidence -> Coordinator summary',
                     'tasks': [step('scope', 'coordinator', capability='planning'),
                               step('research', 'research', ['scope'], 'research'),
                               step('implement', 'executor', ['research'], 'coding'),
                               step('review', 'reviewer', ['implement'], 'review'),
                               step('summary', 'coordinator', ['review'], 'synthesis')]}

    @staticmethod
    def context(messages):
        for message in messages:
            try:
                value = json.loads(message['content'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and isinstance(value.get('task'), dict):
                return value
        raise AssertionError('Missing structured task context')

    @staticmethod
    def results(messages):
        prefix = '动作的真实结果：'
        return [json.loads(message['content'][len(prefix):]) for message in messages
                if message.get('role') == 'user' and message['content'].startswith(prefix)]

    @staticmethod
    def imported(context, name):
        return next(item['path'] for item in context['authorizedArtifacts'] if item['name'] == name)

    def complete(self, messages):
        if 'Plan an executable task DAG' in messages[0]['content']:
            return {'content': json.dumps(self.plan), 'usage': {'total_tokens': 1}}
        context = self.context(messages)
        title = context['task']['title']
        with self.lock:
            index = self.counts[title]
            self.counts[title] += 1
            self.contexts.setdefault(title, copy.deepcopy(context))
        results = self.results(messages)
        if title == 'scope':
            actions = [tool('file.write', path='scope.md', content='# Scope\nResearch Python sum, implement total, test and independently review.\n'),
                       {'type': 'publish_artifact', 'path': 'scope.md'}, finish('Scope and acceptance criteria are published.')]
            action = actions[index]
        elif title == 'research':
            actions = [tool('file.read', path=self.imported(context, 'scope.md')),
                       tool('web.search', query='Python built-in sum iterable start'),
                       tool('web.fetch', url=SOURCE_URL),
                       tool('file.write', path='research.md', content=f'# Research\nSource: {SOURCE_URL}\n{SOURCE_TEXT}\nUse the standard library.\n'),
                       {'type': 'publish_artifact', 'path': 'research.md'}, finish('Source-backed research artifact is ready for Executor.')]
            action = actions[index]
        elif title == 'implement':
            actions = [tool('file.read', path=self.imported(context, 'research.md')),
                       tool('file.write', path='solution.py', content=PROGRAM),
                       tool('file.write', path='test_solution.py', content=EXECUTOR_TEST),
                       tool('code.run', path='solution.py'), tool('code.test', path='test_solution.py'),
                       {'type': 'publish_artifact', 'path': 'solution.py'},
                       {'type': 'publish_artifact', 'path': 'test_solution.py'},
                       finish('Implementation and test sources published; gateway receipts record the test-double execution.')]
            if index == 5:
                # The last code receipt, not an earlier code.run receipt.
                # Capture the actual receipt as soon as code.test returns.
                # Later publication turns may project old tool-result bodies;
                # the server retains their IDs in progressReceipt and calls.
                self.executor_test_call = [item['callId'] for item in results if item.get('exitCode') == 0 and item.get('callId')][-1]
            elif index > 5:
                self.executor_projected_test_ids.append(context['progressReceipt']['successfulTestCallIds'])
            action = actions[index]
        elif title == 'review':
            source = self.imported(context, 'solution.py')
            independent = independent_check(source)
            if index == 0:
                action = tool('file.read', path=source)
            elif index == 1:
                action = tool('file.write', path='independent_check.py', content=independent)
            elif index == 2:
                action = tool('code.test', path='independent_check.py')
            elif index == 3:
                own_id = [item['callId'] for item in results if item.get('exitCode') == 0 and item.get('callId')][-1]
                ids = [own_id] if self.evidence == 'own' else [self.executor_test_call] if self.evidence == 'executor' else []
                action = {'type': 'review', 'taskId': context['dependencyResults'][0]['id'], 'verdict': 'pass',
                          'feedback': 'Check the actor-scoped code.test receipt; prose alone is insufficient.', 'evidenceCallIds': ids}
            elif index == 4:
                validation = next(item['validation'] for item in results if isinstance(item.get('validation'), dict))
                action = tool('file.write', path='review.md', content='# Review\n'+json.dumps(validation, sort_keys=True)+'\n')
            elif index == 5:
                action = {'type': 'publish_artifact', 'path': 'review.md'}
            else:
                action = finish('Review record published; verification is exactly the value in the recorded evidence receipt.')
        elif title == 'summary':
            if index == 0:
                action = tool('file.read', path=self.imported(context, 'review.md'))
            elif index == 1:
                review_text = next(item['content'] for item in results if isinstance(item.get('content'), str))
                action = tool('file.write', path='final_report.md', content='# Final report\nResearch, implementation and reviewer evidence are linked by this run.\n'+review_text)
            elif index == 2:
                action = {'type': 'publish_artifact', 'path': 'final_report.md'}
            else:
                action = finish('Coordinator completed the final report from the explicitly granted review artifact.')
        else:
            raise AssertionError(f'Unexpected role task: {title}')
        return {'content': json.dumps(action), 'usage': {'total_tokens': 1}}


class FourAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), {'LLM_API_KEY': 'not-a-real-unit-test-key',
                            'MAX_RETRIES': '0', 'MAX_MODEL_CALLS': '60', 'MAX_RUN_TOKENS': '300000',
                            'MAX_AGENT_TURNS': '12', 'PARALLEL_TASKS': '2'})
        self.app = None

    def tearDown(self):
        if self.app:
            self.app.shutdown()
            self.app.wait_for_idle(4)
            self.app.repo.close()
        self.temp.cleanup()

    def pipeline(self, evidence='own'):
        model = FourRoleModel(evidence)
        gateway = BoundaryFixtureGateway(self.config)
        self.app = Platform(self.config, provider=model, tools=gateway)
        self.app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                         'text': 'Research Python sum, implement a total function, independently verify, and publish a report.',
                         'participants': list(AGENTS)})
        self.assertTrue(self.app.wait_for_idle(5), 'four-role task must complete without a human gate or deadlock')
        run = self.app.snapshot()['sessions']['group']['run']
        self.assertEqual(run['status'], 'done' if evidence == 'own' else 'partial', json.dumps(run.get('error', run['steps']), ensure_ascii=False))
        return run, model, gateway

    def test_four_roles_complete_real_file_artifact_and_memory_handoffs(self):
        run, model, gateway = self.pipeline()
        tasks = {item['title']: item for item in run['steps'] if item['title'] in model.counts}
        expected = {'scope': 'coordinator', 'research': 'research', 'implement': 'executor', 'review': 'reviewer', 'summary': 'coordinator'}
        self.assertEqual(set(tasks), set(expected))
        for title, owner in expected.items():
            self.assertEqual(tasks[title]['agentId'], owner)
            self.assertEqual(tasks[title]['status'], 'done')
            self.assertTrue(any(call['agentId'] == owner and call['taskId'] == tasks[title]['id'] for call in run['calls']))
        self.assertEqual(set(event['agentId'] for event in run['events'] if event['type'] == 'task_accepted'), set(AGENTS))
        self.assertTrue(any(call['agentId'] == 'research' and call['tool'] == 'web.fetch' and call['result']['fixture'] for call in run['calls']))
        for title, filename, source_owner in [('research', 'scope.md', 'coordinator'),
                                               ('implement', 'research.md', 'research'),
                                               ('review', 'solution.py', 'executor'),
                                               ('summary', 'review.md', 'reviewer')]:
            imported = FourRoleModel.imported(model.contexts[title], filename)
            artifact = next(item for item in run['artifacts'] if item['name'] == filename)
            destination_owner = expected[title]
            path = self.config.data_dir / 'imports' / destination_owner / run['id'] / imported[len('imports/'):]
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(), artifact['content'])
            self.assertEqual(artifact['ownerAgentId'], source_owner)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o222, 0, 'handoff must be read-only')
            self.assertTrue(any(event['type'] == 'artifact_granted' and event['payload']['artifactId'] == artifact['id']
                                and event['payload']['toAgent'] == destination_owner for event in run['events']))
        research_path = FourRoleModel.imported(model.contexts['implement'], 'research.md')
        with self.assertRaises(ToolError):
            gateway.execute('executor', run['id'], 'file.write', {'path': research_path, 'content': 'tampering'}, approved=True)
        validation = tasks['implement']['validation']
        self.assertTrue(validation['verified'])
        self.assertTrue(model.executor_projected_test_ids)
        for shown_ids in model.executor_projected_test_ids:
            self.assertIn(model.executor_test_call, shown_ids)
            for shown_id in shown_ids:
                actual = next(call for call in run['calls'] if call['id'] == shown_id)
                self.assertEqual((actual['taskId'], actual['agentId'], actual['tool'], actual['status']),
                                 (tasks['implement']['id'], 'executor', 'code.test', 'success'))
                self.assertEqual(actual['revision'], tasks['implement']['revision'])
                self.assertEqual(actual['result']['exitCode'], 0)
        self.assertEqual(validation['reviewerTaskId'], tasks['review']['id'])
        evidence = next(call for call in run['calls'] if call['id'] == validation['evidenceCallIds'][0])
        self.assertEqual((evidence['agentId'], evidence['tool'], evidence['status']), ('reviewer', 'code.test', 'success'))
        self.assertEqual(evidence['result']['exitCode'], 0)
        self.assertTrue(evidence['result']['testDouble'], 'this regression must disclose the substituted Docker boundary')
        executor_receipt = next(item for item in gateway.receipts if item['actor'] == 'executor' and item['tool'] == 'code.test')
        reviewer_receipt = next(item for item in gateway.receipts if item['actor'] == 'reviewer')
        self.assertNotEqual(executor_receipt['checkerSha256'], reviewer_receipt['checkerSha256'])
        report = next(item for item in run['artifacts'] if item['name'] == 'final_report.md')
        self.assertIn('"verified": true', report['content'])
        self.assertEqual(report['ownerAgentId'], 'coordinator')
        for owner in AGENTS:
            memories = self.app.memory.list(owner)
            episodes = [item for item in memories if item['kind'] == 'episodic']
            self.assertTrue(episodes, f'{owner} must retain its own execution episode')
            self.assertTrue(all(item['ownerAgentId'] == owner and item['source_run'] == run['id'] for item in episodes))
            self.assertTrue(all(item['status'] == 'candidate' for item in memories if item['kind'] == 'procedural'))
        restored_artifacts = ArtifactStore(self.config.data_dir).list_for_run(run['id'])
        self.assertEqual({item['id'] for item in restored_artifacts}, {item['id'] for item in run['artifacts']})
        self.assertEqual(self.app.repo.get_run(run['id'])['status'], 'done')
        self.assertTrue(self.app.repo.events(run['id']))

    def test_review_without_evidence_never_becomes_verified_acceptance(self):
        run, _, _ = self.pipeline(evidence='none')
        implemented = next(item for item in run['steps'] if item['title'] == 'implement')
        self.assertFalse(implemented['validation']['verified'])
        self.assertEqual(implemented['validation']['evidenceCallIds'], [])
        report = next(item for item in run['artifacts'] if item['name'] == 'final_report.md')
        self.assertIn('"verified": false', report['content'])

    def test_executor_receipt_cannot_be_reused_as_independent_reviewer_evidence(self):
        run, model, _ = self.pipeline(evidence='executor')
        foreign = next(item for item in run['calls'] if item['id'] == model.executor_test_call)
        self.assertEqual((foreign['agentId'], foreign['tool']), ('executor', 'code.test'))
        implemented = next(item for item in run['steps'] if item['title'] == 'implement')
        self.assertFalse(implemented['validation']['verified'])
        self.assertEqual(implemented['validation']['evidenceCallIds'], [])


if __name__ == '__main__':
    unittest.main()
