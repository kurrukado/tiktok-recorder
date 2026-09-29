"""
Khóa cấu hình dùng chung cho config.json.

_ CONFIG_LOCK kiểu threading chỉ chặn được các THREAD trong CÙNG một process.
 api_server, cloud_daemon, tiktok_recorder là 3 process RIÊNG BIỆT cùng đọc-sửa-ghi
 config.json: mỗi bên load -> sửa -> save thì bản ghi của bên sau sẽ GHI ĐÈ mất
 thay đổi của bên trước (mất streamer trong monitored_users).

_ Ghi file đã dùng tmp + os.replace nên file KHÔNG BAO GIỜ bị cụt/chữi; vấn đề còn
 lại chỉ là "lost update". Khóa liên tiến trình giải quyết đúng vấn đề đó.
"""
import contextlib
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(BASE_DIR, "config.lock")

# Khóa cấp thread (mang tính re-entrant: 1 thread có thể lồng nhau)...
_THREAD_LOCK = threading.RLock()
# ...kết hợp khóa cấp file (mang tính liên tiến trình trên cùng máy).
LOCK_TIMEOUT_SECONDS = 10.0

# Theo dõi transaction của TỪNG thread: save_config() gọi trong lúc đã có
# config_transaction() đang giữ khóa thì KHÔNG được khóa file lần 2
# (khóa file không re-entrant -> sẽ tự tước khóa và treo chờ timeout).
_TLS = threading.local()


def _acquire_file_lock(timeout):
    """Mở + giữ khóa byte trên config.lock. Trả về (fd, locked)."""
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o666)
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, True
        except OSError:
            if time.time() >= deadline:
                return fd, False
            time.sleep(0.05)


def _release_file_lock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN | fcntl.LOCK_NB)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@contextlib.contextmanager
def config_transaction(timeout=LOCK_TIMEOUT_SECONDS):
    """
    Bọc toàn bộ một chuỗi đọc-sửa-ghi config.json:

        with config_transaction():
            cfg = load_config()
            ...
            save_config(cfg)

    Khóa được giữ suốt từ lúc đọc tới lúc ghi, nên process khác phải chờ xong
    (hoặc bỏ qua sau `timeout` giây thay vì treo vô hạn).
    """
    thread_ok = False
    fd = None
    locked = False
    if getattr(_TLS, "depth", 0) > 0:
        # Đã nằm trong transaction của CHÍNH thread này (save_config lồng trong
        # config_transaction) -> khóa đã giữ, chỉ tăng độ sâu.
        _TLS.depth += 1
        try:
            yield
        finally:
            _TLS.depth -= 1
        return

    thread_ok = _THREAD_LOCK.acquire(timeout=timeout)
    if thread_ok:
        try:
            fd, locked = _acquire_file_lock(timeout)
        except OSError as e:
            print(f"[config] Không mở được {os.path.basename(LOCK_FILE)}: {e}")
            fd = None
        if not locked:
            print(f"[config] Hết {timeout}s chờ khóa liên tiến trình. Tiếp tục KHÔNG có khóa "
                  f"(risk: process khác có thể ghi đè thay đổi của process này).")
    _TLS.depth = 1
    try:
        yield
    finally:
        _TLS.depth = 0
        if fd is not None:
            if locked:
                _release_file_lock(fd)
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if thread_ok:
            _THREAD_LOCK.release()


_LOCKS_DIR = os.path.join(BASE_DIR, ".locks")

@contextlib.contextmanager
def streamer_recording_lock(user: str):
    """
    Khóa file liên tiến trình độc quyền cho từng streamer.
    Ngăn chặn tuyệt đối tình trạng api_server và cloud_daemon (hoặc 2 worker)
    cùng lúc ghi hình 1 streamer trên cùng một máy chủ.
    Trả về True nếu chiếm được khóa, False nếu streamer đã có tiến trình khác đang quay.
    """
    user_clean = os.path.basename(str(user or "").strip().replace("@", "").lower())
    if not user_clean or user_clean in (".", ".."):
        yield False
        return

    try:
        os.makedirs(_LOCKS_DIR, exist_ok=True)
    except Exception:
        pass

    lock_file = os.path.join(_LOCKS_DIR, f"{user_clean}.recording.lock")
    fd = None
    locked = False
    try:
        fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                locked = False
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                locked = False
        yield locked
    except Exception:
        yield False
    finally:
        if locked and fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN | fcntl.LOCK_NB)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

