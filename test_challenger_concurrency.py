# -*- coding: utf-8 -*-
"""
test_challenger_concurrency.py - Empirical Challenger Stress Harness
Author: Backend Concurrency Challenger (teamwork_preview_challenger)

Tests:
1. Zero Render Bandwidth: stream_video_by_id and stream_video redirect with 302/307,
   Location header to Google CDN / Preview, and 0 bytes transit video payload.
2. Staging Manifest Network Failure: add_to_staging_queue failure safety under
   various network exceptions (ConnectionError, Timeout, HTTP 500/503, JSONDecodeError)
   verifying save_staging_manifest is NEVER invoked.
3. Staging Lock Timeout & Concurrency: Contention timeout triggers cleanly without
   deadlock, per-user lock isolation, and exception lock-release safety.
"""

import os
import sys
import time
import json
import threading
import unittest
import requests
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

import api_server
import staging_queue
import gdrive_manager

class TestChallengerZeroRenderBandwidth(unittest.TestCase):
    """
    Stress-tests for Zero Render Bandwidth compliance:
    Assert 0-byte video payload traversing the Render server.
    """

    def setUp(self):
        self.client = TestClient(api_server.app, follow_redirects=False)

    def test_stream_video_by_id_zero_bytes_payload(self):
        """
        Adversarial Test: stream_video_by_id must return 302 redirect with Location
        pointing to Google CDN and strictly 0 bytes (or empty) transit body payload.
        """
        with patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.make_file_public") as mock_pub:
            
            resp = self.client.get("/api/stream-video-id/test_file_id_001")
            self.assertIn(resp.status_code, (302, 307))
            self.assertEqual(resp.status_code, 302)
            
            # Verify Location header
            expected_location = "https://drive.usercontent.google.com/download?id=test_file_id_001&export=download&authuser=0&confirm=t"
            self.assertEqual(resp.headers.get("location"), expected_location)
            
            # Verify ZERO transit video bytes (body must be empty or negligible redirect text, NOT video stream)
            self.assertLessEqual(len(resp.content), 0, f"Expected 0 transit bytes, got {len(resp.content)} bytes")
            mock_pub.assert_called_once_with("test_file_id_001", access_token="valid_token")

    def test_stream_video_by_id_with_range_header_no_chunk_proxy(self):
        """
        Adversarial Test: Video players send Range headers (e.g. Range: bytes=0-1048576).
        Render MUST NOT intercept Range and proxy binary chunks; it must return 302 redirect.
        """
        with patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.make_file_public"):
            
            headers = {"Range": "bytes=0-1048576"}
            resp = self.client.get("/api/stream-video-id/test_range_file", headers=headers)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("drive.usercontent.google.com", resp.headers.get("location", ""))
            self.assertEqual(len(resp.content), 0)

    def test_stream_video_by_id_permission_error_preview_fallback(self):
        """
        Adversarial Test: If make_file_public throws an exception, verify fallback to 302
        redirect to Google Drive Preview URL with 0 transit video payload.
        """
        with patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.make_file_public", side_effect=RuntimeError("Google Drive API rate limit")):
            
            resp = self.client.get("/api/stream-video-id/test_error_file")
            self.assertEqual(resp.status_code, 302)
            expected_preview = "https://drive.google.com/file/d/test_error_file/preview"
            self.assertEqual(resp.headers.get("location"), expected_preview)
            self.assertEqual(len(resp.content), 0)

    def test_stream_video_drive_file_zero_bytes_payload(self):
        """
        Adversarial Test: stream_video for a file not present locally but present in Drive
        must redirect 302 directly to CDN with 0 transit video payload.
        """
        mock_drive_search = MagicMock(status_code=200)
        mock_drive_search.json.return_value = {"files": [{"id": "drive_fid_456"}]}
        mock_drive_search.__enter__.return_value = mock_drive_search
        mock_drive_search.__exit__.return_value = False

        with patch("os.path.exists", return_value=False), \
             patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="mock_user_fid"), \
             patch("requests.get", return_value=mock_drive_search), \
             patch("gdrive_manager.make_file_public") as mock_pub:
            
            resp = self.client.get("/api/stream-video/streamer_challenger/cloud_recording.mp4")
            self.assertEqual(resp.status_code, 302)
            expected_cdn = "https://drive.usercontent.google.com/download?id=drive_fid_456&export=download&authuser=0&confirm=t"
            self.assertEqual(resp.headers.get("location"), expected_cdn)
            self.assertEqual(len(resp.content), 0)
            mock_pub.assert_called_once_with("drive_fid_456", access_token="valid_token")

    def test_stream_video_path_traversal_attack(self):
        """
        Adversarial Test: Attempt path traversal attacks on stream_video endpoint.
        Must be rejected with 403 or 404 without leaking arbitrary file bytes.
        """
        resp = self.client.get("/api/stream-video/..%2F..%2Fetc/passwd")
        self.assertIn(resp.status_code, (403, 404))

    def test_stream_video_drive_file_not_found_returns_404(self):
        """
        Adversarial Test: When file is not in Drive, return 404 cleanly.
        """
        mock_empty_res = MagicMock(status_code=200)
        mock_empty_res.json.return_value = {"files": []}
        mock_empty_res.__enter__.return_value = mock_empty_res
        mock_empty_res.__exit__.return_value = False

        with patch("os.path.exists", return_value=False), \
             patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="mock_user_fid"), \
             patch("requests.get", return_value=mock_empty_res):

            resp = self.client.get("/api/stream-video/streamer_challenger/missing.mp4")
            self.assertEqual(resp.status_code, 404)

    def test_stream_video_drive_search_network_exception_returns_404(self):
        """
        Adversarial Test: When Google Drive search throws network error, return 404 cleanly without 500 crash.
        """
        with patch("os.path.exists", return_value=False), \
             patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="mock_user_fid"), \
             patch("requests.get", side_effect=requests.exceptions.ConnectionError("Drive connection failed")):

            resp = self.client.get("/api/stream-video/streamer_challenger/network_err.mp4")
            self.assertEqual(resp.status_code, 404)

    def test_stream_video_by_id_no_token_returns_500(self):
        """
        Adversarial Test: When get_access_token returns None, return 500 cleanly with 0 video bytes.
        """
        with patch("gdrive_manager.get_access_token", return_value=None):
            resp = self.client.get("/api/stream-video-id/any_file_id")
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(resp.json().get("detail"), "Không có quyền truy cập Google Drive")

    def test_thumbnail_zero_render_bandwidth_redirect(self):
        """
        Adversarial Test: Thumbnail serving from Google Drive must return 302 redirect
        to Google Drive Thumbnail CDN instead of proxying binary content through Render.
        """
        mock_thumb_search = MagicMock(status_code=200)
        mock_thumb_search.json.return_value = {"files": [{"id": "thumb_drive_id_999"}]}

        with patch("os.path.exists", return_value=False), \
             patch("gdrive_manager.get_access_token", return_value="valid_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="mock_user_fid"), \
             patch("gdrive_manager.make_file_public") as mock_pub, \
             patch("requests.get", return_value=mock_thumb_search):

            resp = self.client.get("/api/thumbnail/streamer_challenger/stream_test.mp4?redirect=true")
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("location"), "https://drive.google.com/thumbnail?id=thumb_drive_id_999&sz=w800")
            mock_pub.assert_called_once_with("thumb_drive_id_999", access_token="valid_token")


