from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from enum import Enum, auto
import json
from pathlib import Path
import time
from typing import AsyncIterator, Callable, List, Optional, Union
import uuid
from pydantic import BaseModel, Field
import websockets
from websockets import ClientConnection

from .config import ASRConfig, SessionConfig
from .audio import AudioEncoder
from .asr_pb2 import AsrRequest, AsrResponse as AsrResponsePb, FrameState

# PCM 音频数据的类型别名
AudioChunk = bytes


class ResponseType(Enum):
    """
    ASR 响应类型
    """
    TASK_STARTED = auto()
    SESSION_STARTED = auto()
    SESSION_FINISHED = auto()
    VAD_START = auto()
    INTERIM_RESULT = auto()
    REVISED_RESULT = auto()
    """二遍纠正（nonstream）结果，已识别内容的改写，不一定是最终结果"""
    FINAL_RESULT = auto()
    HEARTBEAT = auto()
    ERROR = auto()
    UNKNOWN = auto()


@dataclass
class ASRResponse:
    """
    ASR 响应
    """
    type: ResponseType
    text: str = ""
    is_final: bool = False
    vad_start: bool = False
    vad_finished: bool = False
    packet_number: int = -1
    error_msg: str = ""
    raw_json: Optional[dict] = None


class ASRError(Exception):
    """
    ASR 错误
    """
    def __init__(self, message: str, response: Optional[ASRResponse] = None) -> None:
        super().__init__(message)
        self.response = response


class _SessionState(BaseModel):
    """
    ASR 会话状态
    """
    # 官方 PC 客户端的 request_id 形如 ime_win_<uuid>
    request_id: str = Field(default_factory=lambda: f"ime_win_{uuid.uuid4()}")
    final_text: str = ""
    last_text: str = ""
    is_finished: bool = False
    error: Optional[ASRResponse] = None


