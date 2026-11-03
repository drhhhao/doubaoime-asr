from dataclasses import dataclass, field
import base64
import json
from pathlib import Path
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel

from .constants import (
    AID,
    DEFAULT_APP_KEY,
    DEFAULT_DEVICE_CONFIG,
    USER_AGENT,
    WEBSOCKET_URL,
)
from .device import DeviceCredentials, register_device, get_asr_token


class _AudioInfo(BaseModel):
    channel: int
    format: str = "speech_opus"
    sample_rate: int


class SessionConfig(BaseModel):
    """
    ASR 任务开始前需要初始化 Session 的配置

    字段与官方客户端 StartSession 的 payload 对齐（context 默认不带）。
    """
    audio_info: _AudioInfo
    lang: str = "zh"
    enable_speech_rejection: bool = True
    extra: Dict[str, Any]


@dataclass
class ASRConfig:
    """
    ASR 配置

    默认使用官方 PC 客户端内置的 appKey，只需要一个 device_id。
    如果没有提供 device_id，会自动注册虚拟设备并缓存到 credential_path。

    示例:
        # 自动注册设备（不持久化）
        config = ASRConfig()

        # 使用已有设备
        config = ASRConfig(device_id="1234567890123456")

        # 使用凭据文件（推荐，首次注册后自动缓存 device_id）
        config = ASRConfig(credential_path="~/.config/doubao-asr/credentials.json")

        # 改用 settings 接口下发的 appKey（有并发额度限制）
        config = ASRConfig(credential_path="...", use_settings_token=True)
    """
    url: str = WEBSOCKET_URL
    aid: str = str(AID)
    user_agent: str = USER_AGENT

    device_id: Optional[str] = None  # 空则自动注册
    token: Optional[str] = DEFAULT_APP_KEY
    """
    ASR appKey。默认用官方 PC 客户端的硬编码 key；
    设为 None 且 use_settings_token=True 时走 settings 接口。
    """
    use_settings_token: bool = False
    """
    True: 从 settings 接口获取 appKey（会撞 concurrency quota = 5）
    False（默认）: 使用 DEFAULT_APP_KEY
    """
    credential_path: Union[str, Path, None] = None
    """凭据文件路径，只缓存设备信息，appKey 不依赖该文件"""

    # 音频配置
    sample_rate: int = 16000
    channels: int = 1
    opus_frame_ms: int = 20
    """单个 Opus 帧时长。官方为 20ms。"""
    frame_duration_ms: int = 40
    """每个 WebSocket 音频包时长。官方为 40ms（2 个 20ms Opus 帧）。"""
    opus_bitrate: int = 32000
    opus_application: str = "lowdelay"
    """lowdelay / audio / voip，对应 opuslib 的三种 application"""

    # 会话配置（默认值与官方 PC 客户端一致）
    enable_punctuation: bool = True
    show_punc_type: Optional[int] = None
    """标点类型。None 时按 enable_punctuation 自动选择（开=3，关=0）。"""
    enable_speech_rejection: bool = True
    enable_asr_twopass: bool = True
    enable_asr_threepass: bool = True
    enable_text_filter: bool = True
    deep_text_format: bool = True
    format_space_at_newline: bool = True
    enable_print_chinese: bool = False
    enable_asr_format: bool = False
    disable_user_words: bool = False
    remove_space_between_han_eng: bool = False
    remove_space_between_han_num: bool = False
    s2a_send_enable: bool = False
    strong_ddc: bool = False
    use_twopass_retry: bool = True
    cell_compress_rate: int = 4
    input_mode: str = "shortcut_long"
    # 输入法当前作用在哪个应用上，服务器可能据此调整识别策略
    app_name: str = "com.android.chrome"
    app_version: str = "1.5.3"
    device_platform: str = "android"
    lang: str = "zh"

    context: Optional[Union[dict, str]] = None
    """
    可选的识别上下文，对应官方 payload 里 base64 编码的 context。
    传 dict 时会自动 JSON + base64；传 str 视为已经 base64 编码。
    官方会上传最近在各个应用里输入的文字，本项目默认关闭，避免泄露隐私。
    """
    extra_overrides: Dict[str, Any] = field(default_factory=dict)
    """直接覆盖/追加 StartSession.extra 字段"""

    frame_state_mode: str = "sequence"
    """
    sequence（默认）: field 9 发 1,2,3...N，与官方客户端一致，靠 FinishSession 结束
    enum: 旧行为，发 FIRST=1 / MIDDLE=3 / LAST=9
    """

    # 连接配置
    connect_timeout: float = 10.0
    recv_timeout: float = 15.0

    # 内部状态
    _credentials: Optional[DeviceCredentials] = field(default=None, repr=False)
    _initialized: bool = field(default=False, repr=False)

    def _load_credentials_from_file(self) -> Optional[DeviceCredentials]:
        """
        从缓存文件中加载凭据信息
        """
        if self.credential_path is None:
            return None

        path = Path(self.credential_path).expanduser()
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
                return DeviceCredentials(**data)

        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def _save_credentials_to_file(self, creds: DeviceCredentials):
        """
        保存凭据至缓存文件
        """
        if self.credential_path is None:
            return

        path = Path(self.credential_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(creds.model_dump(), f, indent=2, ensure_ascii=False)

    def ensure_credentials(self):
        """
        确保凭据已初始化

        device_id 优先级：
        1. 直接传入的 device_id
        2. credential_path 文件中的 device_id
        3. 自动注册

        appKey 优先级：
        1. 直接传入的 token
        2. use_settings_token=True 时调用 settings 接口
        3. DEFAULT_APP_KEY（默认）
        """
        if self._initialized:
            return

        file_creds = self._load_credentials_from_file()
        if file_creds:
            self._credentials = file_creds
            if self.device_id is None:
                self.device_id = file_creds.device_id

        need_save = False
        if self.device_id is None:
            creds = register_device()
            self._credentials = creds
            self.device_id = creds.device_id
            need_save = True

        if not self.token:
            if self.use_settings_token:
                cdid = self._credentials.cdid if self._credentials else None
                self.token = get_asr_token(self.device_id, cdid)
            else:
                self.token = DEFAULT_APP_KEY

        if self.credential_path and need_save and self._credentials:
            self._credentials.token = self.token
            self._save_credentials_to_file(self._credentials)

        self._initialized = True

    @property
    def ws_url(self) -> str:
        self.ensure_credentials()
        return f"{self.url}?aid={self.aid}&device_id={self.device_id}"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "proto-version": "v2",
            "x-custom-keepalive": "true",
        }

    def session_config(self) -> SessionConfig:
        self.ensure_credentials()

        audio_info = _AudioInfo(
            channel=self.channels,
            format="speech_opus",
            sample_rate=self.sample_rate,
        )

        show_punc_type = (
            self.show_punc_type
            if self.show_punc_type is not None
            else (3 if self.enable_punctuation else 0)
        )

        extra: Dict[str, Any] = {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "cell_compress_rate": self.cell_compress_rate,
            "deep_text_format": self.deep_text_format,
            "device_brand": DEFAULT_DEVICE_CONFIG["device_brand"],
            "device_model": DEFAULT_DEVICE_CONFIG["device_model"],
            "device_platform": self.device_platform,
            "did": self.device_id,
            "disable_user_words": self.disable_user_words,
            "enable_asr_format": self.enable_asr_format,
            "enable_asr_threepass": self.enable_asr_threepass,
            "enable_asr_twopass": self.enable_asr_twopass,
            "enable_print_chinese": self.enable_print_chinese,
            "enable_text_filter": self.enable_text_filter,
            "format_space_at_newline": self.format_space_at_newline,
            "input_mode": self.input_mode,
            "os": DEFAULT_DEVICE_CONFIG["os"],
            "os_version": DEFAULT_DEVICE_CONFIG["os_version"],
            "remove_space_between_han_eng": self.remove_space_between_han_eng,
            "remove_space_between_han_num": self.remove_space_between_han_num,
            "s2a_send_enable": self.s2a_send_enable,
            "show_punc_type": show_punc_type,
            "strong_ddc": self.strong_ddc,
            "use_twopass_retry": self.use_twopass_retry,
        }

        if self.context:
            if isinstance(self.context, str):
                extra["context"] = self.context
            else:
                raw = json.dumps(self.context, ensure_ascii=False).encode("utf-8")
                extra["context"] = base64.b64encode(raw).decode("ascii")

        extra.update(self.extra_overrides)

        return SessionConfig(
            audio_info=audio_info,
            lang=self.lang,
            enable_speech_rejection=self.enable_speech_rejection,
            extra=extra,
        )

    def get_token(self) -> str:
        self.ensure_credentials()
        return self.token
