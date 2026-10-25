# SkySentinel

Local drone-detection dashboard using Flask, OpenCV, and two Ultralytics YOLO weights. The browser UI supports a browser webcam and authorized RTSP/RTSPS CCTV streams.

## Requirements

- Python 3.10 or newer
- The model files `best.pt` and `best1.pt` in the project root
- A webcam, or an RTSP/RTSPS stream reachable from this computer

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Start the dashboard:

```powershell
python app.py
```

Open http://127.0.0.1:5000. Allow webcam permission in the browser, or select **CCTV / RTSP** and enter an authorized camera stream URL. The CCTV URL is submitted to the local server for the duration of the connection; do not share it with others.

## Model selection

Both models run on each frame. The annotated frame from whichever model has the highest single-detection confidence is displayed. A model with no detections receives a score of zero. Keep `best.pt` and `best1.pt` in the repository root, or update their paths in `app.py`.

Model files are included in this repository. Share or publish them only if you have permission to distribute those weights.