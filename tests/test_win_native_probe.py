"""Native probe fixes OS motion conditions without changing product preferences."""
import unittest
from unittest import mock

from scripts.dev.win_native_probe import ClientAreaAnimation


class FakeUser32:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.calls = []
        self.ignore_set = False

    def SystemParametersInfoW(self, action, parameter, value, flags):
        self.calls.append((action, parameter, value, flags))
        if action == ClientAreaAnimation.GET:
            value._obj.value = self.enabled
        elif not self.ignore_set:
            self.enabled = bool(value.value)
        return 1


class MotionPreferenceCase(unittest.TestCase):
    def test_sets_pvparam_reads_back_and_restores_original_once(self):
        native = FakeUser32(False)
        with mock.patch('atexit.register') as register, mock.patch('atexit.unregister') as unregister:
            setting = ClientAreaAnimation(native)
            self.assertFalse(setting.original)
            self.assertTrue(setting.set(True))
            self.assertEqual(native.calls[1][0:2], (0x1043, 0))
            self.assertEqual(native.calls[1][2].value, 1)
            self.assertEqual(native.calls[1][3], 2)  # broadcast; no profile write
            setting.restore()
            setting.restore()
            self.assertFalse(native.enabled)
            self.assertEqual(sum(c[0] == 0x1043 for c in native.calls), 2)
            register.assert_called_once_with(setting.restore)
            unregister.assert_called_once_with(setting.restore)

    def test_mismatched_readback_is_a_failure_and_cleanup_can_restore(self):
        native = FakeUser32(False)
        native.ignore_set = True
        with mock.patch('atexit.register'), mock.patch('atexit.unregister'):
            setting = ClientAreaAnimation(native)
            try:
                with self.assertRaisesRegex(OSError, 'stayed False'):
                    setting.set(True)
            finally:
                setting.restore()
            self.assertFalse(native.enabled)

    def test_failed_spi_read_is_explicit(self):
        native = mock.Mock()
        native.SystemParametersInfoW.return_value = 0
        with self.assertRaisesRegex(OSError, 'GETCLIENTAREAANIMATION'):
            ClientAreaAnimation(native)


if __name__ == '__main__':
    unittest.main()
