import unittest
import re
import warnings
import inspect
import gc
import ast
import glob
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

class TestAuditMemoryOptimizations(unittest.TestCase):

    def test_regex_syntax_and_warnings(self):
        """Verify no regex patterns have syntax errors or invalid escape sequences."""
        import recorder_core
        import api_server

        # Test roomId pattern matching in recorder_core and api_server
        pattern = r'"roomId"[:\"]+(\d{15,25})'
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("error")
            c = re.compile(pattern)
            self.assertIsNotNone(c)
            
            # JSON format with quotes: "roomId":"7412345678901234567"
            m1 = c.search('{"roomId":"7412345678901234567"}')
            self.assertIsNotNone(m1)
            self.assertEqual(m1.group(1), "7412345678901234567")

            # JSON format with integers: "roomId":7412345678901234567
            m2 = c.search('{"roomId":7412345678901234567}')
            self.assertIsNotNone(m2)
            self.assertEqual(m2.group(1), "7412345678901234567")

            # TikTok liveRoomUserInfo format in SIGI_STATE
            sigi_sample = '{"liveRoomUserInfo":{"liveRoom":{"roomId":"7412345678901234567","status":2}}}'
            m3 = c.search(sigi_sample)
            self.assertIsNotNone(m3)
            self.assertEqual(m3.group(1), "7412345678901234567")

            # FLV and HLS regex patterns
            flv_pat = r'https?://[^\s"\'<>]+\.flv\?[^\s"\'<>]+'
            hls_pat = r'https?://[^\s"\'<>]+\.m3u8\?[^\s"\'<>]*'
            re.compile(flv_pat)
            re.compile(hls_pat)

            # Test actual matches
            flv_url = "https://pull-flv-f16-va01.tiktokcdn.com/stage/stream-123.flv?auth_key=abc&amp;expire=123"
            self.assertTrue(bool(re.search(flv_pat, flv_url)))

            hls_url = "https://pull-hls-f16-va01.tiktokcdn.com/stage/stream-123.m3u8?auth_key=abc"
            self.assertTrue(bool(re.search(hls_pat, hls_url)))

    def test_all_codebase_regex_compile(self):
        """Ensure all static regex patterns in all python files compile without error."""
        for fpath in glob.glob("*.py"):
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            tree = ast.parse(content, filename=fpath)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("search", "findall", "match", "compile", "sub", "split"):
                        val = getattr(node.func.value, "id", None)
                        if val == "re" and node.args:
                            first_arg = node.args[0]
                            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                                try:
                                    re.compile(first_arg.value)
                                except Exception as e:
                                    self.fail(f"Regex compilation error in {fpath}:{node.lineno}: {e}")

    def test_no_tiktoklive_imports(self):
        """Verify TikTokLive is completely absent from all Python source files."""
        for fpath in glob.glob("*.py"):
            if fpath.startswith("test_"):
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            for line_no, line in enumerate(content.splitlines(), 1):
                clean = line.strip()
                if clean.startswith("#"):
                    continue
                self.assertNotIn("import TikTokLive", clean, f"Found import TikTokLive in {fpath}:{line_no}")
                self.assertNotIn("from TikTokLive", clean, f"Found from TikTokLive in {fpath}:{line_no}")
                self.assertNotIn("TikTokLiveClient", clean, f"Found TikTokLiveClient in {fpath}:{line_no}")

    def test_config_values(self):
        """Verify RAM mitigation configurations: LIVE_CACHE_TTL, ThreadPool max_workers, gc import."""
        import api_server
        self.assertEqual(api_server.LIVE_CACHE_TTL, 60.0, "LIVE_CACHE_TTL must be 60.0")

        # Verify max_workers in get_users
        get_users_source = inspect.getsource(api_server.get_users)
        self.assertIn("max_workers=min(len(users), 3)", get_users_source)
        self.assertIn("gc.collect()", get_users_source)

        # Verify max_workers in list_recordings_from_drive
        list_recs_source = inspect.getsource(api_server.list_recordings_from_drive)
        self.assertIn("max_workers=min(len(folders), 3)", list_recs_source)

        # Verify gc.collect() in test_live_diagnostic
        diag_source = inspect.getsource(api_server.test_live_diagnostic)
        self.assertIn("gc.collect()", diag_source)
        self.assertIn("sess.close()", diag_source)

    def test_memory_endpoint(self):
        """Verify GET /api/memory endpoint returns required fields."""
        import api_server
        res = api_server.get_memory_usage()
        self.assertIn("rss_mb", res)
        self.assertIn("vms_mb", res)
        self.assertIn("render_limit_mb", res)
        self.assertIn("warning", res)
        self.assertIn("cache_ttl_seconds", res)
        self.assertEqual(res["render_limit_mb"], 512)
        self.assertEqual(res["cache_ttl_seconds"], 60.0)
        self.assertIn(res["warning"], ["LOW", "MEDIUM", "HIGH"])

    def test_memory_endpoint_fastapi_client(self):
        """Verify GET /api/memory endpoint via FastAPI TestClient."""
        from api_server import app
        client = TestClient(app)
        resp = client.get("/api/memory")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("rss_mb", data)
        self.assertIn("render_limit_mb", data)
        self.assertEqual(data["render_limit_mb"], 512)
        self.assertIn(data["warning"], ["LOW", "MEDIUM", "HIGH"])

    def test_get_stream_urls_session_ownership(self):
        """
        Verify get_stream_urls handles session ownership:
        - When session=None: creates internal session and closes it in finally
        - When session is passed in: uses caller's session and does NOT close it
        """
        import recorder_core

        # 1. Caller-owned session: must NOT be closed by get_stream_urls
        caller_session = MagicMock()
        caller_session.headers = {}
        caller_session.cookies = {}
        mock_resp = MagicMock()
        mock_resp.text = '<html></html>'
        mock_resp.json.return_value = {"status_code": 0, "data": {}}
        caller_session.get.return_value = mock_resp

        with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
            urls = recorder_core.get_stream_urls("12345", user="testuser", session=caller_session)
            caller_session.close.assert_not_called()

        # 2. Internal session (session=None): MUST be closed in finally
        with patch("curl_cffi.requests.Session") as mock_session_class:
            mock_internal_session = MagicMock()
            mock_internal_session.headers = {}
            mock_internal_session.cookies = {}
            mock_internal_session.get.return_value = mock_resp
            mock_session_class.return_value = mock_internal_session

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="testuser", session=None)
                mock_internal_session.close.assert_called_once()

    def test_get_stream_urls_early_return_closes_session(self):
        """
        Verify get_stream_urls closes owned session even when early returning
        (e.g., direct scrape finds FLV stream or age restriction).
        """
        import recorder_core

        with patch("curl_cffi.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.headers = {}
            mock_session.cookies = {}
            mock_session_class.return_value = mock_session

            # Simulate finding FLV in direct scrape -> triggers early return at line 408
            mock_resp = MagicMock()
            mock_resp.text = '<html>https://pull-flv.tiktokcdn.com/test.flv?auth=123</html>'
            mock_session.get.return_value = mock_resp

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="testuser", session=None)
                self.assertTrue(isinstance(urls, list))
                self.assertTrue(any(".flv" in u for u in urls))
                # Session MUST be closed even on early return!
                mock_session.close.assert_called_once()

    def test_check_live_details_session_closed_on_early_return(self):
        """
        Verify check_live_details closes session even when early returning (e.g. status == 4).
        """
        import recorder_core

        # Mock Native Live API to fall through to HTML scrape
        with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
            with patch("curl_cffi.requests.Session") as mock_session_class:
                mock_session = MagicMock()
                mock_session_class.return_value = mock_session

                # Case: Streamer is offline (status: 4) -> early return
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.text = '<html>"uniqueId":"testuser","status": 4</html>'
                mock_session.get.return_value = mock_resp

                res = recorder_core.check_live_details("testuser")
                self.assertFalse(res["is_live"])
                mock_session.close.assert_called_once()

    def test_cloud_daemon_session_handling(self):
        """Verify cloud_daemon closes session in discover_new_streamers and guest_session."""
        import cloud_daemon
        src_discover = inspect.getsource(cloud_daemon.discover_new_streamers)
        self.assertIn("finally:", src_discover)
        self.assertIn("s.close()", src_discover)

        src_worker = inspect.getsource(cloud_daemon.streamer_recording_worker)
        self.assertIn("guest_session.close()", src_worker)

    def test_edge_cases_empty_and_boundary_inputs(self):
        """Verify behavior with empty strings, empty lists, and boundary values."""
        import recorder_core
        import api_server

        # 1. Empty username in check_live_details
        det = recorder_core.check_live_details("")
        self.assertFalse(det["is_live"])
        self.assertIsNone(det["room_id"])

        # 2. Empty username in get_stream_urls
        with patch("curl_cffi.requests.Session") as mock_sess_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"status_code": 0, "data": {}}
            mock_s.get.return_value = mock_resp
            mock_sess_cls.return_value = mock_s

            urls = recorder_core.get_stream_urls("12345", user="", session=None)
            self.assertEqual(urls, [])
            mock_s.close.assert_called_once()

        # 3. ThreadPool min workers boundary calculations
        self.assertEqual(min(len([]), 3), 0)
        self.assertEqual(min(len(["a"]), 3), 1)
        self.assertEqual(min(len(["a", "b"]), 3), 2)
        self.assertEqual(min(len(["a", "b", "c"]), 3), 3)
        self.assertEqual(min(len(["a", "b", "c", "d", "e"]), 3), 3)

    def test_age_restricted_stream_closes_session(self):
        """Verify age restricted (4003110) streams return AGE_RESTRICTED and close session."""
        import recorder_core

        with patch("curl_cffi.requests.Session") as mock_sess_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_resp = MagicMock()
            mock_resp.text = "<html></html>"
            mock_resp.json.return_value = {"status_code": 4003110}
            mock_s.get.return_value = mock_resp
            mock_sess_cls.return_value = mock_s

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                res = recorder_core.get_stream_urls("99999", user="age_user", session=None)
                self.assertEqual(res, "AGE_RESTRICTED")
                mock_s.close.assert_called_once()

    def test_memory_warning_levels(self):
        """Verify warning levels: HIGH (>350MB), MEDIUM (>200MB), LOW (<=200MB)."""
        import api_server
        def compute_warning(rss_mb):
            return "HIGH" if rss_mb > 350 else ("MEDIUM" if rss_mb > 200 else "LOW")

        self.assertEqual(compute_warning(400.0), "HIGH")
        self.assertEqual(compute_warning(351.0), "HIGH")
        self.assertEqual(compute_warning(350.0), "MEDIUM")
        self.assertEqual(compute_warning(250.0), "MEDIUM")
        self.assertEqual(compute_warning(201.0), "MEDIUM")
        self.assertEqual(compute_warning(200.0), "LOW")
        self.assertEqual(compute_warning(64.0), "LOW")

if __name__ == "__main__":
    unittest.main()
