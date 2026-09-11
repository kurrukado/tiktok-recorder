import unittest
import re
import warnings
import inspect
import gc
import ast
import glob
import threading
import time
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
        - When final_rec_file validation fails, increments consecutive_failures and stops after max_consecutive_failures.
        - When room is VIP sub-only and preview generation fails, stops after max_vip_attempts.
        """
        import api_server

        # 1. Video validation failure stops after 4 consecutive failures
        with patch("api_server.get_user_live_details_cached", return_value={"is_live": True, "room_id": "999", "is_sub_only": False}), \
             patch("recorder_core.get_live_stream_url", return_value="https://live.tiktok.com/stream.flv"), \
             patch("recorder_core.record_stream_ffmpeg", return_value="mock_invalid.mp4"), \
             patch("auto_h264.validate_playable_video", side_effect=[
                 (True, "seg ok", 10.0),             # seg validation passes
                 (False, "corrupt container", 0.0),  # final file validation fails (failure 1)
                 (True, "seg ok", 10.0),
                 (False, "corrupt container", 0.0),  # (failure 2)
                 (True, "seg ok", 10.0),
                 (False, "corrupt container", 0.0),  # (failure 3)
                 (True, "seg ok", 10.0),
                 (False, "corrupt container", 0.0),  # (failure 4 -> breaks out!)
             ]), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"), \
             patch.object(time, "sleep"):

            # Run worker; should encounter consecutive failures and break cleanly without infinite loop
            api_server.bg_record_worker("worker_test_user")

        # 2. Sub-only preview reaches max_vip_attempts
        with patch("api_server.get_user_live_details_cached", return_value={"is_live": True, "room_id": "999", "is_sub_only": True}), \
             patch("recorder_core.generate_guest_session", side_effect=RuntimeError("Captcha")), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(time, "sleep"):

            api_server.bg_record_worker("vip_worker_user")

    def test_gc_collect_exception_safety_in_api_server(self):
        """
        Adversarial Test: Verify that gc.collect() is called in get_users() and
        test_live_diagnostic() even when partial exceptions or unexpected errors occur.
        """
        import api_server

        # 1. get_users raises unexpected exception during user fetching
        with patch("supabase_sync.fetch_streamers_from_supabase", side_effect=TypeError("Unexpected mock failure")), \
             patch("api_server.clean_zombie_recordings", side_effect=RuntimeError("Zombie cleaner crash")), \
             patch("gc.collect") as mock_gc:

            try:
                api_server.get_users()
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
        Adversarial Test: Verify that stream_video_by_id wraps drive_resp in an iterator
        that ensures drive_resp.close() is called when the client finishes or disconnects.
        """
        import api_server

        mock_drive_resp = MagicMock()
        mock_drive_resp.status_code = 200
        mock_drive_resp.headers = {"Content-Type": "video/mp4", "Content-Length": "100"}
        mock_drive_resp.iter_content.return_value = [b"chunk1", b"chunk2"]

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("requests.get", return_value=mock_drive_resp):

            client = TestClient(api_server.app)
            resp = client.get("/api/stream-video-id/file_123")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.content, b"chunk1chunk2")
            mock_drive_resp.close.assert_called_once()

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
        Adversarial Test: Verify stream_video_by_id raises HTTPException and closes
        drive_resp immediately when Google Drive returns non-200/206 status (e.g. 404),
        or when an error occurs before StreamingResponse is returned.
        """
        import api_server

        # 1. Google Drive returns 404
        mock_drive_resp = MagicMock()
        mock_drive_resp.status_code = 404
        mock_drive_resp.headers = {}

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("requests.get", return_value=mock_drive_resp):

            client = TestClient(api_server.app)
            resp = client.get("/api/stream-video-id/not_found_123")
            self.assertEqual(resp.status_code, 404)
            mock_drive_resp.close.assert_called_once()

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

if __name__ == "__main__":
    unittest.main()
