from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import Settings
from .database import Repository

logger = logging.getLogger(__name__)


def initialize_firestore(settings: Settings) -> tuple[Any | None, str]:
    if not settings.firebase_enabled:
        return None, "disabled"
    if settings.firebase_credentials and not settings.firebase_credentials.is_file():
        return None, "credentials_missing"
    if not settings.firebase_credentials and not settings.firebase_project_id:
        return None, "not_configured"
    try:
        import firebase_admin
        from firebase_admin import firestore
    except ImportError:
        return None, "sdk_unavailable"

    try:
        try:
            firebase_app = firebase_admin.get_app()
        except ValueError:
            options = {}
            if settings.firebase_project_id:
                options["projectId"] = settings.firebase_project_id
            firebase_app = firebase_admin.initialize_app(options=options or None)
        return firestore.client(app=firebase_app), "configured"
    except Exception as error:
        logger.warning("Firestore initialization unavailable (%s)", type(error).__name__)
        return None, "unavailable"


class FirestoreOutboxWorker:
    def __init__(self, repository: Repository, client: Any | None,
                 poll_seconds: float = 5.0) -> None:
        self.repository = repository
        self.client = client
        self.poll_seconds = max(1.0, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "configured" if client is not None else "unavailable"

    def start(self) -> None:
        if self.client is None or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._run, name="skysentinel-firestore-outbox", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def flush_once(self) -> int:
        if self.client is None:
            self.status = "unavailable"
            return 0
        sent = 0
        for item in self.repository.pending_outbox():
            try:
                self.client.collection(item["collection"]).document(
                    item["document_id"]
                ).set(item["payload"], merge=True)
                self.repository.mark_outbox_synced(item["outbox_id"])
                self.status = "connected"
                sent += 1
            except Exception as error:
                self.status = "degraded"
                self.repository.mark_outbox_failed(item["outbox_id"], type(error).__name__)
                logger.warning("Firestore outbox delivery failed (%s)", type(error).__name__)
        return sent

    def _run(self) -> None:
        while not self._stop.is_set():
            self.flush_once()
            self._stop.wait(self.poll_seconds)
