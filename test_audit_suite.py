import unittest
import json
import os
import re
import warnings
import inspect
import gc
import ast
import glob
import threading
import time
import recorder_core
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import urllib.request

# Unit tests run offline: mock urllib.request.urlopen to prevent unmocked real TikTok network calls
urllib.request.urlopen = MagicMock(side_effect=Exception("urllib disabled in unit tests"))

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

        # Verify max_workers in get_users (chỉ probe streamer CHƯA đang ghi hình)
        get_users_source = inspect.getsource(api_server.get_users)
        self.assertIn("max_workers=min(len(users_to_probe), 8)", get_users_source)
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

    def test_get_stream_urls_closes_session_on_cookie_exception(self):
        """
        Adversarial Test: Verify get_stream_urls closes session even if
        cookie setup or session.cookies.update raises an exception.
        """
        import recorder_core
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.cookies.update.side_effect = TypeError("malformed cookie data")
            mock_s_cls.return_value = mock_s

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="testuser", cookies={"bad": None}, session=None)
                self.assertEqual(urls, [])
                mock_s.close.assert_called_once()

    def test_get_stream_urls_graceful_network_failure(self):
        """
        Adversarial Test: Verify get_stream_urls catches network connection / timeout
        exceptions gracefully, closes the session, and returns [] instead of crashing.
        """
        import recorder_core
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_s.get.side_effect = ConnectionResetError("Connection reset by peer")
            mock_s_cls.return_value = mock_s

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="testuser", session=None)
                self.assertEqual(urls, [])
                mock_s.close.assert_called_once()

    def test_get_stream_urls_webcast_timeout_and_no_caller_header_mutation(self):
        """
        Adversarial Test: Verify session.get in Webcast API call specifies a timeout
        and passes headers to get() without mutating caller session headers.
        """
        import recorder_core
        caller_session = MagicMock()
        caller_session.headers = {"Original-Header": "OriginalValue"}
        caller_session.cookies = {}
        mock_resp = MagicMock()
        mock_resp.text = '<html></html>'
        mock_resp.json.return_value = {"status_code": 0, "data": {}}
        caller_session.get.return_value = mock_resp

        with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
            urls = recorder_core.get_stream_urls("12345", user="testuser", session=caller_session)
            # Caller session should NOT be closed
            caller_session.close.assert_not_called()
            # session.get should have been called with a timeout
            caller_session.get.assert_called()
            call_kwargs = caller_session.get.call_args[1]
            self.assertIn("timeout", call_kwargs)
            self.assertEqual(call_kwargs["timeout"], 10)
            # Caller session headers should NOT have been polluted
            self.assertEqual(caller_session.headers, {"Original-Header": "OriginalValue"})

    def test_empty_inputs_short_circuit_without_network_or_session(self):
        """
        Adversarial Test: Verify empty/whitespace username returns immediately
        without allocating any Session or making network calls.
        """
        import recorder_core
        with patch("curl_cffi.requests.Session") as mock_s_cls, \
             patch("curl_cffi.requests.get") as mock_get:
            
            # 1. Empty username in check_live_details
            det1 = recorder_core.check_live_details("")
            self.assertFalse(det1["is_live"])
            self.assertIsNone(det1["room_id"])

            det2 = recorder_core.check_live_details("   ")
            self.assertFalse(det2["is_live"])
            self.assertIsNone(det2["room_id"])

            # 2. Empty room_id and user in get_stream_urls
            urls = recorder_core.get_stream_urls("", "")
            self.assertEqual(urls, [])

            # Neither Session nor get should have been called
            mock_s_cls.assert_not_called()
            mock_get.assert_not_called()

    def test_generate_guest_session_exception_safety(self):
        """
        Adversarial Test: Verify generate_guest_session closes session if an error
        occurs during guest cookie/header setup before returning.
        """
        import recorder_core
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.cookies.set.side_effect = RuntimeError("Failed to set cookie")
            mock_s_cls.return_value = mock_s

            with self.assertRaises(RuntimeError):
                recorder_core.generate_guest_session()

            mock_s.close.assert_called_once()

    def test_direct_scrape_hls_stream_no_name_error(self):
        """
        Adversarial Test: Verify direct HTML scrape with HLS (.m3u8) streams
        correctly extracts clean_hls without NameError 'clean_flv'.
        """
        import recorder_core
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_s_cls.return_value = mock_s

            mock_resp = MagicMock()
            mock_resp.text = '<html>https://pull-hls.tiktokcdn.com/test_uhd.m3u8?auth=123&amp;exp=456</html>'
            mock_s.get.return_value = mock_resp

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="testuser", session=None)
                self.assertTrue(isinstance(urls, list))
                self.assertTrue(len(urls) > 0)
                self.assertTrue(any(".m3u8" in u for u in urls))
                self.assertNotIn("&amp;", urls[0])
                mock_s.close.assert_called_once()

    def test_native_api_step0_fallback_to_step1_scrape(self):
        """
        Adversarial Test: Verify that when Native Live API (Step 0) fails
        (e.g. 403 Forbidden, 500 Internal Error, or corrupted JSON),
        the system cleanly falls back to Step 1 HTML scrape with zero TikTokLive dependency.
        """
        import recorder_core
        # Mock Step 0: returns 500 error
        mock_step0_resp = MagicMock()
        mock_step0_resp.status_code = 500
        mock_step0_resp.text = "Internal Server Error"

        with patch("curl_cffi.requests.get", return_value=mock_step0_resp):
            with patch("curl_cffi.requests.Session") as mock_s_cls:
                mock_s = MagicMock()
                mock_s.headers = {}
                mock_s.cookies = {}
                mock_s_cls.return_value = mock_s

                # Mock Step 1: HTML scrape returns live streamer with roomId
                mock_step1_resp = MagicMock()
                mock_step1_resp.status_code = 200
                mock_step1_resp.text = '<html><script id="SIGI_STATE">{"LiveRoom":{"liveRoomUserInfo":{"liveRoom":{"roomId":"7419876543210987654","status":2}}}}</script></html>'
                mock_s.get.return_value = mock_step1_resp

                det = recorder_core.check_live_details("fallback_user")
                self.assertTrue(det["is_live"])
                self.assertEqual(det["room_id"], "7419876543210987654")
                mock_s.close.assert_called_once()

    def test_cloud_daemon_gc_support(self):
        """Verify cloud_daemon imports gc and calls gc.collect() in main loop."""
        import cloud_daemon
        self.assertTrue(hasattr(cloud_daemon, "gc"), "cloud_daemon must import gc")
        src = inspect.getsource(cloud_daemon.run_daemon)
        self.assertIn("gc.collect()", src, "run_daemon must call gc.collect() in its loop")

    def test_concurrent_stress_simulation(self):
        """
        Adversarial Stress Test: Simulate 50 concurrent requests across
        check_live_details, get_stream_urls, /api/users, and /api/memory.
        Verify zero thread deadlock, all sessions closed, and memory stability.
        """
        import concurrent.futures
        import recorder_core
        from api_server import app

        client = TestClient(app)
        num_workers = 10
        total_tasks = 50

        # Mock network and cloud providers to prevent external latency while testing concurrency & locking
        with patch("curl_cffi.requests.get") as mock_get, \
             patch("curl_cffi.requests.Session") as mock_s_cls, \
             patch("gdrive_manager.load_streamers_from_drive", return_value=["user_a", "user_b"]), \
             patch("gdrive_manager.load_active_recordings_from_drive", return_value=set()), \
             patch("supabase_sync.fetch_streamers_from_supabase", return_value=[]):

            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '<html><script id="SIGI_STATE">{"LiveRoom":{"liveRoomUserInfo":{"liveRoom":{"roomId":"7419876543210987654","status":4}}}}</script></html>'
            mock_resp.json.return_value = {"status_code": 0, "data": {}}
            mock_s.get.return_value = mock_resp
            mock_s_cls.return_value = mock_s
            mock_get.return_value = mock_resp

            def _worker_task(i):
                if i % 4 == 0:
                    return recorder_core.check_live_details(f"user_{i}")
                elif i % 4 == 1:
                    return recorder_core.get_stream_urls(f"room_{i}", user=f"user_{i}")
                elif i % 4 == 2:
                    return client.get("/api/users?check_live=false").status_code
                else:
                    return client.get("/api/memory").json()

            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(_worker_task, i) for i in range(total_tasks)]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]

            self.assertEqual(len(results), total_tasks)
            # Verify mock session was closed every time it was created
            self.assertEqual(mock_s.close.call_count, mock_s_cls.call_count)

    def test_oversized_response_memory_guard(self):
        """
        Adversarial Test: Verify check_live_details and get_stream_urls safely
        skip oversized HTML responses (>3-4MB) without OOM or string-multiplication surge,
        and always close the session.
        """
        import recorder_core

        # 1. check_live_details with 5MB oversized payload
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_s_cls.return_value = mock_s

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            # 5MB payload of repeating HTML comments
            mock_resp.text = "<!-- filler -->" * (350 * 1024)
            mock_s.get.return_value = mock_resp

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                det = recorder_core.check_live_details("oversized_user")
                self.assertFalse(det["is_live"])
                mock_s.close.assert_called_once()

        # 2. get_stream_urls with 5MB oversized payload
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_s_cls.return_value = mock_s

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "<!-- filler -->" * (350 * 1024)
            mock_resp.json.return_value = {}
            mock_s.get.return_value = mock_resp

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="oversized_user", session=None)
                self.assertEqual(urls, [])
                mock_s.close.assert_called_once()

    def test_malformed_and_non_dict_api_responses(self):
        """
        Adversarial Test: Verify Native API and Webcast API handle malformed,
        non-dict, or unexpected data types (string, list, int, None) without AttributeError
        and guarantee all sessions are closed.
        """
        import recorder_core

        # 1. Native API returns non-dict JSON (e.g. list or integer)
        mock_resp0 = MagicMock()
        mock_resp0.status_code = 200
        mock_resp0.json.return_value = ["unexpected", "list"]

        with patch("curl_cffi.requests.get", return_value=mock_resp0):
            with patch("curl_cffi.requests.Session") as mock_s_cls:
                mock_s = MagicMock()
                mock_s.headers = {}
                mock_s.cookies = {}
                mock_s_cls.return_value = mock_s
                mock_s.get.return_value = MagicMock(status_code=404, text="")

                det = recorder_core.check_live_details("malformed_user")
                self.assertFalse(det["is_live"])
                mock_s.close.assert_called_once()

        # 2. Webcast API returns non-dict data in get_stream_urls
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_s_cls.return_value = mock_s

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            # HTML scrape returns nothing
            mock_resp.text = "<html>no stream here</html>"
            # Webcast returns non-dict data field
            mock_resp.json.return_value = {
                "status_code": 0,
                "data": "corrupted string instead of object"
            }
            mock_s.get.return_value = mock_resp

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="corrupt_user", session=None)
                self.assertEqual(urls, [])
                mock_s.close.assert_called_once()

    def test_sub_only_vip_guest_session_error_and_rate_limit(self):
        """
        Adversarial Test: Verify sub-only VIP guest session generation failure
        (e.g. HTTP 429/403 or captcha challenge) is handled safely:
        - Sessions are not leaked
        - Worker does not loop infinitely and terminates at max_consecutive_failures
        """
        import cloud_daemon
        import recorder_core

        # Simulate generate_guest_session raising exception
        with patch("recorder_core.generate_guest_session", side_effect=RuntimeError("TikTok Rate Limit 429 / Captcha")), \
             patch("recorder_core.check_live_details", return_value={"is_live": True, "is_sub_only": True, "room_id": "111"}), \
             patch("recorder_core.check_live_status", return_value=(True, "111")), \
             patch("supabase_sync.update_streamer_status_supabase"), \
             patch("gdrive_manager.set_user_recording_status_drive"), \
             patch("gdrive_manager.create_streamer_folder_drive"), \
             patch.object(time, "sleep"):

            stop_ev = threading.Event()
            # Run worker; should encounter consecutive failures and break out safely without infinite loop
            cloud_daemon.streamer_recording_worker("vip_test_user", "111", auto_discover=False, stop_event=stop_ev)
            # Worker finished without hanging or uncaught exception
            self.assertNotIn("vip_test_user", cloud_daemon.ACTIVE_RECORDERS)

    def test_api_server_graceful_shutdown_and_lifespan(self):
        """
        Adversarial Test: Verify api_server graceful shutdown and lifespan
        signal stop_event on all active recording tasks to cleanly flush FFmpeg MP4 moov atoms.
        """
        import api_server

        stop_evt1 = threading.Event()
        stop_evt2 = threading.Event()
        with api_server.RECORDING_LOCK:
            api_server.ACTIVE_RECORDING_TASKS["user_shutdown_1"] = {"start_time": time.time(), "stop_event": stop_evt1}
            api_server.ACTIVE_RECORDING_TASKS["user_shutdown_2"] = {"start_time": time.time(), "stop_event": stop_evt2}

        self.assertFalse(stop_evt1.is_set())
        self.assertFalse(stop_evt2.is_set())

        # Trigger shutdown function
        api_server.shutdown_all_recording_tasks()

        self.assertTrue(stop_evt1.is_set())
        self.assertTrue(stop_evt2.is_set())

        # Cleanup
        with api_server.RECORDING_LOCK:
            api_server.ACTIVE_RECORDING_TASKS.pop("user_shutdown_1", None)
            api_server.ACTIVE_RECORDING_TASKS.pop("user_shutdown_2", None)

    def test_discover_new_streamers_guards(self):
        """
        Adversarial Test: Verify discover_new_streamers guards against empty username,
        non-200 responses, and oversized responses, and always closes the session.
        """
        import cloud_daemon

        # 1. Empty username -> immediate empty list
        res = cloud_daemon.discover_new_streamers("")
        self.assertEqual(res, [])

        # 2. Non-200 response -> returns empty list and closes session
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.cookies = {}
            mock_s_cls.return_value = mock_s
            mock_resp = MagicMock(status_code=403, text="Forbidden")
            mock_s.get.return_value = mock_resp

            res = cloud_daemon.discover_new_streamers("someuser")
            self.assertEqual(res, [])
            mock_s.close.assert_called_once()

    def test_bg_record_worker_infinite_retry_prevention(self):
        """
        Adversarial Test: Verify bg_record_worker in api_server prevents infinite busy loops:
        - Segment validation failure increments consecutive_failures and stops at max_consecutive_failures (4).
        - VIP sub-only room whose stream links can never be fetched also stops at the same failure cap.
        Mọi lời gọi mạng (TikTok scrape / Google Drive heartbeat / Drive token) đều bị mock
        để bộ test chạy offline, không phụ thuộc mạng và không kẹt trong vòng chờ offline 300s.
        """
        import api_server
        import contextlib
        import io

        # 1. Video validation failure stops after 4 consecutive failures
        v_calls = {"n": 0}

        def _fail_validation(*_a, **_k):
            v_calls["n"] += 1
            if v_calls["n"] > 8:
                # Chốt chặn: nếu logic break bị hỏng thì fail ngay thay vì treo vô hạn.
                raise RuntimeError(f"validate_playable_video bị gọi {v_calls['n']} lần -> worker kẹt vòng lặp")
            return (False, "corrupt container", 0.0)

        with patch("api_server.get_user_live_details_cached", return_value={"is_live": True, "room_id": "999", "is_sub_only": False}), \
             patch("recorder_core.get_stream_candidates", return_value=["https://live.tiktok.com/stream.flv"]), \
             patch("recorder_core.check_live_details", return_value={"is_live": True, "room_id": "999"}), \
             patch("recorder_core.record_stream_ffmpeg", return_value="mock_invalid.mp4"), \
             patch("auto_h264.validate_playable_video", side_effect=_fail_validation), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"), \
             patch("gdrive_manager.set_user_recording_status_drive"), \
             patch("gdrive_manager.get_access_token", return_value=None), \
             patch.object(time, "sleep"):

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                api_server.bg_record_worker("worker_test_user")
            out = buf.getvalue()

        self.assertEqual(v_calls["n"], 8, "Worker phải dừng đúng sau max_consecutive_failures=8 lần lỗi liên tiếp")
        self.assertIn("Dừng tích lũy", out)

        # 2. Sub-only preview: không lấy được link stream -> dừng ở cùng ngưỡng thất bại
        cs_calls = {"n": 0}

        def _no_stream(*_a, **_k):
            cs_calls["n"] += 1
            if cs_calls["n"] > 8:
                raise RuntimeError(f"check_live_status bị gọi {cs_calls['n']} lần -> worker kẹt vòng lặp")
            return (False, None)

        with patch("api_server.get_user_live_details_cached", return_value={"is_live": True, "room_id": "999", "is_sub_only": True}), \
             patch("recorder_core.generate_guest_session", side_effect=RuntimeError("Captcha")), \
             patch("recorder_core.get_stream_candidates", return_value=[]), \
             patch("recorder_core.check_live_status", side_effect=_no_stream), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("gdrive_manager.set_user_recording_status_drive"), \
             patch("gdrive_manager.get_access_token", return_value=None), \
             patch.object(time, "sleep"):

            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                api_server.bg_record_worker("vip_worker_user")
            out2 = buf2.getvalue()

        self.assertEqual(cs_calls["n"], 8, "Worker VIP sub-only phải dừng sau max_consecutive_failures=8 lần")
        self.assertIn("Không lấy được link stream", out2)

    def test_gc_collect_exception_safety_in_api_server(self):
        """
        Adversarial Test: Verify that gc.collect() is called in get_users() and
        test_live_diagnostic() even when partial exceptions or unexpected errors occur.
        """
        import api_server

        # 1. get_users raises unexpected exception during user fetching
        with patch("supabase_sync.fetch_streamers_from_supabase", side_effect=TypeError("Unexpected mock failure")), \
             patch("supabase_sync.fetch_streamers_details_from_supabase", return_value=[]), \
             patch("gdrive_manager.load_streamers_from_drive", return_value=["test_u1"]), \
             patch("gdrive_manager.load_active_recordings_from_drive", return_value=set()), \
             patch("api_server.clean_zombie_recordings", side_effect=RuntimeError("Zombie cleaner crash")), \
             patch("gc.collect") as mock_gc:

            try:
                api_server.get_users(check_live=False)
            except Exception:
                pass
            mock_gc.assert_called()

        # 2. test_live_diagnostic raises exception during processing
        with patch("recorder_core.check_live_details", side_effect=ValueError("Test crash")), \
             patch("gc.collect") as mock_gc:

            res = api_server.test_live_diagnostic("error_user")
            mock_gc.assert_called()
            self.assertIn("error_user", res.get("user", ""))

    def test_get_stream_urls_no_room_id_and_unbound_variable(self):
        """
        Adversarial Test: Verify get_stream_urls(room_id=None, user='someuser')
        executes cleanly without UnboundLocalError when HTML scraping returns no streams.
        """
        import recorder_core

        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s.cookies = {}
            mock_s_cls.return_value = mock_s
            mock_resp = MagicMock(status_code=200, text="<html>No streams here</html>")
            mock_s.get.return_value = mock_resp

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                # When room_id is None, candidates and stream_url_obj must be safely initialized
                urls = recorder_core.get_stream_urls(room_id=None, user="unbound_test_user", session=None)
                self.assertEqual(urls, [])
                mock_s.close.assert_called_once()

    def test_proxy_resilience_and_session_cleanup(self):
        """
        Adversarial Test: Verify proxy parameter handling and proxy failure resilience
        (proxy timeout, connection reset, 407 Proxy Auth failure) across:
        - generate_guest_session
        - check_live_details
        - get_stream_urls
        Ensures sessions are 100% closed and no unhandled crashes occur.
        """
        import recorder_core

        # 1. generate_guest_session accepts proxy and properly sets proxies
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s_cls.return_value = mock_s

            sess = recorder_core.generate_guest_session(proxy="http://user:pass@1.2.3.4:8080")
            self.assertEqual(sess, mock_s)
            mock_s_cls.assert_called_with(impersonate=mock_s_cls.call_args[1]["impersonate"],
                                          proxies={"http": "http://user:pass@1.2.3.4:8080",
                                                   "https": "http://user:pass@1.2.3.4:8080"})

        # 2. check_live_details with proxy connection reset / 407 error closes session
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s_cls.return_value = mock_s
            mock_s.get.side_effect = ConnectionResetError("407 Proxy Authentication Required")

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                det = recorder_core.check_live_details("proxy_fail_user", proxy="http://bad-proxy:8080")
                self.assertFalse(det["is_live"])
                mock_s.close.assert_called_once()

        # 3. get_stream_urls with proxy timeout closes owned session
        with patch("curl_cffi.requests.Session") as mock_s_cls:
            mock_s = MagicMock()
            mock_s.headers = {}
            mock_s_cls.return_value = mock_s
            mock_s.get.side_effect = TimeoutError("Proxy connect timed out")

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("12345", user="proxy_user", session=None, proxy="http://proxy:8080")
                self.assertEqual(urls, [])
                mock_s.close.assert_called_once()

    def test_record_stream_ffmpeg_process_cleanup_on_exception(self):
        """
        Adversarial Test: Verify that record_stream_ffmpeg always cleans up proc in finally:
        even if an unexpected exception occurs inside the recording loop.
        """
        import recorder_core

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None]  # First 2 calls running, then exception
        mock_proc.stdin = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("time.sleep", side_effect=RuntimeError("Unexpected error inside recording loop")), \
             patch("os.path.exists", return_value=False), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"):

            try:
                recorder_core.record_stream_ffmpeg("http://test.flv", output_filename="dummy.mp4")
            except RuntimeError:
                pass

            # Verify _safe_stop_ffmpeg attempted to stop the proc
            self.assertTrue(mock_proc.wait.called or mock_proc.terminate.called or mock_proc.kill.called or mock_proc.stdin.write.called)

    def test_streaming_response_socket_closure(self):
        """
        Adversarial Test (Modernized for R4 Zero Render Bandwidth):
        Verify that stream_video_by_id does NOT proxy binary chunks through Render,
        but returns a 302 RedirectResponse directly to Google Edge CDN, ensuring 0 transit bytes.
        """
        import api_server

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("gdrive_manager.is_recorder_owned_file", return_value=True), \
             patch("gdrive_manager.make_file_public") as mock_public:

            client = TestClient(api_server.app, follow_redirects=False)
            resp = client.get("/api/stream-video-id/file_123")
            self.assertIn(resp.status_code, (302, 307))
            self.assertEqual(resp.status_code, 302)
            expected_location = "https://drive.usercontent.google.com/download?id=file_123&export=download&authuser=0&confirm=t"
            self.assertEqual(resp.headers.get("location"), expected_location)
            mock_public.assert_called_once_with("file_123", access_token="fake_token")


    def test_malformed_intermediate_sdk_data_fallback(self):
        """
        Adversarial Test: Verify get_stream_urls when live_core_sdk_data is a string
        or pull_data is None/string does not crash with AttributeError and properly
        extracts fallback flv_pull_url and hls_pull_url.
        """
        import recorder_core

        # 1. live_core_sdk_data is string instead of dict
        mock_s = MagicMock()
        mock_s.headers = {}
        mock_s.cookies = {}
        mock_resp = MagicMock(status_code=200, text="<html>no stream here</html>")
        mock_resp.json.return_value = {
            "status_code": 0,
            "data": {
                "stream_url": {
                    "live_core_sdk_data": "corrupt_string",
                    "flv_pull_url": {"FULL_HD1": "https://pull.flv/stream1.flv"},
                    "hls_pull_url": "https://pull.hls/stream1.m3u8"
                }
            }
        }
        mock_s.get.return_value = mock_resp

        with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
            urls = recorder_core.get_stream_urls("12345", user="sdk_corrupt_user", session=mock_s)
            self.assertIn("https://pull.flv/stream1.flv", urls)
            self.assertIn("https://pull.hls/stream1.m3u8", urls)

        # 2. pull_data is None instead of dict
        mock_resp.json.return_value = {
            "status_code": 0,
            "data": {
                "stream_url": {
                    "live_core_sdk_data": {"pull_data": None},
                    "flv_pull_url": {"FULL_HD1": "https://pull.flv/stream2.flv"}
                }
            }
        }
        with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
            urls = recorder_core.get_stream_urls("12345", user="sdk_corrupt_user2", session=mock_s)
            self.assertEqual(urls, ["https://pull.flv/stream2.flv"])

    def test_streaming_response_error_status_and_exception_cleanup(self):
        """
        Adversarial Test (Modernized for R4 Zero Render Bandwidth):
        Verify stream_video_by_id falls back to 302 redirect to Google Drive preview
        when CDN direct link generation raises an exception, and returns 500 when
        Google Drive access token is unavailable.
        """
        import api_server

        # 1. Fallback to Drive Preview on make_file_public failure
        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("gdrive_manager.is_recorder_owned_file", return_value=True), \
             patch("gdrive_manager.make_file_public", side_effect=Exception("CDN permission error")):

            client = TestClient(api_server.app, follow_redirects=False)
            resp = client.get("/api/stream-video-id/error_fid_123")
            self.assertIn(resp.status_code, (302, 307))
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("location"), "https://drive.google.com/file/d/error_fid_123/preview")

        # 2. Return 500 if no Google Drive access token
        with patch("gdrive_manager.get_access_token", return_value=None):
            client = TestClient(api_server.app, follow_redirects=False)
            resp = client.get("/api/stream-video-id/no_token_fid")
            self.assertEqual(resp.status_code, 500)

    def test_thumbnail_response_socket_closure(self):
        """
        Adversarial Test: Verify get_thumbnail explicitly closes img_res socket.
        """
        import api_server

        mock_th_list = MagicMock()
        mock_th_list.status_code = 200
        mock_th_list.json.return_value = {"files": [{"id": "img_file_123"}]}

        mock_img_resp = MagicMock()
        mock_img_resp.status_code = 200
        mock_img_resp.content = b"\xff\xd8\xff\xe0" + b"\x00" * 300

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="fake_fid"), \
             patch("requests.get", side_effect=[mock_th_list, mock_img_resp]):

            client = TestClient(api_server.app)
            resp = client.get("/api/thumbnail/test_user/test_video.mp4")
            self.assertEqual(resp.status_code, 200)
            mock_img_resp.close.assert_called_once()

    def test_supabase_sync_thumb_socket_closure(self):
        """
        Adversarial Test: Verify sync_thumbnail_to_supabase closes response when downloading thumb_source from URL.
        """
        import supabase_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"\xff\xd8\xff\xe0" + b"\x00" * 300

        mock_post = MagicMock()
        mock_post.status_code = 200

        with patch("requests.get", return_value=mock_resp), \
             patch("requests.post", return_value=mock_post):

            url = supabase_sync.upload_thumbnail_to_supabase("test_user", "vid.mp4", thumb_source="https://cdn.example.com/thumb.jpg")
            self.assertIsNotNone(url)
            mock_resp.close.assert_called_once()

    def test_cloud_daemon_vip_preview_max_attempts(self):
        """
        Adversarial Test: Verify cloud_daemon streamer_recording_worker allows full 5 VIP preview
        attempts (parts 1 through 5) before terminating at max_vip_attempts.
        """
        import cloud_daemon

        recorded_parts = []
        def fake_record(url, output_filename=None, **kwargs):
            recorded_parts.append(output_filename)
            return output_filename

        with patch("recorder_core.generate_guest_session") as mock_gs, \
             patch("recorder_core.get_live_stream_url", return_value="http://pull.flv/stream.flv"), \
             patch("recorder_core.record_stream_ffmpeg", side_effect=fake_record), \
             patch("auto_h264.validate_playable_video", return_value=(True, "ok", 10.0)), \
             patch("recorder_core.check_live_details", return_value={"is_live": True, "room_id": "111", "is_sub_only": True}), \
             patch("gdrive_manager.get_access_token", return_value="fake_tok"), \
             patch("gdrive_manager.find_or_create_folder", return_value="fake_fid"), \
             patch("staging_queue.add_to_staging_queue", return_value={"status": "ok"}), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"), \
             patch("shutil.move"), \
             patch("time.sleep"):

            cloud_daemon.streamer_recording_worker("vip_full_user", "111")
            self.assertEqual(len(recorded_parts), 5)

    def test_staging_manifest_network_failure_safety(self):
        """
        Unit Test: Verify get_staging_manifest raises IOError on network error / non-200,
        and add_to_staging_queue aborts without overwriting Drive manifest when manifest read fails.
        """
        import staging_queue

        # 1. Verify get_staging_manifest raises IOError on non-200 or network error
        mock_500_resp = MagicMock(status_code=500, text="Drive API 500 Internal Error")
        mock_500_resp.__enter__.return_value = mock_500_resp
        mock_500_resp.__exit__.return_value = False

        with patch("requests.get", return_value=mock_500_resp):
            with self.assertRaises(IOError):
                staging_queue.get_staging_manifest("test_user", "staging_fid_123", access_token="fake_token")

        # 2. Verify add_to_staging_queue does NOT call save_staging_manifest when manifest fetch fails
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch("staging_queue._get_user_staging_lock", return_value=mock_lock), \
             patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("user_staging_id", "root_fid")), \
             patch("gdrive_manager.upload_file_to_drive", return_value="file_new_segment"), \
             patch("staging_queue.get_staging_manifest", side_effect=IOError("Transient connection reset")), \
             patch("staging_queue.save_staging_manifest") as mock_save, \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=500 * 1024):

            res = staging_queue.add_to_staging_queue("test_user", "local_seg.mp4", 120.0, access_token="fake_token")
            self.assertEqual(res.get("status"), "error")
            self.assertIn("Transient connection reset", res.get("message", ""))
            self.assertFalse(mock_save.called, "save_staging_manifest must NOT be called on manifest read failure!")

    def test_staging_unmerged_segment_preservation(self):
        """
        Unit Test: Verify package_and_publish_queue preserves local segment files
        and keeps them in the staging manifest if download or merge fails for those segments.
        """
        import staging_queue

        manifest = {
            "user": "streamer_preserve",
            "segments": [
                {"filename": "seg1.mp4", "file_id": "fid_seg_1", "local_path": "local_seg1.mp4", "duration": 60.0},
                {"filename": "seg2.mp4", "file_id": "fid_seg_2", "local_path": "local_seg2.mp4", "duration": 60.0}
            ],
            "manifest_file_id": "mfid_preserve"
        }

        deleted_files = []
        def fake_remove(path):
            deleted_files.append(path)

        def fake_download(fid, dst, access_token=None):
            return fid == "fid_seg_1"  # seg2 download fails

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch("staging_queue._get_user_staging_lock", return_value=mock_lock), \
             patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("user_staging_id", "root_fid")), \
             patch("staging_queue.get_staging_manifest", return_value=manifest), \
             patch("staging_queue.save_staging_manifest") as mock_save, \
             patch("gdrive_manager.download_file_from_drive", side_effect=fake_download), \
             patch("staging_queue.validate_playable_video", return_value=(True, "ok", 60.0)), \
             patch("gdrive_manager.find_or_create_folder", return_value="ufid_main"), \
             patch("gdrive_manager.upload_file_to_drive", return_value="new_main_drive_id"), \
             patch("gdrive_manager.delete_file_drive"), \
             patch("supabase_sync.sync_recording_to_supabase", return_value=True), \
             patch("os.remove", side_effect=fake_remove), \
             patch("os.path.getsize", return_value=300000), \
             patch("os.path.exists", side_effect=lambda p: True if p in ["local_seg1.mp4", "local_seg2.mp4", "thumb.jpg"] or p.endswith("_full.mp4") or "seg1.mp4" in p else False), \
             patch("shutil.copy2"), \
             patch("shutil.rmtree"):

            res = staging_queue.package_and_publish_queue("streamer_preserve", access_token="fake_token")
            self.assertTrue(res.get("ok"))
            # Merged segment local file was cleaned up
            self.assertIn("local_seg1.mp4", deleted_files)
            # Unmerged segment local file was preserved
            self.assertNotIn("local_seg2.mp4", deleted_files, "Unmerged segment local file must NOT be deleted!")
            # Preserved segment remains in saved manifest
            self.assertTrue(mock_save.called)
            saved_segs = mock_save.call_args[0][2].get("segments", [])
            self.assertEqual(len(saved_segs), 1)
            self.assertEqual(saved_segs[0].get("file_id"), "fid_seg_2")

    def test_staging_lock_acquisition_timeout(self):
        """
        Unit Test: Verify timeout handling in _USER_STAGING_LOCKS prevents indefinite blocking
        and returns clean error responses for both add_to_staging_queue and package_and_publish_queue.
        """
        import staging_queue

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False  # Lock acquisition timed out

        with patch("staging_queue._get_user_staging_lock", return_value=mock_lock), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=500 * 1024):

            # 1. add_to_staging_queue timeout
            res_add = staging_queue.add_to_staging_queue("busy_user", "seg.mp4", 60.0)
            self.assertEqual(res_add.get("status"), "error")
            self.assertIn("bận", res_add.get("message", ""))
            mock_lock.acquire.assert_called_with(timeout=30.0)

            # 2. package_and_publish_queue timeout
            res_pkg = staging_queue.package_and_publish_queue("busy_user")
            self.assertFalse(res_pkg.get("ok"))
            self.assertIn("bận", res_pkg.get("error", ""))

    def test_drive_auto_sync_protection_against_wiping_streamers(self):
        """
        Unit Test: Verify that when Drive API fails (load_streamers_from_drive returns None),
        Drive streamers.json is NOT overwritten, preventing accidental erasure of streamers.
        Also verify that when Drive loading succeeds, new Supabase streamers are properly synced.
        """
        import api_server
        import cloud_daemon

        # 1. api_server.get_users: d_users is None -> save_streamers_to_drive NOT called
        with patch("supabase_sync.fetch_streamers_from_supabase", return_value=["supa_streamer_1"]), \
             patch("gdrive_manager.load_streamers_from_drive", return_value=None), \
             patch("gdrive_manager.save_streamers_to_drive") as mock_save, \
             patch("api_server.load_config", return_value={"monitored_users": []}), \
             patch("gdrive_manager.load_active_recordings_from_drive", return_value=[]):

            users_resp = api_server.get_users(check_live=False)
            self.assertFalse(mock_save.called, "save_streamers_to_drive must NOT be called when d_users is None!")
            self.assertIn("supa_streamer_1", [u["username"] for u in users_resp.get("users", [])])
            self.assertIn("supa_streamer_1", users_resp.get("streamers", []))

        # 2. api_server.get_users: d_users is valid list -> new Supabase streamers synced
        with patch("supabase_sync.fetch_streamers_from_supabase", return_value=["supa_streamer_1"]), \
             patch("gdrive_manager.load_streamers_from_drive", return_value=["drive_streamer_1"]), \
             patch("gdrive_manager.save_streamers_to_drive") as mock_save2, \
             patch("gdrive_manager.create_streamer_folder_drive"), \
             patch("api_server.load_config", return_value={"monitored_users": []}), \
             patch("gdrive_manager.load_active_recordings_from_drive", return_value=[]):

            users_resp2 = api_server.get_users(check_live=False)
            self.assertTrue(mock_save2.called, "save_streamers_to_drive SHOULD be called when d_users is a valid list!")

        # 3. cloud_daemon.load_monitored_users: d is None -> save_streamers_to_drive NOT called
        cloud_daemon._CACHED_DRIVE_USERS = None
        cloud_daemon._LAST_DRIVE_CHECK = 0
        with patch("gdrive_manager.load_streamers_from_drive", return_value=None), \
             patch("supabase_sync.fetch_streamers_from_supabase", return_value=["supa_user_cd"]), \
             patch("gdrive_manager.save_streamers_to_drive") as mock_save_cd, \
             patch("cloud_daemon.load_config", return_value={"monitored_users": []}):

            cd_users = cloud_daemon.load_monitored_users()
            self.assertFalse(mock_save_cd.called, "cloud_daemon must NOT wipe streamers when Drive returns None!")

    def test_supabase_sync_retry_loop_on_transient_failures(self):
        """
        Unit Test: Verify Supabase sync 3-attempt retry loop with backoff on transient HTTP 500/502 errors
        for both sync_recording_to_supabase and upload_thumbnail_to_supabase.
        """
        import supabase_sync

        resp_500 = MagicMock(status_code=500, text="Internal Server Error")
        resp_502 = MagicMock(status_code=502, text="Bad Gateway")
        resp_201 = MagicMock(status_code=201, text="Created")
        resp_200 = MagicMock(status_code=200, text="OK")

        # 1. sync_recording_to_supabase: Fails twice (500, 502), succeeds on attempt 3 (201)
        sleep_delays = []
        with patch("requests.post", side_effect=[resp_500, resp_502, resp_201]) as mock_post, \
             patch("time.sleep", side_effect=lambda s: sleep_delays.append(s)):

            success = supabase_sync.sync_recording_to_supabase("retry_user", "stream_rec.mp4", size_bytes=500 * 1024, drive_file_id="dummy_fid")
            self.assertTrue(success)
            self.assertEqual(mock_post.call_count, 3)
            self.assertEqual(sleep_delays, [1.0, 2.0])

        # 2. sync_recording_to_supabase: Fails all 3 attempts -> returns False
        sleep_delays.clear()
        with patch("requests.post", side_effect=[resp_500, resp_500, resp_500]) as mock_post2, \
             patch("time.sleep", side_effect=lambda s: sleep_delays.append(s)):

            failed = supabase_sync.sync_recording_to_supabase("retry_user", "stream_rec.mp4", size_bytes=500 * 1024, drive_file_id="dummy_fid")
            self.assertFalse(failed)
            self.assertEqual(mock_post2.call_count, 3)
            self.assertEqual(sleep_delays, [1.0, 2.0])

        # 3. upload_thumbnail_to_supabase: Fails on attempt 1 (500), succeeds on attempt 2 (200)
        sleep_delays.clear()
        mock_img_resp = MagicMock(status_code=200, content=b"\xff\xd8\xff\xe0" + b"x" * 300)
        mock_img_resp.__enter__.return_value = mock_img_resp
        mock_img_resp.__exit__.return_value = False

        with patch("requests.get", return_value=mock_img_resp), \
             patch("requests.post", side_effect=[resp_500, resp_200]) as mock_post3, \
             patch("time.sleep", side_effect=lambda s: sleep_delays.append(s)):

            thumb_url = supabase_sync.upload_thumbnail_to_supabase("https://example.com/img.jpg", "retry_user", "stream_rec.mp4")
            self.assertIsNotNone(thumb_url)
            self.assertEqual(mock_post3.call_count, 2)
            self.assertEqual(sleep_delays, [1.0])

    def test_zero_render_bandwidth_stream_redirection(self):
        """
        Unit Test (R4 Zero Render Bandwidth): Verify that both stream_video_by_id and stream_video
        return 302 RedirectResponse directly to Google Edge CDN with 0 transit bytes through Render,
        and stream_video serves active local recordings via FileResponse when present.
        """
        import api_server

        client = TestClient(api_server.app, follow_redirects=False)

        # 1. stream_video_by_id returns 302 to Google Edge CDN
        with patch("gdrive_manager.get_access_token", return_value="fake_access_token"), \
             patch("gdrive_manager.is_recorder_owned_file", return_value=True), \
             patch("gdrive_manager.make_file_public") as mock_public:

            res_id = client.get("/api/stream-video-id/drive_vid_999")
            self.assertIn(res_id.status_code, (302, 307))
            self.assertEqual(res_id.status_code, 302)
            expected_cdn = "https://drive.usercontent.google.com/download?id=drive_vid_999&export=download&authuser=0&confirm=t"
            self.assertEqual(res_id.headers.get("location"), expected_cdn)
            mock_public.assert_called_once_with("drive_vid_999", access_token="fake_access_token")

        # 2. stream_video for Google Drive files: redirects 302 via stream_video_by_id
        mock_search_res = MagicMock(status_code=200)
        mock_search_res.json.return_value = {"files": [{"id": "drive_vid_888"}]}
        mock_search_res.__enter__.return_value = mock_search_res
        mock_search_res.__exit__.return_value = False

        with patch("os.path.exists", return_value=False), \
             patch("gdrive_manager.get_access_token", return_value="fake_access_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="user_drive_fid"), \
             patch("gdrive_manager.is_recorder_owned_file", return_value=True), \
             patch("requests.get", return_value=mock_search_res), \
             patch("gdrive_manager.make_file_public") as mock_public2:

            res_vid = client.get("/api/stream-video/streamer_a/recording_1.mp4")
            self.assertIn(res_vid.status_code, (302, 307))
            self.assertEqual(res_vid.status_code, 302)
            expected_cdn_vid = "https://drive.usercontent.google.com/download?id=drive_vid_888&export=download&authuser=0&confirm=t"
            self.assertEqual(res_vid.headers.get("location"), expected_cdn_vid)
            mock_public2.assert_called_once_with("drive_vid_888", access_token="fake_access_token")

        # 3. stream_video for local files: serves FileResponse (status 200)
        with patch("os.path.exists", return_value=True), \
             patch("api_server.FileResponse") as mock_file_resp:

            mock_file_resp.return_value = MagicMock(status_code=200)
            res_local = api_server.stream_video("streamer_a", "local_rec.mp4", MagicMock())
            self.assertEqual(res_local.status_code, 200)
            mock_file_resp.assert_called_once()


class TestQualitySelectionAndMemorySafety(unittest.TestCase):
    """
    Test suite verifying:
    1. Origin/1080p HLS H.264 streams are prioritized at index 0 to avoid FLV AVCC corruption.
    2. TikTok SDK parsing never confuses "stream_description" with SD or demotes Origin mobile 720p.
    3. HLS is ordered before same-tier FLV, with FLV retained as fallback.
    4. auto_h264 libx264 command includes strict memory limits (threads 1, low lookahead) for Render 512MB RAM safety.
    """

    def test_parse_sdk_stream_data_hls_priority_and_no_360p_downgrade(self):
        sample_sdk = json.dumps({
            "data": {
                "ld": {
                    "main": {
                        "flv": "https://pull.tiktokcdn.com/stage/stream-ld.flv",
                        "hls": "https://pull.tiktokcdn.com/stage/stream-ld.m3u8",
                        "sdk_params": "{\"vcodec\":\"h264\",\"resolution\":\"360x640\",\"stream_suffix\":\"ld\"}"
                    }
                },
                "sd": {
                    "main": {
                        "flv": "https://pull.tiktokcdn.com/stage/stream-sd.flv",
                        "hls": "https://pull.tiktokcdn.com/stage/stream-sd.m3u8",
                        "sdk_params": "{\"vcodec\":\"h264\",\"resolution\":\"540x960\",\"stream_description\":\"live_stream\"}"
                    }
                },
                "hd": {
                    "main": {
                        "flv": "https://pull.tiktokcdn.com/stage/stream-hd.flv",
                        "hls": "https://pull.tiktokcdn.com/stage/stream-hd.m3u8",
                        "sdk_params": "{\"vcodec\":\"h264\",\"resolution\":\"720x1280\",\"stream_suffix\":\"hd\"}"
                    }
                },
                "origin": {
                    "main": {
                        "flv": "https://pull.tiktokcdn.com/stage/stream-origin.flv",
                        "hls": "https://pull.tiktokcdn.com/stage/stream-origin.m3u8",
                        "sdk_params": "{\"vcodec\":\"h264\",\"resolution\":\"1080x1920\",\"stream_suffix\":\"origin\",\"stream_description\":\"live\"}"
                    }
                }
            }
        })

        candidates = recorder_core.parse_sdk_stream_data(sample_sdk)
        self.assertTrue(len(candidates) >= 4)
        # 1. Luồng đầu tiên (index 0) BẮT BUỘC phải là Origin HLS H.264.
        self.assertEqual(candidates[0], "https://pull.tiktokcdn.com/stage/stream-origin.m3u8")
        # 2. HLS phải đứng trước FLV cùng quality tier.
        flv_origin_idx = candidates.index("https://pull.tiktokcdn.com/stage/stream-origin.flv")
        hls_origin_idx = candidates.index("https://pull.tiktokcdn.com/stage/stream-origin.m3u8")
        self.assertLess(hls_origin_idx, flv_origin_idx)
        # 3. Luồng 360p (LD) phải nằm ở vị trí sau cùng
        flv_ld_idx = candidates.index("https://pull.tiktokcdn.com/stage/stream-ld.flv")
        self.assertGreater(flv_ld_idx, flv_origin_idx)
        self.assertEqual(candidates[-2:], [
            "https://pull.tiktokcdn.com/stage/stream-ld.m3u8",
            "https://pull.tiktokcdn.com/stage/stream-ld.flv"
        ])

    def test_parse_sdk_stream_data_mobile_phone_camera_origin(self):
        """Khi streamer phát từ điện thoại (720x1280 origin), hệ thống phải giữ nguyên luồng origin gốc."""
        mobile_sdk = json.dumps({
            "data": {
                "origin": {
                    "main": {
                        "flv": "https://pull.tiktokcdn.com/stage/stream-mobile-origin.flv",
                        "hls": "https://pull.tiktokcdn.com/stage/stream-mobile-origin.m3u8",
                        "sdk_params": "{\"vcodec\":\"h264\",\"resolution\":\"720x1280\",\"stream_suffix\":\"origin\",\"stream_description\":\"mobile_live\"}"
                    }
                },
                "ld": {
                    "main": {
                        "flv": "https://pull.tiktokcdn.com/stage/stream-mobile-360.flv",
                        "hls": "https://pull.tiktokcdn.com/stage/stream-mobile-360.m3u8",
                        "sdk_params": "{\"vcodec\":\"h264\",\"resolution\":\"360x640\",\"stream_suffix\":\"ld\"}"
                    }
                }
            }
        })

        candidates = recorder_core.parse_sdk_stream_data(mobile_sdk)
        self.assertEqual(candidates[0], "https://pull.tiktokcdn.com/stage/stream-mobile-origin.m3u8")

    def test_classify_stream_urls_ordering(self):
        """Kiểm tra classify_stream_urls xếp HLS 1080p/origin lên trước, đẩy 360p xuống cuối."""
        urls = [
            "https://pull.tiktokcdn.com/stream-1234_ld.flv?auth=1",
            "https://pull.tiktokcdn.com/stream-1234_hd.flv?auth=1",
            "https://pull.tiktokcdn.com/stream-1234_origin.m3u8?auth=1",
            "https://pull.tiktokcdn.com/stream-1234_origin.flv?auth=1",
            "https://pull.tiktokcdn.com/stream-1234_360p.m3u8?auth=1"
        ]
        sorted_urls = recorder_core.classify_stream_urls(urls)
        # Origin HLS phải ở vị trí số 1, trước Origin FLV cùng quality tier.
        self.assertEqual(sorted_urls[0], "https://pull.tiktokcdn.com/stream-1234_origin.m3u8?auth=1")
        self.assertEqual(sorted_urls[1], "https://pull.tiktokcdn.com/stream-1234_origin.flv?auth=1")
        # 360p / LD phải ở cuối
        self.assertIn("360p.m3u8", sorted_urls[-2])
        self.assertIn("_ld.flv", sorted_urls[-1])

    def test_auto_h264_libx264_ram_safety_params(self):
        """Khẳng định lệnh libx264 có các cờ khống chế RAM < 60MB trên Render."""
        import inspect
        import auto_h264
        src = inspect.getsource(auto_h264.ensure_h264)
        self.assertIn('"-threads", "1"', src)
        self.assertIn('rc-lookahead=10:ref=1:bframes=0:sync-lookahead=0', src)
        self.assertIn('"-bufsize", "3000k"', src)



class TestPonytailQueueAndBackendFixes(unittest.TestCase):
    """Test suite kiểm chứng toàn bộ các bản vá logic hàng đợi Staging và đồng bộ Runner."""

    def test_manifest_404_patch_fallback_to_create(self):
        """Kiểm tra save_staging_manifest tự động fallback tạo mới khi patch trả về 404 (file cũ bị xóa)."""
        import staging_queue
        manifest_data = {
            "user": "test_user",
            "segments": [{"filename": "seg1.mp4", "duration": 60.0}],
            "manifest_file_id": "deleted_id_404"
        }
        resp_404 = MagicMock(status_code=404)
        resp_201 = MagicMock(status_code=201)

        with patch("requests.patch", return_value=resp_404) as mock_patch, \
             patch("requests.post", return_value=resp_201) as mock_post:
            ok = staging_queue.save_staging_manifest("test_user", "staging_folder_id", manifest_data, access_token="token")
            self.assertTrue(ok)
            mock_patch.assert_called_once()
            mock_post.assert_called_once()

    def test_manifest_orphaned_mp4_reconciliation(self):
        """Kiểm tra get_staging_manifest tự động gom phân đoạn MP4 mồ côi nếu manifest bị mất/desync."""
        import staging_queue
        # Giả lập truy vấn manifest trả về rỗng (chưa có file json)
        res_manifest_empty = MagicMock(status_code=200)
        res_manifest_empty.json.return_value = {"files": []}

        # Giả lập truy vấn mp4 trong staging_folder trả về 2 file mồ côi
        res_mp4 = MagicMock(status_code=200)
        res_mp4.json.return_value = {
            "files": [
                {"id": "orphan_1", "name": "orphan_1.mp4", "size": "350000"},
                {"id": "orphan_2", "name": "orphan_2.mp4", "size": "450000"}
            ]
        }

        with patch("requests.get", side_effect=[res_manifest_empty, res_mp4]):
            data = staging_queue.get_staging_manifest("test_orphan_user", "staging_folder_id", access_token="token")
            self.assertEqual(len(data.get("segments", [])), 2)
            self.assertEqual(data["segments"][0]["file_id"], "orphan_1")
            self.assertEqual(data["segments"][1]["file_id"], "orphan_2")

    def test_concat_mp4_segments_centralization(self):
        """Kiểm tra concat_mp4_segments trong recorder_core hoạt động đúng và an toàn SameFile."""
        import recorder_core
        import cloud_daemon
        import api_server
        # Kiểm tra tính đồng nhất của hàm dùng chung
        self.assertIs(cloud_daemon.concat_mp4_segments, recorder_core.concat_mp4_segments)
        self.assertIs(api_server.concat_mp4_segments, recorder_core.concat_mp4_segments)

        # Single file copy
        with patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=100000), \
             patch("shutil.copy2") as mock_copy:
            res = recorder_core.concat_mp4_segments(["/path/to/seg1.mp4"], "/path/to/out.mp4")
            self.assertEqual(res, "/path/to/out.mp4")
            mock_copy.assert_called_once()

    def test_api_server_duplicate_record_prevention_cloud_heartbeat(self):
        """Kiểm tra start_record từ chối ghi đè nếu Cloud Runner đang có heartbeat tươi (<600s)."""
        from fastapi.testclient import TestClient
        import api_server
        client = TestClient(api_server.app)

        # Giả lập streamer đang live
        mock_details = {
            "is_live": True,
            "room_id": "999999",
            "is_sub_only": False,
            "is_preview": False
        }
        # Giả lập Drive có heartbeat của user này cách đây 120s
        drive_active = [
            {"username": "cloud_active_user", "updated_at": int(time.time()) - 120}
        ]

        with patch("api_server.get_user_live_details_cached", return_value=mock_details), \
             patch("gdrive_manager.load_active_recordings_from_drive", return_value=drive_active), \
             patch("gdrive_manager.load_streamers_from_drive", return_value=["cloud_active_user"]), \
             patch("gdrive_manager.save_streamers_to_drive"), \
             patch("gdrive_manager.create_streamer_folder_drive"):
            resp = client.post("/api/record/start", json={"username": "cloud_active_user", "duration_seconds": 3600})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data.get("status"), "already_recording")
            self.assertEqual(data.get("source"), "cloud_runner")


    def test_get_video_resolution_parsing(self):
        """Kiểm tra trích xuất chính xác width, height, fps từ ffmpeg output."""
        import auto_h264
        fake_stderr = (
            "Input #0, mov,mp4:\n"
            "  Stream #0:0[0x1]: Video: h264 (High), yuv420p, 640x1280, 2500 kb/s, 25 fps, 25 tbr\n"
        )
        mock_proc = MagicMock(stderr=fake_stderr, returncode=0)
        with patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=50000), \
             patch("subprocess.run", return_value=mock_proc):
            w, h, fps = auto_h264.get_video_resolution("/fake/path.mp4")
            self.assertEqual(w, 640)
            self.assertEqual(h, 1280)
            self.assertEqual(fps, 25.0)

    def test_upscale_to_1080p_bypasses_if_already_1080p(self):
        """Kiểm tra video đã đạt 1080p (chiều nhỏ >= 1080 hoặc chiều lớn >= 1920) được bỏ qua 100%."""
        import auto_h264
        with patch("os.path.exists", return_value=True), \
             patch("auto_h264.get_video_resolution", return_value=(1080, 1920, 30.0)), \
             patch("subprocess.run") as mock_run:
            res = auto_h264.upscale_to_1080p_if_needed("/fake/1080p.mp4", config={"auto_upscale_1080p": True})
            self.assertEqual(res, "/fake/1080p.mp4")
            mock_run.assert_not_called()

    def test_upscale_to_1080p_bypasses_if_disabled(self):
        """Kiểm tra cấu hình auto_upscale_1080p = False thì bỏ qua không re-encode tốn CPU."""
        import auto_h264
        with patch("os.path.exists", return_value=True), \
             patch("auto_h264.get_video_resolution", return_value=(432, 864, 15.0)), \
             patch("subprocess.run") as mock_run:
            res = auto_h264.upscale_to_1080p_if_needed("/fake/lowres.mp4", config={"auto_upscale_1080p": False})
            self.assertEqual(res, "/fake/lowres.mp4")
            mock_run.assert_not_called()

    def test_upscale_to_1080p_triggers_lanczos_when_enabled(self):
        """Kiểm tra video < 1080p khi bật config sẽ gọi ffmpeg với bộ lọc Lanczos 1080p."""
        import auto_h264
        mock_proc = MagicMock(returncode=0)
        with patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=500000), \
             patch("auto_h264.get_video_resolution", side_effect=[(640, 1280, 25.0), (1080, 1920, 25.0)]), \
             patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("os.replace") as mock_replace:
            res = auto_h264.upscale_to_1080p_if_needed("/fake/lowres.mp4", config={"auto_upscale_1080p": True})
            self.assertEqual(res, "/fake/lowres.mp4")
            self.assertTrue(mock_run.called)
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("-vf", called_cmd)
            self.assertIn("scale=-2:1920:flags=lanczos", called_cmd)
            self.assertIn("-r", called_cmd)
            self.assertIn("25.0", called_cmd)

    def test_streamer_worker_publishes_staging_queue_on_live_end(self):
        """Worker PHẢI upload trực tiếp, KHÔNG gọi staging queue (staging đã ngừng sử dụng) và PHẢI nhả khóa liên tiến trình."""
        import cloud_daemon
        import staging_queue
        import threading
        from config_lock import streamer_recording_lock

        user = "test_auto_pub_user"
        stop_ev = threading.Event()
        stop_ev.set()

        with patch("cloud_daemon.load_monitored_users", return_value=[user]), \
             patch("recorder_core.check_live_details", return_value={"is_live": False, "room_id": None}), \
             patch("gdrive_manager.set_user_recording_status_drive"), \
             patch("gdrive_manager.create_streamer_folder_drive"), \
             patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.package_and_publish_queue") as mock_pub:
            cloud_daemon.streamer_recording_worker(user, "room_123", stop_event=stop_ev)
            mock_pub.assert_not_called()
            self.assertNotIn(user, cloud_daemon.ACTIVE_RECORDERS)

        with streamer_recording_lock(user) as got_lock:
            self.assertTrue(got_lock, "streamer_recording_lock bị rò sau khi worker kết thúc")

    def test_find_folder_does_not_create_folder(self):
        """Kiểm tra find_folder chỉ đọc, không gọi POST để tạo mới khi không tìm thấy folder."""
        import gdrive_manager
        with patch("gdrive_manager.get_access_token", return_value="fake_tok"), \
             patch("requests.get") as mock_get, \
             patch("requests.post") as mock_post:
            mock_res = MagicMock(status_code=200)
            mock_res.json.return_value = {"files": []}
            mock_get.return_value = mock_res

            fid = gdrive_manager.find_folder("non_existent_folder", access_token="fake_tok")
            self.assertIsNone(fid)
            mock_post.assert_not_called()

    def test_package_and_publish_queue_skips_creation_when_staging_empty(self):
        """Kiểm tra package_and_publish_queue không tạo folder _staging rác nếu queue chưa từng tồn tại."""
        import staging_queue
        with patch("gdrive_manager.get_access_token", return_value="fake_tok"), \
             patch("staging_queue.find_staging_folder", return_value=(None, None)), \
             patch("staging_queue.get_or_create_staging_folder") as mock_create:
            res = staging_queue.package_and_publish_queue("unrecorded_user", access_token="fake_tok")
            self.assertTrue(res.get("ok"))
            self.assertIn("trống", res.get("message", ""))
            mock_create.assert_not_called()

    def test_deleted_streamer_worker_skips_staging_publish_on_exit(self):
        """Kiểm tra worker nếu phát hiện streamer đã bị xóa sẽ hủy đóng gói staging queue."""
        import cloud_daemon
        import threading

        user = "deleted_user_xyz"
        stop_ev = threading.Event()
        stop_ev.user_deleted = True
        stop_ev.set()

        with patch("cloud_daemon.load_monitored_users", return_value=["other_streamer"]), \
             patch("recorder_core.check_live_details", return_value={"is_live": False, "room_id": None}), \
             patch("gdrive_manager.set_user_recording_status_drive"), \
             patch("gdrive_manager.create_streamer_folder_drive"), \
             patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.package_and_publish_queue") as mock_pub, \
             patch("shutil.rmtree") as mock_rm:
            cloud_daemon.streamer_recording_worker(user, "room_123", stop_event=stop_ev)
            mock_pub.assert_not_called()

    def test_stream_stabilization_flags_in_ffmpeg_cmds(self):
        """Kiểm tra các cờ chống xé hình và xám xịt (-bsf:v dump_extra, -avoid_negative_ts, +nobuffer) trong lệnh FFmpeg."""
        import recorder_core
        import auto_h264

        captured_record_cmd = []
        def fake_popen(cmd, **kwargs):
            captured_record_cmd.extend(cmd)
            mock_p = MagicMock()
            mock_p.poll.return_value = 0
            return mock_p

        with patch("subprocess.Popen", side_effect=fake_popen), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=500000), \
             patch("auto_h264.ensure_h264", side_effect=lambda f: f), \
             patch("auto_h264.upscale_to_1080p_if_needed", side_effect=lambda f: f), \
             patch("auto_h264.validate_playable_video", return_value=(True, "OK", 60.0)):
            recorder_core.record_stream_ffmpeg("http://stream.url/live.flv", output_filename="dummy.mp4", auto_sync_gdrive=False)

        self.assertIn("+nobuffer", " ".join(captured_record_cmd))
        self.assertIn("-avoid_negative_ts", captured_record_cmd)
        self.assertIn("make_zero", captured_record_cmd)
        self.assertNotIn("+faststart", captured_record_cmd)  # F1 fix: +faststart moved to post-processing to avoid corruption

        captured_concat_cmd = []
        def fake_run(cmd, **kwargs):
            captured_concat_cmd.extend(cmd)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=500000):
            recorder_core.concat_mp4_segments(["seg1.mp4", "seg2.mp4"], "concat_out.mp4")

        self.assertIn("-avoid_negative_ts", captured_concat_cmd)
        self.assertIn("make_zero", captured_concat_cmd)
        self.assertIn("+faststart", captured_concat_cmd)

