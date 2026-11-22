"""Tiny numpy-only MobileNetV2 feature extractor (ImageNet weights, 1280-d).

The network runs on the server for one cropped webcam frame at a time (about
0.1 s on one CPU core at 160x160), so the kiosk itself does no image work
beyond spotting that something was held up. Weights: ``vision_model/
mobilenetv2_features.npz`` built by ``scripts/build_vision_model.py`` from the
ONNX model zoo MobileNetV2 (Apache-2.0).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent / "vision_model" / "mobilenetv2_features.npz"
INPUT_SIDE = 160
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_lock = threading.Lock()
_model: dict | None = None


def _load() -> dict:
    global _model
    with _lock:
        if _model is None:
            data = np.load(MODEL_PATH)
            spec = json.loads(bytes(data["ops"]).decode("utf-8"))
            weights = {}
            for op in spec["ops"]:
                if op["op"] == "conv":
                    w = data[op["k"] + "_w"].astype(np.float32)  # (Cout, Cin/g, kh, kw)
                    b = data[op["k"] + "_b"].astype(np.float32)
                    if op["group"] == 1 and w.shape[2] == 1:
                        weights[op["k"]] = ("pw", w[:, :, 0, 0].T.copy(), b)  # (Cin, Cout)
                    elif op["group"] == 1:
                        cout, cin, kh, kw = w.shape
                        weights[op["k"]] = ("full", w.transpose(2, 3, 1, 0).reshape(kh * kw * cin, cout).copy(), b)
                    else:
                        weights[op["k"]] = ("dw", w[:, 0].transpose(1, 2, 0).copy(), b)  # (kh, kw, C)
            _model = {"input": spec["input"], "ops": spec["ops"], "weights": weights}
        return _model


def available() -> bool:
    return MODEL_PATH.is_file()


def _conv(x: np.ndarray, kind: str, w: np.ndarray, b: np.ndarray, stride: int, pad: int) -> np.ndarray:
    if kind == "pw":
        h, wd, c = x.shape
        if stride > 1:
            x = x[::stride, ::stride]
        return x @ w + b
    if pad:
        x = np.pad(x, ((pad, pad), (pad, pad), (0, 0)))
    h, wd, c = x.shape
    k = 3
    oh = (h - k) // stride + 1
    ow = (wd - k) // stride + 1
    if kind == "full":
        cols = [x[i:i + stride * oh:stride, j:j + stride * ow:stride] for i in range(k) for j in range(k)]
        return np.concatenate(cols, axis=2) @ w + b
    out = np.zeros((oh, ow, c), dtype=np.float32)
    for i in range(k):
        for j in range(k):
            out += x[i:i + stride * oh:stride, j:j + stride * ow:stride] * w[i, j]
    return out + b


def embed_array(rgb: np.ndarray) -> np.ndarray:
    """``rgb``: (H, W, 3) uint8 already sized to INPUT_SIDE. Returns an L2-normalised 1280-d vector."""
    model = _load()
    x = (rgb.astype(np.float32) / 255.0 - MEAN) / STD
    values = {model["input"]: x}
    for op in model["ops"]:
        kind = op["op"]
        if kind == "conv":
            k, w, b = model["weights"][op["k"]]
            values[op["y"]] = _conv(values[op["x"]], k, w, b, op["stride"], op["pad"])
        elif kind == "clip":
            values[op["y"]] = np.clip(values[op["x"]], op["lo"], op["hi"])
        elif kind == "add":
            values[op["y"]] = values[op["a"]] + values[op["b"]]
        elif kind == "gap":
            feat = values[op["x"]].mean(axis=(0, 1))
            norm = float(np.linalg.norm(feat)) or 1.0
            return (feat / norm).astype(np.float32)
    raise RuntimeError("model has no pooling layer")


def embed_image(image) -> np.ndarray:
    """PIL image (any size) -> embedding. Letterboxed on mid-grey."""
    from PIL import Image

    image = image.convert("RGB")
    image.thumbnail((INPUT_SIDE, INPUT_SIDE), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (INPUT_SIDE, INPUT_SIDE), (128, 128, 128))
    canvas.paste(image, ((INPUT_SIDE - image.width) // 2, (INPUT_SIDE - image.height) // 2))
    return embed_array(np.asarray(canvas, dtype=np.uint8))


def pack(vector: np.ndarray) -> str:
    """int8-quantised base64 (1.7 kB) for storage."""
    import base64

    q = np.clip(np.round(vector * 127.0 / max(float(np.abs(vector).max()), 1e-6)), -127, 127).astype(np.int8)
    return base64.b64encode(q.tobytes()).decode("ascii")


def unpack(text: str) -> np.ndarray:
    import base64

    v = np.frombuffer(base64.b64decode(text), dtype=np.int8).astype(np.float32)
    norm = float(np.linalg.norm(v)) or 1.0
    return v / norm
