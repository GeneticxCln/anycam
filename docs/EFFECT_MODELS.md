# AI effect models

AnyCam's portrait, face and mask effects run ONNX models through
`onnxruntime`. Everything about where they run and which model is used lives
in three modules:

| Module | Job |
|---|---|
| `src/anycam/inference.py` | Creates every model session on the chosen device (CPU or CUDA) |
| `src/anycam/segment.py` | Person segmentation: the bundled MediaPipe model or Robust Video Matting |
| `src/anycam/models.py` | Optional models AnyCam can download, with pinned SHA-256 checks |

## Device: CPU or GPU

**Settings → AI effects → Run models on**, saved per project as
`output.effects_device` (`cpu` or `gpu`); `ANYCAM_EFFECTS_DEVICE` overrides it.

* The CPU is the default. The GPU already runs the canvas, NVENC and the
  virtual camera conversion, so CUDA is opt-in.
* `gpu` is used only when onnxruntime has the CUDA provider. Each CUDA session
  gets a 512 MB arena that grows only as asked. If creating a CUDA session
  fails (missing CUDA or cuDNN libraries), that model runs on the CPU and the
  reason is printed.
* To get the CUDA provider, replace the CPU build:
  `pip uninstall onnxruntime && pip install 'onnxruntime-gpu[cuda,cudnn]'`.
  Version 1.31.1 ships a Python 3.14 Linux wheel.
* Changing the device drops the cached sessions; the effects pick up the new
  ones on their next refresh, without a restart.

The bundled models are small. MediaPipe segmentation runs in about 1–2 ms on a
desktop CPU, so the GPU matters mainly for heavier models such as RVM at higher
resolution.

## Person cut-out model

**Settings → AI effects → Person cut-out**, saved as
`output.segmentation_model`.

| Model | Licence | Input | CPU cost (desktop, measured) | Notes |
|---|---|---|---|---|
| MediaPipe selfie segmentation (default, bundled) | Apache-2.0 | 256×144 | ~2 ms | A person probability; AnyCam sharpens the edge and smooths it over time |
| Robust Video Matting, MobileNetV3 (download) | GPL-3.0 | 640×360, downsample 0.4 | ~10 ms | A real alpha matte with soft edges and hair; recurrent, so stable between frames |

RVM is not shipped with AnyCam: its GPL-3.0 licence does not allow bundling it
in an MIT application. The **Download** button fetches
`rvm_mobilenetv3_fp32.onnx` (15 MB) from the author's GitHub release
(<https://github.com/PeterL1n/RobustVideoMatting>) into
`~/.local/share/anycam/models/`. The file is written as `.part`, checked against
the pinned size and SHA-256
(`88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828`), and only
then renamed into place. AnyCam makes no network request unless you click
Download. Until it is downloaded, choosing RVM keeps using MediaPipe.

Each effect instance gets its own segmenter (`segment.segmenter()`), because
RVM carries recurrent state from frame to frame. Two layers with background
blur must not share it.

Other models checked and not used, as of 2026-11:

* **MODNet**: Apache-2.0 for code and models, but there is no official ONNX file;
  it has to be converted from a PyTorch checkpoint.
* **PP-HumanSeg v2**: Apache-2.0, but published in Paddle format only.
* **BiRefNet**: MIT, but too heavy for live 60 fps on a GPU shared with the
  canvas.

## Your own segmentation model

Set `ANYCAM_SEG_MODEL=/path/to/model.onnx` to replace the bundled MediaPipe
file with any model that has its interface:

* input `pixel_values`: `[N, 3, 144, 256]` RGB in 0..1;
* output `alphas`: `[N, 1, 144, 256]`, the person probability.

## How the effects stay inside the frame budget

Effects run on a worker bin in the layer's filter slot (`processors.py`). At
1080p60 each frame has a 16.7 ms budget:

* Slow, cached work runs on `SlowJob` threads beside the frame thread:
  segmentation, face detection, the blurred/dimmed background, animated
  backdrops, and the next animation step of masks. onnxruntime and Pillow
  release the GIL, so this overlaps for real. A frame composites the newest
  results (one RGBA composite, about 5 ms). The first result, and one after a
  pause, are computed at once so a single still frame (a thumbnail) is
  complete.
* Live **Zoom** and **Auto-frame** crop and scale in GStreamer (`videocrop` and
  `videoscale`). Auto-frame tracks the face on a 384-pixel tap at 15 fps and
  moves the crop smoothly. Pillow could not enlarge a 1080p crop fast enough
  (about 21 ms).
* Face stickers reuse resized and rotated sprites, keyed by size and whole
  degrees.

`scripts/bench-effects.py --sizes 1920x1080 --fps 60` measures the frame-thread
cost of every Python effect. On the reference machine every effect fits the
60 fps budget; background effects are about 9.5 ms and masks about 14 ms.
Worker-thread time is not counted there, but it still uses CPU.
