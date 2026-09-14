import unittest

from src.platforms.x_twitter.page_recovery import (
    XPageRecoveryConfig,
    classify_x_page_failure,
    is_x_transient_error_text,
    resolve_x_page_recovery_config,
    wait_for_x_page_recovery,
)


class FakePage:
    def __init__(self, texts):
        self.texts = list(texts)
        self.reload_count = 0
        self.selector_waits = []
        self.url = "https://x.com/user/status/123"
        self._empty_state_count = 0

    def evaluate(self, _script):
        index = min(self.reload_count, len(self.texts) - 1)
        return self.texts[index]

    def reload(self, wait_until=None, timeout=None):
        self.reload_count += 1

    def wait_for_selector(self, selector, timeout=None):
        self.selector_waits.append((selector, timeout))

    def locator(self, selector):
        return _FakeLocator(self._empty_state_count)


class _FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class TestXPageRecovery(unittest.TestCase):
    def test_detects_japanese_reload_error_text(self):
        text = "問題が発生しました。 再読み込みしてください。"
        self.assertEqual(is_x_transient_error_text(text), "問題が発生しました")

    def test_configurable_wait_values(self):
        config = resolve_x_page_recovery_config(
            {
                "x_recovery_wait_1": 10,
                "x_recovery_wait_2": 20,
                "x_recovery_wait_later": 30,
                "x_network_check_enabled": "否",
                "x_network_check_timeout": 3,
                "x_network_issue_wait": 40,
            }
        )

        self.assertEqual(config.backoff_seconds, (10.0, 20.0, 30.0))
        self.assertFalse(config.network_check_enabled)
        self.assertEqual(config.network_check_timeout, 3.0)
        self.assertEqual(config.network_issue_wait_seconds, 40.0)

    def test_waits_reloads_and_returns_after_recovery(self):
        page = FakePage(
            [
                "問題が発生しました。再読み込みしてください。 やりなおす",
                "Nintendo Everything @NinEverything Latest Nintendo updates",
            ]
        )
        messages = []

        ok = wait_for_x_page_recovery(
            page,
            log_callback=messages.append,
            page_timeout=100,
            context_label="测试页",
            backoff_seconds=(0,),
            recovery_config={"x_network_check_enabled": False},
        )

        self.assertTrue(ok)
        self.assertEqual(page.reload_count, 1)
        self.assertTrue(page.selector_waits)
        self.assertTrue(any("临时错误" in message for message in messages))
        self.assertTrue(any("已恢复正常" in message for message in messages))

    def test_network_problem_uses_network_wait_before_x_backoff(self):
        page = FakePage(
            [
                "Something went wrong. Try reloading.",
                "Recovered profile content",
            ]
        )
        messages = []
        network_checks = []

        def fake_network_checker(config: XPageRecoveryConfig):
            network_checks.append(config.network_check_url)
            return False, "timed out"

        ok = wait_for_x_page_recovery(
            page,
            log_callback=messages.append,
            page_timeout=100,
            context_label="测试页",
            recovery_config={
                "x_recovery_wait_1": 999,
                "x_network_issue_wait": 0,
                "x_network_check_url": "https://www.youtube.com/generate_204",
            },
            network_checker=fake_network_checker,
        )

        self.assertTrue(ok)
        self.assertEqual(page.reload_count, 1)
        self.assertEqual(network_checks, ["https://www.youtube.com/generate_204"])
        self.assertTrue(any("网络检测失败" in message for message in messages))

    def test_classify_rate_limit(self):
        page = FakePage(["Something went wrong. Try reloading."])
        self.assertEqual(classify_x_page_failure(page), "限流")

    def test_classify_not_found_by_text(self):
        page = FakePage(["This tweet is unavailable. It may have been deleted."])
        self.assertEqual(classify_x_page_failure(page), "不存在")

    def test_classify_page_not_exist_chinese(self):
        # X 实际中文文案："唔...该页面不存在。请尝试搜索别的内容。"
        page = FakePage(["唔...该页面不存在。请尝试搜索别的内容。"])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")

    def test_classify_frozen_account_chinese(self):
        # X 实际中文文案："冻结的账号"
        page = FakePage(["冻结的账号"])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")

    def test_classify_frozen_account_english_variant(self):
        page = FakePage(["This account is frozen."])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")

    def test_classify_account_not_exist_with_hint(self):
        # X 实际英文副标题："Try searching for another."
        page = FakePage(["This account doesn't exist. Try searching for another."])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")

    def test_classify_post_deleted_english(self):
        page = FakePage(["That post has been deleted."])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")

    def test_classify_suspended_not_permitted_english(self):
        page = FakePage(["Your account is suspended and is not permitted to access this feature."])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")

    def test_classify_not_found_by_empty_state(self):
        page = FakePage(["This account doesn't exist."])
        page._empty_state_count = 1
        self.assertEqual(classify_x_page_failure(page), "不存在")

    def test_classify_login_redirect(self):
        page = FakePage(["Login to continue"])
        page.url = "https://x.com/i/flow/login"
        self.assertEqual(classify_x_page_failure(page), "未登录")

    def test_classify_not_loaded(self):
        page = FakePage(["普通时间线内容"])
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "未加载")

    def test_classify_redirected_status_url_not_found(self):
        # 原 status URL 被重定向走（不再包含 /status/）→ 推文已不存在
        page = FakePage(["Normal timeline"])
        page.url = "https://x.com/someuser"
        self.assertEqual(classify_x_page_failure(page, wait_seconds=0.05), "不存在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
