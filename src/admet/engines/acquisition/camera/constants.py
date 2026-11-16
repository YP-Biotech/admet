# pylon buffers: enough memory for a quarter second at 4000 fps on a small ROI, so a pause in
# Python (the GUI painting, a parameter read) does not make the camera skip frames.
GRAB_BUFFER_MEMORY = 64 << 20
GRAB_BUFFERS_MIN = 50
GRAB_BUFFERS_MAX = 1000
WRITER_BATCH = 256          # frames written to the raw file in one call
WRITER_QUEUE_SIZE = 10000
WRITER_THREAD_TIMEOUT = 60
QUEUE_GET_TIMEOUT = 0.1
