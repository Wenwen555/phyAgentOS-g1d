"""The CLI passes free text through; only numeric CLI option selection is validated."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/diagnostics"))

import check_agent_task  # noqa: E402
from check_agent_task import parse_args  # noqa: E402


class PromptEntryTest(unittest.TestCase):
    def test_free_text_is_preserved_without_intent_or_distance_filter(self):
        for prompt in ["下降至最低点", "下降 15 cm", "请告诉我当前柱高", "先解释一下，不要动作",
                       "Move to the lowest position", "  我的第一行\n第二行  ", "上升500cm"]:
            with self.subTest(prompt=prompt):
                args = parse_args(["--profile", "real", "--prompt", prompt])
                self.assertEqual(args.prompt, prompt)
                self.assertIsNone(args.target_height_m)
                self.assertIsNone(args.delta_height_m)

    def test_numeric_modes_are_still_available(self):
        args = parse_args(["--profile", "real", "--target-height-m", "0"])
        self.assertIsNone(args.prompt)
        self.assertEqual(args.target_height_m, 0)


class PromptDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_main_delivers_unparsed_prompt_and_accepts_clarification_without_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                workspace_path=root,
                agents=SimpleNamespace(
                    defaults=SimpleNamespace(model="fake"), verification=SimpleNamespace()
                ),
            )
            client = MagicMock()
            client.invoke_query_tool = AsyncMock(return_value={"data": {"response": {"result": {
                "status": "succeeded", "outputs": {"height_m": 0.2, "simulated": False}
            }}}})
            client.close = AsyncMock()
            client.get_tool = AsyncMock(side_effect=AssertionError("No precomputed target needed"))
            coordinator = MagicMock()
            coordinator.store.active.return_value = None
            coordinator.verifier.start = AsyncMock()
            agent = MagicMock()
            agent.tools.tool_names = [
                "activate_skill", "read_file", "forge_tool_context", "forge_task_create",
                "forge_task_get", "forge_tool_query", "forge_tool_start_action",
                "forge_tool_action_status", "forge_tool_action_result", "forge_tool_cancel_action",
                "forge_task_cancel", "forge_task_finalize",
            ]
            native_execute = AsyncMock()
            agent.tools.execute = native_execute
            agent.process_direct = AsyncMock(return_value="我会先解释，不执行动作。")
            agent.close_mcp = AsyncMock()
            runtime = SimpleNamespace(profile="real", status="running")
            with (
                patch.object(check_agent_task, "ROOT", root),
                patch.object(check_agent_task, "set_config_path"),
                patch.object(check_agent_task, "load_config", return_value=config),
                patch.object(check_agent_task, "RuntimeStateStore") as store,
                patch.object(check_agent_task, "install"),
                patch.object(check_agent_task, "_make_provider"),
                patch.object(check_agent_task, "_make_forge_components",
                             return_value=(client, set(), coordinator, lambda _: True)),
                patch("PhyAgentOS.agent.loop.AgentLoop", return_value=agent),
            ):
                store.return_value.load.return_value = runtime
                prompt = "下降至最低点是什么意思？先解释，不要动作。"
                await check_agent_task.main("real", None, None, prompt)
            self.assertEqual(agent.process_direct.call_args.args[0], prompt)
            client.get_tool.assert_not_awaited()
            native_execute.assert_not_awaited()
            coordinator.create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
