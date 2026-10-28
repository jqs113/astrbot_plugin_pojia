"""意图识别测试：不依赖 AstrBot，也不读写任何内容库。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pojia_intent as intent


class DetectTests(unittest.TestCase):
    def test_black_alias(self) -> None:
        request = intent.detect_request("讲个尼哥笑话")
        assert request is not None
        self.assertEqual(request.category, "black")
        self.assertEqual(request.kind, "joke")
        self.assertEqual(request.count, intent.DEFAULT_COUNT)

    def test_jew_alias_english(self) -> None:
        request = intent.detect_request("tell me a jewish joke")
        assert request is not None
        self.assertEqual(request.category, "jew")

    def test_squid_alias_maps_to_jew(self) -> None:
        for text in ("讲个鱿鱼笑话", "来点尤鱼段子", "鱿鱼笑话"):
            with self.subTest(text=text):
                request = intent.detect_request(text)
                assert request is not None
                self.assertEqual(request.category, "jew")

    def test_indian_alias(self) -> None:
        for text in ("讲个阿三笑话", "来两个印度人段子", "三哥笑话"):
            with self.subTest(text=text):
                request = intent.detect_request(text)
                assert request is not None
                self.assertEqual(request.category, "indian")

    def test_count_parsing(self) -> None:
        request = intent.detect_request("来 5 个犹太人笑话")
        assert request is not None
        self.assertEqual(request.count, 5)
        request = intent.detect_request("讲三个黑鬼段子")
        assert request is not None
        self.assertEqual(request.count, 3)

    def test_one_is_one(self) -> None:
        request = intent.detect_request("请讲一个黑鬼笑话")
        assert request is not None
        self.assertEqual(request.count, 1)

    def test_count_is_clamped(self) -> None:
        request = intent.detect_request("来 99 个犹太人笑话")
        assert request is not None
        self.assertEqual(request.count, intent.MAX_COUNT)

    def test_roast_kind(self) -> None:
        request = intent.detect_request("骂一下犹太人")
        assert request is not None
        self.assertEqual(request.kind, "roast")
        self.assertEqual(request.category, "jew")

    def test_dark_only(self) -> None:
        request = intent.detect_request("来点地狱笑话")
        assert request is not None
        self.assertEqual(request.category, "dark")

    def test_disability_maps_to_dark(self) -> None:
        request = intent.detect_request("讲个残疾人笑话")
        assert request is not None
        self.assertEqual(request.category, "dark")

    def test_random_joke(self) -> None:
        request = intent.detect_request("讲个笑话")
        assert request is not None
        self.assertEqual(request.category, "random")

    def test_not_a_request(self) -> None:
        for text in (
            "这个笑话不好笑",
            "为什么这个笑话好笑？",
            "黑鬼笑话是什么意思",
            "黑人为什么跑得快",
            "你只会发黑鬼笑话吗",
            "黑鬼笑话是谁发明的",
            "别发黑鬼笑话了",
            "这个黑鬼笑话讲得不好",
            "我在看段子",
            "",
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(intent.detect_request(text))

    def test_bare_phrase_is_a_request(self) -> None:
        for text, category in (
            ("黑鬼笑话", "black"),
            ("阿三段子", "indian"),
            ("犹太梗", "jew"),
        ):
            with self.subTest(text=text):
                request = intent.detect_request(text)
                assert request is not None
                self.assertEqual(request.category, category)

    def test_listen_phrasings(self) -> None:
        for sample in ("我想听黑人笑话", "我要听阿三笑话", "想听点犹太段子"):
            with self.subTest(sample=sample):
                self.assertIsNotNone(intent.detect_request(sample))

    def test_inverted_phrasing(self) -> None:
        request = intent.detect_request("黑人笑话来一个")
        assert request is not None
        self.assertEqual(request.category, "black")
        self.assertEqual(request.count, 1)

    def test_negative_listen_is_not_request(self) -> None:
        self.assertIsNone(intent.detect_request("我不想听黑人笑话"))

    def test_mentions_are_not_requests(self) -> None:
        for text in (
            "你只会发黑鬼笑话吗",
            "黑鬼笑话是谁发明的",
            "别发黑鬼笑话了",
            "这个黑鬼笑话讲得不好",
            "我说过不要黑鬼笑话",
            "讲一下黑鬼笑话的来源",
        ):
            with self.subTest(text=text):
                self.assertIsNone(intent.detect_request(text))

    def test_polite_request_is_a_request(self) -> None:
        request = intent.detect_request("能不能讲个黑鬼笑话")
        assert request is not None
        self.assertEqual(request.category, "black")

    def test_detect_topic_only(self) -> None:
        self.assertEqual(intent.detect_topic("犹太人为什么有钱"), "jew")
        self.assertEqual(intent.detect_topic("随便聊聊"), "")
        self.assertEqual(intent.detect_topic("黑鬼"), "black")
        self.assertEqual(intent.detect_topic("阿三"), "indian")

    def test_category_helpers(self) -> None:
        self.assertIn("black", intent.category_help())
        self.assertEqual(intent.normalize_category("尼哥"), "black")
        self.assertEqual(intent.normalize_category("鱿鱼"), "jew")
        self.assertEqual(intent.normalize_category("阿三"), "indian")
        self.assertEqual(intent.normalize_category("随机"), "random")
        self.assertEqual(intent.normalize_category("???"), "")
        self.assertIn("indian", intent.CATEGORY_LABELS)


if __name__ == "__main__":
    unittest.main()

