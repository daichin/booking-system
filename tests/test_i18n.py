"""The catalogues must stay in step with each other.

zh-TW is the reference: it defines which keys exist. A key added there and
forgotten in English shows an English reader Chinese; a placeholder renamed
in one and not the other is a formatting crash at runtime, in an email
nobody sees fail. Both are cheap to catch here.
"""

from __future__ import annotations

import importlib
import re
import unittest

from app import i18n
from app.i18n import zh_TW

_PLACEHOLDER = re.compile(r"{(\w+)}")


class CatalogueParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = zh_TW.STRINGS

    def catalogues(self):
        for code in i18n.SUPPORTED:
            if code == i18n.DEFAULT_LOCALE:
                continue
            module = importlib.import_module(f"app.i18n.{code.replace('-', '_')}")
            yield code, module.STRINGS

    def test_every_locale_covers_every_key(self):
        for code, strings in self.catalogues():
            with self.subTest(locale=code):
                missing = sorted(set(self.reference) - set(strings))
                self.assertEqual(missing, [], f"{code} is missing {len(missing)} keys")

    def test_no_locale_invents_keys(self):
        for code, strings in self.catalogues():
            with self.subTest(locale=code):
                extra = sorted(set(strings) - set(self.reference))
                self.assertEqual(extra, [], f"{code} has keys zh-TW does not")

    def test_placeholders_match_exactly(self):
        for code, strings in self.catalogues():
            for key, reference in self.reference.items():
                expected = sorted(_PLACEHOLDER.findall(reference))
                actual = sorted(_PLACEHOLDER.findall(strings[key]))
                with self.subTest(locale=code, key=key):
                    self.assertEqual(
                        actual, expected, f"{key} placeholders differ in {code}"
                    )

    def test_no_translation_is_empty(self):
        for code, strings in self.catalogues():
            for key, value in strings.items():
                with self.subTest(locale=code, key=key):
                    self.assertTrue(value.strip(), f"{key} is blank in {code}")

    def test_the_reference_catalogue_has_no_duplicate_keys(self):
        """A dict literal silently keeps the last of a repeated key."""
        import pathlib

        for name in ("zh_TW", "en"):
            path = pathlib.Path(f"app/i18n/{name}.py")
            keys = re.findall(
                r"^    (['\"])([^'\"]+)\1:", path.read_text(encoding="utf-8"), re.M
            )
            names = [key for _, key in keys]
            duplicates = sorted({k for k in names if names.count(k) > 1})
            with self.subTest(catalogue=name):
                self.assertEqual(duplicates, [], f"{name}.py repeats these keys")


class LocaleResolutionTests(unittest.TestCase):
    def test_english_is_the_default_language(self):
        """A deliberate product decision, not an accident of ordering.

        Most use of this deployment is in English, so it is what a visitor
        with no stated preference gets. zh-TW stays a first-class locale:
        every string exists in both, and a member who picks it keeps it.
        """
        self.assertEqual(i18n.DEFAULT_LOCALE, "en")
        self.assertEqual(i18n.AVAILABLE_LOCALES[0][0], "en")

    def test_regional_variants_map_onto_what_we_have(self):
        self.assertEqual(i18n.normalise("en-GB"), "en")
        self.assertEqual(i18n.normalise("zh-Hant-TW"), "zh-TW")
        # Anything we do not speak, and no preference at all, get the default.
        self.assertEqual(i18n.normalise("fr"), i18n.DEFAULT_LOCALE)
        self.assertEqual(i18n.normalise(None), i18n.DEFAULT_LOCALE)

    def test_accept_language_picks_the_highest_quality_match(self):
        self.assertEqual(
            i18n.from_accept_header("fr;q=1.0,en;q=0.8,zh-TW;q=0.5"), "en"
        )
        self.assertEqual(i18n.from_accept_header("zh-TW,en;q=0.3"), "zh-TW")
        self.assertIsNone(i18n.from_accept_header("fr,de"))

    def test_an_unknown_key_returns_itself_rather_than_raising(self):
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")

    def test_a_missing_placeholder_does_not_crash_the_page(self):
        # Renders the raw template rather than raising mid-response.
        self.assertIsInstance(i18n.t("error.OFF_GRID"), str)
