from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RiskConfig:
    confidence_max_points: float = 20.0
    persistence_points: tuple[float, float, float, float, float] = (0, 5, 12, 18, 25)
    proximity_far_points: float = 5.0
    proximity_medium_points: float = 15.0
    proximity_near_points: float = 30.0
    motion_points: float = 15.0
    sensor_points: float = 10.0
    medium_threshold: int = 30
    high_threshold: int = 60

    def __post_init__(self) -> None:
        if len(self.persistence_points) != 5:
            raise ValueError("Exactly five persistence score values are required.")
        point_values = (
            self.confidence_max_points,
            *self.persistence_points,
            self.proximity_far_points,
            self.proximity_medium_points,
            self.proximity_near_points,
            self.motion_points,
            self.sensor_points,
        )
        if any(value < 0 for value in point_values):
            raise ValueError("Risk component points cannot be negative.")
        if not 0 <= self.medium_threshold < self.high_threshold <= 100:
            raise ValueError("Risk thresholds must satisfy 0 <= medium < high <= 100.")


def load_risk_config() -> RiskConfig:
    values = RiskConfig(
        confidence_max_points=float(os.environ.get("SKY_RISK_CONFIDENCE_POINTS", "20")),
        persistence_points=(
            float(os.environ.get("SKY_RISK_PERSISTENCE_UNDER_2", "0")),
            float(os.environ.get("SKY_RISK_PERSISTENCE_2_TO_5", "5")),
            float(os.environ.get("SKY_RISK_PERSISTENCE_5_TO_15", "12")),
            float(os.environ.get("SKY_RISK_PERSISTENCE_15_TO_30", "18")),
            float(os.environ.get("SKY_RISK_PERSISTENCE_OVER_30", "25")),
        ),
        proximity_far_points=float(os.environ.get("SKY_RISK_PROXIMITY_FAR", "5")),
        proximity_medium_points=float(os.environ.get("SKY_RISK_PROXIMITY_MEDIUM", "15")),
        proximity_near_points=float(os.environ.get("SKY_RISK_PROXIMITY_NEAR", "30")),
        motion_points=float(os.environ.get("SKY_RISK_MOTION_POINTS", "15")),
        sensor_points=float(os.environ.get("SKY_RISK_SENSOR_POINTS", "10")),
        medium_threshold=int(os.environ.get("SKY_RISK_MEDIUM_THRESHOLD", "30")),
        high_threshold=int(os.environ.get("SKY_RISK_HIGH_THRESHOLD", "60")),
    )
    return values


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    level: str
    components: dict[str, float]
    reasons: list[str]
    missing_information: list[str]
    assessed_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def assess_risk(
    confidence: float,
    duration_seconds: float,
    *,
    proximity: str | None = None,
    motion_validated: bool = False,
    correlated_sensor: bool = False,
    high_threshold: int | None = None,
    config: RiskConfig | None = None,
) -> RiskAssessment:
    configuration = config or RiskConfig()
    high_band = high_threshold if high_threshold is not None else configuration.high_threshold
    if not 0 <= configuration.medium_threshold < high_band <= 100:
        raise ValueError("Risk thresholds must satisfy 0 <= medium < high <= 100.")

    confidence_points = configuration.confidence_max_points * max(0.0, min(1.0, confidence))
    if duration_seconds < 2:
        persistence_points = configuration.persistence_points[0]
    elif duration_seconds < 5:
        persistence_points = configuration.persistence_points[1]
    elif duration_seconds < 15:
        persistence_points = configuration.persistence_points[2]
    elif duration_seconds < 30:
        persistence_points = configuration.persistence_points[3]
    else:
        persistence_points = configuration.persistence_points[4]

    proximity_points = {
        "far": configuration.proximity_far_points,
        "medium": configuration.proximity_medium_points,
        "near": configuration.proximity_near_points,
    }.get(proximity or "", 0.0)
    motion_points = configuration.motion_points if motion_validated else 0.0
    sensor_points = configuration.sensor_points if correlated_sensor else 0.0
    components = {
        "confidence": round(confidence_points, 2),
        "persistence": float(persistence_points),
        "proximity": float(proximity_points),
        "motion": float(motion_points),
        "sensor_corroboration": float(sensor_points),
    }
    score = min(100, max(0, round(sum(components.values()))))
    level = "high" if score >= high_band else (
        "medium" if score >= configuration.medium_threshold else "low"
    )

    reasons = []
    if confidence_points:
        reasons.append(
            f"Drone detection confidence contributed {confidence_points:.1f}/"
            f"{configuration.confidence_max_points:g} points."
        )
    if persistence_points:
        reasons.append(f"Encounter persistence contributed {persistence_points:g} points.")
    if proximity_points:
        reasons.append(f"Calibrated {proximity} proximity contributed {proximity_points:g} points.")
    if motion_points:
        reasons.append(f"Validated motion evidence contributed {motion_points:g} points.")
    if sensor_points:
        reasons.append(f"Recent correlated sensor evidence contributed {sensor_points:g} points.")
    missing = []
    if proximity is None:
        missing.append("No calibrated distance measurement is available.")
    if not correlated_sensor:
        missing.append("No recent timestamped sensor event is correlated with this encounter.")
    if not motion_validated:
        missing.append("No independently validated motion evidence is available.")

    return RiskAssessment(
        score=score,
        level=level,
        components=components,
        reasons=reasons,
        missing_information=missing,
        assessed_at=datetime.now(timezone.utc).isoformat(),
    )
