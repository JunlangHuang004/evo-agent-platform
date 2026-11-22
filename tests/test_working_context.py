"""Working-context board and receipt indexes. No model and no network."""
import json
import unittest

from backend.working_context import build_team_board, compact_working_receipts


def receipt(body):
    return {'role': 'user', 'content': '动作的真实结果：' + json.dumps(body, ensure_ascii=False)}


class WorkingContextTests(unittest.TestCase):
    def test_older_file_body_becomes_an_index_and_the_latest_stays(self):
        messages = [
            {'role': 'system', 'content': 'protocol'},
            receipt({'ok': True, 'path': 'index.html', 'sha256': 'aa' * 32, 'callId': 'call-1', 'content': 'first page'}),
            receipt({'ok': True, 'path': 'index.html', 'sha256': 'bb' * 32, 'callId': 'call-2', 'content': 'second page'}),
        ]
        compacted = compact_working_receipts(messages)
        older = json.loads(compacted[1]['content'].split('：', 1)[1])
        latest = json.loads(compacted[2]['content'].split('：', 1)[1])
        self.assertTrue(older['contentOmitted'])
        self.assertNotIn('content', older)
        self.assertEqual(older['callId'], 'call-1')
        self.assertEqual(older['sha256'], 'aa' * 32)
        self.assertEqual(latest['content'], 'second page')
        self.assertEqual(older['readNote'], '正文已省略。按路径和 SHA256 用 file.read 再读；省略不是已读，也不是测试通过。')

    def test_fetched_page_stays_rereadable_by_snapshot_and_call_id(self):
        messages = [
            receipt({'ok': True, 'url': 'https://example.test/a', 'title': 'A', 'callId': 'fetch-1',
                     'content': 'old article body',
                     'sourceSnapshot': {'path': 'imports/web-source-old/body.txt', 'sha256': 'cc' * 32}}),
            receipt({'ok': True, 'url': 'https://example.test/a', 'title': 'A', 'callId': 'fetch-2',
                     'content': 'new article body',
                     'sourceSnapshot': {'path': 'imports/web-source-new/body.txt', 'sha256': 'dd' * 32}}),
        ]
        older = json.loads(compact_working_receipts(messages)[0]['content'].split('：', 1)[1])
        self.assertTrue(older['contentOmitted'])
        self.assertNotIn('old article body', json.dumps(older))
        self.assertEqual(older['callId'], 'fetch-1')
        self.assertEqual(older['sourceSnapshot']['path'], 'imports/web-source-old/body.txt')

    def test_board_keeps_clarifications_and_hashes_without_private_text(self):
        run = {
            'id': 'run-1', 'title': '预约系统',
            'steps': [{'id': 'implement', 'title': '实现页面', 'agentId': 'dev', 'status': 'partial', 'revision': 1,
                       'error': {'code': 'response_schema'}}],
            'artifacts': [{'name': 'index.html', 'path': 'index.html', 'ownerAgentId': 'dev', 'sha256': 'ab' * 32,
                           'content': 'private page source'}],
            'acceptanceGaps': ['尚未独立验收'],
            'calls': [{'id': 'fetch-9', 'tool': 'web.fetch', 'status': 'success', 'result': {
                'url': 'https://example.test/source', 'title': '来源', 'content': 'full fetched article',
                'sourceSnapshot': {'path': 'imports/web-source-9/body.txt', 'sha256': 'ee' * 32}}}],
            'conversation': [{'role': 'agent', 'agentId': 'dev', 'text': '开发者的失败尝试和私有推理'}],
        }
        clarifications = [{'receiptId': 'clarify-1', 'question': '用哪一年的记录？', 'answer': '用已经发布的预约记录'}]
        board = build_team_board(run, clarifications)
        encoded = json.dumps(board, ensure_ascii=False)
        self.assertEqual(board['clarificationReceiptIds'], ['clarify-1'])
        self.assertNotIn('用已经发布的预约记录', encoded)
        self.assertEqual(board['files'][0]['sha256'], 'ab' * 32)
        self.assertEqual(board['sources'][0]['callId'], 'fetch-9')
        self.assertEqual(board['sources'][0]['path'], 'imports/web-source-9/body.txt')
        self.assertNotIn('private page source', encoded)
        self.assertNotIn('full fetched article', encoded)
        self.assertNotIn('私有推理', encoded)
        self.assertNotIn('ownTaskExperience', encoded)

    def test_unchanged_receipts_keep_the_same_list(self):
        messages = [receipt({'ok': True, 'path': 'notes.md', 'sha256': 'ff' * 32, 'content': 'only body'})]
        self.assertIs(compact_working_receipts(messages), messages)


if __name__ == '__main__':
    unittest.main()
