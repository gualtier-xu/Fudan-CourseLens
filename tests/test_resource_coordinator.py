import threading
import unittest

from src.runtime.resources import ResourceCoordinator


class ResourceCoordinatorTests(unittest.TestCase):
    @staticmethod
    def probe(*, on_ac=True, cpu=20, memory_gb=16):
        return {
            "on_ac_power": on_ac,
            "cpu_percent": cpu,
            "available_memory_bytes": int(memory_gb * 1024**3),
        }

    def test_online_client_limits_are_bounded(self):
        coordinator = ResourceCoordinator(lambda: self.probe(), refresh_seconds=999)
        self.assertEqual(coordinator.limits()["remote_streams"], 4)
        self.assertEqual(coordinator.limits()["search_batch_size"], 250)

        battery = ResourceCoordinator(lambda: self.probe(on_ac=False), refresh_seconds=999)
        self.assertEqual(battery.limits()["search_batch_size"], 100)

    def test_playback_reduces_background_limits(self):
        coordinator = ResourceCoordinator(lambda: self.probe(), refresh_seconds=999)
        coordinator.begin_player_stream()
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["player_streams"], 1)
        self.assertEqual(snapshot["limits"]["search_batch_size"], 50)
        self.assertIn("interactive_playback", snapshot["throttle_reasons"])
        coordinator.end_player_stream()
        self.assertEqual(coordinator.snapshot()["player_streams"], 0)

    def test_pressure_blocks_search_until_canceled(self):
        coordinator = ResourceCoordinator(
            lambda: self.probe(cpu=95), refresh_seconds=999
        )
        canceled = threading.Event()
        canceled.set()
        with self.assertRaises(InterruptedError):
            with coordinator.claim("search", cancel_event=canceled):
                pass

    def test_claims_are_visible_and_released(self):
        coordinator = ResourceCoordinator(lambda: self.probe(), refresh_seconds=999)
        with coordinator.claim("interactive_stream"):
            self.assertEqual(coordinator.snapshot()["active_claims"], {"interactive_stream": 1})
        self.assertEqual(coordinator.snapshot()["active_claims"], {})

    def test_local_compute_claims_are_rejected(self):
        coordinator = ResourceCoordinator(lambda: self.probe(), refresh_seconds=999)
        with self.assertRaises(ValueError):
            with coordinator.claim("ocr"):
                pass


if __name__ == "__main__":
    unittest.main()
