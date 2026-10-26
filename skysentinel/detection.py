from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .config import ModelConfig
from .tracking import intersection_over_union


def merge_detections(
    detections: list[dict], iou_threshold: float = 0.55
) -> list[dict]:
    """Suppress overlapping duplicate boxes while retaining distinct drones."""
    merged: list[dict] = []
    for detection in sorted(detections, key=lambda item: item["confidence"], reverse=True):
        duplicate = next(
            (
                item for item in merged
                if intersection_over_union(item["box"], detection["box"]) >= iou_threshold
            ),
            None,
        )
        if duplicate:
            duplicate["model_sources"] = sorted(
                set(duplicate["model_sources"]) | set(detection["model_sources"])
            )
            duplicate["confidence"] = max(duplicate["confidence"], detection["confidence"])
        else:
            merged.append({**detection, "model_sources": list(detection["model_sources"])})
    return merged


class DetectionEngine:
    def __init__(self, configs: tuple[ModelConfig, ...], iou_threshold: float = 0.55) -> None:
        self.configs = configs
        self.iou_threshold = iou_threshold
        self._models: dict[str, Any] = {}
        self._lock = threading.RLock()

    def model_status(self) -> list[dict]:
        return [
            {
                "key": config.key,
                "role": config.role,
                "enabled": config.enabled,
                "available": config.path.is_file(),
                "path": config.path.name,
                "source_model": config.source_model,
                "confidence_threshold": config.confidence,
                "image_size": config.image_size,
                "supported_image_types": list(config.supported_image_types),
            }
            for config in self.configs
        ]

    def _get_models(self) -> dict[str, Any]:
        with self._lock:
            for config in self.configs:
                if not config.enabled or config.key in self._models:
                    continue
                if not config.path.is_file():
                    continue
                from ultralytics import YOLO
                model = YOLO(str(config.path))
                labels = {str(label).lower() for label in model.names.values()}
                if labels != {"drone"}:
                    raise ValueError(
                        f"Configured weights for {config.key} are not drone-only; "
                        "only a single 'drone' class is supported."
                    )
                self._models[config.key] = model
            missing = [
                config.key for config in self.configs
                if config.enabled and not config.path.is_file()
            ]
            if missing:
                raise FileNotFoundError("Missing enabled model weights: " + ", ".join(missing))
            return dict(self._models)

    def infer(self, frame: np.ndarray, frame_type: str = "visible_rgb") -> tuple[np.ndarray, dict]:
        started = time.perf_counter()
        predictions: list[dict] = []
        model_scores: dict[str, float] = {}
        with self._lock:
            loaded = self._get_models()
            eligible = [
                config for config in self.configs
                if config.enabled and frame_type in config.supported_image_types
            ]
            if not eligible:
                raise ValueError(f"No enabled model supports frame type '{frame_type}'.")
            for config in self.configs:
                if not config.enabled:
                    model_scores[config.key] = 0.0
                    continue
                if frame_type not in config.supported_image_types:
                    model_scores[config.key] = 0.0
                    continue
                model = loaded[config.key]
                result = model.predict(
                    source=frame,
                    imgsz=config.image_size,
                    conf=config.confidence,
                    verbose=False,
                )[0]
                for box in result.boxes:
                    class_id = int(box.cls[0])
                    label = result.names[class_id]
                    if str(label).lower() != "drone":
                        continue
                    confidence = float(box.conf[0])
                    predictions.append({
                        "label": "drone",
                        "confidence": confidence,
                        "box": [round(float(value), 1) for value in box.xyxy[0].tolist()],
                        "model_sources": [config.key],
                        "source_model_metadata": config.source_model,
                    })
                model_scores[config.key] = max(
                    (
                        prediction["confidence"] for prediction in predictions
                        if config.key in prediction["model_sources"]
                    ),
                    default=0.0,
                )

        detections = merge_detections(predictions, self.iou_threshold)
        model_metadata = {config.key: config.source_model for config in self.configs}
        for detection in detections:
            detection["source_model_metadata"] = {
                key: model_metadata[key] for key in detection["model_sources"]
            }
        annotated = frame.copy()
        for detection in detections:
            x1, y1, x2, y2 = [int(round(value)) for value in detection["box"]]
            sources = ",".join(detection["model_sources"])
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (60, 235, 175), 2)
            cv2.putText(
                annotated,
                f"drone {detection['confidence']:.0%} [{sources}]",
                (x1, max(20, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (60, 235, 175),
                2,
                cv2.LINE_AA,
            )
        return annotated, {
            "winner": max(model_scores, key=model_scores.get) if model_scores else None,
            "confidence": max(model_scores.values(), default=0.0),
            "scores": model_scores,
            "detections": detections,
            "inference_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
