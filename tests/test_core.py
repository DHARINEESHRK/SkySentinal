from __future__ import annotations

import tempfile
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app import create_app
from skysentinel.config import load_settings
from skysentinel.database import Repository
from skysentinel.detection import DetectionEngine, merge_detections
from skysentinel.firebase_sync import FirestoreOutboxWorker, initialize_firestore
from skysentinel.risk import assess_risk
from skysentinel.tracking import IoUTracker


class DummyDetector:
    def model_status(self) -> list[dict]:
        return []

    def infer(self, frame: np.ndarray, frame_type: str = "visible_rgb") -> tuple[np.ndarray, dict]:
        return frame.copy(), {
            "winner": None, "confidence": 0.0, "scores": {},
            "detections": [], "inference_latency_ms": 1.0,
        }


class RiskTests(unittest.TestCase):
    def test_persistence_score_boundaries(self) -> None:
        expected = [(1.99, 0), (2, 5), (5, 12), (15, 18), (30, 25)]
        for duration, score in expected:
            with self.subTest(duration=duration):
                result = assess_risk(0, duration)
                self.assertEqual(result.components["persistence"], score)

    def test_components_missing_data_and_bands(self) -> None:
        low = assess_risk(0.5, 1)
        self.assertEqual(low.score, 10)
        self.assertEqual(low.level, "low")
        self.assertTrue(any("calibrated distance" in item for item in low.missing_information))

        medium = assess_risk(1, 5, proximity="far")
        self.assertEqual(medium.score, 37)
        self.assertEqual(medium.level, "medium")

        high = assess_risk(
            1, 30, proximity="near", motion_validated=True, correlated_sensor=True
        )
        self.assertEqual(high.score, 100)
        self.assertEqual(high.level, "high")
        self.assertTrue(high.assessed_at.endswith("+00:00"))

    def test_scores_are_clamped_and_unknown_proximity_is_not_inferred(self) -> None:
        result = assess_risk(2, 60, proximity="unknown", high_threshold=100)
        self.assertEqual(result.score, 45)
        self.assertEqual(result.components["proximity"], 0)
        self.assertEqual(result.level, "medium")

    def test_custom_risk_components_and_bands(self) -> None:
        from skysentinel.risk import RiskConfig
        config = RiskConfig(confidence_max_points=10, medium_threshold=20, high_threshold=40)
        result = assess_risk(1, 1, config=config)
        self.assertEqual(result.score, 10)
        self.assertEqual(result.level, "low")


