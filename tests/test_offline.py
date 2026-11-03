"""
离线单元测试：不联网

- 官方格式的 Opus 包构造（40ms = 2 x 20ms code-3 复用）
- 响应解析（中间 / 最终 / 二遍纠正）
"""
import json

import opuslib
import pytest

from doubaoime_asr.asr import _parse_response, ResponseType
from doubaoime_asr.asr_pb2 import AsrResponse as AsrResponsePb
from doubaoime_asr.audio import AudioEncoder
from doubaoime_asr.config import ASRConfig


def _make_encoder():
    config = ASRConfig()
    return AudioEncoder(config), config


def _sine_pcm(seconds: float = 0.2, sample_rate: int = 16000, freq: float = 440.0) -> bytes:
    import math
    import struct

    n = int(seconds * sample_rate)
    return b"".join(
        struct.pack("<h", int(3200 * math.sin(2 * math.pi * freq * i / sample_rate)))
        for i in range(n)
    )


def test_packet_is_two_20ms_frames():
    """每个包 40ms：TOC code=3，包含 2 个 20ms 帧"""
    encoder, config = _make_encoder()
    packets = encoder.pcm_to_opus_packets(_sine_pcm(0.2))

    assert len(packets) == 5  # 200ms / 40ms
    for packet in packets:
        assert (packet[0] & 0x03) == 3  # TOC code 3
        assert packet[1] == 0x80 | 2  # VBR, 2 帧


def test_packet_matches_reference_implementation():
    """
    与逆向时验证过的官方重放组包逻辑逐字节一致
    （reverse/asr_test/official_params.py 里的手工 repacketize）
    """
    encoder, config = _make_encoder()
    pcm = _sine_pcm(0.1)

    # 官方重放代码的组包方式
    enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_RESTRICTED_LOWDELAY)
    enc.bitrate = 32000
    expected = []
    for i in range(0, len(pcm), 640 * 2):
        chunk = pcm[i:i + 1280].ljust(1280, b"\0")
        a, b = enc.encode(chunk[:640], 320), enc.encode(chunk[640:], 320)
        toc = (a[0] & 0xFC) | 3
        la = len(a) - 1
        expected.append(
            bytes([toc, 0x80 | 2])
            + (bytes([la]) if la < 252 else bytes([252 + (la - 252) % 4, (la - 252) // 4]))
            + a[1:] + b[1:]
        )

    assert encoder.pcm_to_opus_packets(pcm) == expected


def test_packet_decodes_back_to_40ms():
    """构造出来的包是标准 Opus 包，能解出 640 个样本（40ms）"""
    encoder, config = _make_encoder()
    packets = encoder.pcm_to_opus_packets(_sine_pcm(0.2))

    decoder = opuslib.Decoder(16000, 1)
    pcm = decoder.decode(packets[0], 640)
    assert len(pcm) == 640 * 2
    assert any(pcm)  # 不是全静音


def test_odd_pcm_padded_to_full_packet():
    encoder, config = _make_encoder()
    packets = encoder.pcm_to_opus_packets(_sine_pcm(0.09))  # 2.25 个包
    assert len(packets) == 3
    for packet in packets:
        assert (packet[0] & 0x03) == 3


def _wrap(result_json: dict) -> bytes:
    pb = AsrResponsePb()
    pb.message_type = "TaskResponse"
    pb.result_json = json.dumps(result_json, ensure_ascii=False)
    return pb.SerializeToString()


def test_parse_interim_result():
    data = _wrap({"results": [
        {"text": "今天天气不", "is_interim": True, "index": 0},
    ], "extra": {}})
    resp = _parse_response(data)
    assert resp.type == ResponseType.INTERIM_RESULT
    assert resp.text == "今天天气不"
    assert resp.is_final is False


def test_parse_final_result():
    data = _wrap({"results": [
        {"text": "今天天气不错，我们下午去公园。", "is_interim": False, "is_vad_finished": True, "index": 0},
    ], "extra": {}})
    resp = _parse_response(data)
    assert resp.type == ResponseType.FINAL_RESULT
    assert resp.text == "今天天气不错，我们下午去公园。"
    assert resp.is_final is True


def test_parse_multi_segment_final_uses_finalized_segments():
    """多分句时不能拿最后一句当全文"""
    data = _wrap({"results": [
        {"text": "今天天气不错，我们下午去公园。第三句在这里。", "is_interim": False, "is_vad_finished": True, "index": 0},
        {"text": "今天天气不错，", "is_interim": False, "index": 1},
        {"text": "我们下午去公园。", "is_interim": False, "index": 2},
        {"text": "第三句在这里。", "is_interim": False, "index": 3},
    ], "extra": {}})
    resp = _parse_response(data)
    assert resp.type == ResponseType.FINAL_RESULT
    assert resp.text == "今天天气不错，我们下午去公园。第三句在这里。"


def test_parse_nonstream_is_revised_not_final():
    """nonstream_result 是二遍纠正，不是终态"""
    data = _wrap({"results": [
        {"text": "1. 第一句 2. 第二句", "is_interim": True, "index": 0},
        {"text": "第一句", "is_interim": False, "index": 1,
         "extra": {"nonstream_result": True}},
    ], "extra": {}})
    resp = _parse_response(data)
    assert resp.type == ResponseType.REVISED_RESULT
    assert resp.text == "第一句"
    assert resp.is_final is False


def test_parse_heartbeat():
    data = _wrap({"extra": {"packet_number": 42}})
    resp = _parse_response(data)
    assert resp.type == ResponseType.HEARTBEAT
    assert resp.packet_number == 42


def test_parse_session_failed():
    pb = AsrResponsePb()
    pb.message_type = "SessionFailed"
    pb.status_message = "no access right"
    resp = _parse_response(pb.SerializeToString())
    assert resp.type == ResponseType.ERROR
    assert "no access right" in resp.error_msg


def test_session_config_keys():
    """StartSession payload 与官方字段对齐"""
    config = ASRConfig(device_id="1234567890123456")
    payload = json.loads(config.session_config().model_dump_json())

    assert payload["audio_info"] == {
        "channel": 1, "format": "speech_opus", "sample_rate": 16000,
    }
    assert payload["lang"] == "zh"
    assert payload["enable_speech_rejection"] is True

    extra = payload["extra"]
    assert extra["app_name"] == "com.android.chrome"
    assert extra["app_version"] == "1.5.3"
    assert extra["did"] == "1234567890123456"
    assert extra["cell_compress_rate"] == 4
    assert extra["input_mode"] == "shortcut_long"
    assert extra["enable_asr_twopass"] is True
    assert extra["enable_asr_threepass"] is True
    assert extra["enable_text_filter"] is True
    assert extra["show_punc_type"] == 3
    assert "enable_punctuation" not in payload  # 官方不发这个字段
    assert "context" not in extra  # 默认不上传输入上下文


def test_punctuation_off_sets_show_punc_type():
    config = ASRConfig(device_id="1234567890123456", enable_punctuation=False)
    payload = json.loads(config.session_config().model_dump_json())
    assert payload["extra"]["show_punc_type"] == 0


def test_context_encoded_as_base64():
    config = ASRConfig(
        device_id="1234567890123456",
        context={"chat": [{"data": {"text": "你好"}}]},
    )
    payload = json.loads(config.session_config().model_dump_json())
    import base64
    decoded = json.loads(base64.b64decode(payload["extra"]["context"]))
    assert decoded["chat"][0]["data"]["text"] == "你好"


def test_default_token_is_official_appkey():
    config = ASRConfig(device_id="1234567890123456")
    assert config.get_token()  # 不为空
