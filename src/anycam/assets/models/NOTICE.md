# Bundled models and sample

| File | What | Source | License |
|---|---|---|---|
| `yunet.onnx` | YuNet face detector (boxes and 5 landmarks) | OpenCV Zoo | Apache-2.0 |
| `selfie_segmentation_landscape.onnx` | MediaPipe selfie segmentation, 256x144, ONNX conversion | https://huggingface.co/onnx-community/mediapipe_selfie_segmentation_landscape, revision `2497d5bec26c626c7b3c4edc6e1fefc21b64f6c3`, file `onnx/model.onnx` | Apache-2.0 |

`selfie_segmentation_landscape.onnx` SHA-256:
`7a0adcfdb1715d3b0ff0f61486d8a181c33f4a208343a5abfccbc6540e872d24`

Input `pixel_values` is `[N, 3, 144, 256]` RGB scaled to 0..1 (no mean/std);
output `alphas` is `[N, 1, 144, 256]`, the probability that a pixel is a person.

`../sample-portrait.jpg` is a 480x600 copy of NASA's official portrait of Neil
Armstrong (public domain in the United States). AnyCam uses it only as the
preview picture for face and portrait effects when no camera frame exists.

The Apache License 2.0 text is at https://www.apache.org/licenses/LICENSE-2.0.