class DoubaoASR:
    """
    豆包输入法 ASR 客户端
    """
    def __init__(self, config: Optional[ASRConfig] = None):
        self.config = config if config is not None else ASRConfig()
        self._encoder = AudioEncoder(self.config)

    async def __aenter__(self) -> DoubaoASR:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    async def transcribe(self, audio: Union[str, Path, bytes], *, realtime = False, on_interim: Callable[[str], None] = None) -> str:
        """
        非流式语音识别

        :param audio: 音频文件路径或 PCM 音频数据
        :param on_interim: 可选的中间结果回调
        :return: 最终识别文本
        """
        final_text = ""
        async for response in self.transcribe_stream(audio, realtime=realtime):
            if response.type == ResponseType.INTERIM_RESULT and on_interim:
                on_interim(response.text)
            elif response.type == ResponseType.FINAL_RESULT:
                final_text = response.text
            elif response.type == ResponseType.ERROR:
                raise ASRError(response.error_msg, response)
        return final_text

    async def transcribe_stream(self, audio: Union[str, Path, bytes], *, realtime: bool = False) -> AsyncIterator[ASRResponse]:
        """
        流式语音识别（完整音频）

        :param audio: 音频文件路径或 PCM 音频数据
        :param realtime: 是否按实时速度发送
        :return: ASR 响应流，包括中间结果和最终结果
        """
        if isinstance(audio, (str, Path)):
            pcm_data = self._encoder.convert_audio_to_pcm(
                audio, self.config.sample_rate, self.config.channels,
            )
        else:
            pcm_data = audio

        # 官方格式：每个包 40ms（2 个 20ms Opus 帧，code-3 复用）
        opus_packets = self._encoder.pcm_to_opus_packets(pcm_data)
        state = _SessionState()

        try:
            async with websockets.connect(
                self.config.ws_url,
                additional_headers=self.config.headers,
                open_timeout=self.config.connect_timeout,
            ) as ws:
                # 初始化会话
                async for resp in self._initialize_session(ws, state):
                    yield resp

                # 响应队列
                response_queue: asyncio.Queue[Optional[ASRResponse]] = asyncio.Queue()

                # 启动发送和接收任务
                send_task = asyncio.create_task(
                    self._send_audio(ws, opus_packets, state, realtime)
                )
                recv_task = asyncio.create_task(
                    self._receive_responses(ws, state, response_queue)
                )

                try:
                    # 从队列中获取服务器响应
                    while True:
                        try:
                            resp = await asyncio.wait_for(
                                response_queue.get(),
                                timeout=self.config.recv_timeout,
                            )
                            if resp is None: # 结束标记
                                break

                            # 心跳包仅用于重置超时，不转发给用户
                            if resp.type == ResponseType.HEARTBEAT:
                                continue

                            yield resp
                            if resp.type == ResponseType.ERROR:
                                break

                        except asyncio.TimeoutError:
                            break

                    await send_task
                finally:
                    send_task.cancel()
                    recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await send_task
                    with contextlib.suppress(asyncio.CancelledError):
                        await recv_task

        except websockets.exceptions.WebSocketException as e:
            raise ASRError(f"WebSocket 错误: {e}") from e

    async def transcribe_realtime(
        self,
        audio_source: AsyncIterator[AudioChunk],
    ) -> AsyncIterator[ASRResponse]:
        """
        实时流式语音识别（支持麦克风等持续音频源）

        :param audio_source: PCM 音频数据的异步迭代器
            - 每个 chunk 应为 16-bit PCM 数据
            - 采样率和声道数应与 config 中配置一致
            - 迭代器结束时会自动发送 FinishSession
        :return: ASR 响应流
        """
        state = _SessionState()

        try:
            async with websockets.connect(
                self.config.ws_url,
                additional_headers=self.config.headers,
                open_timeout=self.config.connect_timeout,
            ) as ws:
                # 初始化会话
                async for resp in self._initialize_session(ws, state):
                    yield resp

                # 响应队列
                response_queue: asyncio.Queue[Optional[ASRResponse]] = asyncio.Queue()

                # 启动发送和接收任务
                send_task = asyncio.create_task(
                    self._send_audio_realtime(ws, audio_source, state)
                )
                recv_task = asyncio.create_task(
                    self._receive_responses(ws, state, response_queue)
                )

                try:
                    # 实时模式不使用超时，依靠 WebSocket 层检测断开
                    while True:
                        resp = await response_queue.get()
                        if resp is None:
                            break

                        if resp.type == ResponseType.HEARTBEAT:
                            continue

                        yield resp
                        if resp.type == ResponseType.ERROR:
                            break

                    await send_task
                finally:
                    send_task.cancel()
                    recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await send_task
                    with contextlib.suppress(asyncio.CancelledError):
                        await recv_task

        except websockets.exceptions.WebSocketException as e:
            raise ASRError(f"WebSocket 错误: {e}") from e

    def _next_frame_state(self, frame_index: int, is_last: bool = False) -> int:
        """
        计算第 frame_index 个音频包的 field 9 取值（从 0 开始计）

        sequence 模式（官方行为）：恒为 frame_index + 1，没有单独的“最后一帧”标记，
        结束只靠 FinishSession。
        enum 模式（旧行为）：FIRST=1 / MIDDLE=3 / LAST=9。
        """
        if self.config.frame_state_mode == "enum":
            if frame_index == 0 and is_last:
                return FrameState.FRAME_STATE_FIRST
            if frame_index == 0:
                return FrameState.FRAME_STATE_FIRST
            if is_last:
                return FrameState.FRAME_STATE_LAST
            return FrameState.FRAME_STATE_MIDDLE
        return frame_index + 1

    async def _send_audio_realtime(
        self,
        ws: ClientConnection,
        audio_source: AsyncIterator[AudioChunk],
        state: _SessionState,
    ):
        """
        从异步迭代器读取 PCM 数据并实时发送
        """
        frame_index = 0
        pcm_buffer = b""
        bytes_per_packet = self._encoder.bytes_per_packet
        packet_interval = self.config.frame_duration_ms / 1000.0

        async for chunk in audio_source:
            if state.is_finished:
                break

            pcm_buffer += chunk

            # 当缓冲区有足够数据时，编码并发送
            while len(pcm_buffer) >= bytes_per_packet:
                pcm_packet = pcm_buffer[:bytes_per_packet]
                pcm_buffer = pcm_buffer[bytes_per_packet:]

                opus_packet = self._encoder.encode_packet(pcm_packet)

                msg = _build_asr_request(
                    opus_packet,
                    state.request_id,
                    self._next_frame_state(frame_index),
                )
                await ws.send(msg)
                frame_index += 1
                await asyncio.sleep(packet_interval)

        # 迭代器结束，处理剩余数据：不足一个包时重复最后一个完整包补齐（官方行为）
        if pcm_buffer and not state.is_finished:
            last_packet = pcm_buffer
            while len(last_packet) < bytes_per_packet:
                last_packet += last_packet[: min(len(last_packet), bytes_per_packet - len(last_packet))]

            opus_packet = self._encoder.encode_packet(last_packet)
            msg = _build_asr_request(
                opus_packet,
                state.request_id,
                self._next_frame_state(frame_index, is_last=True),
            )
            await ws.send(msg)
            frame_index += 1

        # FinishSession
        if not state.is_finished:
            await ws.send(_build_finish_session(state.request_id, self.config.get_token()))

    async def _initialize_session(self, ws: ClientConnection, state: _SessionState) -> AsyncIterator[ASRResponse]:
        """
        初始化 ASR 会话
        """
        token = self.config.get_token()

        # StartTask
        await ws.send(_build_start_task(state.request_id, token))
        resp = await ws.recv()
        parsed = _parse_response(resp)
        if parsed.type == ResponseType.ERROR:
            raise ASRError(f'StartTask 失败：{parsed.error_msg}', parsed)
        yield parsed

        # StartSession
        await ws.send(
            _build_start_session(state.request_id, token, self.config.session_config())
        )
        resp = await ws.recv()
        parsed = _parse_response(resp)
        if parsed.type == ResponseType.ERROR:
            raise ASRError(f'StartSession 失败：{parsed.error_msg}', parsed)
        yield parsed

    async def _send_audio(
        self,
        ws: ClientConnection,
        opus_packets: List[bytes],
        state: _SessionState,
        realtime: bool,
    ):
        """
        发送音频包
        """
        packet_interval = self.config.frame_duration_ms / 1000.0
        total = len(opus_packets)

        for i, opus_packet in enumerate(opus_packets):
            if state.is_finished:
                break

            msg = _build_asr_request(
                opus_packet,
                state.request_id,
                self._next_frame_state(i, is_last=(i == total - 1)),
            )
            await ws.send(msg)

            if realtime:
                await asyncio.sleep(packet_interval)

        # 结束只靠 FinishSession（官方没有“最后一帧”标记）
        if not state.is_finished:
            await ws.send(_build_finish_session(state.request_id, self.config.get_token()))

    async def _receive_responses(
        self,
        ws: ClientConnection,
        state: _SessionState,
        queue: asyncio.Queue[Optional[ASRResponse]],
    ):
        """
        接受响应并放入队列
        """
        try:
            while not state.is_finished:
                response = await ws.recv()
                resp = _parse_response(response)

                # 维护最近一次有文本的结果，用于 SessionFinished 前没收到
                # 明确 FINAL_RESULT 时的兜底
                if resp.text:
                    state.last_text = resp.text

                if resp.type == ResponseType.ERROR:
                    state.error = resp
                    state.is_finished = True
                    await queue.put(resp)
                    break
                elif resp.type == ResponseType.HEARTBEAT:
                    # 心跳包也放入队列，用于重置超时计时器
                    await queue.put(resp)
                elif resp.type == ResponseType.SESSION_FINISHED:
                    state.is_finished = True
                    if not state.final_text and state.last_text:
                        # 兜底：正常流程 results[0].is_interim=False 且
                        # is_vad_finished=True 即为最终结果，但个别会话在
                        # SessionFinished 前没有这样的包，用最近文本补一个
                        await queue.put(ASRResponse(
                            type=ResponseType.FINAL_RESULT,
                            text=state.last_text,
                            is_final=True,
                            raw_json=resp.raw_json,
                        ))
                    await queue.put(resp)
                    break
                elif resp.type == ResponseType.FINAL_RESULT:
                    state.final_text = resp.text
                    await queue.put(resp)
                else:
                    await queue.put(resp)

        except websockets.exceptions.ConnectionClosed:
            state.is_finished = True
        finally:
            # 结束信号
            await queue.put(None)


