import unittest

from app.agent_intent import classify


class AgentIntent(unittest.TestCase):
    def check(self, expected, *texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(classify(text), expected)

    def test_quota_queries(self):
        self.check('quota',
                   '지금 쿼터 얼마나 남았어?',
                   '지금 주간 쿼터 얼마나 남았어',
                   '쿼터 남은 거 알려줘',
                   '사용량 확인해줘',
                   '세션 한도 언제 리셋돼?',
                   '주간 사용량 몇 퍼 남았어',
                   '5시간 리밋 언제 풀려?',
                   'how much quota do I have left?',
                   'when does my weekly limit reset?',
                   "what's my session usage",
                   'quota remaining')

    def test_quota_actions_are_not_queries(self):
        for text in ('쿼터 늘려줘', '주간 한도 올려줘', '쿼터를 100으로 변경해줘', '사용량 로그 삭제해줘',
                     'increase my limit', 'change quota to 50', 'delete usage logs', 'reset my usage'):
            with self.subTest(text=text):
                self.assertNotEqual(classify(text), 'quota')
                self.assertNotEqual(classify(text), 'answer')
        self.check('work', '사용량 로그 삭제해줘', 'delete usage logs')

    def test_quoted_or_past_quota_questions_are_conversation(self):
        self.check('conversation',
                   '아까 "쿼터 얼마나 남았어?"라고 물어봤잖아',
                   '전에 쿼터 얼마 남았냐고 물어봤는데 왜 답 안 했어?',
                   'I asked how much quota is left earlier, why no answer?')

    def test_status(self):
        self.check('status', '진행 상황 알려줘', '어디까지 했어?', 'status')

    def test_conversation(self):
        self.check('conversation',
                   '안녕', '안녕하세요!', '고마워',
                   '이 방에서 뭐 할 수 있어?',
                   '이 결과에 대해 어떻게 생각해?',
                   '왜 그런거야?',
                   '쿼터가 뭐야?',
                   'README가 뭐야?',
                   '파이썬 데코레이터 설명해줘',
                   'hello', 'what can you do?', 'why did that happen?')

    def test_work(self):
        self.check('work',
                   'README 읽고 요약해줘',
                   '이 파일 삭제해줘',
                   'app/bot.py 수정해줘',
                   '`app/bot.py` 수정해줘',
                   '테스트 돌려줘',
                   '로그인 기능 구현해줘',
                   'fix the bug in config.py',
                   'read the README and summarize it')

    def test_work_wins_mixed_conversation(self):
        self.check('work', '왜 그런지 설명하고 이 파일 고쳐줘', '안녕, README 읽어줘', '좋아, 그럼 이 파일 삭제해줘')

    def test_quota_or_status_mixed_with_work_is_ambiguous(self):
        self.check('ambiguous',
                   'quota 얼마 남았어 그리고 파일 삭제해',
                   '쿼터 얼마 남았어? 그리고 README 수정해줘',
                   'how much quota is left and then delete the log file',
                   '진행 상황 알려줘 그리고 파일 삭제해줘')

    def test_answers(self):
        self.check('answer',
                   'alpha나 beta 말고 gamma로 써줘',
                   'alpha 대신 gamma로 해줘',
                   '1번', '2번으로 해줘', '두번째', '두 번째 거로', '첫째',
                   '좋아', '진행해', '진행해줘', '네', '아니요', '그렇게 해줘',
                   '파란색으로',
                   'option 2', 'the second one', 'go with gamma', 'yes',
                   'alpha로 하되 진행 상황은 수시로 보고해줘.',
                   'status.py', 'gamma', 'beta-2')

    def test_unclear_is_ambiguous_not_answer(self):
        self.check('ambiguous',
                   '', '   ', '음', '해줘', '그거', '이번', '흠 글쎄',
                   'asdf qwer zxcv', '1번?', 'gamma?', 'hmm', 'asdf')
        for text in ( '앞으로 어떻게 할까?', '진행해도 돼?', 'gamma로 하면 어때?'):
            with self.subTest(text=text):
                self.assertNotEqual(classify(text), 'answer')

    def test_never_infers_project(self):
        # Mentioning a project word alone is not work; the caller decides any project binding.
        self.check('conversation', '이 프로젝트는 뭐 하는 거야?')
        self.check('ambiguous', '프로젝트 목록 보여줘 주세요 음')

    def test_freeform_answer_does_not_hide_work_or_quota(self):
        self.check('work', 'alpha로 하되 README도 수정해줘', 'status.py 삭제해줘')
        self.check('quota', 'alpha로 하되 쿼터 얼마 남았어?')
        self.check('status', 'status')


if __name__ == '__main__':
    unittest.main()
