import tempfile
import unittest
from pathlib import Path

from app import db


class IsolatedDbTestCase(unittest.TestCase):
    """Give every database test its own temporary SQLite file."""

    def setUp(self):
        super().setUp()
        self._test_directory=tempfile.TemporaryDirectory(prefix="vlog-test-")
        self.test_root=Path(self._test_directory.name)
        self._original_db_path=db.DB_PATH
        db.DB_PATH=self.test_root/"data"/"vlog.db"
        db.DB_PATH.parent.mkdir(parents=True,exist_ok=True)
        self.addCleanup(self._restore_database)
        db.init_db()

    def _restore_database(self):
        db.DB_PATH=self._original_db_path
        self._test_directory.cleanup()
