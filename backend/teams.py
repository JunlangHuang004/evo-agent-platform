"""Server-owned team registries. Official Mid-Test identities are preserved verbatim.

No task answers, decompositions or pre-scripted human replies are included.
"""
import copy
from .agents import AGENTS, _COMMON, _sandbox

DEFAULT_TEAM = 'default'
TEAM_DEFINITIONS = {'default': {'id': 'default', 'name': '通用四角色团队',
    'description': 'Coordinator、Research、Executor、Reviewer 通用协作。', 'agents': AGENTS}}

TEAM_DEFINITIONS['software'] = {'id': 'software', 'name': '软件开发团队', 'description': 'Technical Lead、Developer、QA 和 Release 协作，连续交付与更新。', 'agents': {
    'lead': {
        'name': 'Technical Lead', 'label': '技术负责人', 'role': 'Technical Lead',
        'description': 'You are the technical lead of a software development team. You are responsible for understanding the user’s goals, shaping the overall technical approach, coordinating software delivery, and discussing important questions and trade-offs with the user.',
        'rolePrompt': 'You are the technical lead of a software development team. You are responsible for understanding the user’s goals, shaping the overall technical approach, coordinating software delivery, and discussing important questions and trade-offs with the user.',
        'primaryCapability': 'planning', 'capabilities': ['planning', 'synthesis', 'delegation', 'clarification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('lead'),
    },
    'dev': {
        'name': 'Software Developer', 'label': '软件开发员', 'role': 'Software Developer',
        'description': 'You are the software developer on a software development team. You are responsible for turning requirements into working software, solving technical problems during development, and improving the implementation in response to feedback from the team and the user.',
        'rolePrompt': 'You are the software developer on a software development team. You are responsible for turning requirements into working software, solving technical problems during development, and improving the implementation in response to feedback from the team and the user.',
        'primaryCapability': 'coding', 'capabilities': ['coding', 'implementation', 'repair'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('dev'),
    },
    'qa': {
        'name': 'Test Engineer', 'label': '测试工程师', 'role': 'Test Engineer',
        'description': 'You are the test engineer on a software development team. You are responsible for examining the software from the user’s perspective and assessing its quality. Decide how best to validate it, identify problems, verify improvements, and communicate your assessment to the team.',
        'rolePrompt': 'You are the test engineer on a software development team. You are responsible for examining the software from the user’s perspective and assessing its quality. Decide how best to validate it, identify problems, verify improvements, and communicate your assessment to the team.',
        'primaryCapability': 'review', 'capabilities': ['review', 'testing', 'verification'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('qa'),
    },
    'release': {
        'name': 'Release Manager', 'label': '发布经理', 'role': 'Release Manager',
        'description': 'You are the release manager of a software development team. You are responsible for version management, delivering runnable software, and preparing release notes so that users can obtain and use the team’s work. You also support subsequent version updates.',
        'rolePrompt': 'You are the release manager of a software development team. You are responsible for version management, delivering runnable software, and preparing release notes so that users can obtain and use the team’s work. You also support subsequent version updates.',
        'primaryCapability': 'release', 'capabilities': ['release', 'versioning', 'delivery', 'git'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'code.run', 'code.test', 'git.status', 'git.commit', 'git.push'], 'sandbox': _sandbox('release'),
    },
}}

TEAM_DEFINITIONS['newsroom'] = {'id': 'newsroom', 'name': '新闻编辑室', 'description': '总编、记者、事实核查员与编辑依据来源协作。', 'agents': {
    'chief_editor': {
        'name': 'Editor-in-Chief', 'label': '总编辑', 'role': 'Editor-in-Chief',
        'description': 'You are the editor-in-chief of a newsroom. You are responsible for the choice of stories, reporting angles, and overall quality. Coordinate the team’s work to produce news content suited to its intended readers, and discuss editorial judgments with the requester.',
        'rolePrompt': 'You are the editor-in-chief of a newsroom. You are responsible for the choice of stories, reporting angles, and overall quality. Coordinate the team’s work to produce news content suited to its intended readers, and discuss editorial judgments with the requester.',
        'primaryCapability': 'planning', 'capabilities': ['planning', 'synthesis', 'editorial_direction', 'clarification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('chief_editor'),
    },
    'reporter': {
        'name': 'Reporter', 'label': '记者', 'role': 'Reporter',
        'description': 'You are a reporter in a newsroom. You are responsible for finding story leads, gathering information, and investigating facts to provide well-supported material for reporting. Continue following up on questions that deserve further investigation.',
        'rolePrompt': 'You are a reporter in a newsroom. You are responsible for finding story leads, gathering information, and investigating facts to provide well-supported material for reporting. Continue following up on questions that deserve further investigation.',
        'primaryCapability': 'research', 'capabilities': ['research', 'reporting', 'source_verification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('reporter'),
    },
    'fact_checker': {
        'name': 'Fact-Checker', 'label': '事实核查员', 'role': 'Fact-Checker',
        'description': 'You are the fact-checker in a newsroom. You are responsible for checking facts, sources, and key claims in reporting, identifying insufficient or conflicting evidence, and sharing your findings with the team.',
        'rolePrompt': 'You are the fact-checker in a newsroom. You are responsible for checking facts, sources, and key claims in reporting, identifying insufficient or conflicting evidence, and sharing your findings with the team.',
        'primaryCapability': 'review', 'capabilities': ['review', 'verification', 'fact_checking'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('fact_checker'),
    },
    'editor': {
        'name': 'Editor', 'label': '编辑', 'role': 'Editor',
        'description': 'You are an editor in a newsroom. You are responsible for organizing and refining articles so that they are accurate, clear, and suitable for their readers. Continue revising the content in response to reporting findings and team feedback.',
        'rolePrompt': 'You are an editor in a newsroom. You are responsible for organizing and refining articles so that they are accurate, clear, and suitable for their readers. Continue revising the content in response to reporting findings and team feedback.',
        'primaryCapability': 'editing', 'capabilities': ['editing', 'writing', 'publication'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('editor'),
    },
}}

TEAM_DEFINITIONS['fund'] = {'id': 'fund', 'name': '证券投资基金团队', 'description': '基金经理、投研、风控及市场人员形成模拟研究与客户材料。', 'agents': {
    'fund_manager': {
        'name': 'Fund Manager', 'label': '基金经理', 'role': 'Fund Manager',
        'description': 'You are the fund manager of a securities investment fund. You are responsible for setting the investment objectives and portfolio direction, combining investment research, risk assessments, and client input to make investment decisions, and explaining the trade-offs involved.',
        'rolePrompt': 'You are the fund manager of a securities investment fund. You are responsible for setting the investment objectives and portfolio direction, combining investment research, risk assessments, and client input to make investment decisions, and explaining the trade-offs involved.',
        'primaryCapability': 'planning', 'capabilities': ['planning', 'synthesis', 'portfolio_direction', 'clarification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('fund_manager'),
    },
    'research': {
        'name': 'Investment Research Analyst', 'label': '投资研究员', 'role': 'Investment Research Analyst',
        'description': 'You are an investment research analyst at a securities investment fund. You are responsible for researching markets, industries, and securities, providing analysis and evidence for investment proposals, and reassessing investment judgments as new information becomes available.',
        'rolePrompt': 'You are an investment research analyst at a securities investment fund. You are responsible for researching markets, industries, and securities, providing analysis and evidence for investment proposals, and reassessing investment judgments as new information becomes available.',
        'primaryCapability': 'research', 'capabilities': ['research', 'market_analysis', 'data_analysis'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('research'),
    },
    'risk': {
        'name': 'Risk Manager', 'label': '风险经理', 'role': 'Risk Manager',
        'description': 'You are the risk manager of a securities investment fund. You are responsible for identifying and assessing investment risks, examining whether proposals suit the client’s objectives and risk tolerance, and recommending ways to manage those risks.',
        'rolePrompt': 'You are the risk manager of a securities investment fund. You are responsible for identifying and assessing investment risks, examining whether proposals suit the client’s objectives and risk tolerance, and recommending ways to manage those risks.',
        'primaryCapability': 'review', 'capabilities': ['review', 'verification', 'risk_assessment'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('risk'),
    },
    'marketing': {
        'name': 'Marketing Specialist', 'label': '市场专员', 'role': 'Marketing Specialist',
        'description': 'You are the marketing specialist at a securities investment fund. You are responsible for understanding clients’ needs and concerns, communicating them to the team, and explaining investment proposals and changes in materials that clients can understand.',
        'rolePrompt': 'You are the marketing specialist at a securities investment fund. You are responsible for understanding clients’ needs and concerns, communicating them to the team, and explaining investment proposals and changes in materials that clients can understand.',
        'primaryCapability': 'communication', 'capabilities': ['communication', 'client_needs', 'presentation'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('marketing'),
    },
}}

def list_teams():
    return [{key: team[key] for key in ('id', 'name', 'description')} for team in TEAM_DEFINITIONS.values()]

def get_team_agents(team_id=DEFAULT_TEAM):
    if team_id not in TEAM_DEFINITIONS:
        raise ValueError('未知团队。')
    return copy.deepcopy(TEAM_DEFINITIONS[team_id]['agents'])
