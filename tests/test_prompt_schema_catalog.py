"""Prompt compaction preserves complete schemas and each role's actual allowlist."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.runtime import action_prompt
from backend.teams import get_team_agents
from backend.tools import ToolGateway


class PromptSchemaCatalogTests(unittest.TestCase):
    def test_all_roles_can_resolve_every_original_schema_without_permissions_or_fields_changing(self):
        with tempfile.TemporaryDirectory() as folder:
            for team in ('software','newsroom','fund'):
                agents=get_team_agents(team)
                gateway=ToolGateway(Config(Path(folder)/team,{}),agents=agents)
                for actor,agent in agents.items():
                    definitions=gateway.definitions(actor);before=copy.deepcopy(definitions)
                    prompt=action_prompt({'id':actor,**agent},definitions)
                    self.assertIn(agent['rolePrompt'],prompt)
                    catalog=json.loads(prompt.split('Allowed tool schemas (including optional fields): ',1)[1])
                    shared=catalog.get('parameterSchemas',{}) if isinstance(catalog,dict) else {}
                    tools=catalog['tools'] if isinstance(catalog,dict) else catalog
                    self.assertEqual([item['name'] for item in tools],[item['name'] for item in definitions])
                    for shown,original in zip(tools,definitions):
                        schema=shared[shown['parametersRef']] if 'parametersRef' in shown else shown['parameters']
                        self.assertEqual(schema,original['parameters'])
                        self.assertEqual(shown['permission'],original['permission'])
                        self.assertEqual(shown['timeout'],original['timeout'])
                    self.assertEqual(definitions,before)
                    by_name={item['name']:item for item in tools}
                    if 'code.run' in by_name and 'code.test' in by_name:
                        self.assertEqual(by_name['code.run']['parametersRef'],by_name['code.test']['parametersRef'])


if __name__=='__main__':unittest.main()