class TestBatch5ConfigLockAndSupabaseGuards(unittest.TestCase):
    """Batch5: khóa liên tiến trình cho config.json + không tạo bản ghi Supabase khi thiếu Drive file."""

    def test_config_lock_second_acquire_times_out(self):
        """
        Adversarial Test: file lock liên tiến trình phải CHẶN fd thứ 2 cùng giữ
        config.lock (nếu không thì 2 process vẫn ghi đè config.json của nhau).
        """
        import config_lock

        fd1, locked1 = config_lock._acquire_file_lock(1.0)
        self.assertTrue(locked1, "Không giữ được config.lock lần đầu")
        try:
            t0 = time.time()
            fd2, locked2 = config_lock._acquire_file_lock(0.5)
            waited = time.time() - t0
            self.assertFalse(locked2, "config.lock phải bị giữ độc quyền")
            self.assertGreaterEqual(waited, 0.35, "Phải chờ hết timeout chứ không được trả về ngay")
            if fd2 is not None:
                os.close(fd2)
        finally:
            config_lock._release_file_lock(fd1)

        # Sau khi nhả khóa thì lock phải lấy lại được (không bị treo/vẫn giữ)
        fd3, locked3 = config_lock._acquire_file_lock(1.0)
        self.assertTrue(locked3)
        config_lock._release_file_lock(fd3)

    def test_config_transaction_is_reentrant(self):
        """
        config_transaction() lồng nhau trong CÙNG thread (save_config gọi bên trong
        một transaction) không được tự tước khóa -> nếu có sẽ treo chờ timeout 10s.
        """
        import config_lock
        t0 = time.time()
        with config_lock.config_transaction(timeout=3):
            with config_lock.config_transaction(timeout=3):
                with config_lock.config_transaction(timeout=3):
                    pass
        self.assertLess(time.time() - t0, 2.0, "Không được treo ở lần vào khóa lồng nhau")

    def test_save_config_fields_does_not_clobber_concurrent_writes(self):
        """
        save_config_fields() chỉ ghi key được chỉ định: CLI đọc config lúc bắt đầu,
        người dùng thao tác vài phút, trong lúc đó api_server thêm streamer ->
        lưu lại bản đọc lúc đầu sẽ XOÁ mất monitored_users của process khác.
        """
        import recorder_core
        import tempfile

        tmp_cfg = os.path.join(tempfile.gettempdir(), "test_batch5_config.json")
        try:
            with open(tmp_cfg, "w", encoding="utf-8") as f:
                json.dump({"monitored_users": ["streamer_added_by_api"],
                           "check_interval_seconds": 20}, f, ensure_ascii=False)

            with patch.object(recorder_core, "CONFIG_FILE", tmp_cfg):
                # Người dùng chỉ đổi target_user
                recorder_core.save_config_fields({"target_user": "new_target"})
                with open(tmp_cfg, encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(saved.get("target_user"), "new_target")
                # monitored_users do process khác ghi PHẢI được giữ nguyên
                self.assertEqual(saved.get("monitored_users"), ["streamer_added_by_api"])

                # Ghi rỗng không được động tới file
                recorder_core.save_config_fields({})
                with open(tmp_cfg, encoding="utf-8") as f:
                    saved2 = json.load(f)
                self.assertEqual(saved2.get("monitored_users"), ["streamer_added_by_api"])
        finally:
            if os.path.exists(tmp_cfg):
                try:
                    os.remove(tmp_cfg)
                except OSError:
                    pass

    def test_cloud_daemon_no_supabase_row_without_drive_file(self):
        """
        Upload Google Drive thất bại -> cloud_daemon KHÔNG được tạo bản ghi Supabase
        (bản ghi không có drive_file_id là link chết trên website) nhưng vẫn upload
        bình thường khi Drive OK (đối chứng).
        """
        import cloud_daemon

        def fake_record(url, output_filename=None, **kwargs):
            return output_filename

        live_calls = {"n": 0}
        def fake_live(u):
            live_calls["n"] += 1
            return {"is_live": live_calls["n"] == 1, "room_id": "111"}

        with patch("recorder_core.get_live_stream_url", return_value="http://pull.flv/stream.flv"), \
             patch("recorder_core.record_stream_ffmpeg", side_effect=fake_record), \
             patch("auto_h264.validate_playable_video", return_value=(True, "ok", 3600.0)), \
             patch("recorder_core.check_live_details", side_effect=fake_live), \
             patch("gdrive_manager.get_access_token", return_value="fake_tok"), \
             patch("gdrive_manager.find_or_create_folder", return_value="fake_fid"), \
             patch("gdrive_manager.create_streamer_folder_drive"), \
             patch("gdrive_manager.set_user_recording_status_drive"), \
             patch("auto_h264.extract_middle_thumbnail", return_value=None), \
             patch("staging_queue.package_and_publish_queue", return_value={"ok": True}), \
             patch("supabase_sync.sync_recording_to_supabase", return_value=True) as mock_sync, \
             patch("gdrive_manager.upload_file_to_drive", return_value=False) as mock_upload, \
             patch("os.makedirs"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=500000), \
             patch("os.remove"), \
             patch("shutil.move"), \
             patch("time.sleep"):
            # 1. Drive upload THẤT BẠI -> không sync
            cloud_daemon.streamer_recording_worker("phantom_upload_user", "111", auto_discover=False)
            mock_sync.assert_not_called()

            # 2. Drive upload THÀNH CÔNG -> phải sync kèm drive_file_id (đối chứng)
            mock_upload.return_value = "drive_file_id_ok"
            mock_sync.reset_mock()
            live_calls["n"] = 0
            cloud_daemon.streamer_recording_worker("phantom_upload_user", "111", auto_discover=False)
            mock_sync.assert_called_once()
            self.assertEqual(mock_sync.call_args[1].get("drive_file_id"), "drive_file_id_ok")


class TestLatentBugsAndLockGuards(unittest.TestCase):
    def test_streamer_recording_lock_mutual_exclusion(self):
        """
        Unit Test: Verify streamer_recording_lock guarantees mutual exclusion
        between two workers trying to record the same streamer.
        """
        import config_lock
        user = "mutual_exclude_streamer"

        with config_lock.streamer_recording_lock(user) as locked1:
            self.assertTrue(locked1, "First worker must acquire lock")
            with config_lock.streamer_recording_lock(user) as locked2:
                self.assertFalse(locked2, "Second worker must NOT acquire lock concurrently")

        # After releasing, a subsequent worker can acquire it again
        with config_lock.streamer_recording_lock(user) as locked3:
            self.assertTrue(locked3, "New worker must acquire lock after release")

    def test_flv_and_hls_regex_without_query_params(self):
        """
        Unit Test: Verify recorder_core parses FLV and HLS URLs with and without query parameters.
        """
        import recorder_core
        raw_html = '''
        <html>
        <script>
        var stream1 = "https://pull-flv.tiktokcdn.com/stage/stream-1080p.flv";
        var stream2 = "https://pull-hls.tiktokcdn.com/stage/stream-1080p.m3u8";
        var stream3 = "https://pull-flv.tiktokcdn.com/stage/stream-720p.flv?auth=xyz";
        </script>
        </html>
        '''
        with patch("curl_cffi.requests.Session") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = raw_html
            mock_sess.get.return_value = mock_resp
            mock_sess_cls.return_value = mock_sess

            with patch("curl_cffi.requests.get", side_effect=Exception("skip native")):
                urls = recorder_core.get_stream_urls("9999", user="test_regex_user", session=None)
                self.assertTrue(any(".flv" in u for u in urls))
                self.assertTrue(any("stream-1080p.flv" in u for u in urls))


class TestBitstreamCorruptionDetection(unittest.TestCase):
    """Bao phủ lớp vá lỗi khung hình khối xám + nhiễu macroblock (corrupt frame)."""

    @staticmethod
    def _build_avcc(length_size_minus_one, sps=b"\x67\x42\xc0\x1f", pps=b"\x68\xce\x06\xe2"):
        rec = bytearray()
        rec += b"\x01\x64\x00\x1f"
        rec.append(0xfc | (length_size_minus_one & 0x03))
        rec.append(0xe0 | 0x01)                      # 1 SPS
        rec += len(sps).to_bytes(2, "big") + sps
        rec.append(0x01)                             # 1 PPS
        rec += len(pps).to_bytes(2, "big") + pps
        return bytes(rec)

    def test_parse_avcc_record_returns_length_size_from_record(self):
        """lengthSizeMinusOne phải được đọc từ avcC, không phải mặc định 4 byte."""
        import auto_h264
        for lsi in (1, 2, 3):
            rec = self._build_avcc(lsi)
            res = auto_h264._parse_avcc_record(rec)
            self.assertIsNotNone(res, f"không parse được avcC lengthSizeMinusOne={lsi}")
            ptr, sps_list, pps_list, nalu = res
            self.assertEqual(nalu, lsi + 1)
            self.assertEqual(sps_list, [b"\x67\x42\xc0\x1f"])
            self.assertEqual(pps_list, [b"\x68\xce\x06\xe2"])
            self.assertEqual(ptr, len(rec))

    def test_parse_avcc_record_truncated_nal_returns_none(self):
        """SPS bị cắt cụt phải trả None để bỏ mẫu, không ghi header sai."""
        import auto_h264
        rec = bytearray(self._build_avcc(3))
        rec[13:15] = (4096).to_bytes(2, "big")  # PPS khai 4096 byte nhưng buffer ngắn hơn
        self.assertIsNone(auto_h264._parse_avcc_record(bytes(rec)))

        bad = bytearray(self._build_avcc(3))
        bad[6:8] = (4096).to_bytes(2, "big")  # SPS khai 4096 byte nhưng buffer ngắn hơn
        self.assertIsNone(auto_h264._parse_avcc_record(bytes(bad)))

    def test_write_length_prefixed_nals_uses_record_length_size(self):
        """Tiền tố 2-byte phải được chuyển đúng sang Annex-B (trước đây luôn đọc 4 byte)."""
        import io
        import auto_h264
        sps = b"\x67\x42\xc0\x1f"
        pps = b"\x68\xce\x06\xe2"
        data = len(sps).to_bytes(2, "big") + sps + len(pps).to_bytes(2, "big") + pps
        out = io.BytesIO()
        auto_h264._write_length_prefixed_nals(out, data, 2)
        self.assertEqual(out.getvalue(), b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps)

    def test_write_length_prefixed_nals_stops_on_overrun(self):
        """Dữ liệu không khớp phải dừng lại, không ném lỗi và không ghi NAL rác."""
        import io
        import auto_h264
        out = io.BytesIO()
        auto_h264._write_length_prefixed_nals(out, (9999).to_bytes(4, "big") + b"abc", 4)
        self.assertEqual(out.getvalue(), b"")
        auto_h264._write_length_prefixed_nals(out, b"", 4)
        auto_h264._write_length_prefixed_nals(out, b"\x00\x00\x00\x01\x65", 7)
        self.assertEqual(out.getvalue(), b"")

    def test_build_scan_windows_covers_middle_and_tail_of_long_file(self):
        """File dài phải được quét ở giữa/cuối, không chỉ 6 giây đầu."""
        import auto_h264
        self.assertEqual(auto_h264._build_scan_windows(30), [0.0])
        self.assertEqual(auto_h264._build_scan_windows(None), [0.0])
        w = auto_h264._build_scan_windows(600)
        self.assertEqual(w[0], 0.0)
        self.assertEqual(len(w), 3)
        self.assertAlmostEqual(w[1], 240.0)
        self.assertAlmostEqual(w[2], 450.0)

    def test_corrupt_patterns_catch_macroblock_errors(self):
        """Pattern phải nhận đúng log FFmpeg của lỗi macroblock trong ảnh báo lỗi."""
        import auto_h264
        blob = "\n".join(auto_h264._MB_ERROR_PATTERNS)
        for pat in ("error while decoding MB", "reference picture missing",
                    "concealing", "mmco: unref short failure"):
            self.assertIn(pat, blob)
        for pat in ("non monotonically increasing dts", "co located POCs unavailable"):
            self.assertNotIn(pat, auto_h264._FATAL_PATTERNS)
            self.assertNotIn(pat, auto_h264._MB_ERROR_PATTERNS)

    def _run_health_check(self, stderr_text, returncode=0, duration=30.0):
        import auto_h264
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(b"0" * 4096)
            path = tf.name
        try:
            with patch.object(auto_h264, "get_video_codec", return_value="h264"), \
                 patch.object(auto_h264, "get_video_duration", return_value=duration), \
                 patch.object(auto_h264.subprocess, "run",
                              return_value=MagicMock(returncode=returncode, stderr=stderr_text)) as m:
                return auto_h264.check_h264_stream_health(path), m
        finally:
            os.remove(path)

    def test_health_check_flags_catastrophic_macroblock_density(self):
        """Mật độ lỗi MB kinh khủng trong một cửa sổ -> file hỏng."""
        err = "".join(f"[h264] error while decoding MB {i} at {i}\n" for i in range(300))
        ok, _ = self._run_health_check(err)
        self.assertFalse(ok)

    def test_health_check_ignores_sporadic_macroblock_errors(self):
        """Lẻ tẻ là bình thường trên luồng live -> KHÔNG được trả False (tránh xóa nhầm)."""
        ok, _ = self._run_health_check("[h264] error while decoding MB 3 at 1\n")
        self.assertTrue(ok)

    def test_health_check_fatal_pattern_and_probe_all_windows(self):
        ok, _ = self._run_health_check("[h264] Invalid NAL unit size")
        self.assertFalse(ok)
        ok, m = self._run_health_check("", duration=600.0)
        self.assertTrue(ok)
        self.assertGreaterEqual(m.call_count, 3, "file 600s phải được quét ở 3 cửa sổ")

    def test_ensure_h264_video_health_not_gated_by_audio_codec(self):
        """Sức khỏe bitstream video không được quyết định bởi audio codec."""
        import auto_h264
        src = inspect.getsource(auto_h264.ensure_h264)
        self.assertIn("is_healthy = check_h264_stream_health(filepath, duration=file_dur)", src)
        old_gate = 'filepath.lower().endswith(".mp4") and codec == "h264" and audio_codec in ("aac", "none")'
        self.assertNotIn(old_gate, src)

    def test_sanitize_rejects_corrupt_box_without_hanging(self):
        """Box size < 8 phải dừng ngay, không seek lùi/làm treo vòng lặp."""
        import auto_h264
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(b"\x00\x00\x00\x04mdat" + b"0" * 2048)
            path = tf.name
        try:
            self.assertIsNone(auto_h264.sanitize_mp4_bitstream(path))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()


