import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("delivery_diagnostic", ROOT / "scripts/diagnose_account13_delivery.py")
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)
logging.getLogger('asyncio').setLevel(logging.WARNING)
URL = "https://www.amazon.co.jp/s?rh=n%3A3456990051%2Cp_n_deal_type%3A10343614051%2Cp_36%3A300000-"
TARGET = {"category": "synthetic", "effective_url": URL}


def observation(code="US", blocked=None):
    return {"country_code": code, "restriction": blocked, "cards": 0, "requested_facet_active": False}


class CountryAndConfigTests(unittest.TestCase):
    def test_only_explicit_country_is_retained(self):
        self.assertEqual(diagnostic.country_from_labels(["お届け先", "アメリカ合衆国"])['country_code'], "US")
        self.assertEqual(diagnostic.country_from_labels(["Deliver to", "United States"])['country_code'], "US")
        self.assertEqual(diagnostic.country_from_labels(["日本"])['country_code'], "JP")

    def test_unknown_postal_or_multiple_countries_are_not_guessed_or_saved(self):
        for labels in (["お届け先", "PRIVATE_POSTAL_123-4567"], ["United States", "日本"], ["unknown"]):
            value = diagnostic.country_from_labels(labels)
            self.assertIsNone(value['country_code'])
            self.assertNotIn("PRIVATE_POSTAL", json.dumps(value))

    def test_actual_first_category_uses_exact_cloud_effective_url(self):
        value = diagnostic.first_search(diagnostic.load_config(str(ROOT / "categories13.yaml")))
        self.assertEqual(value["effective_url"], URL)
        self.assertEqual(value["category"], "ミネラル")

    def test_non_search_first_category_is_rejected(self):
        with self.assertRaises(ValueError):
            diagnostic.first_search({"categories": [{"url": URL}]})


class BoundedFlowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, first, ui=None, second=None, status=200):
        with patch.object(diagnostic, "open_search", AsyncMock(return_value=status)) as navigate, \
             patch.object(diagnostic, "inspect_search", AsyncMock(side_effect=[first, second])) as inspect, \
             patch.object(diagnostic, "save_search_failure_diagnostic", AsyncMock(return_value=True)) as capture, \
             patch.object(diagnostic, "choose_japan_from_public_ui", AsyncMock(return_value=ui)) as choose:
            result = await diagnostic.diagnose_on_page(object(), TARGET)
            return result, navigate, choose, capture

    async def test_unknown_country_stops_without_opening_ui(self):
        result, nav, choose, capture = await self.exercise(observation(None))
        self.assertEqual(result['stop_reason'], 'delivery_country_not_explicit')
        choose.assert_not_awaited()
        self.assertEqual(nav.await_count, 1)

    async def test_already_japan_stops_without_opening_ui(self):
        result, nav, choose, capture = await self.exercise(observation('JP'))
        self.assertEqual(result['stop_reason'], 'delivery_country_already_japan')
        choose.assert_not_awaited()

    async def test_captcha_and_access_restrictions_stop_without_ui_or_retry(self):
        for reason in ('captcha', 'access_restricted', 'non_public_destination'):
            result, nav, choose, capture = await self.exercise(observation(blocked=reason))
            self.assertEqual(result['stop_reason'], reason)
            choose.assert_not_awaited()
            self.assertEqual(nav.await_count, 1)

    async def test_http_failure_stops_without_ui(self):
        result, nav, choose, capture = await self.exercise(observation(), status=503)
        self.assertEqual(result['stop_reason'], 'search_http_not_200')
        choose.assert_not_awaited()

    async def test_postal_or_missing_japan_stops_without_second_search(self):
        for reason in ('postal_input_present_after_japan_selection', 'japan_option_unavailable', 'country_only_control_unavailable'):
            result, nav, choose, capture = await self.exercise(observation(), {'stop_reason': reason})
            self.assertEqual(result['stop_reason'], reason)
            self.assertEqual(nav.await_count, 1)
            self.assertEqual(capture.await_count, 1)

    async def test_success_revisits_identical_effective_url_once_only(self):
        result, nav, choose, capture = await self.exercise(observation(), {'stop_reason': None}, observation('JP'))
        self.assertEqual(result['stop_reason'], 'comparison_complete')
        self.assertTrue(result['japan_country_confirmed_after'])
        self.assertEqual([call.args[1] for call in nav.await_args_list], [URL, URL])
        self.assertEqual(capture.await_count, 2)

    async def test_unknown_after_is_not_misreported_as_japan_confirmed(self):
        result, nav, choose, capture = await self.exercise(observation(), {'stop_reason': None}, observation(None))
        self.assertFalse(result['japan_country_confirmed_after'])

    async def test_output_inside_repository_is_rejected_before_browser_or_write(self):
        with patch.object(diagnostic, 'async_playwright') as launch:
            with self.assertRaises(ValueError):
                await diagnostic.run(ROOT / 'data/account13/forbidden-diagnostic')
            launch.assert_not_called()
        self.assertFalse((ROOT / 'data/account13/forbidden-diagnostic').exists())


