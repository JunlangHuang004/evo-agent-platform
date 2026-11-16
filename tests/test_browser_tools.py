"""Offline JS/browser tool regression tests. Opt in to actual local Docker execution."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.artifacts import ArtifactStore
from backend.config import Config
from backend.sandbox import ToolError, write_text
from backend.tools import ToolGateway

HTML = '''<!doctype html><meta charset="utf-8"><title>Booking harness fixture</title>
<button id="book">Book</button><button id="cancel">Cancel</button>
<span id="active"></span><span id="cancelled"></span><canvas id="chart" width="100" height="100"></canvas>
<script src="app.js"></script>'''
APP = '''const key = 'bookings';
let records = JSON.parse(localStorage.getItem(key) || '[]');
function render() {
 const active = records.filter(x => x.status === 'active').length;
 const cancelled = records.filter(x => x.status === 'cancelled').length;
 document.querySelector('#active').textContent = String(active);
 document.querySelector('#cancelled').textContent = String(cancelled);
 const ctx = document.querySelector('#chart').getContext('2d');
 ctx.clearRect(0, 0, 100, 100); ctx.fillStyle = '#1477cc'; ctx.fillRect(0, 100-active*30, 50, active*30);
}
function save() { localStorage.setItem(key, JSON.stringify(records)); render(); }
document.querySelector('#book').onclick = () => { records.push({status:'active'}); save(); };
document.querySelector('#cancel').onclick = () => { const item = records.find(x => x.status==='active'); if(item)item.status='cancelled'; save(); };
console.log('Actual application JavaScript executed'); render();'''


class BrowserToolUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), {})
        self.gateway = ToolGateway(self.config)
        self.manager = self.gateway.sandbox
        self.manager.docker = '/fake/docker'
        self.workspace = self.gateway.workspace('executor', 'run1')
        write_text(self.workspace, 'index.html', '<h1 id="result">Rendered</h1>')
        self.ready = patch.object(self.manager, 'capabilities', return_value={
            'dockerAvailable': True, 'dockerImageReady': True, 'browserImageReady': True})
        self.ready.start()

    def tearDown(self):
        self.ready.stop()
        self.temp.cleanup()

    def call(self, args, agent='executor'):
        return self.gateway.execute(agent, 'run1', 'code.test', args)

    def args(self, **extra):
        return {'language': 'browser', 'path': 'index.html', 'checks': [
            {'action': 'assert_text', 'selector': '#result', 'text': 'Rendered'}], **extra}

    def fake_browser(self, commands, transform=None):
        def run(argv, **kwargs):
            commands.append(argv)
            if argv[1] != 'run':
                return {'exitCode': 0, 'stdout': '', 'stderr': '', 'timedOut': False,
                        'outputLimitExceeded': False, 'durationMs': 1}
            mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == '--mount']
            source = next(item for item in mounts if 'dst=/input,' in item).split('src=', 1)[1].split(',dst=', 1)[0]
            output = next(item for item in mounts if item.endswith('dst=/evidence')).split('src=', 1)[1].split(',dst=', 1)[0]
            spec = json.loads((Path(source) / 'request.json').read_text())
            report = {'engine': 'chromium', 'executed': True, 'ok': True, 'entry': {'path': 'untrusted', 'sha256': 'bad'},
                      'testedFiles': [{'path': item['path'], 'sha256': item['sha256']} for item in spec['files']],
                      'assertions': [{'action': item['action'], 'passed': True} for item in spec['checks'] if item['action'].startswith('assert_')],
                      'console': [], 'pageErrors': [], 'blockedRequests': [], 'httpErrors': []}
            if transform:
                transform(report, spec)
            (Path(output) / 'report.json').write_text(json.dumps(report))
            (Path(output) / 'screenshot.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            return {'exitCode': 0, 'stdout': '{"ok":true}', 'stderr': '', 'timedOut': False,
                    'outputLimitExceeded': False, 'durationMs': 10}
        return run

    def test_browser_image_unavailable_has_no_host_fallback(self):
        with patch.object(self.manager, 'capabilities', return_value={'dockerAvailable': True, 'browserImageReady': False}), patch('backend.sandbox.run_process') as run:
            with self.assertRaises(ToolError): self.call(self.args())
            run.assert_not_called()

    def test_permissions_and_invalid_declarative_inputs_are_rejected(self):
        with self.assertRaises(ToolError): self.call(self.args(), agent='research')
        invalid = [self.args(code='arbitrary source'), self.args(path='https://example.com/'),
            self.args(checks=[{'action': 'click', 'selector': '#result'}]),
            self.args(checks=[{'action': 'evaluate', 'code': '1'}]),
            self.args(checks=[{'action': 'assert_count', 'selector': '*', 'count': True}]),
            self.args(checks=[{'action': 'assert_text', 'selector': '#result', 'text': 'Rendered', 'execute': 'bad'}]),
            self.args(resources={'../escape.js': 'index.html'}), self.args(resources={'x.js': '../another-agent/x.js'}),
            self.args(resources={'x.js': '.env'}), self.args(resources={'index.html': 'index.html'}),
            self.args(language='shell'), self.args(storage={'x': {'not': 'string'}})]
        with patch('backend.sandbox.run_process') as run:
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(ToolError): self.call(args)
            self.assertFalse(any(call.args[0][1] == 'run' for call in run.call_args_list))

    def test_javascript_uses_node_with_original_offline_and_readonly_import_guards(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            return {'exitCode': 0, 'stdout': 'Node fixture', 'stderr': '', 'timedOut': False, 'outputLimitExceeded': False, 'durationMs': 1}
        with patch('backend.sandbox.run_process', side_effect=run):
            result = self.call({'language': 'javascript', 'code': 'console.log(2+2)', 'args': ['fixture']})
        command = commands[0]
        for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pull=never'):
            self.assertIn(flag, command)
        self.assertIn('node', command)
        self.assertIn(self.manager.browser_image, command)
        self.assertTrue(any('dst=/imports,readonly' in item for item in command))
        self.assertTrue(result['ok']); self.assertEqual(result['runtime'], 'javascript')
        self.assertFalse(list(self.workspace.glob('run_*.cjs')))

    def test_browser_frozen_bundle_and_host_hashes_cannot_be_overridden_by_report(self):
        commands = []
        imported = self.manager.imports('executor', 'run1')
        write_text(imported, 'artifact-script/app.js', 'document.body.dataset.loaded="true";')
        with patch('backend.sandbox.run_process', side_effect=self.fake_browser(commands)):
            result = self.call(self.args(resources={'app.js': 'imports/artifact-script/app.js'}))
        self.assertTrue(result['ok'])
        expected = hashlib.sha256((self.workspace / 'index.html').read_bytes()).hexdigest()
        self.assertEqual(result['browser']['entry'], {'path': 'index.html', 'sha256': expected})
        self.assertEqual(len(result['browser']['sourceManifest']), 2)
        command = commands[0]
        for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=512m', '--pids-limit=128', '--shm-size=128m', '--init'):
            self.assertIn(flag, command)
        self.assertNotIn('--ipc=host', command); self.assertNotIn('--privileged', command)
        self.assertTrue(any('dst=/input,readonly' in item for item in command))
        self.assertTrue(any('dst=/runner/browser_runner.cjs,readonly' in item for item in command))
        self.assertFalse(any('dst=/workspace' in item for item in command))
        self.assertTrue((self.workspace / result['browser']['screenshot']['path']).is_file())
        frozen = self.config.data_dir / 'browser-inputs' / 'executor' / 'run1'
        self.assertFalse(list(frozen.iterdir()))

    def test_forged_or_incomplete_tested_hashes_and_missing_assertions_fail_closed(self):
        transformations = [lambda report, spec: report.update(testedFiles=[{'path': 'index.html', 'sha256': '0' * 64}]),
            lambda report, spec: report.update(testedFiles=[]), lambda report, spec: report.update(assertions=[]),
            lambda report, spec: report.update(pageErrors=['actual page failure'])]
        for transform in transformations:
            with self.subTest(transform=transform), patch('backend.sandbox.run_process', side_effect=self.fake_browser([], transform)):
                result = self.call(self.args())
            self.assertFalse(result['ok']); self.assertFalse(result['browser']['ok'])

    def test_stdout_claim_without_trusted_report_cannot_pass(self):
        with patch('backend.sandbox.run_process', return_value={'exitCode': 0, 'stdout': '{"ok":true,"executed":true}', 'stderr': '', 'timedOut': False, 'outputLimitExceeded': False, 'durationMs': 1}):
            result = self.call(self.args())
        self.assertFalse(result['ok']); self.assertFalse(result['browser']['executed'])

    def test_symlink_resource_and_cancelled_run_do_not_execute(self):
        (self.workspace / 'linked.js').symlink_to(self.workspace / 'index.html')
        with patch('backend.sandbox.run_process') as run:
            with self.assertRaises(ToolError): self.call(self.args(resources={'app.js': 'linked.js'}))
            self.manager._cancelled.add('run1')
            with self.assertRaises(ToolError): self.call(self.args())
            self.assertFalse(any(call.args[0][1] == 'run' for call in run.call_args_list))


@unittest.skipUnless(os.environ.get('RUN_BROWSER_DOCKER_TESTS') == '1', 'Set RUN_BROWSER_DOCKER_TESTS=1 for the pinned local Docker image')
class BrowserToolDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), {})
        self.gateway = ToolGateway(self.config)
        self.run_id = 'actual-browser'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, content):
        self.gateway.execute('executor', self.run_id, 'file.write', {'path': path, 'content': content})

    def browser(self, path='index.html', **extra):
        return self.gateway.execute('executor', self.run_id, 'code.test', {
            'language': 'browser', 'path': path,
            'checks': [{'action': 'assert_text', 'selector': '#result', 'text': 'Rendered', 'exact': True}],
            'timeout': 30, **extra})

    def test_actual_python_and_javascript_execution_remain_compatible(self):
        for args, runtime in [({'code': 'assert 2+2==4; print("Python assertion passed")'}, 'python'),
            ({'language': 'javascript', 'code': 'require("node:assert/strict").equal(2+2,4); console.log("JS assertion passed");'}, 'javascript')]:
            with self.subTest(runtime=runtime):
                result = self.gateway.execute('executor', self.run_id, 'code.test', args)
                self.assertTrue(result['ok'], result); self.assertEqual(result['runtime'], runtime)

    def test_actual_granted_resources_booking_cancel_chart_and_reload(self):
        self.write('dashboard/index.html', HTML); self.write('dashboard/app.js', APP)
        store = ArtifactStore(self.config.data_dir)
        grants = []
        for path in ['dashboard/index.html', 'dashboard/app.js']:
            artifact = store.register('executor', self.run_id, path, 'implementation')
            grants.append(store.grant(artifact['id'], 'reviewer', self.run_id, 'orchestrator:review'))
        checks = [
            {'action': 'assert_text', 'selector': '#active', 'text': '0', 'exact': True},
            {'action': 'click', 'selector': '#book'},
            {'action': 'assert_text', 'selector': '#active', 'text': '1', 'exact': True},
            {'action': 'assert_storage', 'key': 'bookings', 'value': '[{"status":"active"}]'},
            {'action': 'reload'}, {'action': 'assert_text', 'selector': '#active', 'text': '1', 'exact': True},
            {'action': 'capture_canvas', 'selector': '#chart', 'key': 'before-cancel'},
            {'action': 'click', 'selector': '#cancel'},
            {'action': 'assert_text', 'selector': '#active', 'text': '0', 'exact': True},
            {'action': 'assert_text', 'selector': '#cancelled', 'text': '1', 'exact': True},
            {'action': 'assert_canvas_changed', 'selector': '#chart', 'key': 'before-cancel'},
            {'action': 'assert_storage', 'key': 'bookings', 'value': '[{"status":"cancelled"}]'},
            {'action': 'reload'}, {'action': 'assert_text', 'selector': '#active', 'text': '0', 'exact': True},
            {'action': 'assert_text', 'selector': '#cancelled', 'text': '1', 'exact': True},
            {'action': 'assert_storage', 'key': 'bookings', 'value': '[{"status":"cancelled"}]'}]
        result = self.gateway.execute('reviewer', self.run_id, 'code.test', {'language': 'browser',
            'path': 'imports/' + grants[0]['path'], 'resources': {'app.js': 'imports/' + grants[1]['path']},
            'checks': checks, 'storage': {'bookings': '[]'}, 'timeout': 30})
        self.assertTrue(result['ok'], result)
        report = result['browser']
        self.assertTrue(report['executed']); self.assertEqual(report['assertionCount'], 11)
        self.assertEqual(report['assertionsPassed'], 11)
        self.assertEqual({item['sha256'] for item in report['testedFiles']}, {item['sha256'] for item in grants})
        self.assertIn('Actual application JavaScript executed', str(report['console']))
        screenshot = self.gateway.workspace('reviewer', self.run_id) / report['screenshot']['path']
        self.assertEqual(hashlib.sha256(screenshot.read_bytes()).hexdigest(), report['screenshot']['sha256'])
        self.assertTrue(screenshot.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))

    def test_actual_page_error_failed_assertion_and_external_request_never_pass(self):
        for suffix, source, field in [
            ('error', '<h1 id="result">Rendered</h1><script>throw new Error("Fixture real page error")</script>', 'pageErrors'),
            ('assertion', '<h1 id="result">Wrong output</h1>', 'assertions'),
            ('network', '<h1 id="result">Rendered</h1><img src="http://169.254.169.254/metadata">', 'blockedRequests')]:
            with self.subTest(case=suffix):
                self.write(suffix + '.html', source)
                result = self.browser(suffix + '.html')
                self.assertFalse(result['ok'], result)
                self.assertTrue(result['browser'][field], result)
                if suffix == 'assertion': self.assertFalse(result['browser']['assertions'][0]['passed'])


if __name__ == '__main__':
    unittest.main()
