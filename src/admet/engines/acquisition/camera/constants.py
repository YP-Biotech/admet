# pylon buffers: enough memory for a quarter second at 4000 fps on a small ROI, so a pause in
# Python (the GUI painting, a parameter read) does not make the camera skip frames.
GRAB_BUFFER_MEMORY = 64 << 20
GRAB_BUFFERS_MIN = 50
GRAB_BUFFERS_MAX = 1000
BLOCK_BYTES = 16 << 20      # a block of frames the writer puts on the disk in one write
BLOCK_SECONDS = 0.25        # and no more than this much recording, so a slow one reaches the disk soon
MEMORY_LIMIT_SHARE = 0.5    # of the free memory, what frames waiting for the disk may take
MEMORY_LIMIT_MAX = 8 << 30  # however much is free
WRITER_THREAD_TIMEOUT = 60
QUEUE_GET_TIMEOUT = 0.1
