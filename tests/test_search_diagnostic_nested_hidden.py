import unittest

from bs4 import BeautifulSoup

from scraper import sanitize_public_diagnostic_html


class NestedHiddenDiagnosticTests(unittest.TestCase):
    def test_hidden_parent_and_styled_descendant_do_not_abort_public_capture(self):
        for style in ('display:none', 'visibility:hidden'):
            with self.subTest(style=style):
                raw = f'''<div id="search"><div style="{style}">
                <span style="color:red">PRIVATE_NESTED_TEXT</span>
                <div style="display:none"><b style="font-weight:bold">PRIVATE_DEEP_TEXT</b></div>
                </div><p>Public result</p>
                <li id="p_n_deal_type/10343614051"><a aria-current="true">本日のタイムセール</a></li>
                </div>'''
                clean = sanitize_public_diagnostic_html(raw)
                self.assertNotIn('PRIVATE_', clean)
                self.assertIn('Public result', clean)
                soup = BeautifulSoup(clean, 'html.parser')
                self.assertIsNotNone(soup.select_one('[id="p_n_deal_type/10343614051"] a[aria-current="true"]'))

    def test_visible_sibling_survives_after_nested_private_subtree(self):
        clean = sanitize_public_diagnostic_html('''<div id="search">
        <section hidden><span style="color:red">PRIVATE_HIDDEN</span></section>
        <div style="display:none"><span style="display:none">PRIVATE_STYLE</span></div>
        <div data-component-type="s-search-result" data-asin="B000000001">
        <span style="color:red">Visible product</span></div></div>''')
        self.assertNotIn('PRIVATE_', clean)
        soup = BeautifulSoup(clean, 'html.parser')
        card = soup.select_one('[data-component-type="s-search-result"][data-asin="B000000001"]')
        self.assertEqual(card.get_text(' ', strip=True), 'Visible product')


if __name__ == '__main__':
    unittest.main()