class DetectionAndTrackingTests(unittest.TestCase):
    def test_duplicate_suppression_keeps_model_sources_and_distinct_boxes(self) -> None:
        result = merge_detections([
            {"label": "drone", "confidence": 0.8, "box": [0, 0, 100, 100],
             "model_sources": ["close_range"]},
            {"label": "drone", "confidence": 0.9, "box": [3, 2, 102, 101],
             "model_sources": ["long_range"]},
            {"label": "drone", "confidence": 0.7, "box": [200, 200, 260, 260],
             "model_sources": ["close_range"]},
        ])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["confidence"], 0.9)
        self.assertEqual(result[0]["model_sources"], ["close_range", "long_range"])

    def test_iou_tracker_preserves_track_and_separates_objects(self) -> None:
        tracker = IoUTracker()
        first = tracker.update([
            {"box": [0, 0, 50, 50], "confidence": 0.8},
            {"box": [200, 200, 250, 250], "confidence": 0.7},
        ], now=1)
        second = tracker.update([
            {"box": [2, 1, 51, 51], "confidence": 0.85},
            {"box": [202, 200, 251, 250], "confidence": 0.75},
        ], now=2)
        self.assertEqual([item["tracking_id"] for item in first],
                         [item["tracking_id"] for item in second])

    def test_non_drone_model_labels_are_not_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                load_settings().models[0],
                path=Path(directory) / "missing.pt",
                enabled=True,
            )
            engine = DetectionEngine((config,))
            with self.assertRaisesRegex(FileNotFoundError, "close_range"):
                engine.infer(np.zeros((16, 16, 3), dtype=np.uint8))


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def detection(tracking_id: str = "1") -> dict:
        return {
            "label": "drone",
            "confidence": 0.8,
            "box": [10, 20, 60, 80],
            "model_sources": ["close_range", "long_range"],
            "tracking_id": tracking_id,
        }

    def test_observations_update_stable_encounter_and_history(self) -> None:
        started = datetime.now(timezone.utc)
        first, _ = self.repository.record_observation(
            "webcam-1", self.detection(), observed_at=started
        )
        second, _ = self.repository.record_observation(
            "webcam-1", self.detection(), observed_at=started + timedelta(seconds=3)
        )
        self.assertEqual(first["encounter_id"], second["encounter_id"])
        self.assertEqual(second["observation_count"], 2)
        self.assertAlmostEqual(second["duration_seconds"], 3)
        self.assertEqual(second["object_class"], "drone")
        self.assertEqual(second["model_sources"], ["close_range", "long_range"])
        self.assertTrue(self.repository.risk_history(second["encounter_id"]))

    def test_encounters_page_filter_acknowledge_resolve_and_csv(self) -> None:
        item, _ = self.repository.record_observation("camera-a", self.detection())
        page = self.repository.list_encounters(limit=1, risk_level="low")
        self.assertEqual(page["total"], 1)
        self.assertTrue(self.repository.export_csv().startswith("encounter_id,"))
        acknowledged = self.repository.update_encounter_status(
            item["encounter_id"], acknowledge=True
        )
        self.assertTrue(acknowledged["acknowledged"])
        resolved = self.repository.update_encounter_status(item["encounter_id"], resolve=True)
        self.assertTrue(resolved["resolved"])

    def test_sensor_event_deduplicates_and_requires_explicit_encounter_correlation(self) -> None:
        item, _ = self.repository.record_observation("camera-a", self.detection())
        event = {
            "event_id": "evt-1",
            "sensor_type": "fc51_zone_1",
            "state": "active",
            "device_id": "esp32",
            "encounter_id": item["encounter_id"],
        }
        self.assertTrue(self.repository.save_sensor_event(event))
        self.assertFalse(self.repository.save_sensor_event(event))
        next_item, _ = self.repository.record_observation("camera-a", self.detection())
        self.assertIn("evt-1", next_item["sensor_event_ids"])
        self.assertEqual(next_item["risk_components"]["sensor_corroboration"], 10)

    def test_outbox_is_idempotent_and_sync_worker_marks_success(self) -> None:
        self.repository.record_observation("camera-a", self.detection())

        class Document:
            def set(self, payload: dict, merge: bool = False) -> None:
                self_payload.append((payload, merge))

        class Collection:
            def document(self, document_id: str) -> Document:
                return Document()

        class Client:
            def collection(self, name: str) -> Collection:
                return Collection()

        self_payload: list = []
        worker = FirestoreOutboxWorker(self.repository, Client())
        self.assertGreater(worker.flush_once(), 0)
        self.assertTrue(self_payload)
        self.assertGreater(self.repository.outbox_counts()["synced"], 0)

    def test_failed_outbox_delivery_is_recorded_without_raising(self) -> None:
        self.repository.record_observation("camera-a", self.detection())

        class Collection:
            def document(self, document_id: str):
                return self

            def set(self, payload: dict, merge: bool = False) -> None:
                raise OSError("offline")

        class Client:
            def collection(self, name: str) -> Collection:
                return Collection()

        worker = FirestoreOutboxWorker(self.repository, Client())
        worker.flush_once()
        self.assertEqual(worker.status, "degraded")
        self.assertGreater(self.repository.outbox_counts()["failed"], 0)

    def test_high_risk_alert_has_cooldown_and_queues_fixed_outputs(self) -> None:
        path = Path(self.temp.name) / "alerts.sqlite3"
        from skysentinel.risk import RiskConfig
        repository = Repository(
            path, high_risk_threshold=10, alert_cooldown_seconds=300,
            risk_config=RiskConfig(medium_threshold=5, high_threshold=10),
        )
        detection = {**self.detection(), "confidence": 0.9}
        repository.record_observation("camera-a", detection)
        repository.record_observation("camera-a", detection)
        self.assertEqual(len(repository.recent_alerts()), 1)
        commands = repository.recent_hardware_commands()
        self.assertEqual({command["command"] for command in commands},
                         {"buzzer_brief", "red_led_on"})
        self.assertTrue(all(command["status"] == "pending" for command in commands))
        self.assertEqual(repository.recent_alerts()[0]["delivery_status"], "pending")
        for command in commands:
            self.assertTrue(repository.acknowledge_command(command["command_id"]))
        self.assertEqual(repository.recent_alerts()[0]["delivery_status"], "delivered")

    def test_risk_escalation_bypasses_existing_alert_cooldown(self) -> None:
        from skysentinel.risk import RiskConfig
        path = Path(self.temp.name) / "escalation.sqlite3"
        repository = Repository(
            path,
            high_risk_threshold=20,
            alert_cooldown_seconds=300,
            risk_config=RiskConfig(),
        )
        started = datetime.now(timezone.utc)
        for confidence, elapsed in ((0.5, 0), (0.8, 3), (0.9, 6)):
            repository.record_observation(
                "camera-a",
                {**self.detection(), "confidence": confidence},
                observed_at=started + timedelta(seconds=elapsed),
            )
        alerts = repository.recent_alerts()
        self.assertEqual(len(alerts), 2)
        self.assertIn("risk_escalation", {alert["alert_type"] for alert in alerts})


