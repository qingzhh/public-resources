import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector import Collector, FetchError
from ed2k import Message, Page, normalize, parse_page
from state import Store, run_lock

MD4 = "0123456789abcdef0123456789abcdef"
NOW = 2_000_000.0


def link(name="示例.mkv", size=123, md4=MD4):
    return f"ed2k://|file|{name}|{size}|{md4}|/"


def message(ident, text=None, published_at=NOW - 100):
    return Message("regeng115", ident, published_at, text if text is not None else link(str(ident) + ".mkv", md4=f"{ident:032x}"))


def document(items, before=1):
    body = '<a class="tme_messages_more" data-before="' + str(before) + '"></a>' if before else ""
    for ident, text in items:
        body += f'<div class="tgme_widget_message" data-post="regeng115/{ident}"><div class="bubble"><div class="tgme_widget_message_text">{text}</div><a><time datetime="2026-10-07T12:34:56+00:00"></time></a></div></div>'
    return '<html><body>' + body + '</body></html>'


class NormalizeTests(unittest.TestCase):
    def test_exact_user_error_is_repaired_without_changing_hash_or_size(self):
        raw = "解析 ED2K 失败: ed2k://|file|烈焰狂沙.Lie.Yan.Kuang.Sha.2026.2160p.WEB-DL.H.265.HDR.DDP5.1.2Audios-HHWEB.mkv|10989138278|52b96bd023ab3ce9435429f4d01f2363| err=结构错误plugin/ed2k_hash_reporter.go:184"
        result = normalize(raw)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.links), 1)
        parsed = result.links[0]
        self.assertEqual(parsed.size, 10989138278)
        self.assertEqual(parsed.md4, "52b96bd023ab3ce9435429f4d01f2363")
        self.assertEqual(parsed.normalized, raw.split("ed2k://", 1)[1].split(" err=", 1)[0].join(["ed2k://", "/"]))
        self.assertTrue(parsed.repaired)
        self.assertNotIn("err=", parsed.raw)

    def test_missing_final_slash_or_separator_can_be_completed(self):
        for tail in ("|", "", "||", "|/|"):
            with self.subTest(tail=tail):
                result = normalize(link()[:-2] + tail)
                self.assertEqual(result.links[0].normalized, link())

    def test_extension_fields_removed(self):
        result = normalize(link().replace("|/", "|h=AAAA|s=source||/"))
        self.assertEqual(result.links[0].normalized, link())

    def test_html_escaping_is_decoded(self):
        raw = link("A&amp;B.%20.mkv").replace("|", "&#124;")
        self.assertEqual(normalize(raw).links[0].normalized, link("A&B.%20.mkv"))

    def test_hash_case_and_size_leading_zeroes(self):
        parsed = normalize(link(size="000123", md4=MD4.upper()).upper().replace("示例.MKV", "示例.mkv")).links[0]
        self.assertEqual(parsed.md4, MD4)
        self.assertEqual(parsed.size, 123)
        self.assertEqual(parsed.report_hash, "ed2k:" + MD4 + ":123")

    def test_int64_exact(self):
        self.assertEqual(normalize(link(size=2**63 - 1)).links[0].size, 2**63 - 1)

    def test_invalid_sizes_rejected(self):
        for size in (0, -1, "1.5", "", "not-a-number", 2**63, "9" * 1000):
            with self.subTest(size=str(size)[:25]):
                result = normalize(link(size=size))
                self.assertFalse(result.links)
                self.assertEqual(result.errors[0].reason, "invalid_size")

    def test_hash_cannot_be_completed_or_truncated(self):
        for md4 in ("", "A" * 31, "A" * 33, "G" * 32, MD4 + "other"):
            with self.subTest(md4=md4):
                result = normalize(link(md4=md4))
                self.assertFalse(result.links)
                self.assertEqual(result.errors[0].reason, "invalid_hash")

    def test_invalid_names(self):
        for name in ("", " ", "a\x00b.mkv", "x" * 5000):
            self.assertFalse(normalize(link(name=name)).links)

    def test_missing_fields_not_invented(self):
        self.assertFalse(normalize("ed2k://|file|bad.mkv|/").links)

    def test_incomplete_link_does_not_consume_next_link(self):
        result = normalize(link(md4="ABC")[:-2] + " " + link("valid.mkv"))
        self.assertEqual(len(result.links), 1)
        self.assertEqual(result.links[0].name, "valid.mkv")
        self.assertEqual(len(result.errors), 1)

    def test_log_prefix_multiple_links_and_newlines(self):
        result = normalize("not an ED2K\r\nfail: " + link() + " " + link("B.mkv") + "\n" + link("C.mkv"))
        self.assertEqual([item.name for item in result.links], ["示例.mkv", "B.mkv", "C.mkv"])

    def test_normalization_is_idempotent(self):
        first = normalize(link().replace("|/", "|h=ABC||/"))
        second = normalize(first.links[0].normalized)
        self.assertFalse(second.links[0].repaired)
        self.assertEqual(first.links[0].normalized, second.links[0].normalized)


