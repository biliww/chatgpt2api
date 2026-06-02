import json
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ROOT_CONFIG_FILE = ROOT_DIR / "config.json"


class ConfigLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._created_root_config = False
        if not ROOT_CONFIG_FILE.exists():
            ROOT_CONFIG_FILE.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            cls._created_root_config = True

        from services import config as config_module

        cls.config_module = config_module

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._created_root_config and ROOT_CONFIG_FILE.exists():
            ROOT_CONFIG_FILE.unlink()

    def test_load_settings_ignores_directory_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            data_dir = base_dir / "data"
            config_dir = base_dir / "config.json"
            os_auth_key = "env-auth"

            config_dir.mkdir()

            module = self.config_module
            old_base_dir = module.BASE_DIR
            old_data_dir = module.DATA_DIR
            old_config_file = module.CONFIG_FILE
            old_env_auth_key = module.os.environ.get("CHATGPT2API_AUTH_KEY")
            try:
                module.BASE_DIR = base_dir
                module.DATA_DIR = data_dir
                module.CONFIG_FILE = config_dir
                module.os.environ["CHATGPT2API_AUTH_KEY"] = os_auth_key

                settings = module._load_settings()

                self.assertEqual(settings.auth_key, os_auth_key)
                self.assertEqual(settings.refresh_account_interval_minute, 5)
            finally:
                module.BASE_DIR = old_base_dir
                module.DATA_DIR = old_data_dir
                module.CONFIG_FILE = old_config_file
                if old_env_auth_key is None:
                    module.os.environ.pop("CHATGPT2API_AUTH_KEY", None)
                else:
                    module.os.environ["CHATGPT2API_AUTH_KEY"] = old_env_auth_key

    def test_normalize_image_storage_accepts_imgbb_mode(self) -> None:
        module = self.config_module

        settings = module._normalize_image_storage_settings({
            "enabled": True,
            "mode": "imgbb",
            "imgbb_key": "test-key",
            "imgbb_expiration": "600",
        })

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["mode"], "imgbb")
        self.assertEqual(settings["imgbb_key"], "test-key")
        self.assertEqual(settings["imgbb_expiration"], 600)

    def test_validate_image_storage_requires_imgbb_key(self) -> None:
        module = self.config_module
        settings = module._normalize_image_storage_settings({"enabled": True, "mode": "imgbb"})

        with self.assertRaises(ValueError):
            module._validate_image_storage_settings(settings)

    def test_validate_image_storage_rejects_invalid_imgbb_expiration(self) -> None:
        module = self.config_module
        settings = module._normalize_image_storage_settings({
            "enabled": True,
            "mode": "imgbb",
            "imgbb_key": "test-key",
            "imgbb_expiration": 10,
        })

        with self.assertRaises(ValueError):
            module._validate_image_storage_settings(settings)


if __name__ == "__main__":
    unittest.main()
