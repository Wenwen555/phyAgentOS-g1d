"""Run with the Core environment: python -m unittest discover -s tests_core."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PhyAgentOS.bus.queue import MessageBus  # noqa: E402
from PhyAgentOS.providers.base import LLMProvider, LLMResponse  # noqa: E402

from paos_g1d.agent_integration import G1dAgentLoop  # noqa: E402


class RecordingProvider(LLMProvider):
    def get_default_model(self):
        return "test-vision"

    async def chat(self, messages, **kwargs):
        self.messages = messages
        return LLMResponse(content="observed test image")


class ImageRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_grasp_verifier_is_disabled_with_forge(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SimpleNamespace(invoke_query_tool=AsyncMock(return_value={"data": {"response": {"result": {"status": "failed"}}}}))
            agent = G1dAgentLoop(MessageBus(), RecordingProvider(), Path(directory), forge_tool_client=client)
            self.assertFalse(agent.tools.has("grasp_verify"))
            self.assertTrue(agent.tools.has("image"))
            client.invoke_query_tool.assert_not_awaited()

    def test_project_entry_install_is_idempotent(self):
        from PhyAgentOS.agent import loop

        from paos_g1d.agent_integration import install

        original = loop.AgentLoop
        try:
            install()
            install()
            self.assertIs(loop.AgentLoop, G1dAgentLoop)
        finally:
            loop.AgentLoop = original

    async def test_native_image_tool_sends_image_to_existing_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingProvider()
            agent = G1dAgentLoop(MessageBus(), provider, Path(directory))
            self.assertIs(agent.provider, provider)
            self.assertTrue(agent.tools.has("image"))
            path = Path(directory) / "sample.png"
            # Payload transport is tested here; actual image decoding is covered online.
            path.write_bytes(b"synthetic-image-payload")
            result = await agent.tools.execute(
                "image",
                {
                    "mode": "vision",
                    "image_path": str(path),
                    "text": "Describe the image",
                },
            )
            self.assertEqual(result, "observed test image")
            self.assertTrue(
                provider.messages[-1]["content"][1]["image_url"]["url"].startswith(
                    "data:image/png;base64,"
                )
            )
            provider.messages = None
            result = await agent.tools.execute(
                "image",
                {
                    "mode": "vision",
                    "image_path": str(path.with_name("absent.png")),
                    "text": "Describe",
                },
            )
            self.assertIn("Error", result)
            self.assertIsNone(provider.messages)
