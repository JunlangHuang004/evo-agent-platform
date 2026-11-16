"""Explicit process-local delivery profile; no listening server or external model call."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.config import Config
from backend.server import TeamHub, main


PROFILE = {'maxAgentTurns': 24, 'maxModelCalls': 60, 'maxRunTokens': 400000,
           'maxRunSeconds': 1800, 'maxContextChars': 40000, 'maxRetries': 1}
ATTRIBUTES = {'max_agent_turns', 'max_model_calls', 'max_run_tokens', 'max_run_seconds',
              'max_context_chars', 'max_retries'}


class DeliveryProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **extra):
        return Config(self.root, {'LLM_API_KEY': 'fixture-delivery-private-key',
                                 'GIT_TOKEN': 'fixture-private-git-token', **extra})

    def test_ordinary_configuration_keeps_existing_defaults(self):
        config = self.config()
        self.assertEqual((config.max_agent_turns, config.max_model_calls, config.max_run_tokens,
                          config.max_run_seconds, config.max_context_chars, config.max_retries),
                         (8, 28, 48000, 600, 28000, 1))
        self.assertEqual((config.max_completion_tokens, config.planning_max_tokens), (6000, 3000))

    def test_explicit_profile_changes_only_named_limits_and_never_writes_env(self):
        env_file = self.root / '.env'
        env_file.write_text('# Local fixture only\nMAX_MODEL_CALLS=12\nMAX_RETRIES=0\n')
        original_file = env_file.read_bytes()
        config = self.config(LLM_TRANSPORT_MODE='compatible', LLM_THINKING_MODE='disabled',
                             LLM_JSON_MODE='json_object', MODEL_MAX_TOKENS='4000', PLANNING_MAX_TOKENS='1500')
        before = copy.deepcopy(vars(config))
        result = config.apply_delivery_profile()
        self.assertEqual(result, PROFILE)
        changed = {key for key, value in vars(config).items() if before.get(key) != value}
        self.assertTrue(changed <= ATTRIBUTES)
        self.assertEqual({key: value for key, value in vars(config).items() if key not in ATTRIBUTES},
                         {key: value for key, value in before.items() if key not in ATTRIBUTES})
        self.assertEqual((config.max_completion_tokens, config.planning_max_tokens), (4000, 1500))
        self.assertEqual(env_file.read_bytes(), original_file)
        self.assertEqual(config.apply_delivery_profile(), result, 'the profile is idempotent, not a budget refill')
        self.assertNotIn(config.api_key, json.dumps(result))
        self.assertNotIn(config.git_token, json.dumps(result))

    def invoke(self, config, *flags):
        output = io.StringIO()
        with patch('backend.server.Config', return_value=config), \
             patch('backend.server.ThreadingHTTPServer') as factory, \
             patch('sys.argv', ['backend.server', '--port', '8199', *flags]), \
             redirect_stdout(output):
            factory.return_value.serve_forever.side_effect = KeyboardInterrupt
            main()
            self.assertEqual(factory.call_args.args[0], ('127.0.0.1', 8199))
            factory.return_value.server_close.assert_called_once()
        return output.getvalue()

    def test_default_cli_does_not_apply_or_print_delivery_profile(self):
        config = self.config()
        with patch.object(config, 'apply_delivery_profile', wraps=config.apply_delivery_profile) as apply:
            output = self.invoke(config)
            apply.assert_not_called()
        self.assertEqual(config.max_model_calls, 28)
        self.assertEqual(config.max_run_tokens, 48000)
        self.assertNotIn('Delivery profile', output)
        self.assertNotIn(config.api_key, output)
        self.assertNotIn(config.git_token, output)
        self.assertFalse((self.root / '.env').exists())

    def test_delivery_cli_applies_once_and_prints_only_effective_limit_summary(self):
        config = self.config()
        with patch.object(config, 'apply_delivery_profile', wraps=config.apply_delivery_profile) as apply:
            output = self.invoke(config, '--delivery')
            apply.assert_called_once_with()
        summary = next(line for line in output.splitlines() if line.startswith('Delivery profile limits'))
        self.assertEqual(json.loads(summary.split(': ', 1)[1]), PROFILE)
        self.assertIn('upper bounds, not estimated usage', summary)
        self.assertNotIn(config.api_key, output)
        self.assertNotIn(config.git_token, output)
        self.assertFalse((self.root / '.env').exists())

    def test_lazy_team_configs_inherit_profile_without_changing_data_isolation(self):
        config = self.config()
        config.apply_delivery_profile()
        class Instance:
            def __init__(self, config, **kwargs): self.config = config
        hub = TeamHub(config, platform_factory=Instance)
        software = hub.get('software').config
        newsroom = hub.get('newsroom').config
        self.assertIsNot(software, config)
        self.assertEqual(software.data_dir, config.data_dir / 'teams' / 'software')
        self.assertEqual(newsroom.data_dir, config.data_dir / 'teams' / 'newsroom')
        for team_config in (software, newsroom):
            self.assertEqual((team_config.max_agent_turns, team_config.max_model_calls,
                              team_config.max_run_tokens, team_config.max_run_seconds,
                              team_config.max_context_chars, team_config.max_retries),
                             tuple(PROFILE.values()))


if __name__ == '__main__':
    unittest.main()
