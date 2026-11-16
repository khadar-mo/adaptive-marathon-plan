from concurrent.futures import ThreadPoolExecutor
import threading

from storage import Store


def test_concurrent_read_only_and_writable_handles_preserve_all_commits(tmp_path):
    # macOS SQLite 3.54.0 can raise SQLITE_IOERR_LOCK when mixed-mode handles
    # overlap in this process. Exercise their complete open/commit/close lifetimes.
    store = Store(tmp_path / "lock-test.sqlite3")
    with store.connect(write=True, create=True) as db:
        db.execute("CREATE TABLE counter (n INTEGER)")
        db.execute("INSERT INTO counter VALUES (0)")
    together = threading.Barrier(2)
    def read():
        together.wait(timeout=5)
        values = []
        for _ in range(500):
            with store.connect() as db:
                values.append(db.execute("SELECT n FROM counter").fetchone()[0])
        assert values == sorted(values)
    def write():
        together.wait(timeout=5)
        for _ in range(500):
            with store.connect(write=True) as db:
                db.execute("UPDATE counter SET n=n+1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fn) for fn in (read, write)]
        for future in futures:
            future.result(timeout=60)
    with store.connect() as db:
        assert db.execute("SELECT n FROM counter").fetchone()[0] == 500
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
