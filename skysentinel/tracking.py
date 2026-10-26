from __future__ import annotations

import time


def intersection_over_union(left: list[float], right: list[float]) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


class IoUTracker:
    """Small dependency-free tracker; IDs remain stable while boxes overlap."""

    def __init__(self, iou_threshold: float = 0.25, max_age_seconds: float = 2.0) -> None:
        self.iou_threshold = iou_threshold
        self.max_age_seconds = max_age_seconds
        self._next_id = 1
        self._tracks: dict[int, tuple[list[float], float]] = {}

    def update(self, detections: list[dict], now: float | None = None) -> list[dict]:
        timestamp = time.monotonic() if now is None else now
        available = {
            track_id: item for track_id, item in self._tracks.items()
            if timestamp - item[1] <= self.max_age_seconds
        }
        candidates = sorted(
            (
                (intersection_over_union(detection["box"], item[0]), index, track_id)
                for index, detection in enumerate(detections)
                for track_id, item in available.items()
            ),
            reverse=True,
        )
        assignments: dict[int, int] = {}
        used_tracks: set[int] = set()
        for score, index, track_id in candidates:
            if score < self.iou_threshold:
                break
            if index not in assignments and track_id not in used_tracks:
                assignments[index] = track_id
                used_tracks.add(track_id)

        output = []
        for index, detection in enumerate(detections):
            track_id = assignments.get(index)
            if track_id is None:
                track_id = self._next_id
                self._next_id += 1
            box = [float(value) for value in detection["box"]]
            self._tracks[track_id] = (box, timestamp)
            output.append({**detection, "tracking_id": str(track_id)})

        self._tracks = {
            track_id: item for track_id, item in self._tracks.items()
            if timestamp - item[1] <= self.max_age_seconds
        }
        return output
