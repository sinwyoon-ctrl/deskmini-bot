"""Pure tests for app/cards.py (spec F-1/F-3): layout, short-name rule, star, other button, decided_text."""
import unittest
from datetime import UTC, datetime

from app import cards


def flat(markup):
    return [[(b['text'], b['callback_data']) for b in row] for row in markup['inline_keyboard']]


class ChoiceButtonTests(unittest.TestCase):
    def test_up_to_five_numbers_in_one_row(self):
        rows = flat(cards.choice_buttons(4, 'cx:k:{n}'))
        self.assertEqual(rows, [[('1', 'cx:k:1'), ('2', 'cx:k:2'), ('3', 'cx:k:3'), ('4', 'cx:k:4')]])

    def test_six_to_eight_use_two_rows_of_four(self):
        rows = flat(cards.choice_buttons(8, 'cx:k:{n}'))
        self.assertEqual([len(r) for r in rows], [4, 4])
        rows = flat(cards.choice_buttons(6, 'cx:k:{n}'))
        self.assertEqual([len(r) for r in rows], [4, 2])

    def test_nine_or_more_page_of_seven_with_more_and_back(self):
        first = flat(cards.choice_buttons(12, 'c:{n}', more=lambda page: 'pg:%d' % page))
        self.assertEqual(sum(len(r) for r in first[:-1]), 7)
        self.assertEqual(first[-1], [('더 보기 ▸', 'pg:1')])
        second = flat(cards.choice_buttons(12, 'c:{n}', page=1, more=lambda page: 'pg:%d' % page))
        self.assertEqual(second[0][0], ('8', 'c:8'))
        self.assertEqual(second[-1], [('◂ 이전', 'pg:0')])
        capped = flat(cards.choice_buttons(12, 'c:{n}'))
        self.assertEqual(sum(len(r) for r in capped), 8)

    def test_short_names_join_number_only_when_all_short_and_total_small(self):
        rows = flat(cards.choice_buttons(3, 'c:{n}', labels=['alpha', 'beta', 'gamma']))
        self.assertEqual([t for t, _ in rows[0]], ['1 · alpha', '2 · beta', '3 · gamma'])
        long_name = flat(cards.choice_buttons(3, 'c:{n}', labels=['alpha', 'beta', 'a-very-long-name']))
        self.assertEqual([t for t, _ in long_name[0]], ['1', '2', '3'])
        wide = flat(cards.choice_buttons(5, 'c:{n}', labels=['abcdefgh'] * 5))
        self.assertEqual([t for t, _ in wide[0]], ['1', '2', '3', '4', '5'])

    def test_recommended_star_and_other_button(self):
        rows = flat(cards.choice_buttons(3, 'c:{n}', labels=['alpha', 'beta', 'gamma'], recommended=1, other='c:free'))
        self.assertEqual(rows[0][0][0], '1 ★ · alpha')
        self.assertEqual(rows[0][-1], ('✏️ 직접 입력', 'c:free'))
        many = flat(cards.choice_buttons(5, 'c:{n}', other='c:free'))
        self.assertEqual(many[-1], [('✏️ 직접 입력', 'c:free')])

    def test_callable_callback_data_is_used_as_is(self):
        rows = flat(cards.choice_buttons(2, lambda n: 'ar:7:' + ('work', 'chat')[n - 1]))
        self.assertEqual([d for _, d in rows[0]], ['ar:7:work', 'ar:7:chat'])


class CardTextTests(unittest.TestCase):
    def test_approval_card_lines_and_escape(self):
        text = cards.approval_card('[승인 요청 · A-1]', '결제 <승인>', impact='money', body='본문', expires='10/21 09:00 까지')
        lines = text.split('\n')
        self.assertEqual(lines[0], '[승인 요청 · A-1]')
        self.assertEqual(lines[1], '결제 &lt;승인&gt;')
        self.assertEqual(lines[2], '영향: 비용 발생')
        self.assertEqual(lines[-1], '⏳ 10/21 09:00 까지')

    def test_no_impact_line_when_none_and_long_title_is_clipped(self):
        text = cards.approval_card('[x]', '가' * 90, impact='none', expires='10분 안에')
        lines = text.split('\n')
        self.assertEqual(len(lines[1]), cards.TITLE_LIMIT)
        self.assertFalse(any(line.startswith('영향') for line in lines))
        self.assertIn('가' * 90, text)

    def test_choice_card_lists_numbers_star_and_reason(self):
        text = cards.choice_card('[코덱스]', '이름을 고르세요', [('alpha', ''), ('beta', '설명')], recommended=1, reason='규칙이 같음',
                                 expires='10분 안에', escape=False)
        self.assertIn('1) alpha ★ — 규칙이 같음\n2) beta\n   설명', text)
        self.assertTrue(text.endswith('⏳ 10분 안에'))

    def test_secret_in_body_is_masked(self):
        text = cards.approval_card('[x]', '확인', body='token=supersecretvalue1234567890')
        self.assertNotIn('supersecretvalue1234567890', text)


class DecidedTextTests(unittest.TestCase):
    def test_replaces_last_hourglass_line_keeping_footer(self):
        original = '[코덱스]\n질문\n⏳ 10분 안에\n\n확인 번호: q-1'
        out = cards.decided_text(original, '✅ 2번 선택 · 10/20 21:05 · 대표')
        self.assertEqual(out, '[코덱스]\n질문\n✅ 2번 선택 · 10/20 21:05 · 대표\n\n확인 번호: q-1')

    def test_appends_when_no_hourglass(self):
        self.assertEqual(cards.decided_text('a\nb', 'V'), 'a\nb\nV')

    def test_verdict_line_uses_korean_time(self):
        when = datetime(2026, 10, 20, 12, 3, tzinfo=UTC)
        self.assertEqual(cards.verdict_line('approved', when), '✅ 승인됨 · 10/20 21:03 · 대표')
        self.assertEqual(cards.verdict_line('rejected', when, None), '⛔ 거절됨 · 10/20 21:03')
        self.assertEqual(cards.verdict_line('✅ 2번 선택', when), '✅ 2번 선택 · 10/20 21:03 · 대표')


if __name__ == '__main__':
    unittest.main()