class ParserTests(unittest.TestCase):
    def test_visible_code_and_older_cursor(self):
        page = parse_page(document([(3, "<code>" + link() + "</code>")], before=3), "regeng115")
        self.assertEqual(page.before, 3)
        self.assertEqual(page.messages[0].message_id, 3)
        self.assertEqual(normalize(page.messages[0].text).links[0].normalized, link())

    def test_link_href_when_display_is_label(self):
        page = parse_page(document([(1, '<a href="' + link() + '">复制ED2K</a>')]), "regeng115")
        self.assertEqual(len(normalize(page.messages[0].text).links), 1)

    def test_multiple_links_separated_by_break(self):
        page = parse_page(document([(1, link("A.mkv") + '<br/>' + link("B.mkv"))]), "regeng115")
        self.assertEqual(len(normalize(page.messages[0].text).links), 2)

    def test_unrelated_channel_ignored(self):
        with self.assertRaises(ValueError):
            parse_page(document([(1, link())]).replace("regeng115/", "other/"), "regeng115")

    def test_message_time_required(self):
        with self.assertRaises(ValueError):
            parse_page(document([(1, link())]).replace('datetime="2026-10-07T12:34:56+00:00"', ''), "regeng115")

    def test_partial_html_rejected(self):
        with self.assertRaises(ValueError):
            parse_page(document([(1, link())]).split("</div>")[0], "regeng115")

    def test_login_challenge_or_empty_page_rejected(self):
        for raw in ("", "<html>blocked</html>", "<div>Please log in</div>"):
            with self.assertRaises(ValueError):
                parse_page(raw, "regeng115")

    def test_duplicates_rejected_and_messages_sorted(self):
        with self.assertRaises(ValueError):
            parse_page(document([(1, link()), (1, link())]), "regeng115")
        page = parse_page(document([(3, link()), (1, link())]), "regeng115")
        self.assertEqual([item.message_id for item in page.messages], [1, 3])

    def test_html_escape_and_nested_text(self):
        page = parse_page(document([(1, '<b>title</b><br><code>' + link("A&amp;B.mkv") + '</code>')]), "regeng115")
        self.assertEqual(normalize(page.messages[0].text).links[0].name, "A&B.mkv")

    def test_verified_empty_incremental_page(self):
        raw = '<html><head><meta property="al:ios:url" content="tg://resolve?domain=regeng115"></head><body><section class="tgme_channel_history js-message_history"></section></body></html><!-- generated -->'
        self.assertEqual(parse_page(raw, "regeng115", allow_empty=True).messages, ())
        with self.assertRaises(ValueError):
            parse_page(raw, "regeng115")

    def test_empty_page_requires_identity_history_and_complete_html(self):
        raw = '<html><meta property="al:ios:url" content="tg://resolve?domain=regeng115"><section class="tgme_channel_history"></section></html>'
        for malformed in (raw.replace('regeng115', 'other'), raw.replace('tgme_channel_history', 'challenge'), raw.replace('</html>', ''), '<html>login</html>'):
            with self.subTest(raw=malformed):
                with self.assertRaises(ValueError):
                    parse_page(malformed, "regeng115", allow_empty=True)