def public_fixture(options='<option value="US">United States</option><option value="JP">日本</option>', postal=False,
                   custom=False, captcha=False, no_done=False):
    country = (f'<select id="GLUXCountryList" style="display:none">{options}</select>'
               '<button id="GLUXCountryListDropdown" onclick="document.querySelector(\'#options\').hidden=false">Country</button>'
               '<div id="options" hidden><a class="a-dropdown-link" href="#" onclick="this.parentElement.hidden=true">日本</a></div>'
               if custom else f'<select id="GLUXCountryList">{options}</select>')
    return ('<!doctype html><meta charset="utf-8"><nav><div id="glow-ingress-line1">Deliver to</div>'
            '<div id="glow-ingress-line2">United States</div>'
            '<button id="nav-global-location-popover-link" onclick="document.querySelector(\'#GLUXContainer\').hidden=false">Location</button></nav>'
            '<div id="GLUXContainer" role="dialog" hidden>' + country +
            ('<input id="GLUXZipUpdateInput" value="PRIVATE_POSTAL_DO_NOT_SAVE">' if postal else '') +
            ('' if no_done else '<button id="GLUXConfirmClose" onclick="document.querySelector(\'#GLUXContainer\').hidden=true;document.querySelector(\'#glow-ingress-line2\').textContent=\'日本\'">完了</button>') +
            '</div><div id="search"><h1>Public search fixture</h1></div>' +
            ('<input id="captchacharacters">' if captcha else ''))


class PublicUiFixtureTests(unittest.IsolatedAsyncioTestCase):
    """Real Chromium, every request fulfilled locally; no Amazon/network requests leave the test."""
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--remote-debugging-port=0'])
        self.context = await self.browser.new_context(service_workers='block')
        self.page = await self.context.new_page()
        self.page.wait_for_timeout = AsyncMock()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def fixture(self, **kwargs):
        html = public_fixture(**kwargs)
        async def route(request):
            await request.fulfill(status=200, content_type='text/html', body=html)
        await self.context.route('**/*', route)
        await self.page.goto(URL)

    async def test_visible_native_japan_select_and_done_succeed(self):
        await self.fixture()
        result = await diagnostic.choose_japan_from_public_ui(self.page)
        self.assertIsNone(result['stop_reason'])
        self.assertTrue(result['japan_selected'])
        self.assertTrue(result['confirmation_clicked'])
        self.assertEqual((await diagnostic.country_header(self.page))['country_code'], 'JP')

    async def test_custom_public_dropdown_succeeds_without_forcing_hidden_select(self):
        await self.fixture(custom=True)
        result = await diagnostic.choose_japan_from_public_ui(self.page)
        self.assertIsNone(result['stop_reason'])
        self.assertTrue(result['japan_option_present'])

    async def test_japan_absent_stops_and_never_edits_postal_value(self):
        await self.fixture(options='<option value="US">United States</option>', postal=True)
        result = await diagnostic.choose_japan_from_public_ui(self.page)
        self.assertFalse(result['japan_selected'])
        self.assertFalse(result['confirmation_clicked'])
        self.assertEqual(await self.page.locator('#GLUXZipUpdateInput').input_value(), 'PRIVATE_POSTAL_DO_NOT_SAVE')
        self.assertNotIn('PRIVATE_POSTAL', json.dumps(result))

    async def test_postal_field_after_japan_selection_stops_without_done(self):
        await self.fixture(postal=True)
        result = await diagnostic.choose_japan_from_public_ui(self.page)
        self.assertEqual(result['stop_reason'], 'postal_input_present_after_japan_selection')
        self.assertFalse(result['confirmation_clicked'])
        self.assertEqual(await self.page.locator('#GLUXZipUpdateInput').input_value(), 'PRIVATE_POSTAL_DO_NOT_SAVE')

    async def test_missing_done_control_stops(self):
        await self.fixture(no_done=True)
        result = await diagnostic.choose_japan_from_public_ui(self.page)
        self.assertEqual(result['stop_reason'], 'country_confirmation_unavailable')

    async def test_captcha_stops_before_country_selection(self):
        await self.fixture(captcha=True)
        result = await diagnostic.choose_japan_from_public_ui(self.page)
        self.assertEqual(result['stop_reason'], 'captcha')
        self.assertFalse(result['japan_selected'])


class WorkflowIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load((ROOT / '.github/workflows/scrape13.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)

    def job_enabled(self, job, event, diagnosis, message='[scrape:account13]'):
        expression = self.workflow['jobs'][job]['if'].removeprefix('${{').removesuffix('}}').strip()
        for name, value in {'github.event_name': event, 'github.event.head_commit.message': message,
                            'inputs.diagnose_delivery_country': diagnosis}.items():
            expression = expression.replace(name, repr(value))
        expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('true', 'True')
        return eval(expression, {'__builtins__': {}, 'contains': lambda text, part: part in text})

    def test_diagnostic_input_is_boolean_default_false(self):
        value = self.workflow['on']['workflow_dispatch']['inputs']['diagnose_delivery_country']
        self.assertEqual(value['type'], 'boolean')
        self.assertEqual(value['default'], 'false')

    def test_diagnostic_true_never_schedules_normal_job(self):
        self.assertFalse(self.job_enabled('scrape_account13', 'workflow_dispatch', True))
        self.assertTrue(self.job_enabled('diagnose_account13_delivery', 'workflow_dispatch', True))

    def test_normal_dispatch_and_push_routes_preserved(self):
        for event, value in [('workflow_dispatch', False), ('push', None)]:
            self.assertTrue(self.job_enabled('scrape_account13', event, value))
            self.assertFalse(self.job_enabled('diagnose_account13_delivery', event, value))
        self.assertFalse(self.job_enabled('scrape_account13', 'push', None, 'unrelated'))

    def test_diagnostic_job_has_read_only_token_no_credentials_or_product_commands(self):
        job = self.workflow['jobs']['diagnose_account13_delivery']
        self.assertEqual(job['permissions'], {'contents': 'read'})
        checkout = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        commands = '\n'.join(step.get('run', '') for step in job['steps'])
        for forbidden in ('scrape_main', 'ensure_daily_scrape', 'git ', 'asin_history', 'sync_asin', 'PINEFIELD_TARGET_DATE'):
            self.assertNotIn(forbidden, commands)
        self.assertIn('diagnose_account13_delivery.py', commands)

    def test_normal_write_steps_have_independent_input_guard(self):
        steps = self.workflow['jobs']['scrape_account13']['steps']
        for name in ('Resolve target date', 'Scrape account13', 'Validate account13 output', 'Commit account13 output'):
            step = next(step for step in steps if step['name'] == name)
            self.assertEqual(step['if'], '${{ inputs.diagnose_delivery_country != true }}')

    def test_script_cannot_invoke_production_or_input_credentials(self):
        tree = ast.parse((ROOT / 'scripts/diagnose_account13_delivery.py').read_text(encoding='utf-8'))
        names = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                 for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))}
        for forbidden in ('fetch_and_save', 'fetch_products', 'scrape_search', 'load_product_exclusion_registry',
                          'fill', 'press', 'connect_over_cdp', 'launch_persistent_context',
                          'storage_state', 'cookies', 'post', 'Popen', 'system'):
            self.assertNotIn(forbidden, names)
        attribute_calls = {node.func.attr for node in ast.walk(tree)
                           if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn('type', attribute_calls)  # Builtin type(exc) is intentionally safe.


if __name__ == '__main__':
    unittest.main()
