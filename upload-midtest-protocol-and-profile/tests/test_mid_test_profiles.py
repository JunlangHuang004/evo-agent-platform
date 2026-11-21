"""CLI receipts must describe effective limits, including explicit wall-only runs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from benchmarks import mid_test_live


class MidTestProfileTests(unittest.TestCase):
    def test_cli_selects_profile_and_records_effective_limits_before_first_case(self):
        for time_only in (False,True):
            with self.subTest(time_only=time_only),tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                argv=['mid_test_live.py','--live','--suite-root',str(root/'suite'),
                      '--config-root',str(root),'--data-dir',str(root/'state'),
                      '--output',str(root/'output'),'--deadline','120','--max-calls','8']
                if time_only:argv.append('--time-only')
                server=mock.Mock()
                with mock.patch('sys.argv',argv),mock.patch.object(mid_test_live,'load_suite',return_value=[]), \
                     mock.patch('backend.server.TeamHub') as hub, \
                     mock.patch('backend.server.make_handler'), \
                     mock.patch.object(mid_test_live,'ThreadingHTTPServer',return_value=server), \
                     mock.patch('builtins.print'):
                    self.assertEqual(mid_test_live.main(),0)
                config=hub.call_args.args[0]
                saved=json.loads((root/'output'/'suite-results.json').read_text())
                settings=saved['settings']
                self.assertEqual(settings['deadlinePerTask'],120)
                self.assertEqual(config.max_run_seconds,120)
                self.assertEqual(settings['executionProfile'],'time_only' if time_only else 'bounded')
                self.assertEqual(settings['maxModelCallsPerTask'],None if time_only else 8)
                self.assertEqual(settings['maxAgentTurns'],None if time_only else 16)
                self.assertEqual(settings['maxRunTokens'],None if time_only else 200000)
                self.assertEqual(saved['cases'],[])
                self.assertEqual(saved['qualityVerdict'],'unassessed')
                self.assertEqual(config.git_remote,'')
                self.assertEqual(config.git_token,'')
                hub.return_value.shutdown.assert_called_once()
