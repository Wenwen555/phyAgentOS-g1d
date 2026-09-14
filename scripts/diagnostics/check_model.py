"""Online Core provider checks using synthetic inputs and a local, read-only tool."""

import asyncio
import base64
import json
import os
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from init_instance import CONFIG, ROOT  # noqa: E402
from PhyAgentOS.cli.commands import _make_provider  # noqa: E402
from PhyAgentOS.config.loader import load_config, set_config_path  # noqa: E402


def test_image():
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes([255, 0, 0]) * 128 + bytes([0, 0, 255]) * 128
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 256, 128, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * 128))
        + chunk(b"IEND", b"")
    )


async def main():
    set_config_path(CONFIG)
    config = load_config()
    provider = _make_provider(config)
    secret = config.get_provider(config.agents.defaults.model).api_key
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": config.agents.defaults.model,
        "path": "Core _make_provider -> LiteLLMProvider.chat -> configured API",
        "synthetic_inputs_only": True,
        "checks": {},
    }

    async def call(messages, **kwargs):
        response = await asyncio.wait_for(
            provider.chat(messages, max_tokens=2048, temperature=0.1, **kwargs), timeout=60
        )
        if response.finish_reason == "error":
            raise RuntimeError((response.content or "API error").replace(secret, "[REDACTED]"))
        return response

    async def text_check():
        response = await call(
            [{"role": "user", "content": "Calculate 17 + 26. Reply with only the integer."}]
        )
        assert (response.content or "").strip() == "43", response.content
        return {"reply": response.content, "usage": response.usage}

    async def vision_check():
        response = await call(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": 'Identify the solid color on the left half and on the right half. Reply only as JSON: {"left":"color","right":"color"}. Use English color names.',
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(test_image()).decode()
                            },
                        },
                    ],
                }
            ]
        )
        from json_repair import loads

        result = loads(response.content or "")
        assert result == {"left": "red", "right": "blue"}, response.content
        return {"reply": result, "usage": response.usage}

    async def tools_check():
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_test_label",
                    "description": "Read a synthetic test label from a local dictionary. No hardware or network access.",
                    "parameters": {
                        "type": "object",
                        "properties": {"label_id": {"type": "string", "enum": ["sample"]}},
                        "required": ["label_id"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        messages = [
            {
                "role": "user",
                "content": "Call read_test_label for label_id sample. Then reply with only the returned label value. You must obtain it from the tool.",
            }
        ]
        response = await call(messages, tools=tools)
        assert len(response.tool_calls) == 1, "Expected one structured tool call"
        tool = response.tool_calls[0]
        assert tool.name == "read_test_label" and tool.arguments == {"label_id": "sample"}
        assistant = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [tool.to_openai_tool_call()],
        }
        if response.reasoning_content is not None:
            assistant["reasoning_content"] = response.reasoning_content
        value = "SYNTHETIC-" + os.urandom(4).hex().upper()
        messages.extend(
            [
                assistant,
                {"role": "tool", "tool_call_id": tool.id, "content": json.dumps({"label": value})},
            ]
        )
        final = await call(messages, tools=tools)
        assert not final.tool_calls and (final.content or "").strip() == value, final.content
        return {
            "tool": tool.name,
            "arguments": tool.arguments,
            "result_roundtrip": True,
            "usage": [response.usage, final.usage],
        }

    async def check(name, operation):
        started = time.monotonic()
        try:
            result = await operation()
            result["passed"] = True
        except Exception as error:
            result = {
                "passed": False,
                "error": (str(error) or type(error).__name__).replace(secret, "[REDACTED]")[:1500],
            }
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        report["checks"][name] = result
        print(json.dumps({name: result}, ensure_ascii=False), flush=True)

    await check("text", text_check)
    await asyncio.gather(check("vision", vision_check), check("tools", tools_check))
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "dist/model-check-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    if not all(result["passed"] for result in report["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