class TemporaryStore(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()


class StateTests(TemporaryStore):
    def test_same_hash_different_names_and_sources_report_once(self):
        result = self.store.ingest([message(1, link("A.mkv")), message(2, link("B.mkv", size="000123", md4=MD4.upper()))], NOW)
        self.assertEqual(result["new"], 1)
        self.assertEqual(self.store.status()["total_unique"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0], 2)

    def test_same_hash_different_size_not_collapsed(self):
        self.store.ingest([message(1, link(size=1)), message(2, link(size=2))], NOW)
        self.assertEqual(self.store.status()["total_unique"], 2)

    def test_reported_record_never_requeued(self):
        self.store.ingest([message(1, link())], NOW)
        item = self.store.due(NOW, 1)[0]
        self.store.begin(item, NOW)
        self.store.finish(item, "reported", NOW, receipt={"code": 0})
        self.store.ingest([message(2, link("another.mkv"))], NOW + 1)
        self.assertFalse(self.store.due(NOW + 1, 10))
        self.assertEqual(self.store.status()["counts"], {"reported": 1})

    def test_edited_message_additional_link_captured(self):
        self.store.ingest([message(1, link())], NOW)
        result = self.store.ingest([message(1, link() + "\n" + link(md4="a" * 32))], NOW + 1)
        self.assertEqual(result["new"], 1)
        self.assertEqual(self.store.status()["total_unique"], 2)

    def test_failed_page_rolls_back_items_and_cursor(self):
        self.store.ensure_channel("regeng115", NOW - 86400)
        with self.assertRaises(AttributeError):
            self.store.ingest([message(1), Message("regeng115", 2, NOW, None)], NOW, channel="regeng115", cursor=2)
        self.assertEqual(self.store.status()["total_unique"], 0)
        self.assertEqual(self.store.channel("regeng115")["cursor"], 0)

    def test_restart_recovery_does_not_blindly_resend(self):
        self.store.ingest([message(1)], NOW)
        item = self.store.due(NOW, 1)[0]
        self.store.begin(item, NOW)
        self.store.close()
        self.store = Store(self.directory)
        self.store.recover(NOW + 1)
        self.assertFalse(self.store.due(NOW + 1000, 10))
        self.assertEqual(len(self.store.uncertain()), 1)

    def test_retry_waits_until_due_and_preserves_attempt_count(self):
        self.store.ingest([message(1)], NOW)
        item = self.store.due(NOW, 1)[0]
        self.store.begin(item, NOW)
        self.store.finish(item, "retry", NOW, error="network", retry_at=NOW + 300)
        self.assertFalse(self.store.due(NOW + 299, 1))
        self.assertEqual(self.store.due(NOW + 300, 1)[0]["attempts"], 1)

    def test_invalid_record_is_retained_and_deduplicated(self):
        text = link(md4="bad")
        self.store.ingest([message(1, text)], NOW)
        self.store.ingest([message(1, text)], NOW + 1)
        self.assertEqual(self.store.status()["parse_errors"], 1)
        self.assertEqual(self.store.status()["total_unique"], 0)

    def test_readonly_status_and_export_utf8(self):
        self.store.ingest([message(1, link("中文名.mkv"))], NOW)
        reader = Store(self.directory, readonly=True)
        self.assertEqual(reader.status()["total_unique"], 1)
        reader.close()
        target = self.directory / "normalized.txt"
        self.store.export(target)
        self.assertEqual(target.read_text(encoding="utf-8"), link("中文名.mkv") + "\n")

    def test_second_worker_cannot_take_lock(self):
        with run_lock(self.directory):
            with self.assertRaises(RuntimeError):
                with run_lock(self.directory):
                    self.fail("second lock acquired")


class FakeTelegram:
    def __init__(self, newest, *, before=None, after=None):
        self.newest, self.before, self.after = newest, before or {}, after or {}
        self.calls = []

    def fetch(self, channel, *, before=None, after=None):
        self.calls.append((before, after))
        value = self.before.get(before, self.newest) if after is None else self.after.get(after, self.newest)
        if isinstance(value, Exception):
            raise value
        return value


class CollectorTests(TemporaryStore):
    def settings(self, pages=20, edits=2):
        return {"initial_days": 7, "max_pages_per_cycle": pages, "edit_pages": edits}

    def collect(self, telegram, settings=None):
        return Collector(self.store, telegram, settings or self.settings(), clock=lambda: NOW).collect("regeng115")

    def test_history_cutoff_and_incremental_deduplication(self):
        latest = Page((message(20), message(21)), 20)
        older = Page((message(18, published_at=NOW - 8 * 86400), message(19)), 18)
        telegram = FakeTelegram(latest, before={20: older})
        result = self.collect(telegram)
        self.assertTrue(result["bootstrap_done"])
        self.assertEqual(self.store.status()["total_unique"], 3)
        self.assertEqual(self.store.channel("regeng115")["cursor"], 21)
        self.collect(telegram)
        self.assertEqual(self.store.status()["total_unique"], 3)

    def test_network_failure_does_not_advance_cursor(self):
        self.store.ensure_channel("regeng115", NOW - 86400)
        telegram = FakeTelegram(FetchError("network"))
        with self.assertRaises(FetchError):
            self.collect(telegram)
        self.assertEqual(self.store.channel("regeng115")["cursor"], 0)
        self.assertEqual(self.store.status()["total_unique"], 0)

    def test_bootstrap_budget_continues_after_restart(self):
        latest = Page((message(20), message(21)), 20)
        older = Page((message(19, published_at=NOW - 8 * 86400),), 19)
        telegram = FakeTelegram(latest, before={20: older})
        first = self.collect(telegram, self.settings(pages=1, edits=0))
        self.assertFalse(first["bootstrap_done"])
        self.assertEqual(self.store.channel("regeng115")["bootstrap_before"], 20)
        self.store.close()
        self.store = Store(self.directory)
        second = self.collect(telegram, self.settings(pages=1, edits=0))
        self.assertTrue(second["bootstrap_done"])
        self.assertEqual(self.store.status()["total_unique"], 2)

    def test_outage_more_than_one_page_caught_up(self):
        self.store.ensure_channel("regeng115", NOW - 86400)
        self.store.ingest([message(1)], NOW, channel="regeng115", cursor=1, bootstrap_done=True)
        latest = Page((message(5), message(6)), 5)
        telegram = FakeTelegram(latest, after={1: Page((message(2), message(3)), 2), 3: Page((message(4), message(5)), 4)})
        self.collect(telegram, self.settings(edits=0))
        self.assertEqual(self.store.status()["total_unique"], 6)
        self.assertEqual(self.store.channel("regeng115")["cursor"], 6)
        self.assertIn((None, 3), telegram.calls)

    def test_edit_check_captures_new_link_without_cursor_change(self):
        self.store.ensure_channel("regeng115", NOW - 86400)
        self.store.ingest([message(10, link())], NOW, channel="regeng115", cursor=10, bootstrap_done=True)
        latest = Page((message(10, link() + "\n" + link(md4="a" * 32)),), None)
        self.collect(FakeTelegram(latest))
        self.assertEqual(self.store.status()["total_unique"], 2)
        self.assertEqual(self.store.channel("regeng115")["cursor"], 10)

    def test_nonprogressing_history_fails_instead_of_marking_done(self):
        latest = Page((message(20), message(21)), 20)
        with self.assertRaises(FetchError):
            self.collect(FakeTelegram(latest))
        self.assertFalse(self.store.channel("regeng115")["bootstrap_done"])
        self.assertEqual(self.store.channel("regeng115")["bootstrap_before"], 20)

    def test_failed_later_page_keeps_preceding_page_transaction(self):
        latest = Page((message(20), message(21)), 20)
        with self.assertRaises(FetchError):
            self.collect(FakeTelegram(latest, before={20: FetchError("outage")}))
        self.assertEqual(self.store.status()["total_unique"], 2)
        self.assertEqual(self.store.channel("regeng115")["bootstrap_before"], 20)

    def test_empty_incremental_page_keeps_cursor_and_items(self):
        self.store.ensure_channel("regeng115", NOW - 86400)
        self.store.ingest([message(1)], NOW, channel="regeng115", cursor=1, bootstrap_done=True)
        telegram = FakeTelegram(Page((message(1),), None), after={1: Page((), None)})
        result = self.collect(telegram, self.settings(edits=0))
        self.assertEqual(result["new"], 0)
        self.assertEqual(self.store.channel("regeng115")["cursor"], 1)
        self.assertEqual(self.store.status()["total_unique"], 1)


if __name__ == "__main__":
    unittest.main()
