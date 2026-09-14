# 依赖来源

- PhyAgentOS Core：同级 `PhyAgentOS-core`，MIT；通过导入使用其配置、客户端、校验和打包代码。
- Forge Gateway 1.0.2：<https://github.com/Forgelab-Robotics/adapter-forge-gateway/tree/v1.0.2>，Apache-2.0。
  `forge_tool` 尚未单独分发，直接使用该版本源码内的实现。源归档摘要锁定在
  `scripts/prepare_dependency.py`，解包到被忽略的 `.deps/forge-gateway/`；不修改协议源码。
  打包的设备节点包含它实际导入的 `forge_tool` 代码，Bundle 随附原 LICENSE 与 NOTICE。
- Gateway 可执行制品的锁取自 Resource Registry 的 `move-arm-by-ee 0.3.1` Bundle，已核对：
  `gateway-1.0.2-linux-x86_64-tar-gz`，归档 SHA-256
  `07584846f16012c6137b10cf743073cd2fe9a7f64564ba2322a7090e66fd3076`。
- Dora CLI / Python：0.4.1，`dora-message` 0.7.0；<https://github.com/dora-rs/dora/tree/v0.4.1>。
- `forge-msgs`：1.0.1；沿用 Gateway 依赖的 Arrow `CompressedImage` / `JointState` 格式。
- Unitree SDK2：同级 `unitree_sdk2-main`。本项目链接现有静态库与 DDS 动态库，不复制示例程序。
  随 Bundle 保留 SDK 及其已附带 CycloneDDS、CycloneDDS-CXX、iceoryx、RapidJSON 的许可证。
- Python 依赖的精确版本、来源和摘要见 `uv.lock`；构建节点使用 PyInstaller。

构建时把 Python 构建环境附带的 LICENSE/COPYING/NOTICE 文件保存到 Bundle 的
assets/python-licenses。本次仅本地集成，没有发布、上传制品或提交 PR。
