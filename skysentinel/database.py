from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .risk import RiskConfig, assess_risk


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


class Repository:
    """SQLite source of truth and durable Firestore synchronization outbox."""

    def __init__(self, path: Path | str, *, high_risk_threshold: int = 60,
                 alert_cooldown_seconds: int = 300,
                 risk_config: RiskConfig | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.high_risk_threshold = high_risk_threshold
        self.risk_config = risk_config or RiskConfig(high_threshold=high_risk_threshold)
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS encounters (
                    encounter_id TEXT PRIMARY KEY,
                    object_class TEXT NOT NULL DEFAULT 'drone',
                    tracking_id TEXT,
                    source_id TEXT NOT NULL,
                    camera_id TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    model_sources TEXT NOT NULL,
                    latest_confidence REAL NOT NULL,
                    maximum_confidence REAL NOT NULL,
                    observation_count INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    bounding_box TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_components TEXT NOT NULL,
                    risk_reasons TEXT NOT NULL,
                    risk_missing TEXT NOT NULL,
                    alert_status TEXT NOT NULL DEFAULT 'none',
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    evidence_image TEXT,
                    sensor_event_ids TEXT NOT NULL DEFAULT '[]',
                    cloud_sync_status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_encounters_last_seen ON encounters(last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_encounters_source_tracking ON encounters(source_id, tracking_id, last_seen);
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    encounter_id TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    FOREIGN KEY(encounter_id) REFERENCES encounters(encounter_id)
                );
                CREATE TABLE IF NOT EXISTS risk_history (
                    history_id TEXT PRIMARY KEY,
                    encounter_id TEXT NOT NULL,
                    previous_score INTEGER,
                    current_score INTEGER NOT NULL,
                    previous_level TEXT,
                    current_level TEXT NOT NULL,
                    components TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(encounter_id) REFERENCES encounters(encounter_id)
                );
                CREATE TABLE IF NOT EXISTS sensor_events (
                    event_id TEXT PRIMARY KEY,
                    sensor_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    device_timestamp TEXT,
                    received_at TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    correlated_encounter_id TEXT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    last_seen TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sensor_states TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(collection, document_id)
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_attempt);
                CREATE TABLE IF NOT EXISTS hardware_commands (
                    command_id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at TEXT,
                    simulation INTEGER NOT NULL DEFAULT 0,
                    alert_id TEXT
                );
            """)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(hardware_commands)")
            }
            if "alert_id" not in columns:
                db.execute("ALTER TABLE hardware_commands ADD COLUMN alert_id TEXT")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    def _queue(self, db: sqlite3.Connection, collection: str, document_id: str, payload: dict) -> None:
        db.execute(
            """INSERT INTO outbox(outbox_id, collection, document_id, payload)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(collection, document_id) DO UPDATE SET
                 payload=excluded.payload, status='pending', attempts=0,
                 next_attempt=0, last_error=NULL, outbox_id=excluded.outbox_id""",
            (str(uuid.uuid4()), collection, document_id, self._json(payload)),
        )

    @staticmethod
    def _encounter_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("model_sources", "bounding_box", "risk_components", "risk_reasons",
                    "risk_missing", "sensor_event_ids"):
            item[key] = json.loads(item[key])
        item["acknowledged"] = bool(item["acknowledged"])
        item["resolved"] = bool(item["resolved"])
        return item

    def record_observation(self, source_id: str, detection: dict, *,
                           timeout_seconds: int = 30, observed_at: datetime | None = None,
                           evidence_image: str | None = None,
                           simulate_hardware: bool = False) -> tuple[dict, dict | None]:
        now = observed_at or utc_now()
        stamp = utc_text(now)
        tracking_id = str(detection.get("tracking_id", ""))
        confidence = max(0.0, min(1.0, float(detection["confidence"])))
        box = [round(float(value), 1) for value in detection["box"]]
        model_sources = sorted(set(detection.get("model_sources", [])))
        with self._lock, self._connect() as db:
            previous_row = db.execute(
                """SELECT * FROM encounters
                   WHERE source_id=? AND tracking_id=? AND resolved=0
                     AND last_seen >= ?
                   ORDER BY last_seen DESC LIMIT 1""",
                (source_id, tracking_id, utc_text(now - timedelta(seconds=timeout_seconds))),
            ).fetchone()
            created = previous_row is None
            if created:
                encounter_id = str(uuid.uuid4())
                first_seen = stamp
                observations = 1
                maximum = confidence
                previous_score = None
                previous_level = None
                prior = None
            else:
                prior = self._encounter_dict(previous_row)
                encounter_id = prior["encounter_id"]
                first_seen = prior["first_seen"]
                observations = prior["observation_count"] + 1
                maximum = max(prior["maximum_confidence"], confidence)
                previous_score = prior["risk_score"]
                previous_level = prior["risk_level"]
            correlated_rows = db.execute(
                """SELECT event_id FROM sensor_events WHERE correlated_encounter_id=?
                   AND state='active' AND received_at >= ?
                   ORDER BY received_at DESC LIMIT 20""",
                (encounter_id, utc_text(now - timedelta(seconds=60))),
            ).fetchall()
            correlated_event_ids = [row["event_id"] for row in correlated_rows]
            duration = max(0.0, (now - datetime.fromisoformat(first_seen)).total_seconds())
            risk = assess_risk(
                confidence,
                duration,
                motion_validated=observations >= 3,
                correlated_sensor=bool(correlated_event_ids),
                config=self.risk_config,
            )
            risk_data = risk.as_dict()
            correlated_events = correlated_event_ids
            db.execute(
                """INSERT INTO encounters(
                    encounter_id, object_class, tracking_id, source_id, camera_id,
                    first_seen, last_seen, model_sources, latest_confidence,
                    maximum_confidence, observation_count, duration_seconds, bounding_box,
                    risk_score, risk_level, risk_components, risk_reasons, risk_missing,
                    evidence_image, sensor_event_ids, cloud_sync_status
                ) VALUES (?, 'drone', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(encounter_id) DO UPDATE SET
                    last_seen=excluded.last_seen, model_sources=excluded.model_sources,
                    latest_confidence=excluded.latest_confidence,
                    maximum_confidence=excluded.maximum_confidence,
                    observation_count=excluded.observation_count,
                    duration_seconds=excluded.duration_seconds,
                    bounding_box=excluded.bounding_box, risk_score=excluded.risk_score,
                    risk_level=excluded.risk_level, risk_components=excluded.risk_components,
                    risk_reasons=excluded.risk_reasons, risk_missing=excluded.risk_missing,
                    sensor_event_ids=excluded.sensor_event_ids,
                    evidence_image=COALESCE(encounters.evidence_image, excluded.evidence_image),
                    cloud_sync_status='pending'""",
                (
                    encounter_id, tracking_id, source_id, source_id, first_seen, stamp,
                    self._json(model_sources), confidence, maximum, observations, duration,
                    self._json(box), risk.score, risk.level, self._json(risk.components),
                    self._json(risk.reasons), self._json(risk.missing_information),
                    evidence_image if created else None, self._json(correlated_events),
                ),
            )
            current_row = db.execute(
                "SELECT * FROM encounters WHERE encounter_id=?", (encounter_id,)
            ).fetchone()
            encounter = self._encounter_dict(current_row)
            if created or previous_score is None or abs(risk.score - previous_score) >= 5 \
                    or risk.level != previous_level:
                db.execute(
                    """INSERT INTO risk_history(history_id, encounter_id, previous_score,
                       current_score, previous_level, current_level, components, explanation, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), encounter_id, previous_score, risk.score,
                        previous_level, risk.level, self._json(risk.components),
                        self._json(risk.reasons), stamp,
                    ),
                )
                self._queue(db, "risk_history", f"{encounter_id}-{stamp}", {
                    "encounter_id": encounter_id, "previous_score": previous_score,
                    "current_score": risk.score, "previous_level": previous_level,
                    "current_level": risk.level, "components": risk.components,
                    "explanation": risk.reasons, "timestamp": stamp,
                })
            existing_alert = db.execute(
                "SELECT timestamp FROM alerts WHERE encounter_id=? ORDER BY timestamp DESC LIMIT 1",
                (encounter_id,),
            ).fetchone()
            levels = {"low": 0, "medium": 1, "high": 2}
            escalated = previous_level is not None and levels[risk.level] > levels[previous_level]
            if risk.score >= self.high_risk_threshold and (
                existing_alert is None or
                escalated or
                (now - datetime.fromisoformat(existing_alert["timestamp"])).total_seconds()
                >= self.alert_cooldown_seconds
            ):
                alert_id = str(uuid.uuid4())
                alert_type = "risk_escalation" if escalated else "risk_threshold"
                alert_payload = {
                    "alert_id": alert_id, "encounter_id": encounter_id,
                    "alert_type": alert_type, "risk_level": risk.level,
                    "risk_score": risk.score, "timestamp": stamp,
                    "acknowledged": False, "resolved": False, "delivery_status": "pending",
                }
                db.execute(
                    """INSERT INTO alerts(alert_id, encounter_id, alert_type, risk_level,
                       risk_score, timestamp) VALUES (?, ?, ?, ?, ?, ?)""",
                    (alert_id, encounter_id, alert_type, risk.level, risk.score, stamp),
                )
                self._queue(db, "alerts", alert_id, alert_payload)
                db.execute(
                    "UPDATE encounters SET alert_status='active' WHERE encounter_id=?",
                    (encounter_id,),
                )
                for command in ("buzzer_brief", "red_led_on"):
                    db.execute(
                        """INSERT INTO hardware_commands(command_id, command, created_at,
                           status, simulation, alert_id) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            str(uuid.uuid4()), command, stamp,
                            "simulated" if simulate_hardware else "pending",
                            simulate_hardware, alert_id,
                        ),
                    )
                encounter["alert_status"] = "active"

            if created or observations % 10 == 0 or risk.level != previous_level:
                self._queue(db, "encounters", encounter_id, encounter)
            return encounter, risk_data if created or risk.level != previous_level else None

    def get_encounter(self, encounter_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM encounters WHERE encounter_id=?", (encounter_id,)
            ).fetchone()
        return self._encounter_dict(row) if row else None

    def list_encounters(self, *, limit: int = 50, offset: int = 0,
                        risk_level: str | None = None, source_id: str | None = None,
                        include_resolved: bool = True) -> dict:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        conditions, params = [], []
        if risk_level:
            conditions.append("risk_level=?")
            params.append(risk_level.lower())
        if source_id:
            conditions.append("source_id=?")
            params.append(source_id)
        if not include_resolved:
            conditions.append("resolved=0")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM encounters{where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM encounters{where} ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"items": [self._encounter_dict(row) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def risk_history(self, encounter_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM risk_history WHERE encounter_id=? ORDER BY timestamp DESC LIMIT 100",
                (encounter_id,),
            ).fetchall()
        return [
            {**dict(row), "components": json.loads(row["components"]),
             "explanation": json.loads(row["explanation"])}
            for row in rows
        ]

    def update_encounter_status(self, encounter_id: str, *, acknowledge: bool = False,
                                resolve: bool = False) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM encounters WHERE encounter_id=?", (encounter_id,)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE encounters SET acknowledged=CASE WHEN ? THEN 1 ELSE acknowledged END,
                   resolved=CASE WHEN ? THEN 1 ELSE resolved END,
                   alert_status=CASE WHEN ? THEN 'resolved' WHEN ? THEN 'acknowledged'
                   ELSE alert_status END WHERE encounter_id=?""",
                (acknowledge, resolve, resolve, acknowledge, encounter_id),
            )
            db.execute(
                """UPDATE alerts SET acknowledged=CASE WHEN ? THEN 1 ELSE acknowledged END,
                   resolved=CASE WHEN ? THEN 1 ELSE resolved END WHERE encounter_id=?""",
                (acknowledge, resolve, encounter_id),
            )
            updated = self._encounter_dict(db.execute(
                "SELECT * FROM encounters WHERE encounter_id=?", (encounter_id,)
            ).fetchone())
            self._queue(db, "encounters", encounter_id, updated)
            for alert in db.execute("SELECT * FROM alerts WHERE encounter_id=?", (encounter_id,)):
                self._queue(db, "alerts", alert["alert_id"], dict(alert))
            return updated

    def export_csv(self, *, risk_level: str | None = None, source_id: str | None = None) -> str:
        conditions, params = [], []
        if risk_level:
            conditions.append("risk_level=?")
            params.append(risk_level)
        if source_id:
            conditions.append("source_id=?")
            params.append(source_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM encounters{where} ORDER BY last_seen DESC", params
            ).fetchall()
        items = [self._encounter_dict(row) for row in rows]
        output = io.StringIO()
        fields = [
            "encounter_id", "object_class", "tracking_id", "source_id", "camera_id",
            "first_seen", "last_seen", "model_sources", "latest_confidence",
            "maximum_confidence", "observation_count", "duration_seconds", "bounding_box",
            "risk_score", "risk_level", "risk_reasons", "alert_status", "acknowledged",
            "resolved", "cloud_sync_status",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({key: json.dumps(item[key]) if isinstance(item[key], (list, dict))
                             else item[key] for key in fields})
        return output.getvalue()

    def save_sensor_event(self, event: dict) -> bool:
        with self._lock, self._connect() as db:
            correlated_id = event.get("encounter_id")
            if correlated_id:
                encounter = db.execute(
                    "SELECT encounter_id FROM encounters WHERE encounter_id=? AND resolved=0",
                    (correlated_id,),
                ).fetchone()
                if encounter is None:
                    return False
            cursor = db.execute(
                """INSERT OR IGNORE INTO sensor_events(event_id, sensor_type, state,
                   device_timestamp, received_at, device_id, correlated_encounter_id, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_id"], event["sensor_type"], event["state"],
                    event.get("device_timestamp"), utc_text(), event["device_id"],
                    correlated_id, self._json(event),
                ),
            )
            if cursor.rowcount:
                self._queue(db, "sensor_events", event["event_id"], {
                    **event, "received_at": utc_text(),
                    "correlated_encounter_id": correlated_id,
                })
            return bool(cursor.rowcount)

    def update_device(self, device_id: str, sensor_states: dict | None = None) -> None:
        stamp = utc_text()
        with self._connect() as db:
            existing = db.execute(
                "SELECT sensor_states FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
            states = json.loads(existing["sensor_states"]) if existing else {}
            states.update(sensor_states or {})
            db.execute(
                """INSERT INTO devices(device_id, last_seen, status, sensor_states)
                   VALUES (?, ?, 'online', ?)
                   ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen,
                   status='online', sensor_states=excluded.sensor_states""",
                (device_id, stamp, self._json(states)),
            )

    def device_status(self, timeout_seconds: int = 90) -> dict:
        cutoff = utc_text(utc_now() - timedelta(seconds=timeout_seconds))
        with self._connect() as db:
            row = db.execute("SELECT * FROM devices ORDER BY last_seen DESC LIMIT 1").fetchone()
        if not row:
            return {"online": False, "device_id": None, "last_seen": None, "sensor_states": {}}
        return {
            "online": row["last_seen"] >= cutoff,
            "device_id": row["device_id"],
            "last_seen": row["last_seen"],
            "sensor_states": json.loads(row["sensor_states"]),
        }

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            self._refresh_alert_delivery_status(db)
            rows = db.execute(
                "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [dict(row) for row in rows]

    def sensor_status(self) -> dict:
        with self._connect() as db:
            rows = db.execute(
                """SELECT sensor_type, state, MAX(received_at) received_at
                   FROM sensor_events GROUP BY sensor_type"""
            ).fetchall()
        states = {
            row["sensor_type"]: {"state": row["state"], "received_at": row["received_at"]}
            for row in rows if row["sensor_type"] != "heartbeat"
        }
        for index in range(1, 6):
            states.setdefault(f"fc51_zone_{index}", {"state": "unknown", "received_at": None})
        states.setdefault("pir", {"state": "unknown", "received_at": None})
        return states

    def enqueue_command(self, command: str, *, simulation: bool = False) -> str:
        command_id, stamp = str(uuid.uuid4()), utc_text()
        status = "simulated" if simulation else "pending"
        with self._connect() as db:
            db.execute(
                """INSERT INTO hardware_commands(command_id, command, created_at, status, simulation)
                   VALUES (?, ?, ?, ?, ?)""",
                (command_id, command, stamp, status, simulation),
            )
        return command_id

    def hardware_command_recent(self, command: str, seconds: int) -> bool:
        cutoff = utc_text(utc_now() - timedelta(seconds=seconds))
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM hardware_commands WHERE command=? AND created_at>=?
                   ORDER BY created_at DESC LIMIT 1""",
                (command, cutoff),
            ).fetchone()
        return row is not None

    def pending_commands(self, limit: int = 20) -> list[dict]:
        cutoff = utc_text(utc_now() - timedelta(seconds=15))
        with self._connect() as db:
            db.execute(
                """UPDATE hardware_commands SET status='expired'
                   WHERE status='pending' AND created_at < ?""",
                (cutoff,),
            )
            self._refresh_alert_delivery_status(db)
            rows = db.execute(
                """SELECT command_id, command, created_at FROM hardware_commands
                   WHERE status='pending' ORDER BY created_at LIMIT ?""",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_hardware_commands(self, limit: int = 10) -> list[dict]:
        cutoff = utc_text(utc_now() - timedelta(seconds=15))
        with self._connect() as db:
            db.execute(
                """UPDATE hardware_commands SET status='expired'
                   WHERE status='pending' AND created_at < ?""",
                (cutoff,),
            )
            self._refresh_alert_delivery_status(db)
            rows = db.execute(
                """SELECT command_id, command, created_at, status, delivered_at, simulation, alert_id
                   FROM hardware_commands ORDER BY created_at DESC LIMIT ?""",
                (min(max(limit, 1), 50),),
            ).fetchall()
        return [{**dict(row), "simulation": bool(row["simulation"])} for row in rows]

    def acknowledge_command(self, command_id: str, status: str = "delivered") -> bool:
        if status not in {"delivered", "failed"}:
            return False
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT alert_id FROM hardware_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            cursor = db.execute(
                """UPDATE hardware_commands SET status=?, delivered_at=?
                   WHERE command_id=? AND status='pending'""",
                (status, utc_text(), command_id),
            )
            if cursor.rowcount and row and row["alert_id"]:
                self._refresh_alert_delivery_status(db, row["alert_id"])
        return bool(cursor.rowcount)

    @staticmethod
    def _refresh_alert_delivery_status(db: sqlite3.Connection, alert_id: str | None = None) -> None:
        selector = "WHERE alert_id=?" if alert_id else "WHERE alert_id IS NOT NULL"
        params = (alert_id,) if alert_id else ()
        db.execute(
            f"""UPDATE alerts SET delivery_status=CASE
                WHEN EXISTS(SELECT 1 FROM hardware_commands AS hc
                    WHERE hc.alert_id=alerts.alert_id AND hc.status='simulated')
                    THEN 'simulated'
                WHEN EXISTS(SELECT 1 FROM hardware_commands AS hc
                    WHERE hc.alert_id=alerts.alert_id
                    AND hc.status IN ('failed', 'expired')) THEN 'failed'
                WHEN EXISTS(SELECT 1 FROM hardware_commands AS hc
                    WHERE hc.alert_id=alerts.alert_id)
                 AND NOT EXISTS(SELECT 1 FROM hardware_commands AS hc
                    WHERE hc.alert_id=alerts.alert_id AND hc.status!='delivered')
                    THEN 'delivered'
                ELSE 'pending' END {selector}""",
            params,
        )

    def outbox_counts(self) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) count FROM outbox GROUP BY status").fetchall()
        counts = {"pending": 0, "synced": 0, "failed": 0}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts

    def pending_outbox(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM outbox WHERE status IN ('pending', 'failed')
                   AND next_attempt <= ? ORDER BY next_attempt LIMIT ?""",
                (utc_now().timestamp(), min(max(limit, 1), 100)),
            ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def mark_outbox_synced(self, outbox_id: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT collection, document_id FROM outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            db.execute("UPDATE outbox SET status='synced', last_error=NULL WHERE outbox_id=?",
                       (outbox_id,))
            if row and row["collection"] == "encounters":
                db.execute("UPDATE encounters SET cloud_sync_status='synced' WHERE encounter_id=?",
                           (row["document_id"],))

    def mark_outbox_failed(self, outbox_id: str, error: str) -> None:
        safe_error = error[:300]
        with self._connect() as db:
            row = db.execute(
                "SELECT attempts, collection, document_id FROM outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            if not row:
                return
            attempts = row["attempts"] + 1
            delay = min(300, 2 ** min(attempts, 8))
            db.execute(
                """UPDATE outbox SET status='failed', attempts=?, next_attempt=?, last_error=?
                   WHERE outbox_id=?""",
                (attempts, utc_now().timestamp() + delay, safe_error, outbox_id),
            )
            if row["collection"] == "encounters":
                db.execute(
                    "UPDATE encounters SET cloud_sync_status='failed' WHERE encounter_id=?",
                    (row["document_id"],),
                )
