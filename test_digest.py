import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import digest


def story(title, n, tag=None):
    return {"title": f"{title} {n}", "url": f"https://example.org/{n}",
            "source": "HN", "published": digest.NOW, "hn_tag": tag}


class NewsPoolTests(unittest.TestCase):
    def test_reserve_only_the_shared_links_agent_needs(self):
        # 8 个通用 AI 条目、8 个 Agent 专属条目和 4 个共有条目，刚好能分成两组 10 条。
        shared = [story("AI coding agent", n) for n in range(4)]
        general = [story("AI", n) for n in range(4, 12)]
        agent_only = [story("Tool use", n) for n in range(12, 20)]
        with patch.object(digest, "collect_hn_news") as backfill:
            ai, agent = digest.news_pools(shared + general + agent_only, set())
            backfill.assert_not_called()
        first = digest.pad_news([], ai, set())
        taken = {item["url"] for item in first}
        second = digest.pad_news([], agent, taken)
        self.assertEqual((len(first), len(second)), (10, 10))
        self.assertTrue(taken.isdisjoint(item["url"] for item in second))

    def test_backfill_counts_unseen_unique_urls_and_includes_mcp(self):
        initial = [story("AI agent", n) for n in range(10)]
        initial.append({**initial[-1], "title": "Another agent headline"})
        seen = {initial[0]["url"]}
        extra = story("Toolbox release", 10, tag="MCP")
        with patch.object(digest, "collect_hn_news", return_value=[extra]) as backfill:
            ai, agent = digest.news_pools(initial, seen)
            backfill.assert_called_once_with(digest.HN_AGENT_BACKFILL)
        self.assertEqual(len({c["url"] for c in agent} - seen), 10)
        self.assertFalse({c["url"] for c in ai} - seen)

    def test_repeated_model_choices_count_once(self):
        article = story("MCP news", 1)
        choice = {"url": article["url"]}
        self.assertEqual(len(digest.sanitize_news([choice, choice], [article])), 1)
        repo = {"url": "https://github.com/a/b", "name": "a/b", "stars": 1, "desc": ""}
        self.assertEqual(len(digest.sanitize_repos([{"name": "A/B"}, {"url": repo["url"]}], [repo])), 1)


class DeliveryTests(unittest.TestCase):
    def run_short_digest(self, *, dry_run, smtp_ok):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as ctx:
            root = Path(tmp)
            output = root / "output"
            ctx.enter_context(patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "DEEPSEEK_API_KEY": ""}))
            ctx.enter_context(patch.multiple(digest, ROOT=root, STATE_FILE=root / "seen.json", DRY_RUN=dry_run))
            ctx.enter_context(patch.object(digest, "collect_feed_news", return_value=[story("AI agent", 1)]))
            ctx.enter_context(patch.object(digest, "collect_hn_news", return_value=[]))
            ctx.enter_context(patch.object(digest, "collect_repos", return_value=[]))
            send = ctx.enter_context(patch.object(digest, "send_mail", return_value=smtp_ok))
            status = digest.main()
            self.assertEqual(send.call_count, int(not dry_run))
            self.assertEqual((root / "preview.html").exists(), dry_run)
            self.assertEqual((root / "preview.txt").exists(), dry_run)
            ready = output.read_text().splitlines() if output.exists() else []
            return status, digest.load_seen(), ready

    def test_delivered_short_digest_is_recorded_and_still_reports_shortage(self):
        self.assertEqual(self.run_short_digest(dry_run=False, smtp_ok=True),
                         (1, {"https://example.org/1"}, ["digest_ready=true"]))

    def test_failed_delivery_has_priority_and_does_not_advance_state(self):
        self.assertEqual(self.run_short_digest(dry_run=False, smtp_ok=False), (3, set(), []))

    def test_short_preview_is_available_without_marking_articles_as_sent(self):
        self.assertEqual(self.run_short_digest(dry_run=True, smtp_ok=False),
                         (1, set(), ["digest_ready=true"]))


if __name__ == "__main__":
    unittest.main()
