# 安装机械臂与 observe（现有项目环境）

以下在 `/home/robot/project/dfx-inspire-service/unitree-g1d/PhyAgentOS-g1d` 执行。
`g1d-robot` 已包含观察工具，不需要同时启动 `g1d-observe`，两者默认共用 19002 端口。
项目入口自动把 `socks://` 代理别名转换成 `socks5://`，无需为本地安装清空代理。

## 1. 检查机器人网络

```bash
nmcli connection up uuid 6c75cd12-99e7-3b8e-ae08-656666c97c0a
ip -brief address
nmcli -f GENERAL.STATE,GENERAL.CONNECTION,WIRED-PROPERTIES.CARRIER device show enx9c69d36fbc0b
```

USB 网卡应为 UP，地址为 192.168.123.99/24，carrier 应开启。
NetworkManager 显示“已连接”或 IP 已配置，不代表网线物理链路已建立。
如果 carrier 关闭，检查网线、转接器以及机器人是否开机，先不要重试启动。

## 2. 停止旧运行时

先让机械臂回位或获得支撑，停止运行时会结束持续姿态保持。
```bash
./scripts/core-env.sh paos skill stop g1d-robot
```
不要用 force 跳过正在执行的动作。

## 3. 安装 Skill 与锁定的两个 Node

```bash
./scripts/core-env.sh paos skill install dist/skills/g1d-robot-0.1.6.tar.gz --local --yes
./scripts/core-env.sh paos forge-node install g1d-robot gateway --archive dist/vendor/gateway-1.0.2.tar.gz
./scripts/core-env.sh paos forge-node install g1d-robot g1d --archive dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz
```

## 4. 启动真实运行时并检查

```bash
./scripts/core-env.sh paos skill start g1d-robot --profile real
./scripts/core-env.sh paos skill status g1d-robot
./scripts/core-env.sh python scripts/run_arm_agent.py --profile real --check-only
./scripts/core-env.sh python scripts/observe_once.py --sources head left_wrist right_wrist
```

后两条仅检查/观察，不发送运动动作。observe 成功返回 JSON 和图像/清单路径。
若观察失败，按返回的图像过期、状态过期或接收时间差原因排查，不代表应该重新运动。

## 5. 进入 PAOS 对话

```bash
./scripts/core-env.sh paos agent
```

输入：
> 激活 g1d-robot，调用 g1d.observe 获取头部、左腕和右腕的最新图像及机械臂状态，再调用视觉工具告诉我看到了什么。只观察，不移动机器人。

## 启动失败日志

```bash
tail -80 .paos-instance/logs/skills/paos-g1d-robot-real-dora.log
```

2026-09-10 这次失败的第一处错误是 `G1-D DDS initialization failed`，当时网卡 DOWN 且 carrier=0。
`Dora flow is not running` 是节点初始化失败后的健康检查结果，不是延长超时即可解决的问题。