def _build_start_task(request_id: str, token: str) -> bytes:
    """构建 StartTask 消息 pb 数据"""
    request = AsrRequest()
    request.token = token
    request.service_name = "ASR"
    request.method_name = "StartTask"
    request.request_id = request_id
    return request.SerializeToString()


def _build_start_session(request_id: str, token: str, config: SessionConfig) -> bytes:
    """构建 StartSession 消息 pb 数据"""
    request = AsrRequest()
    request.token = token
    request.service_name = "ASR"
    request.method_name = "StartSession"
    request.request_id = request_id
    request.payload = config.model_dump_json()
    return request.SerializeToString()


def _build_finish_session(request_id: str, token: str) -> bytes:
    """构建 FinishSession 消息 pb 数据"""
    request = AsrRequest()
    request.token = token
    request.service_name = "ASR"
    request.method_name = "FinishSession"
    request.request_id = request_id
    return request.SerializeToString()


def _build_asr_request(
    audio_data: bytes,
    request_id: str,
    frame_state: int,
    timestamp_ms: Optional[int] = None,
) -> bytes:
    """
    构建 TaskRequest 消息 pb 数据

    官方 payload 只有 {"timestamp_ms": <unix ms>}。
    frame_state（field 9）官方发的是递增序号 1,2,3…N。
    """
    request = AsrRequest()
    metadata = json.dumps({"timestamp_ms": timestamp_ms or int(time.time() * 1000)})

    request.service_name = "ASR"
    request.method_name = "TaskRequest"
    request.payload = metadata
    request.audio_data = audio_data
    request.request_id = request_id
    request.frame_state = frame_state
    return request.SerializeToString()


