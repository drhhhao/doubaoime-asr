from typing import Dict, List, Optional, Union
from pathlib import Path

import opuslib
import miniaudio

from .config import ASRConfig


# opuslib 的 application 类型
_APPLICATIONS: Dict[str, int] = {
    "audio": opuslib.APPLICATION_AUDIO,
    "voip": opuslib.APPLICATION_VOIP,
    "lowdelay": opuslib.APPLICATION_RESTRICTED_LOWDELAY,
}


class AudioEncoder:
    """
    进行音频格式转换

    官方的音频包是 40ms：两个 20ms 的 Opus 帧再用 code-3 复用一个包里。
    这里按同样的方式组包（frame_duration_ms / opus_frame_ms）。
    """
    def __init__(self, config: ASRConfig) -> None:
        self.config = config
        self._encoder: Optional[opuslib.Encoder] = None

    @property
    def encoder(self) -> opuslib.Encoder:
        if self._encoder is None:
            application = _APPLICATIONS.get(
                self.config.opus_application, opuslib.APPLICATION_RESTRICTED_LOWDELAY
            )
            self._encoder = opuslib.Encoder(
                self.config.sample_rate,
                self.config.channels,
                application,
            )
            self._encoder.bitrate = self.config.opus_bitrate
        return self._encoder

    @property
    def bytes_per_opus_frame(self) -> int:
        samples = self.config.sample_rate * self.config.opus_frame_ms // 1000
        return samples * 2 * self.config.channels  # 16-bit

    @property
    def bytes_per_packet(self) -> int:
        samples = self.config.sample_rate * self.config.frame_duration_ms // 1000
        return samples * 2 * self.config.channels  # 16-bit

    def pcm_to_opus_frames(self, pcm_data: bytes) -> List[bytes]:
        """把 PCM 编码成独立的 Opus 帧（每个包只有一帧）"""
        bytes_per_frame = self.bytes_per_opus_frame
        samples_per_frame = self.config.sample_rate * self.config.opus_frame_ms // 1000

        frames = []
        for i in range(0, len(pcm_data), bytes_per_frame):
            chunk = pcm_data[i:i + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                chunk = chunk + b"\x00" * (bytes_per_frame - len(chunk))

            frames.append(self.encoder.encode(chunk, samples_per_frame))

        return frames

    def pcm_to_opus_packets(self, pcm_data: bytes) -> List[bytes]:
        """
        把 PCM 编码成官方格式的 Opus 包

        默认每个包 40ms = 2 个 20ms 帧，用 TOC code 3 复用一个包。
        尾部不足一个包时补静音，保证每个包都是完整的多帧包。
        """
        bytes_per_packet = self.bytes_per_packet
        frames_per_packet = max(1, self.config.frame_duration_ms // self.config.opus_frame_ms)

        if frames_per_packet == 1:
            return self.pcm_to_opus_frames(pcm_data)

        # 补齐到整数个包
        remainder = len(pcm_data) % bytes_per_packet
        if remainder:
            pcm_data = pcm_data + b"\x00" * (bytes_per_packet - remainder)

        frames = self.pcm_to_opus_frames(pcm_data)

        packets = []
        for i in range(0, len(frames), frames_per_packet):
            packets.append(self._repacketize(frames[i:i + frames_per_packet]))
        return packets

    def encode_packet(self, pcm_chunk: bytes) -> bytes:
        """编码一个完整的音频包（不足时补静音）"""
        packet = pcm_chunk[:self.bytes_per_packet]
        if len(packet) < self.bytes_per_packet:
            packet = packet + b"\x00" * (self.bytes_per_packet - len(packet))

        frames_per_packet = max(1, self.config.frame_duration_ms // self.config.opus_frame_ms)
        samples_per_frame = self.config.sample_rate * self.config.opus_frame_ms // 1000
        bytes_per_frame = self.bytes_per_opus_frame

        frames = [
            self.encoder.encode(
                packet[i:i + bytes_per_frame],
                samples_per_frame,
            )
            for i in range(0, len(packet), bytes_per_frame)
        ][:frames_per_packet]

        return self._repacketize(frames)

    @staticmethod
    def _repacketize(frames: List[bytes]) -> bytes:
        """
        把多个 Opus 帧合并成一个 code-3 包

        格式: TOC(code=3) | 帧数(VBR flag) | 每帧长度 | 帧数据
        """
        if not frames:
            return b""
        if len(frames) == 1:
            return frames[0]

        toc = (frames[0][0] & 0xFC) | 3  # 保留 config/stereo 位，code 改成 3
        header = bytes([toc, 0x80 | len(frames)])

        # VBR 长度表只写前 N-1 帧（最后一帧的长度由包尾隐含）
        lengths = bytearray()
        for frame in frames[:-1]:
            length = len(frame) - 1
            if length < 252:
                lengths.append(length)
            else:
                lengths.append(252 + (length - 252) % 4)
                lengths.append((length - 252) // 4)

        payload = b"".join(frame[1:] for frame in frames)
        return header + bytes(lengths) + payload

    @staticmethod
    def convert_audio_to_pcm(
        audio_path: Union[Path, str],
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> bytes:
        decoded = miniaudio.decode_file(
            str(audio_path),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=channels,
            sample_rate=sample_rate,
        )
        return decoded.samples.tobytes()
