"""Caption pipeline tests (spec section 6 order)."""

import unittest

from bot.utils import apply_caption


def cfg(**kw):
    base = {
        "nocaption": False,
        "remove_links": False,
        "remove_usernames": False,
        "blocklist": [],
        "replacements": {},
        "template": "",
        "prefix": "",
        "suffix": "",
    }
    base.update(kw)
    return base


class CaptionTests(unittest.TestCase):
    def test_username_removal(self):
        out = apply_caption(
            "hello @someuser bye", settings=cfg(remove_usernames=True)
        )
        self.assertNotIn("@someuser", out)
        self.assertIn("hello", out)
        self.assertIn("bye", out)

    def test_username_min_length(self):
        # 2-char "usernames" are not valid Telegram usernames -> kept
        out = apply_caption("hi @ab ok", settings=cfg(remove_usernames=True))
        self.assertIn("@ab", out)

    def test_username_not_in_word(self):
        out = apply_caption(
            "mail me a@b.com", settings=cfg(remove_usernames=True)
        )
        self.assertIn("a@b.com", out)

    def test_link_removal(self):
        out = apply_caption(
            'see <a href="https://example.com/x">this</a> and https://t.me/foo',
            settings=cfg(remove_links=True),
        )
        self.assertNotIn("example.com", out)
        self.assertNotIn("t.me/foo", out)
        self.assertIn("this", out)  # anchor text kept

    def test_username_only_unwraps_telegram_anchors(self):
        html = '<a href="https://other.com">keep</a> <a href="https://t.me/x">tm</a>'
        out = apply_caption(html, settings=cfg(remove_usernames=True))
        self.assertIn('<a href="https://other.com">keep</a>', out)
        self.assertNotIn("t.me/x", out)
        self.assertIn("tm", out)

    def test_blocklist_case_insensitive(self):
        out = apply_caption(
            "FREE promo here", settings=cfg(blocklist=["promo"])
        )
        self.assertNotIn("promo", out.lower())
        self.assertIn("FREE", out)

    def test_replacements(self):
        out = apply_caption(
            "join my Channel now",
            settings=cfg(replacements={"channel": "group"}),
        )
        self.assertIn("group", out)
        self.assertNotIn("Channel", out)

    def test_html_preserved(self):
        out = apply_caption("<b>bold</b> and <i>ital</i> @u12345",
                            settings=cfg(remove_usernames=True))
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<i>ital</i>", out)
        self.assertNotIn("@u12345", out)

    def test_prefix_suffix_template(self):
        out = apply_caption(
            "orig",
            filename="vid.mp4",
            size=2048,
            msg_id=42,
            settings=cfg(
                template="[{id}] {caption} ({filename}, {size})",
                prefix="TOP",
                suffix="BOT",
            ),
        )
        self.assertTrue(out.startswith("TOP\n"))
        self.assertTrue(out.endswith("\nBOT"))
        self.assertIn("[42] orig (vid.mp4, 2.0 KB)", out)

    def test_unknown_placeholder_stays_literal(self):
        out = apply_caption(
            "x", settings=cfg(template="{caption} {nope}")
        )
        self.assertIn("{nope}", out)

    def test_bad_template_falls_back(self):
        out = apply_caption("raw cap", settings=cfg(template="{caption"))
        self.assertEqual(out, "raw cap")

    def test_nocaption_skips_everything(self):
        out = apply_caption(
            "hello @u12345 https://x.com",
            settings=cfg(nocaption=True, remove_usernames=True,
                         remove_links=True, prefix="P"),
        )
        self.assertEqual(out, "")

    def test_cleanup(self):
        out = apply_caption("a   b  \n\n\n\nc <b></b>", settings=cfg())
        self.assertEqual(out, "a b\n\nc")


if __name__ == "__main__":
    unittest.main()
