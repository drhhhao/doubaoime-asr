# doubaoime-asr

豆包输入法语音识别 Python 客户端。

> 本 fork 基于 2026-11 对豆包输入法 PC 版 v0.9.3.20（ImeService.exe）的
> Frida 抓包逆向结果重写了协议实现，与官方客户端行为对齐。

## 免责声明

本项目通过对安卓豆包输入法客户端通信协议分析并参考客户端代码实现，**非官方提供的 API**。

- 本项目仅供学习和研究目的
- 不保证未来的可用性和稳定性
- 服务端协议可能随时变更导致功能失效

## 与上游（yangmoling/doubaoime-asr）的差异

| 项目 | 上游 | 本 fork |
|---|---|---|
| appKey 来源 | settings 接口下发（并发额度 = 5，多开即撞 `concurrency quota exceeded`） | 官方 PC 客户端硬编码 key（`OrnqKvSSrs`，Android 1.5.3 dex 同款），settings 接口改为可选 |
| 音频包 | 20ms 单帧 | 40ms = 2×20ms Opus 帧 code-3 复用，与官方一致 |
| protobuf field 9 | FIRST=1 / MIDDLE=3 / LAST=9 帧状态 | 递增序号 1,2,3…N（官方行为），结束只靠 FinishSession；旧行为可用 `frame_state_mode="enum"` 切回 |
| request_id | 裸 UUID | `ime_win_<uuid>` 前缀（官方格式） |
| StartSession payload | 精简版（`enable_punctuation`、`input_mode="tool"` 等） | 与官方 PC 客户端对齐的完整字段（`show_punc_type`、`enable_text_filter`、`deep_text_format` 等），可选 `context` 识别上下文 |
| 最终结果判定 | 遇到 `nonstream_result` 即视为最终结果（**有误**） | `results[0].is_interim == false 且 is_vad_finished == true` 才是终态；nonstream 片段单独作为 `REVISED_RESULT`（二遍纠正）事件 |
| 多分句结果 | `text` 会被最后一个分句覆盖 | 优先拼接已定稿分句，否则用 `results[0]` 累积全文 |
| 兜底 | 无 | SessionFinished 前未收到明确终态时，用最近文本补一个 FINAL_RESULT |
| 测试 | 无 | `tests/test_offline.py`：组包与官方重放逻辑逐字节比对、响应解析、payload 字段校验（离线，不联网） |

## 安装

```bash
# 从本地安装
git clone https://github.com/starccy/doubaoime-asr.git
cd doubaoime-asr
pip install -e .

# 或从 Git 仓库安装
pip install git+https://github.com/starccy/doubaoime-asr.git
```

### 系统依赖

本项目依赖 Opus 音频编解码库，需要先安装系统库：

```bash
# Debian/Ubuntu
sudo apt install libopus0

# Arch Linux
sudo pacman -S opus

# macOS
brew install opus
```

## 快速开始

### 基本用法

```python
import asyncio
from doubaoime_asr import transcribe, ASRConfig

async def main():
    # 配置（首次运行会自动注册设备，并将凭据保存到指定文件）
    config = ASRConfig(credential_path="./credentials.json")

    # 识别音频文件
    result = await transcribe("audio.wav", config=config)
    print(f"识别结果: {result}")

asyncio.run(main())
```

### 流式识别

如果需要获取中间结果或更详细的状态信息，可以使用 `transcribe_stream`：

```python
import asyncio
from doubaoime_asr import transcribe_stream, ASRConfig, ResponseType

async def main():
    config = ASRConfig(credential_path="./credentials.json")

    async for response in transcribe_stream("audio.wav", config=config):
        match response.type:
            case ResponseType.INTERIM_RESULT:
                print(f"[中间结果] {response.text}")
            case ResponseType.REVISED_RESULT:
                # 二遍纠正（nonstream）结果：服务端对已识别内容的改写
                print(f"[纠正结果] {response.text}")
            case ResponseType.FINAL_RESULT:
                print(f"[最终结果] {response.text}")
            case ResponseType.ERROR:
                print(f"[错误] {response.error_msg}")

asyncio.run(main())
```

### 识别上下文（可选）

官方客户端会把用户最近在各个应用里输入的文字作为识别上下文上传（`context` 字段，
base64 编码的 JSON）。本 fork 默认**不上传**，避免泄露隐私。需要提升专有名词识别率时可以自行开启：

