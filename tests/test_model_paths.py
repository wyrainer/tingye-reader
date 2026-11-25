import tempfile
import unittest
from pathlib import Path

from novel_reader.model_paths import MODEL_FILES, PACK, locate_models


class ModelPathsTests(unittest.TestCase):
    def test_packaged_layout_and_remembered_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "模型库" / PACK
            custom = root / "其他位置" / PACK
            for folder in (shared, custom, root / "models"):
                for item in MODEL_FILES:
                    path = folder / item
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
            exe = root / "程序" / "新版" / "听页" / "听页.exe"
            self.assertEqual(locate_models(root, exe, str(custom)), shared)
            (shared / MODEL_FILES[0]).unlink()
            self.assertEqual(locate_models(root, exe, str(custom)), custom)
            self.assertIsNone(locate_models(root, exe))
            self.assertEqual(locate_models(root), root / "models")
