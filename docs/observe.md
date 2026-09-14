# g1d.observe

只读 Query，位于独立模块 `src/paos_g1d/observe.py`，通过现有 Forge Gateway 调用。
支持 g1d-observe 0.1.1、g1d-robot 0.1.6、g1d-lift 0.1.1；不需要启动第二个控制器。

```bash
./scripts/core-env.sh python scripts/observe_once.py --sources head
./scripts/core-env.sh python scripts/observe_once.py --sources head left_wrist right_wrist
```

仅模拟数据使用 `--profile mock`。默认 Gateway 为 http://127.0.0.1:19002，可用 --gateway 修改。
CLI 只执行观察 Query，不调用模型、不触发 Action，不切换或重启运行时。

相机配置位于 `configs/cameras.yaml`，打包时复制到各 Skill 的 `assets/cameras.yaml`。
2026-09-10 按实机左右位置修正映射：

| source | 实际位置 | 网页端口 | JPEG ZMQ 端口 |
| --- | --- | --- | --- |
| head | 头部 | 60001 | 55555 |
| left_wrist | 左腕 | 60003 | 55557 |
| right_wrist | 右腕 | 60002 | 55556 |

设备节点通过 `stream_url` 订阅 ZMQ 图像流；网页地址通过 `web_url` 提供。
配置在设备运行时启动时读取，修改后需要重启才生效。历史观察清单不会重新标注。

OS 调用参数：
```json
{"sources":["head","left_wrist"],"max_age_ms":500,"max_skew_ms":100}
```

默认 head，摄像头最大接收年龄 500 ms，最大时间差 100 ms；关节年龄仍不超过 100 ms。
所有请求的图像都要可用；不请求的摄像头缺失不影响本次观察。关节反馈不依赖升降柱。

返回 observation_id、manifest_path、images（图像路径/摘要/序列/接收时间/年龄）、
robot_state（关节角/参考/IMU等）、state_read_window_ms 与 receive_skew_upper_bound_ms。
图像采用内容摘要路径，清单独立保存。请求中固定图像帧，避免异步读状态期间替换成另一帧。
本机 monotonic 时间仅可在同一主机同一启动周期比较；collected_at_utc 是整理时间，不是曝光时间。
时间差包含状态读取调用的时间不确定区间，是接收时间差上界，不是物理采样时间误差上界。

association 固定为 best_effort_receive_time。尚未提供检测、深度、相机标定或三维物体坐标。
head 为拼接双目图（1280×480 内并排两个 640×480 眼）。此前右半模糊的问题已修复，
2026-09-10 起的实拍帧两半清晰度相当。下游可按 observation_id 引用证据，
再接入检测和标定后的定位；不能把图像像素坐标直接当成机器人目标坐标。

验证：测试覆盖内容摘要与清单一致、帧固定、缺失/过期/黑暗/时间差过大、状态过期、
模拟身份不一致、输入校验及无动作副作用。真实帧验证需要正在运行的上述新版本。