```python
config = ASRConfig(
    credential_path="./credentials.json",
    context={"chat": [{"app_apk_name": "myapp", "type": "asr_input",
                       "time": 1730000000000, "data": {"text": "蒜鸟"}}]},
)
```

### 实时麦克风识别

实时语音识别需要配合音频采集库使用，请参考 [examples/mic_realtime.py](examples/mic_realtime.py)。

运行示例需要安装额外依赖：

```bash
pip install sounddevice numpy
# 或
pip install doubaoime-asr[examples]
```

## API 参考

### transcribe

非流式语音识别，直接返回最终结果。

```python
async def transcribe(
    audio: str | Path | bytes,
    *,
    config: ASRConfig | None = None,
    on_interim: Callable[[str], None] | None = None,
    realtime: bool = False,
) -> str
```

参数：
- `audio`: 音频文件路径或 PCM 字节数据
- `config`: ASR 配置
- `on_interim`: 中间结果回调
- `realtime`: 是否模拟实时发送（按音频时长插入延迟）

### transcribe_stream

流式语音识别，返回 `ASRResponse` 异步迭代器。

```python
async def transcribe_stream(
    audio: str | Path | bytes,
    *,
    config: ASRConfig | None = None,
    realtime: bool = False,
) -> AsyncIterator[ASRResponse]
```

### transcribe_realtime

实时流式语音识别，接收 PCM 音频数据的异步迭代器。

```python
async def transcribe_realtime(
    audio_source: AsyncIterator[bytes],
    *,
    config: ASRConfig | None = None,
) -> AsyncIterator[ASRResponse]
```

### ASRConfig

配置类，支持以下主要参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `credential_path` | str | None | 凭据缓存文件路径（只缓存设备信息） |
| `device_id` | str | None | 设备 ID（空则自动注册） |
| `token` | str | 官方 PC 客户端 appKey | ASR appKey（覆盖默认值） |
| `use_settings_token` | bool | False | True 时改用 settings 接口下发的 appKey（并发额度 = 5） |
| `sample_rate` | int | 16000 | 采样率 |
| `channels` | int | 1 | 声道数 |
| `frame_duration_ms` | int | 40 | 每个音频包时长（官方 40ms） |
| `opus_frame_ms` | int | 20 | 单个 Opus 帧时长 |
| `opus_bitrate` | int | 32000 | Opus 码率 |
| `opus_application` | str | "lowdelay" | Opus application（lowdelay / audio / voip） |
| `enable_punctuation` | bool | True | 是否启用标点（映射到 `show_punc_type`） |
| `show_punc_type` | int | None | 直接指定标点类型（None 时按 `enable_punctuation` 自动选择） |
| `context` | dict / str | None | 识别上下文（dict 自动 JSON+base64；默认不上传） |
| `extra_overrides` | dict | {} | 覆盖/追加 StartSession.extra 字段 |
| `frame_state_mode` | str | "sequence" | field 9 模式：sequence（官方递增序号）/ enum（旧 FIRST/MIDDLE/LAST） |
| `app_name` | str | "com.android.chrome" | 输入法作用的目标应用 |

### ResponseType

响应类型枚举：

| 类型 | 说明 |
|------|------|
| `TASK_STARTED` | 任务已启动 |
| `SESSION_STARTED` | 会话已启动 |
| `VAD_START` | 检测到语音开始 |
| `INTERIM_RESULT` | 中间识别结果 |
| `REVISED_RESULT` | 二遍纠正（nonstream）结果，对已识别内容的改写，不是终态 |
| `FINAL_RESULT` | 最终识别结果 |
| `SESSION_FINISHED` | 会话结束 |
| `ERROR` | 错误 |

## 凭据管理

首次使用时会自动向服务器注册虚拟设备（设备参数定义在 `constants.py` 的 `DEFAULT_DEVICE_CONFIG` 中）。

appKey 默认使用官方 PC 客户端硬编码的值（见 `constants.py` 的 `DEFAULT_APP_KEY`），
不依赖注册流程；settings 接口下发的 appKey 有并发额度限制，仅作为可选方案（`use_settings_token=True`）。

推荐指定 `credential_path` 参数，设备信息会自动缓存到文件，避免重复注册：

```python
config = ASRConfig(credential_path="~/.config/doubaoime-asr/credentials.json")
```