def _parse_response(data: bytes) -> ASRResponse:
    """解析 ASR 响应 (使用 protobuf)"""
    pb = AsrResponsePb()
    pb.ParseFromString(data)

    message_type = pb.message_type
    result_json = pb.result_json  # 字段 7: 识别结果 JSON
    status_message = pb.status_message  # 字段 6: 状态消息

    # 根据 message_type 判断响应类型
    if message_type == "TaskStarted":
        return ASRResponse(type=ResponseType.TASK_STARTED)

    if message_type == "SessionStarted":
        return ASRResponse(type=ResponseType.SESSION_STARTED)

    if message_type == "SessionFinished":
        return ASRResponse(type=ResponseType.SESSION_FINISHED)

    if message_type in ("TaskFailed", "SessionFailed"):
        return ASRResponse(type=ResponseType.ERROR, error_msg=status_message)

    # 识别结果在 result_json 字段（字段 7）
    if not result_json:
        return ASRResponse(type=ResponseType.UNKNOWN)

    try:
        json_data = json.loads(result_json)
    except json.JSONDecodeError:
        return ASRResponse(type=ResponseType.UNKNOWN)

    results = json_data.get("results")
    extra = json_data.get("extra", {})

    # 无 results，可能是心跳包
    if results is None:
        return ASRResponse(
            type=ResponseType.HEARTBEAT,
            packet_number=extra.get("packet_number", -1),
            raw_json=json_data,
        )

    # VAD 开始
    if extra.get("vad_start"):
        return ASRResponse(type=ResponseType.VAD_START, vad_start=True, raw_json=json_data)

    # 解析识别结果
    # results[0] 是到目前为止累积的全文；results[1..] 是分句
    text = ""
    index0 = next((r for r in results if r.get("index", 0) == 0), results[0])
    # 已经定稿的片段（is_interim=False）优先，否则用累积全文
    finalized = [r for r in results if r.get("is_interim") is False and r.get("index", 0) != 0]
    if finalized:
        text = "".join(r.get("text", "") for r in finalized)
    elif index0.get("text"):
        text = index0["text"]

    is_interim = index0.get("is_interim", True)
    vad_finished = bool(index0.get("is_vad_finished"))
    nonstream_result = any(
        r.get("extra", {}).get("nonstream_result") for r in results
    )

    # 最终结果：results[0] 定稿且 VAD 结束
    if is_interim is False and vad_finished:
        return ASRResponse(
            type=ResponseType.FINAL_RESULT,
            text=text,
            is_final=True,
            vad_finished=True,
            raw_json=json_data,
        )

    # 二遍纠正（nonstream）结果：对已识别内容的改写，不是终态
    if nonstream_result:
        return ASRResponse(
            type=ResponseType.REVISED_RESULT,
            text=text,
            is_final=False,
            raw_json=json_data,
        )

    # 中间结果
    return ASRResponse(
        type=ResponseType.INTERIM_RESULT,
        text=text,
        is_final=False,
        raw_json=json_data,
    )


# =============
# 便捷函数
# =============


async def transcribe(
    audio: str | Path | bytes,
    *,
    config: ASRConfig | None = None,
    on_interim: Callable[[str], None] | None = None,
    realtime: bool = False,
) -> str:
    """
    便捷函数：非流式语音识别

    Args:
        audio: 音频文件路径或 PCM 音频数据
        config: ASR 配置（可选）
        on_interim: 中间结果回调（可选）
        realtime: 是否模拟实时语音输入
            - True: 按音频实际时长发送，每帧间插入延迟，模拟实时的语音输入
            - False（默认）: 尽快发送所有帧，会更快拿到结果（不知道会不会被风控）

    Returns:
        最终识别文本
    """
    async with DoubaoASR(config) as asr:
        return await asr.transcribe(audio, on_interim=on_interim, realtime=realtime)


async def transcribe_stream(
    audio: str | Path | bytes,
    *,
    config: ASRConfig | None = None,
    realtime: bool = False,
) -> AsyncIterator[ASRResponse]:
    """
    便捷函数：流式语音识别（完整音频）

    Args:
        audio: 音频文件路径或 PCM 音频数据
        config: ASR 配置（可选）
        realtime: 是否模拟实时语音输入

    Yields:
        ASRResponse 对象
    """
    async with DoubaoASR(config) as asr:
        async for resp in asr.transcribe_stream(audio, realtime=realtime):
            yield resp


async def transcribe_realtime(
    audio_source: AsyncIterator[AudioChunk],
    *,
    config: ASRConfig | None = None,
) -> AsyncIterator[ASRResponse]:
    """
    便捷函数：实时流式语音识别（支持麦克风等持续音频源）

    Args:
        audio_source: PCM 音频数据的异步迭代器
            - 每个 chunk 应为 16-bit PCM 数据
            - 采样率和声道数应与 config 中配置一致
        config: ASR 配置（可选）

    Yields:
        ASRResponse 对象
    """
    async with DoubaoASR(config) as asr:
        async for resp in asr.transcribe_realtime(audio_source):
            yield resp
