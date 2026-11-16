"""A bounded time-only ceiling retains actual offered bytes, never lost text."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.context_budget import adapt_context,visible_bodies,missing_bodies


class ContextCeilingTests(unittest.TestCase):
    def messages(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/context_actual_bodies_r6.json').read_text())
        # Source bodies/results are the exact actual bytes. Neutral headers of
        # the observed protected size avoid copying requester/rolepack history.
        system='Original immutable protocol. '
        system+='P'*(fixture['originalSystemCharacters']-len(system))
        context={'task':{'id':'capacity-audit','goal':''},'runGoal':'Offline capacity check','groupHistory':[]}
        encode=lambda:json.dumps(context,ensure_ascii=False,separators=(',',':'))
        context['task']['goal']='R'*(fixture['originalInitialCharacters']-len(encode()))
        return [{'role':'system','content':system},{'role':'user','content':encode()},*fixture['toolMessages']]

    def test_time_only_120k_preserves_real_offered_windows_that_80k_cannot_fit(self):
        with tempfile.TemporaryDirectory() as root:
            config=Config(Path(root),{'LLM_API_KEY':'offline-capacity'})
            self.assertEqual(config.max_context_chars,28000)
            capped=Config(Path(root)/'bounded',{'LLM_API_KEY':'offline','MAX_CONTEXT_CHARS':'200000'})
            self.assertEqual(capped.max_context_chars,80000,'bounded maximum unchanged')
            before=(config.max_completion_tokens,config.model_timeout,config.max_tasks,config.max_depth)
            profile=config.apply_time_only_profile()
            self.assertEqual(profile['maxContextChars'],120000)
            self.assertEqual(profile['initialContextChars'],40000)
            self.assertEqual(config.max_run_seconds,1800)
            self.assertTrue(all(profile[key] is None for key in ('maxAgentTurns','maxModelCalls','maxRunTokens')))
            self.assertEqual(before,(config.max_completion_tokens,config.model_timeout,config.max_tasks,config.max_depth))
            messages=self.messages();original=copy.deepcopy(messages);bodies=visible_bodies(messages)
            self.assertEqual(len(bodies),9,'actual independent line windows, not fabricated full files')
            small,small_state=adapt_context(messages,40000,80000,40000)
            self.assertTrue(missing_bodies(bodies,visible_bodies(small)))
            large,state=adapt_context(messages,config.initial_context_chars,config.max_context_chars,config.initial_context_chars)
            self.assertEqual([row['limit'] for row in state['attempts']],[40000,60000,80000,120000])
            self.assertEqual(state['missingBodiesAfter'],0)
            self.assertEqual(missing_bodies(bodies,visible_bodies(large)),[])
            self.assertLessEqual(sum(len(row['content']) for row in large),120000)
            self.assertEqual(messages,original,'projection cannot mutate source evidence')

    def test_omitted_indexes_remain_omitted_after_a_larger_ceiling(self):
        messages=self.messages()
        for message in messages[2:]:
            prefix='动作的真实结果：'
            if message['role']!='user' or not message['content'].startswith(prefix):continue
            value=json.loads(message['content'][len(prefix):])
            if isinstance(value.get('content'),str):
                size=len(value.pop('content'))
                value.update(contentOmitted=True,contentCharacters=size,contextBudgetReason='prior_projection_omitted_body')
                message['content']=prefix+json.dumps(value,ensure_ascii=False,separators=(',',':'))
        self.assertEqual(visible_bodies(messages),[])
        original=copy.deepcopy(messages)
        for ceiling in (80000,120000):
            projected,state=adapt_context(messages,40000,ceiling,40000)
            self.assertEqual(visible_bodies(projected),[])
            self.assertEqual(state['visibleBodiesBefore'],0)
            self.assertTrue(any('contentOmitted' in row['content'] for row in projected))
            self.assertLessEqual(state['characters'],ceiling)
        self.assertEqual(messages,original)


if __name__=='__main__':unittest.main()
