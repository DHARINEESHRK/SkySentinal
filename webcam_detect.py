
import cv2
import time
from pathlib import Path
from ultralytics import YOLO

# ---------------- CONFIGURATION ----------------
MODEL_PATH = Path(__file__).parent / "best.pt"
CAMERA_INDEX = 0
IMAGE_SIZE = 640
CONFIDENCE = 0.25

# ---------------- LOAD MODEL ----------------
if not MODEL_PATH.exists():
    raise FileNotFoundError(
        f"Model not found: {MODEL_PATH}\n"
        "Place best.pt in the same folder as this script."
    )

print("Loading SkySentinel AI...")
model = YOLO(str(MODEL_PATH))
print("Model loaded:", model.names)

# ---------------- OPEN WEBCAM ----------------
cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)

if not cap.isOpened():
    cap.release()
    cap = cv2.VideoCapture(CAMERA_INDEX)

if not cap.isOpened():
    raise RuntimeError(
        "Webcam unavailable. Check Windows camera permissions "
        "or try CAMERA_INDEX = 1."
    )

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

print("Webcam started! Press Q to quit.")

previous_time = time.time()
last_detections = []

try:
    while True:
        success, frame = cap.read()

        if not success:
            print("Could not read webcam frame.")
            break

        # Run object detection
        result = model.predict(
            source=frame,
            imgsz=IMAGE_SIZE,
            conf=CONFIDENCE,
            verbose=False
        )[0]

        # Draw bounding boxes and labels
        output = result.plot()

        # Calculate approximate FPS
        now = time.time()
        fps = 1.0 / max(now - previous_time, 1e-6)
        previous_time = now

        cv2.putText(
            output,
            f"SkySentinel AI | FPS: {fps:.1f}",
            (15, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2
        )

        # Print detections only when they change
        detections = []
        for box in result.boxes:
            class_id = int(box.cls[0])
            confidence = float(box.conf[0])
            detections.append(
                (model.names[class_id], round(confidence, 2))
            )

        if detections != last_detections:
            if detections:
                print("\nDetected:")
                for name, confidence in detections:
                    print(f"  {name}: {confidence:.0%}")
            else:
                print("\nNo objects detected.")
            last_detections = detections

        cv2.imshow("SkySentinel AI - Live Webcam", output)

        # Press Q to exit
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:
    cap.release()
    cv2.destroyAllWindows()
    print("SkySentinel AI stopped.")
