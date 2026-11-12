"""Engine internals.

``anycam.pipeline`` is a thin facade over these modules:

* ``controller``  - ``AnyCamEngine``: build, start, pump, stop (composed from the mixins below)
* ``layers``      - adding, removing, hiding and swapping layers (and whole scenes) on the live canvas
* ``canvas``      - the compositor graph, frame counter and preview branches
* ``audio``       - the audio mixer, microphone and the shared AAC tee
* ``health``      - per-source health and the one-second performance sample
* ``encoders``    - NVENC / VA-API / x264 selection shared by recording and streaming
* ``source_pool`` - ref-counted capture pipelines
* ``outputs``     - the persistent recorder, the virtual camera and the canvas taps / RTMP stream
* ``verify``      - MP4 verification helpers
"""
