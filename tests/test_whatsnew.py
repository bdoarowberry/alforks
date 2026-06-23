"""Tests for the in-app What's New page: CHANGELOG.md parsing, the /whatsnew
route, and the app-version meta injected app-wide via _head.html."""
import unittest

import app


class TestChangelogParse(unittest.TestCase):
    def test_parses_versions_and_items(self):
        releases = app._parse_changelog()
        self.assertGreaterEqual(len(releases), 2)
        versions = [r["version"] for r in releases]
        # Versions are now per-release (date + sequence) so multiple releases on
        # one day aren't lumped under a single heading.
        self.assertEqual(versions[0], "2026.06.23.5")    # newest first (file order)
        self.assertIn("2026.06.23.4", versions)
        self.assertIn("2026.06.23.1", versions)
        self.assertIn("2026.06.21", versions)
        self.assertTrue(all(r["items"] for r in releases))

    def test_inline_bold_becomes_strong(self):
        items = " ".join(str(i) for r in app._parse_changelog() for i in r["items"])
        self.assertIn("<strong>", items)
        self.assertNotIn("**", items)                    # markdown emphasis consumed

    def test_md_inline_escapes_then_formats(self):
        out = str(app._md_inline("a **b** <x> `c`"))
        self.assertIn("<strong>b</strong>", out)
        self.assertIn("&lt;x&gt;", out)                  # raw HTML escaped first
        self.assertIn("<code>c</code>", out)


class TestWhatsNewRoute(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def test_whatsnew_page_renders(self):
        r = self.c.get("/whatsnew")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("2026.06.23", html)
        self.assertIn("What's New", html)
        self.assertIn("Region zoom", html)               # a seeded feature
        self.assertIn("Latest", html)                    # newest release is badged

    def test_version_meta_injected_appwide(self):
        # _head.html stamps the running version into every page's <head>
        html = self.c.get("/whatsnew").get_data(as_text=True)
        self.assertIn('name="alforks-version"', html)
        self.assertIn(app._app_version(), html)


if __name__ == "__main__":
    unittest.main()
