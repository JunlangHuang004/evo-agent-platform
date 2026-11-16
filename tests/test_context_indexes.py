"""All dependency/evidence/read indexes survive compaction or fail explicitly."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from backend.agents import AGENTS
from backend.config import Config
from backend.runtime import ProtocolError, action_prompt, compress_messages, planning_prompt
from backend.teams import get_team_agents
from backend.tools import ToolGateway


class ContextIndexTests(unittest.TestCase):
    def context(self):
        artifacts=[{'id':'artifact-'+str(i),'name':'source-'+str(i)+'.md',
                    'path':'imports/artifact-'+str(i)+'/'+'nested-'*48+'source-'+str(i)+'.md',
                    'sha256':format(i,'064x'),'size':123+i,'readOnly':True,
                    'contentAccess':{'tool':'file.read','workspacePath':'source-'+str(i)+'.md'}} for i in range(8)]
        validation={'verdict':'pass','verified':True,'targetRevision':9,
                    'artifactHashes':{a['id']:a['sha256'] for a in artifacts},
                    'evidenceCallIds':['call-'+str(i) for i in range(8)],
                    'reviewerTaskId':'independent-review','feedback':'Exact review conditions. '*12}
        dependencies=[]
        for i in range(8):
            dependencies.append({'id':'task-'+str(i),'agentId':'executor','revision':i,
                'outputs':[a['id'] for a in artifacts],
                'validation':validation if i%2 else None,'result':'Full prose result '+str(i)+' '+'body '*3000,
                'taskOutput':{'taskId':'task-'+str(i),'status':'done','artifactIds':[a['id'] for a in artifacts],
                              'reviewValidation':validation if i%2 else None,
                              'outputValidation':{'status':'passed','matchedArtifactIds':[a['id'] for a in artifacts],
                                                  'issues':[],'semanticQuality':'unassessed'},
                              'rawInDependencyResult':True,'toolFailures':None,'toolUncertainties':None}})
        return {'runGoal':'Deliver the requested product without changing its requirements.',
                'task':{'id':'summary','title':'Finish requested delivery','goal':'Original task '*30,
                        'dependsOn':[d['id'] for d in dependencies],'childIds':['child-'+str(i) for i in range(8)],
                        'acceptanceCriteria':['Criterion '+str(i) for i in range(8)],
                        'outputContract':{'kind':'artifact','extensions':['.md'],'minCount':1}},
                'participants':list(AGENTS),'dependencyResults':dependencies,
                'sourceReferences':[{'url':'https://example.com/'+'segment-'*80+str(i),
                                     'title':'Source '+str(i),'callId':'source-'+str(i)} for i in range(8)],
                'authorizedArtifacts':artifacts,
                'ownArtifacts':[{'id':'own-'+str(i),'name':'own-'+str(i)+'.md','path':'own-'+str(i)+'.md',
                                 'sha256':'a'*64,'content':'private file body '*3000} for i in range(6)],
                'previousArtifacts':[{'id':'prior-'+str(i),'path':'imports/prior-'+str(i)+'/prior.md',
                                      'sourceArtifactId':'origin-'+str(i),'sourceSha256':'f'*64} for i in range(6)],
                'groupHistory':[{'role':'agent','text':'old history '*10000}]}

    def test_all_eight_dependencies_and_read_paths_remain_exact(self):
        context=self.context();messages=[{'role':'system','content':'Complete original role and tool protocol.'},
            {'role':'user','content':json.dumps(context)}, {'role':'user','content':'latest large output '*4000}]
        original=copy.deepcopy(messages);compressed=compress_messages(messages,50000)
        self.assertEqual(messages,original);self.assertLessEqual(sum(len(m['content']) for m in compressed),50000)
        reduced=json.loads(compressed[1]['content']);self.assertEqual(reduced['task'],context['task'])
        self.assertEqual(reduced['runGoal'],context['runGoal'])
        self.assertEqual(reduced['authorizedArtifacts'],context['authorizedArtifacts'])
        self.assertEqual(reduced['previousArtifacts'],context['previousArtifacts'])
        self.assertEqual(reduced['sourceReferences'],context['sourceReferences'])
        self.assertEqual(len(reduced['ownArtifacts']),6)
        self.assertTrue(all('content' not in a and a['contentOmitted'] for a in reduced['ownArtifacts']))
        self.assertEqual(len(reduced['dependencyResults']),8)
        for before,after in zip(context['dependencyResults'],reduced['dependencyResults']):
            for field in ('id','revision','outputs','validation'):self.assertEqual(after[field],before[field])
            for field in ('artifactIds','outputValidation','reviewValidation'):
                self.assertEqual(after['taskOutput'][field],before['taskOutput'][field])
            self.assertTrue(after['resultTruncated']);self.assertFalse(after['taskOutput']['rawInDependencyResult'])
            self.assertTrue(after['taskOutput']['rawTruncatedInDependencyResult'])

    def test_index_capacity_failure_does_not_keep_only_first_four(self):
        context=self.context();messages=[{'role':'system','content':'Original protocol'},
                                        {'role':'user','content':json.dumps(context)}]
        original=copy.deepcopy(messages)
        with self.assertRaisesRegex(ProtocolError,'未静默丢弃索引'):
            compress_messages(messages,4000)
        self.assertEqual(messages,original)

    def test_own_authorized_and_previous_references_are_preserved_without_dependencies(self):
        context=self.context();context['dependencyResults']=[];context['task']={'id':'reader','goal':'Read every allowed input'}
        messages=[{'role':'system','content':'Protocol'},{'role':'user','content':json.dumps(context)}]
        compressed=compress_messages(messages,15000);reduced=json.loads(compressed[1]['content'])
        self.assertEqual(reduced['authorizedArtifacts'],context['authorizedArtifacts'])
        self.assertEqual(reduced['previousArtifacts'],context['previousArtifacts'])
        self.assertEqual([a['id'] for a in reduced['ownArtifacts']],[a['id'] for a in context['ownArtifacts']])

    def test_role_protocol_and_task_are_never_clipped_to_fit(self):
        role='Verbatim role instructions and tool schemas. '*90
        context={'task':{'id':'one','goal':'Exact original task. '*80},'groupHistory':[{'text':'old '*10000}]}
        messages=[{'role':'system','content':role},{'role':'user','content':json.dumps(context)}]
        compressed=compress_messages(messages,10000)
        self.assertEqual(compressed[0]['content'],role)
        self.assertEqual(json.loads(compressed[1]['content'])['task'],context['task'])
        with self.assertRaisesRegex(ProtocolError,'角色原文'):
            compress_messages(messages,1000)

    def test_invalid_index_shape_fails_instead_of_silently_becoming_empty(self):
        for field,value in (('dependencyResults',None),('dependencyResults',[None]),
                            ('authorizedArtifacts',{}),('ownArtifacts',[None]),('previousArtifacts',[None])):
            context={'task':{'id':'one','goal':'Use evidence'},field:value,
                     'groupHistory':[{'text':'old bulk '*10000}]}
            messages=[{'role':'system','content':'Protocol'},{'role':'user','content':json.dumps(context)}]
            with self.subTest(field=field,value=value),self.assertRaisesRegex(ProtocolError,'格式无效'):
                compress_messages(messages,4000)

    def test_raw_json_task_is_not_replaced_by_an_empty_context_shell(self):
        request=json.dumps({'request':'Build a useful general product','constraints':['Keep this exact policy. '*20],
                            'audience':{'language':'Chinese','accessibility':True}},indent=2)
        messages=[{'role':'system','content':'Original planning protocol'},
                  {'role':'user','content':request}, {'role':'assistant','content':'old large reply '*5000}]
        compressed=compress_messages(messages,4000)
        self.assertEqual(compressed[1]['content'],request)
        with self.assertRaisesRegex(ProtocolError,'JSON 任务原文'):
            compress_messages(messages,300)

    def test_raw_task_string_and_unknown_requirements_are_not_disposable_fields(self):
        for request in ({'task':'Build a usable app','constraints':['Keep this exact policy. '*30]},
                        {'task':{'id':'user-defined-id','goal':'Build a usable app'},
                         'constraints':['Keep this exact policy. '*30]}):
            messages=[{'role':'system','content':'Protocol'},{'role':'user','content':json.dumps(request)},
                      {'role':'assistant','content':'bulk '*10000}]
            compressed=compress_messages(messages,3000)
            reduced=json.loads(compressed[1]['content'])
            self.assertEqual(reduced['task'],request['task']);self.assertEqual(reduced['constraints'],request['constraints'])
            with self.assertRaisesRegex(ProtocolError,'上下文容量'):
                compress_messages(messages,500)

    def test_repeated_compaction_keeps_original_prose_truncation_evidence(self):
        context={'task':{'id':'summary'},'dependencyResults':[{'id':'dependency','revision':2,'outputs':[],
                 'validation':None,'result':'Full result '+'body '*3000}], 'groupHistory':[{'text':'bulk '*10000}]}
        first=compress_messages([{'role':'system','content':'Protocol'}, {'role':'user','content':json.dumps(context)}],2000)
        original_length=len(context['dependencyResults'][0]['result'])
        for _ in range(3):
            first=compress_messages(first+[{'role':'assistant','content':'later '*1000}],1500)
            dependency=json.loads(first[1]['content'])['dependencyResults'][0]
            self.assertTrue(dependency['resultTruncated'])
            self.assertEqual(dependency['resultCharacters'],original_length)

    def test_product_prompts_keep_roles_and_describe_compact_evidence_based_delivery(self):
        for team in ('software','newsroom','fund'):
            registry=get_team_agents(team)
            agents=[{'id':key,**value} for key,value in registry.items()]
            plan=planning_prompt(agents,10)
            for agent in agents:
                self.assertIn(agent['rolePrompt'],plan)
                with tempfile.TemporaryDirectory() as folder:
                    definitions=ToolGateway(Config(Path(folder),{}),agents=registry).definitions(agent['id'])
                prompt=action_prompt(agent,definitions);self.assertIn(agent['rolePrompt'],prompt)
                self.assertIn('humanClarifications',prompt);self.assertIn('childIds/dependencyResults',prompt)
                self.assertIn('verification evidence',prompt)
                self.assertIn('"type":"publish_artifacts"',prompt)
                self.assertIn('"type":"package_delivery"',prompt)
                self.assertIn('delivery.id',prompt)
                has_code=any(item['name'].startswith('code.') for item in definitions)
                self.assertIn('offline Docker only',prompt)
                if has_code:
                    self.assertIn('language="browser"',prompt)
                    self.assertIn('language="javascript"',prompt)
                    self.assertIn('explicit resources',prompt)
                else:
                    self.assertIn('no host fallback',prompt)
                    self.assertNotIn('language="browser"',prompt)
                    self.assertNotIn('language="javascript"',prompt)
                if 'review' in agent.get('capabilities',[]) and has_code:
                    self.assertIn('key user workflows',prompt)
                    self.assertIn('failure branches',prompt)
                    self.assertIn('persistence across reload',prompt)
            self.assertIn('ceiling, not a target',plan)
            self.assertIn('do not make a separate DAG task for every question',plan)
            self.assertIn('independent validation',plan)
            self.assertIn('Do not preallocate repeated fix/retest tasks',plan)
            self.assertIn('{"kind":"delivery"}',plan)
            self.assertIn('independent QA',plan)
            self.assertIn('runnable software or website products',plan)
            self.assertIn('pure analysis needs no forced package',plan)

    def test_final_synthesis_reports_failed_dependencies_without_replacing_the_implementation(self):
        agent={'id':'coordinator',**AGENTS['coordinator']};prompt=action_prompt(agent,[])
        self.assertIn('If task.finalSummary is true',prompt)
        self.assertIn('Do not rebuild failed dependencies',prompt)
        self.assertIn('or delegate around their failure',prompt)
        self.assertIn('missing or failed independent acceptance cannot become success',prompt)
        self.assertIn('"type":"final","status":"partial"',prompt)
        plan=planning_prompt([{'id':key,**value} for key,value in AGENTS.items()],10)
        self.assertIn('combine clarification and design in one lead-owned task',plan)
        self.assertIn('usually use five real tasks',plan)


if __name__=='__main__':unittest.main()
