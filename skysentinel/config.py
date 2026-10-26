from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .risk import RiskConfig, load_risk_config

ROOT = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ModelConfig:
    key: str
    path: Path
    enabled: bool
    confidence: float
    image_size: int
    supported_image_types: tuple[str, ...]
    source_model: str
    role: str


@dataclass(frozen=True)
class Settings:
    database_path: Path
    evidence_dir: Path
    models: tuple[ModelConfig, ...]
    frame_type: str
    iou_threshold: float
    encounter_timeout_seconds: int
    high_risk_threshold: int
    risk_config: RiskConfig
    alert_cooldown_seconds: int
    hardware_command_cooldown_seconds: int
    sensor_token: str
    sensor_simulation: bool
    firebase_project_id: str | None
    firebase_credentials: Path | None
    firebase_enabled: bool
    firebase_poll_seconds: float
    host: str
    port: int


def load_settings() -> Settings:
    credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    configured_project = os.environ.get("FIREBASE_PROJECT_ID")
    models = (
        ModelConfig(
            key="close_range",
            path=Path(os.environ.get("SKY_CLOSE_MODEL", ROOT / "best.pt")),
            enabled=_bool_env("SKY_CLOSE_MODEL_ENABLED", True),
            confidence=float(os.environ.get("SKY_CLOSE_CONFIDENCE", "0.25")),
            image_size=int(os.environ.get("SKY_CLOSE_IMAGE_SIZE", "640")),
            supported_image_types=("visible_rgb",),
            source_model="YOLO11n drone-only weights (best.pt)",
            role="close_range",
        ),
        ModelConfig(
            key="long_range",
            path=Path(os.environ.get("SKY_LONG_MODEL", ROOT / "best1.pt")),
            enabled=_bool_env("SKY_LONG_MODEL_ENABLED", True),
            confidence=float(os.environ.get("SKY_LONG_CONFIDENCE", "0.25")),
            image_size=int(os.environ.get("SKY_LONG_IMAGE_SIZE", "640")),
            supported_image_types=("visible_rgb",),
            source_model="YOLO11n drone-only weights (best1.pt)",
            role="long_range",
        ),
    )
    for model in models:
        if not 0 <= model.confidence <= 1:
            raise ValueError(f"{model.key} confidence must be between 0 and 1")
        if model.image_size < 32:
            raise ValueError(f"{model.key} image size must be at least 32")

    iou_threshold = float(os.environ.get("SKY_MERGE_IOU", "0.55"))
    high_risk_threshold = int(os.environ.get("SKY_ALERT_THRESHOLD", "60"))
    if not 0 <= iou_threshold <= 1:
        raise ValueError("SKY_MERGE_IOU must be between 0 and 1")
    if not 0 <= high_risk_threshold <= 100:
        raise ValueError("SKY_ALERT_THRESHOLD must be between 0 and 100")
    return Settings(
        database_path=Path(os.environ.get("SKY_DATABASE_PATH", ROOT / "data" / "skysentinel.sqlite3")),
        evidence_dir=Path(os.environ.get("SKY_EVIDENCE_DIR", ROOT / "evidence")),
        models=models,
        frame_type=os.environ.get("SKY_FRAME_TYPE", "visible_rgb"),
        iou_threshold=iou_threshold,
        encounter_timeout_seconds=int(os.environ.get("SKY_ENCOUNTER_TIMEOUT", "30")),
        high_risk_threshold=high_risk_threshold,
        risk_config=load_risk_config(),
        alert_cooldown_seconds=int(os.environ.get("SKY_ALERT_COOLDOWN", "300")),
        hardware_command_cooldown_seconds=int(
            os.environ.get("SKY_HARDWARE_COMMAND_COOLDOWN", "3")
        ),
        sensor_token=os.environ.get("SKY_SENSOR_TOKEN", ""),
        sensor_simulation=_bool_env("SKY_SENSOR_SIMULATION", False),
        firebase_project_id=configured_project,
        firebase_credentials=Path(credentials) if credentials else None,
        firebase_enabled=_bool_env("SKY_FIREBASE_ENABLED", True),
        firebase_poll_seconds=max(1.0, float(os.environ.get("SKY_FIREBASE_POLL_SECONDS", "5"))),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
