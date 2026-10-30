"""네트워크·API 없이 도는 핵심 로직 테스트 — python -m unittest discover -s tests"""
import os
import unittest
from datetime import datetime

from agent import guard, judge, match, reply
from agent.queue import KST


class MatchKey(unittest.TestCase):
    def test_key_strips_region_maeul_and_brackets(self):
        base = match._regions("구미동", "성남시 분당구")
        self.assertEqual(match._key("무지개마을3단지", base), "무지개3")
        self.assertEqual(match._key("무지개(3단지)(신한)", base), "무지개3")

    def test_same_needs_equal_numbers_and_not_brand_only(self):
        self.assertTrue(match._same("무지개3", "무지개3"))
        self.assertFalse(match._same("무지개3", "무지개12"))
        self.assertFalse(match._same("아이파크", "이문아이파크자이"))      # 브랜드만 겹치면 불인정
        self.assertTrue(match._same("하이스트", "더샵분당하이스트"))


class Pyeong(unittest.TestCase):
    def test_exact_and_band(self):
        self.assertEqual(reply._pyeong(None, "수지삼성1차 (49평) vs 만현마을 (37평)\n본문"), [-49, -37])
        self.assertEqual(reply._pyeong(None, "진도2차 20평대 와 우성2차 40평대\n본문"), [20, 40])
        self.assertEqual(reply._pyeong(None, "제목\n2.5평 베란다"), [])


class CodeCheck(unittest.TestCase):
    def test_unit_numbers_must_be_in_facts(self):
        facts = ["디큐브시티 세대수 524세대", "디큐브시티 전세가율 63%"]
        ok = "디큐브시티는 524세대이고 전세가율은 63%예요.\n제가 운영하는 리블에 정리해 뒀어요 https://reevl.co.kr/c/x"
        bad = ok.replace("524세대", "999세대")
        self.assertEqual(judge.code_check(ok, facts), [])
        self.assertTrue(any("999세대" in i for i in judge.code_check(bad, facts)))

    def test_disclosure_required(self):
        self.assertTrue(any("관계 공개" in i for i in judge.code_check("광고 https://x.kr", [])))


class Guard(unittest.TestCase):
    def test_no_disclosure_and_cap(self):
        os.environ["GTM_CAFE_DAILY_CAP"] = "0"
        issues = guard.check("https://cafe.naver.com/testcafe/1", "링크 https://x.kr", now=datetime(2026, 10, 30, 12, 0, tzinfo=KST))
        os.environ.pop("GTM_CAFE_DAILY_CAP")
        self.assertTrue(any("관계 공개" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
