import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import core


A = "88:2F:92:4D:24:ED"
B = "11:22:33:44:55:66"


class PolicyTests(unittest.TestCase):
    def test_authorization_boundary(self):
        state = {"devices": {A: {}}}
        self.assertIs(core.decision(state, A.lower(), core.HID, True, True), True)
        for paired, bonded in [(False, False), (True, False), (False, True)]:
            self.assertIs(core.decision(state, A, core.HID, paired, bonded), False)
        for uuid in ["0000110a-0000-1000-8000-00805f9b34fb", "0000111e-0000-1000-8000-00805f9b34fb", "unknown", "00001105-0000-1000-8000-00805f9b34fb"]:
            self.assertIs(core.decision(state, A, uuid, True, True), False)
        self.assertIsNone(core.decision(state, B, core.HID, True, True))

    def test_restoring_agent_preserves_unrelated_plugin_changes(self):
        settings = Mock()
        settings.get_strv.return_value = ["!AuthAgent", "NewPlugin", "!OtherPlugin"]
        core.restore_authagent(settings, ["AuthAgent"])
        settings.set_strv.assert_called_once_with("plugin-list", ["NewPlugin", "!OtherPlugin", "AuthAgent"])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for attribute, name in [("STATE_DIR", "state"), ("STATE_FILE", "state/state.json"), ("UNIT", "unit"), ("AUTOSTART", "desktop")]:
            patcher = patch.object(core, attribute, root / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = {"name": "Phone", "original_trusted": True, "original_blocked": False}
        self.settings = Mock()
        self.settings.get_strv.return_value = ["!AuthAgent", "OtherPlugin"]
        for name in ["systemctl", "install_agent", "set_property", "legacy_setup"]:
            patcher = patch.object(core, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.legacy_setup.return_value = None
        patcher = patch.object(core.Gio.Settings, "new", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_reset_one_retains_other_policy_and_agent(self):
        core.save_state({"version": 1, "devices": {A: self.saved, B: self.saved}, "authagent_original": []})
        with patch.object(core, "paired_devices", return_value=[("/phone", {"Address": A})]):
            core.reset_devices(A)
        self.assertEqual(list(core.read_state()["devices"]), [B])
        self.set_property.assert_any_call("/phone", "Trusted", True)
        self.systemctl.assert_called_once_with("restart", core.SERVICE)
        self.settings.set_strv.assert_not_called()

    def test_reset_last_restores_agent_and_removes_startup(self):
        core.save_state({"version": 1, "devices": {A: self.saved}, "authagent_original": []})
        core.UNIT.touch()
        core.AUTOSTART.touch()
        with patch.object(core, "paired_devices", return_value=[("/phone", {"Address": A})]):
            core.reset_devices(A)
        self.assertEqual(core.read_state()["devices"], {})
        self.assertFalse(core.UNIT.exists())
        self.assertFalse(core.AUTOSTART.exists())
        self.settings.set_strv.assert_called_once_with("plugin-list", ["OtherPlugin"])

    def test_repeated_enable_keeps_original_trust(self):
        core.save_state({"version": 1, "devices": {A: self.saved}, "authagent_original": []})
        device = {"Address": A, "Trusted": False, "Blocked": False, "Bonded": True, "UUIDs": [core.HID]}
        with patch.object(core, "select_device", return_value=("/phone", device)):
            core.enable_device(A)
        self.assertTrue(core.read_state()["devices"][A]["original_trusted"])

    def test_failed_enable_restores_configuration_and_trust(self):
        device = {"Address": A, "Trusted": True, "Blocked": False, "Bonded": True, "UUIDs": [core.HID]}
        self.install_agent.side_effect = RuntimeError("startup failed")
        with patch.object(core, "select_device", return_value=("/phone", device)):
            with self.assertRaises(RuntimeError):
                core.enable_device(A)
        self.assertEqual(core.read_state()["devices"], {})
        self.set_property.assert_any_call("/phone", "Trusted", True)

    def test_reset_unpaired_device_removes_stale_policy(self):
        core.save_state({"version": 1, "devices": {A: self.saved}, "authagent_original": []})
        with patch.object(core, "paired_devices", return_value=[]):
            core.reset_devices(A)
        self.assertEqual(core.read_state()["devices"], {})
        self.set_property.assert_not_called()


if __name__ == "__main__":
    unittest.main()