class TestChallengerStagingManifestResilience(unittest.TestCase):
    """
    Stress-tests for Staging Queue Manifest Network Failure Safety:
    Verify that under various network failure modes, add_to_staging_queue
    NEVER invokes save_staging_manifest and aborts safely.
    """

    def setUp(self):
        self.dummy_file = os.path.join(staging_queue.BASE_DIR, "challenger_dummy_seg.mp4")
        with open(self.dummy_file, "wb") as f:
            f.write(b"X" * (300 * 1024)) # 300KB (passes > 250KB filter)

    def tearDown(self):
        if os.path.exists(self.dummy_file):
            try:
                os.remove(self.dummy_file)
            except Exception:
                pass

    def test_network_connection_error_preserves_manifest(self):
        """
        Adversarial Test: requests.get throws ConnectionError during get_staging_manifest.
        Verify add_to_staging_queue catches it, does NOT call save_staging_manifest,
        returns error status, and releases lock.
        """
        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("stg_fid", "root_stg")), \
             patch("gdrive_manager.upload_file_to_drive", return_value="seg_file_id_001"), \
             patch("requests.get", side_effect=requests.exceptions.ConnectionError("Connection refused by Google")), \
             patch("staging_queue.save_staging_manifest") as mock_save:

            res = staging_queue.add_to_staging_queue("challenger_user", self.dummy_file, 120.0, access_token="fake_token")
            self.assertEqual(res["status"], "error")
            self.assertIn("Connection refused", res["message"])
            mock_save.assert_not_called()

            # Verify lock is freed
            lock = staging_queue._get_user_staging_lock("challenger_user")
            acquired = lock.acquire(blocking=False)
            self.assertTrue(acquired, "Staging lock was NOT released after ConnectionError!")
            if acquired:
                lock.release()

    def test_network_timeout_preserves_manifest(self):
        """
        Adversarial Test: requests.get throws Timeout during get_staging_manifest.
        Verify manifest is NOT saved, error is reported, and lock is released.
        """
        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("stg_fid", "root_stg")), \
             patch("gdrive_manager.upload_file_to_drive", return_value="seg_file_id_002"), \
             patch("requests.get", side_effect=requests.exceptions.Timeout("Read timeout after 10s")), \
             patch("staging_queue.save_staging_manifest") as mock_save:

            res = staging_queue.add_to_staging_queue("challenger_user", self.dummy_file, 60.0, access_token="fake_token")
            self.assertEqual(res["status"], "error")
            self.assertIn("Read timeout", res["message"])
            mock_save.assert_not_called()

    def test_http_503_error_preserves_manifest(self):
        """
        Adversarial Test: Drive API returns 503 Service Unavailable.
        Verify get_staging_manifest raises IOError and save_staging_manifest is not called.
        """
        mock_503 = MagicMock(status_code=503, text="Service Unavailable")
        mock_503.__enter__.return_value = mock_503
        mock_503.__exit__.return_value = False

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("stg_fid", "root_stg")), \
             patch("gdrive_manager.upload_file_to_drive", return_value="seg_file_id_003"), \
             patch("requests.get", return_value=mock_503), \
             patch("staging_queue.save_staging_manifest") as mock_save:

            res = staging_queue.add_to_staging_queue("challenger_user", self.dummy_file, 90.0, access_token="fake_token")
            self.assertEqual(res["status"], "error")
            self.assertIn("503", res["message"])
            mock_save.assert_not_called()

    def test_corrupted_json_in_manifest_download_preserves_manifest(self):
        """
        Adversarial Test: Manifest download returns corrupt JSON data.
        Verify exception is raised and save_staging_manifest is never called.
        """
        mock_meta = MagicMock(status_code=200)
        mock_meta.json.return_value = {"files": [{"id": "manifest_id_001"}]}
        mock_meta.__enter__.return_value = mock_meta
        mock_meta.__exit__.return_value = False

        mock_content = MagicMock(status_code=200)
        mock_content.json.side_effect = json.decoder.JSONDecodeError("Expecting value", "corrupt_data", 0)
        mock_content.__enter__.return_value = mock_content
        mock_content.__exit__.return_value = False

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("stg_fid", "root_stg")), \
             patch("gdrive_manager.upload_file_to_drive", return_value="seg_file_id_004"), \
             patch("requests.get", side_effect=[mock_meta, mock_content]), \
             patch("staging_queue.save_staging_manifest") as mock_save:

            res = staging_queue.add_to_staging_queue("challenger_user", self.dummy_file, 45.0, access_token="fake_token")
            self.assertEqual(res["status"], "error")
            mock_save.assert_not_called()


