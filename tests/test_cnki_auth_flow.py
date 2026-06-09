import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import literature  # noqa: E402
from cnki import driver  # noqa: E402


class FakeDriver:
    def __init__(self, text=""):
        self.visited = []
        self.saved = False
        self.closed = False
        self.current_url = "https://kns.cnki.net/"
        self.title = "中国知网"
        self.text = text

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def execute_script(self, script):
        if "querySelectorAll('a')" in script:
            return []
        return self.text

    def get_cookies(self):
        return [{"name": "Ecp_ClientId", "domain": ".cnki.net"}]

    def quit(self):
        self.closed = True


class LinkDriver(FakeDriver):
    def execute_script(self, script):
        if "querySelectorAll('a')" in script:
            return [
                {"text": "Example University", "href": "https://idp.example.edu/login"},
                {"text": "Other School", "href": "https://idp.other.edu/login"},
            ]
        return super().execute_script(script)


class CnkiAuthFlowTests(unittest.TestCase):
    def test_proxy_bypass_includes_cnki_carsi_and_custom_domains(self):
        with patch.dict(
            "os.environ",
            {"SCHOLAR_CNKI_DIRECT_DOMAINS": "*.school.edu.cn;idp.example.edu"},
            clear=False,
        ):
            domains = driver._cnki_proxy_bypass_domains(["vpn.example.edu,library.example.edu"])

        self.assertIn("*.cnki.net", domains)
        self.assertIn("*.carsi.edu.cn", domains)
        self.assertIn("fsso.cnki.net", domains)
        self.assertIn("*.school.edu.cn", domains)
        self.assertIn("idp.example.edu", domains)
        self.assertIn("vpn.example.edu", domains)
        self.assertIn("library.example.edu", domains)
        self.assertEqual(domains.count("*.cnki.net"), 1)

    def test_select_cnki_institution_is_optional_and_generic(self):
        fake = LinkDriver()

        selected = driver._select_cnki_institution(fake, "Example University")

        self.assertTrue(selected)
        self.assertEqual(fake.visited[-1], "https://idp.example.edu/login")

    def test_authenticate_cnki_reuses_existing_session_without_login(self):
        fake = FakeDriver(text="机构馆 个人中心 我的知网")

        with patch.object(driver, "_detect_browser", return_value="chrome"), \
             patch.object(driver, "_create_driver", return_value=fake), \
             patch.object(driver, "_show_browser"), \
             patch.object(driver, "_hide_browser"), \
             patch.object(driver, "_load_cookies"), \
             patch.object(driver, "_save_cookies", side_effect=lambda d: setattr(d, "saved", True) or True):
            result = driver.authenticate_cnki(
                auth_url="https://fsso.cnki.net/",
                verify_url="https://kns.cnki.net/",
                wait_seconds=30,
            )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["already_authenticated"])
        self.assertTrue(result["access_confirmed"])
        self.assertTrue(result["cookies_saved"])
        self.assertEqual(fake.visited, ["https://kns.cnki.net/", "https://kns.cnki.net/"])
        self.assertTrue(fake.closed)

    def test_cmd_auth_cnki_outputs_json_and_passes_generic_options(self):
        with patch.object(
            literature,
            "authenticate_cnki",
            return_value={
                "status": "success",
                "access_confirmed": True,
                "direct_domains": ["idp.example.edu"],
            },
        ) as auth:
            out = io.StringIO()
            with redirect_stdout(out):
                literature.cmd_auth_cnki(
                    argparse.Namespace(
                        auth_url="https://library.example.edu/cnki",
                        verify_url="https://kns.cnki.net/",
                        institution=None,
                        wait_seconds=120,
                        captcha_timeout=60,
                        direct_domain=["idp.example.edu"],
                        debug_snapshot=None,
                        keep_browser=True,
                        force=True,
                    )
                )

        data = json.loads(out.getvalue())
        self.assertEqual(data["status"], "success")
        auth.assert_called_once()
        self.assertTrue(auth.call_args.kwargs["keep_browser"])
        self.assertTrue(auth.call_args.kwargs["force"])
        self.assertEqual(auth.call_args.kwargs["direct_domains"], ["idp.example.edu"])


if __name__ == "__main__":
    unittest.main()