class FirebaseTests(unittest.TestCase):
    def test_firebase_client_initialization_is_optional_and_reuses_admin_app(self) -> None:
        settings = replace(
            load_settings(),
            firebase_enabled=True,
            firebase_project_id="test-project",
            firebase_credentials=None,
        )
        firebase_app = object()
        fake_admin = Mock()
        fake_admin.get_app.side_effect = ValueError("not initialized")
        fake_admin.initialize_app.return_value = firebase_app
        fake_client = object()
        fake_admin.firestore = SimpleNamespace(client=Mock(return_value=fake_client))
        with patch.dict(sys.modules, {"firebase_admin": fake_admin}):
            client, status = initialize_firestore(settings)
        self.assertIs(client, fake_client)
        self.assertEqual(status, "configured")
        fake_admin.initialize_app.assert_called_once()
        fake_admin.firestore.client.assert_called_once_with(app=firebase_app)

    def test_firebase_disabled_does_not_initialize_sdk(self) -> None:
        settings = replace(load_settings(), firebase_enabled=False)
        client, status = initialize_firestore(settings)
        self.assertIsNone(client)
        self.assertEqual(status, "disabled")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        settings = replace(
            load_settings(),
            database_path=Path(self.temp.name) / "api.sqlite3",
            firebase_enabled=False,
            sensor_token="test-secret",
            sensor_simulation=True,
        )
        self.app = create_app(
            settings,
            repository=Repository(settings.database_path),
            detector=DummyDetector(),
            start_workers=False,
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sensor_api_requires_shared_token_and_deduplicates(self) -> None:
        payload = {
            "event_id": "event-1", "sensor_type": "fc51_zone_1",
            "state": "active", "device_id": "esp32",
        }
        self.assertEqual(self.client.post("/api/sensors/events", json=payload).status_code, 401)
        headers = {"Authorization": "Bearer invalid"}
        self.assertEqual(self.client.post(
            "/api/sensors/events", json=payload, headers=headers
        ).status_code, 401)
        self.assertEqual(self.client.get("/api/hardware/commands").status_code, 401)
        headers["Authorization"] = "Bearer test-secret"
        self.assertEqual(self.client.post(
            "/api/sensors/events", json=payload, headers=headers
        ).status_code, 200)
        duplicate = self.client.post(
            "/api/sensors/events", json=payload, headers=headers
        ).get_json()
        self.assertTrue(duplicate["duplicate"])
        heartbeat = {
            "event_id": "heartbeat-1", "sensor_type": "heartbeat",
            "state": "online", "device_id": "esp32",
        }
        self.assertFalse(self.client.post(
            "/api/sensors/events", json=heartbeat, headers=headers
        ).get_json()["duplicate"])
        self.assertTrue(self.client.post(
            "/api/sensors/events", json=heartbeat, headers=headers
        ).get_json()["duplicate"])

    def test_sensor_validation_and_remote_controls(self) -> None:
        bad = self.client.post(
            "/api/sensors/events",
            json={"event_id": "x", "sensor_type": "camera", "state": "active"},
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(bad.status_code, 400)
        remote = self.app.test_client()
        response = remote.post(
            "/api/hardware/alerts/test",
            json={"command": "buzzer_brief"},
            environ_base={"REMOTE_ADDR": "192.168.1.8"},
        )
        self.assertEqual(response.status_code, 401)

    def test_status_does_not_expose_shared_token(self) -> None:
        response = self.client.get("/api/dashboard/status")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-secret", response.get_data(as_text=True))

    def test_missing_model_weights_return_service_unavailable(self) -> None:
        base = load_settings()
        missing_models = tuple(
            replace(model, path=Path(self.temp.name) / f"{model.key}.missing")
            for model in base.models
        )
        settings = replace(
            base,
            database_path=Path(self.temp.name) / "missing-model.sqlite3",
            firebase_enabled=False,
            models=missing_models,
        )
        missing_app = create_app(settings, start_workers=False)
        ok, encoded = __import__("cv2").imencode(
            ".jpg", np.zeros((16, 16, 3), dtype=np.uint8)
        )
        self.assertTrue(ok)
        import base64
        payload = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
        response = missing_app.test_client().post("/api/detect", json={"image": payload})
        self.assertEqual(response.status_code, 503)

    def test_sensor_simulation_and_fixed_hardware_commands(self) -> None:
        simulated = self.client.post(
            "/api/sensors/simulate",
            json={"sensor_type": "fc51_zone_1", "state": "active"},
        )
        self.assertEqual(simulated.status_code, 201)
        invalid = self.client.post(
            "/api/hardware/alerts/test", json={"command": "gpio_99_on"}
        )
        self.assertEqual(invalid.status_code, 400)
        command = self.client.post(
            "/api/hardware/alerts/test", json={"command": "buzzer_brief"}
        )
        self.assertEqual(command.status_code, 200)
        self.assertEqual(command.get_json()["delivery_status"], "simulated")


if __name__ == "__main__":
    unittest.main()
