"""Studio — the local batch production console for AI 视频工厂.

Backlot (backlot/) is a read-only storyboard viewer. Studio is the control
plane: it enumerates every registered tool, renders forms from each tool's
input_schema, and runs single or batched jobs through a worker pool.

Everything runs locally — rendering needs the filesystem, FFmpeg and Node.
"""

__version__ = "1.0.0"