class TestChallengerStagingLockTimeout(unittest.TestCase):
    """
    Stress-tests for Staging Lock Timeout and Concurrency Safety:
    Verify that lock acquisition timeout triggers cleanly without deadlocks,
    per-user locks are completely isolated, and locks are always released.
    """

    def setUp(self):
        self.dummy_file = os.path.join(staging_queue.BASE_DIR, "challenger_lock_test.mp4")
        with open(self.dummy_file, "wb") as f:
            f.write(b"L" * (300 * 1024))

    def tearDown(self):
        if os.path.exists(self.dummy_file):
            try:
                os.remove(self.dummy_file)
            except Exception:
                pass

    def test_lock_timeout_triggers_cleanly_on_contention(self):
        """
        Adversarial Test: Mock _get_user_staging_lock returning a lock where acquire(timeout=30.0)
        returns False (simulating lock timeout).
        Verify both add_to_staging_queue and package_and_publish_queue return clean structured
        error dicts without hanging or crashing.
        """
        user = "challenger_mock_lock_user"
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False

        with patch("staging_queue._get_user_staging_lock", return_value=mock_lock):
            # Test add_to_staging_queue
            res = staging_queue.add_to_staging_queue(user, self.dummy_file, 100.0)
            self.assertEqual(res["status"], "error")
            self.assertIn("timeout 30s", res["message"])
            mock_lock.acquire.assert_called_with(timeout=30.0)
            mock_lock.release.assert_not_called()

            # Test package_and_publish_queue
            res_pkg = staging_queue.package_and_publish_queue(user)
            self.assertFalse(res_pkg["ok"])
            self.assertIn("timeout 30s", res_pkg["error"])

    def test_per_user_lock_isolation(self):
        """
        Adversarial Test: Verify User A holding a lock does NOT block User B.
        """
        user_a = "user_alpha"
        user_b = "user_beta"
        
        lock_a = staging_queue._get_user_staging_lock(user_a)
        lock_b = staging_queue._get_user_staging_lock(user_b)
        
        self.assertIsNot(lock_a, lock_b, "Locks for different users must be distinct objects!")

        # Acquire lock A
        acquired_a = lock_a.acquire(timeout=1.0)
        self.assertTrue(acquired_a)

        try:
            # Lock B should still be immediately acquirable
            acquired_b = lock_b.acquire(timeout=0.5)
            self.assertTrue(acquired_b, "Lock B was blocked by Lock A!")
            if acquired_b:
                lock_b.release()
        finally:
            lock_a.release()

    def test_high_concurrency_stress_threads(self):
        """
        Adversarial Stress Test: Spawn 8 concurrent threads calling add_to_staging_queue
        across different users simultaneously. Verify no deadlocks or uncaught crashes.
        """
        results = []
        errors = []

        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("gdrive_manager.find_or_create_folder", return_value="fake_folder_fid"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("sfid", "rfid")), \
             patch("gdrive_manager.upload_file_to_drive", return_value="file_id_stress"), \
             patch("staging_queue.get_staging_manifest", return_value={"segments": [], "manifest_file_id": "mid"}), \
             patch("staging_queue.save_staging_manifest", return_value=True):

            def worker_task(worker_id):
                user = f"stress_user_{worker_id % 3}" # 3 users, 8 threads -> contention on same users
                try:
                    res = staging_queue.add_to_staging_queue(user, self.dummy_file, 60.0, access_token="fake_token")
                    results.append((worker_id, res))
                except Exception as ex:
                    errors.append((worker_id, ex))

            threads = [threading.Thread(target=worker_task, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive(), "Worker thread hung - potential deadlock!")

        self.assertEqual(len(errors), 0, f"Encountered unexpected exceptions: {errors}")
        self.assertEqual(len(results), 8, "All 8 worker threads must complete.")
        for worker_id, res in results:
            self.assertIn(res.get("status"), ("queued", "packaged", "error"))

    def test_package_and_publish_queue_exception_releases_lock(self):
        """
        Adversarial Test: If package_and_publish_queue raises an unexpected exception
        inside the try block (e.g. during get_staging_manifest), verify lock is freed
        in finally block and can be immediately acquired by another thread.
        """
        user = "challenger_exc_user"
        with patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", side_effect=RuntimeError("Unforeseen cloud crash")):

            res = staging_queue.package_and_publish_queue(user)
            self.assertFalse(res.get("ok"))
            self.assertIn("Unforeseen cloud crash", res.get("error"))

            # Verify lock is released and can be acquired immediately
            lock = staging_queue._get_user_staging_lock(user)
            acquired = lock.acquire(blocking=False)
            self.assertTrue(acquired, "Staging lock was NOT released after package_and_publish_queue exception!")
            if acquired:
                lock.release()

    def test_add_to_staging_queue_sub_250kb_rejected_no_lock_leak(self):
        """
        Adversarial Test: When file is < 250KB, it should return rejected immediately
        without leaving any locks held.
        """
        tiny_file = os.path.join(staging_queue.BASE_DIR, "tiny_test.mp4")
        with open(tiny_file, "wb") as f:
            f.write(b"T" * 1024) # 1KB < 250KB
        try:
            res = staging_queue.add_to_staging_queue("tiny_user", tiny_file, 10.0)
            self.assertEqual(res["status"], "rejected")
            self.assertIn("< 250KB", res["message"])
            
            lock = staging_queue._get_user_staging_lock("tiny_user")
            acquired = lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                lock.release()
        finally:
            if os.path.exists(tiny_file):
                os.remove(tiny_file)

    def test_add_to_staging_queue_nonexistent_file_returns_error(self):
        """
        Adversarial Test: Non-existent local file returns error immediately.
        """
        res = staging_queue.add_to_staging_queue("any_user", "non_existent_file.mp4", 10.0)
        self.assertEqual(res["status"], "error")
        self.assertIn("không tồn tại", res["message"])


class TestChallengerFixVerifications(unittest.TestCase):
    """
    Empirical tests verifying Bug 1 and Bug 2 root-cause fixes:
    1. Exact size matching in gdrive_manager.upload_file_to_drive
    2. Reliable filename-based remaining segments filtering in staging_queue
    """

    def test_gdrive_upload_exact_size_match_discards_partial_files(self):
        """
        Adversarial Test (Bug 1):
        - If drive file has 500KB and local file has 600KB (previously mistaken as matching),
          upload_file_to_drive MUST detect mismatch, delete the partial drive file, and re-upload.
        - If drive file has exactly 600KB, it recognizes exact match and returns existing id.
        """
        dummy_file = os.path.join(staging_queue.BASE_DIR, "challenger_size_test.mp4")
        file_size = 600 * 1024
        with open(dummy_file, "wb") as f:
            f.write(b"X" * file_size)

        try:
            # Case A: Partial file on Drive (500KB != 600KB)
            mock_check_partial = MagicMock(status_code=200)
            mock_check_partial.json.return_value = {"files": [{"id": "partial_id_123", "size": str(500 * 1024)}]}

            # Case B: Exact file on Drive (600KB == 600KB)
            mock_check_exact = MagicMock(status_code=200)
            mock_check_exact.json.return_value = {"files": [{"id": "exact_id_456", "size": str(file_size)}]}

            with patch("requests.get", return_value=mock_check_exact):
                result_exact = gdrive_manager.upload_file_to_drive(dummy_file, "parent_fid", access_token="tok")
                self.assertEqual(result_exact, "exact_id_456")

            with patch("requests.get", return_value=mock_check_partial), \
                 patch("requests.delete") as mock_del, \
                 patch("requests.post") as mock_init:
                # Simulating init upload failing to inspect the delete call
                mock_init.return_value = MagicMock(status_code=500, text="stop_here")
                gdrive_manager.upload_file_to_drive(dummy_file, "parent_fid", access_token="tok")
                mock_del.assert_called_once_with(
                    "https://www.googleapis.com/drive/v3/files/partial_id_123",
                    headers={"Authorization": "Bearer tok"},
                    timeout=10
                )
        finally:
            if os.path.exists(dummy_file):
                os.remove(dummy_file)

    def test_staging_queue_package_filters_by_filename_preserves_unmerged(self):
        """
        Adversarial Test (Bug 2):
        When packaging queue, remaining segments must be filtered by filename,
        preserving any unmerged segments even if their file_id is None.
        """
        user = "test_filter_user"
        seg1 = {"filename": "seg1.mp4", "file_id": "fid_1", "duration": 3000.0, "size_bytes": 500000}
        seg2 = {"filename": "seg2.mp4", "file_id": None, "duration": 100.0, "size_bytes": 300000}

        # Simulate manifest with seg1 and seg2
        manifest = {"segments": [seg1, seg2], "manifest_file_id": "mf_id"}

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with patch("staging_queue._get_user_staging_lock", return_value=mock_lock), \
             patch("gdrive_manager.get_access_token", return_value="fake_token"), \
             patch("staging_queue.get_or_create_staging_folder", return_value=("stg_fid", "root_fid")), \
             patch("staging_queue.get_staging_manifest", return_value=manifest), \
             patch("staging_queue.save_staging_manifest") as mock_save_manifest, \
             patch("gdrive_manager.find_or_create_folder", return_value="ufid_main"), \
             patch("gdrive_manager.upload_file_to_drive", return_value="final_drive_id"), \
             patch("gdrive_manager.delete_file_drive"), \
             patch("supabase_sync.sync_recording_to_supabase", return_value=True), \
             patch("staging_queue.validate_playable_video", return_value=(True, "OK", 3000.0)), \
             patch("os.remove"), \
             patch("shutil.copy2"), \
             patch("shutil.rmtree"), \
             patch("os.path.getsize", return_value=500000), \
             patch("os.path.exists", side_effect=lambda p: True if p in ["seg1.mp4", "seg2.mp4", "thumb.jpg"] or p.endswith("_full.mp4") or "seg1.mp4" in p else False):

            # Make seg1 succeed, but seg2 fail to download
            def dl_side_effect(fid, target_local, access_token=None):
                if fid == "fid_1":
                    return True
                return False

            with patch("gdrive_manager.download_file_from_drive", side_effect=dl_side_effect):
                res = staging_queue.package_and_publish_queue(user, access_token="tok")
                self.assertTrue(res.get("ok"))
                mock_save_manifest.assert_called_once()
                saved_manifest = mock_save_manifest.call_args[0][2]
                # seg2 MUST be preserved in saved_manifest["segments"]!
                self.assertEqual(len(saved_manifest["segments"]), 1)
                self.assertEqual(saved_manifest["segments"][0]["filename"], "seg2.mp4")


if __name__ == "__main__":
    unittest.main()
