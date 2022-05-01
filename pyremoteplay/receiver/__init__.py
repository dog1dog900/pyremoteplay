"""AV Receivers for pyremoteplay."""

from __future__ import annotations
import abc
from struct import unpack_from
import warnings
import logging
from collections import deque

from pyremoteplay.const import FFMPEG_PADDING

_LOGGER = logging.getLogger(__name__)

try:
    import av
except ModuleNotFoundError:
    warnings.warn("av not installed")


class AVReceiver(abc.ABC):
    """Base Class for AV Receiver."""

    AV_CODEC_OPTIONS_H264 = {
        # "profile": "0",
        # "level": "3.2",
        "tune": "zerolatency",
        "preset": "ultrafast",
    }

    AV_CODEC_OPTIONS_HEVC = {
        "tune": "zerolatency",
        "preset": "ultrafast",
    }

    @staticmethod
    def audio_frame(buf, codec_ctx):
        """Return decoded audio frame."""
        packet = av.packet.Packet(buf)
        frames = codec_ctx.decode(packet)
        if not frames:
            return None
        frame = frames[0]
        return frame

    @staticmethod
    def video_frame(buf, codec_ctx, to_rgb=True):
        """Decode H264 Frame to raw image.
        Return AV Frame.

        Frame Format:
        AV_PIX_FMT_YUV420P (libavutil)
        YUV 4:2:0, 12bpp
        (1 Cr & Cb sample per 2x2 Y samples)
        """
        packet = av.packet.Packet(b"".join([buf, bytes(FFMPEG_PADDING)]))
        frames = codec_ctx.decode(packet)
        if not frames:
            return None
        frame = frames[0]
        if frame.is_corrupt:
            _LOGGER.error("Corrupt Frame: %s", frame)
            return None
        # _LOGGER.debug(
        #     "Frame: Key:%s, Interlaced:%s Pict:%s",
        #     frame.key_frame,
        #     frame.interlaced_frame,
        #     frame.pict_type,
        # )
        if to_rgb:
            frame = frame.reformat(frame.width, frame.height, "rgb24")
        elif frame.format.name == "nv12":  # HW Decode will output NV12 frames
            frame = frame.reformat(format="yuv420p")
        return frame

    @staticmethod
    def find_video_decoder(video_format="h264", use_hw=False):
        """Return all decoders found."""
        found = []
        decoders = (
            ("amf", "AMD"),
            ("cuvid", "Nvidia"),
            ("qsv", "Intel"),
            ("videotoolbox", "Apple"),
            (video_format, "CPU"),
        )

        decoder = None
        _LOGGER.debug("Using HW: %s", use_hw)
        if not use_hw:
            _LOGGER.debug("%s - %s - %s", video_format, use_hw, decoders)
            return [(video_format, "CPU")]
        for decoder in decoders:
            if decoder[0] == video_format:
                name = video_format
            else:
                name = f"{video_format}_{decoder[0]}"
            try:
                av.codec.Codec(name, "r")
            except (av.codec.codec.UnknownCodecError, av.error.PermissionError):
                _LOGGER.debug("Could not find Decoder: %s", name)
                continue
            found.append((name, decoder[1]))
            _LOGGER.debug("Found Decoder: %s", name)
        return found

    @staticmethod
    def video_codec(codec_name: str):
        """Return Video Codec Context."""
        try:
            codec_ctx = av.codec.Codec(codec_name, "r").create()
        except av.codec.codec.UnknownCodecError:
            _LOGGER.error("Invalid codec: %s", codec_name)
        _LOGGER.info("Using Decoder: %s", codec_name)
        if codec_name.startswith("h264"):
            codec_ctx.options = AVReceiver.AV_CODEC_OPTIONS_H264
        elif codec_name.startswith("hevc"):
            codec_ctx.options = AVReceiver.AV_CODEC_OPTIONS_HEVC
        codec_ctx.pix_fmt = "yuv420p"
        codec_ctx.flags = av.codec.context.Flags.LOW_DELAY
        codec_ctx.flags2 = av.codec.context.Flags2.FAST
        codec_ctx.thread_type = av.codec.context.ThreadType.AUTO
        return codec_ctx

    @staticmethod
    def audio_codec(codec_name: str = "opus"):
        """Return Audio Codec Context."""
        codec_ctx = av.codec.Codec(codec_name, "r").create()
        codec_ctx.format = "s16"
        return codec_ctx

    def __init__(self):
        self._session = None
        self.rgb = False
        self.video_decoder = None
        self.audio_decoder = None
        self.audio_resampler = None
        self.audio_config = {}

    def get_audio_config(self, header: bytes):
        """Get Audio config from header."""
        self.audio_config = {
            "channels": header[0],
            "bits": header[1],
            "rate": unpack_from("!I", header, 2)[0],
            "frame_size": unpack_from("!I", header, 6)[0],
            "unknown": unpack_from("!I", header, 10)[0],
        }
        self.audio_config["packet_size"] = (
            self.audio_config["channels"]
            * (self.audio_config["bits"] // 8)
            * self.audio_config["frame_size"]
        )
        _LOGGER.info("Audio Config: %s", self.audio_config)

        if not self.audio_decoder:
            self.audio_decoder = AVReceiver.audio_codec()
            self.audio_resampler = av.audio.resampler.AudioResampler(
                "s16",
                self.audio_config["channels"],
                self.audio_config["rate"],
            )
            self._session.events.emit("audio_config")

    def get_video_codec(self):
        """Get Codec Context."""
        codec_name = self._session.video_format
        self.video_decoder = AVReceiver.video_codec(codec_name)
        try:
            self.video_decoder.open()
        except av.error.ValueError as error:
            if self._session:
                try:
                    msg = error.log[2]
                except Exception:  # pylint: disable=broad-except
                    msg = str(error)
                self._session.error = msg
                self._session.stop()

    def set_session(self, session):
        """Set Session."""
        self._session = session

    def notify_started(self):
        """Notify session that receiver has started."""
        self._session.receiver_started.set()

    def start(self):
        """Start receiver."""
        self.notify_started()

    def decode_video_frame(self, buf: bytes) -> av.VideoFrame:
        """Return decoded Video Frame."""
        if not self.video_decoder:
            return None
        frame = AVReceiver.video_frame(buf, self.video_decoder, self.rgb)
        return frame

    def decode_audio_frame(self, buf: bytes) -> av.AudioFrame:
        """Return decoded Audio Frame."""
        if not self.audio_config or not self.audio_decoder:
            return None

        frame = AVReceiver.audio_frame(buf, self.audio_decoder)
        if frame:
            # Need format to be s16. Format is float.
            frame = self.audio_resampler.resample(frame)
        return frame

    def get_video_frame(self):
        """Return Video Frame."""
        raise NotImplementedError

    def get_audio_frame(self):
        """Return Audio Frame."""
        raise NotImplementedError

    def handle_video(self, buf: bytes):
        """Handle video frame."""
        raise NotImplementedError

    def handle_audio(self, buf: bytes):
        """Handle audio frame."""
        raise NotImplementedError

    def close(self):
        """Close Receiver."""
        if self.video_decoder is not None:
            self.video_decoder.close()


class QueueReceiver(AVReceiver):
    """Receiver which stores decoded frames in queues."""

    def __init__(self, max_frames=10, max_video_frames=-1, max_audio_frames=-1):
        super().__init__()
        max_video_frames = max_frames if max_video_frames < 0 else max_video_frames
        max_audio_frames = max_frames if max_audio_frames < 0 else max_audio_frames
        self._v_queue = deque(maxlen=max_video_frames)
        self._a_queue = deque(maxlen=max_audio_frames)

    def close(self):
        """Close Receiver."""
        super().close()
        self._v_queue.clear()
        self._a_queue.clear()

    def get_video_frame(self) -> av.VideoFrame:
        """Return oldest Video Frame from queue."""
        try:
            frame = self._v_queue[0]
            return frame
        except IndexError:
            return None

    def get_audio_frame(self) -> av.AudioFrame:
        """Return oldest Audio Frame from queue."""
        try:
            frame = self._a_queue[0]
            return frame
        except IndexError:
            return None

    def get_latest_video_frame(self) -> av.VideoFrame:
        """Return latest Video Frame from queue."""
        try:
            frame = self._v_queue[-1]
            return frame
        except IndexError:
            return None

    def get_latest_audio_frame(self) -> av.AudioFrame:
        """Return latest Audio Frame from queue."""
        try:
            frame = self._a_queue[-1]
            return frame
        except IndexError:
            return None

    def handle_video(self, buf):
        """Handle video frame. Add to queue."""
        frame = self.decode_video_frame(buf)
        if frame is None:
            return
        self._v_queue.append(frame)
        self._session.events.emit("video_frame")

    def handle_audio(self, buf):
        """Handle Audio Frame. Add to queue."""
        frame = self.decode_audio_frame(buf)
        if frame is None:
            return
        self._a_queue.append(frame)
        self._session.events.emit("audio_frame")

    @property
    def video_frames(self) -> list[av.VideoFrame]:
        """Return Latest Video Frames."""
        frames = list(self._v_queue)
        return frames

    @property
    def audio_frames(self) -> list[av.AudioFrame]:
        """Return Latest Audio Frames."""
        frames = list(self._a_queue)
        return frames