"""Evaluate a vision model on real G1-D frames whose ground truth was read by a human.

Run from the project root:

    ./scripts/core-env.sh python scripts/diagnostics/evaluate_vision.py [--model deepseek-flash]

Each question is asked in its own request so one wrong answer cannot contaminate the next, and
every answer is compared against a ground truth recorded by inspecting the frame directly.

The frames come from the 2026-09-11 real-robot sessions:
  16:56 / 17:23  left_wrist  a white box-like object held close to the wrist camera
  12:22          left_wrist  a different pose: the robot's own white shell and a wooden cabinet
  16:56 / 17:23  head        binocular pair, robot arm and a human hand in frame

Sends images to the configured model endpoint. Read-only: no robot connection, no motion.
"""

import argparse
import base64
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]

# (frame, question, expected answer, note). Ground truth read from the frames themselves.
CASES = [
    (
        "16:56 left_wrist (白盒近景)",
        "left_wrist-86cde5b1929c974d4652817ea11180b2c05f1ba9486b766b9b39cef013a8efe2.jpg",
        "画面正前方是否有一个白色盒状物体，近距离占据画面大部分？",
        "yes",
        "白盒占画面中央大半，上下被裁切",
    ),
    (
        "16:56 left_wrist",
        "left_wrist-86cde5b1929c974d4652817ea11180b2c05f1ba9486b766b9b39cef013a8efe2.jpg",
        "画面中能否看到机器人的夹爪手指？",
        "yes",
        "两个黑色带斜纹的锥形手指在画面下方左右两侧，向中间收拢（放大 3x 可确认）。"
        "此前误记为 no，并把模型答 no 记成正确——实际是模型漏判",
    ),
    (
        "16:56 left_wrist",
        "left_wrist-86cde5b1929c974d4652817ea11180b2c05f1ba9486b766b9b39cef013a8efe2.jpg",
        "画面中是否出现不锈钢/金属材质的壶或杯？",
        "yes",
        "左侧有金属壶",
    ),
    (
        "16:56 left_wrist",
        "left_wrist-86cde5b1929c974d4652817ea11180b2c05f1ba9486b766b9b39cef013a8efe2.jpg",
        "画面中是否有人的面部或人体？",
        "yes",
        "背景两人",
    ),
    (
        "17:23 left_wrist (白盒近景)",
        "left_wrist-02cfe216b4a0285fb17f19cb6e117f97af176a3f72fbca32a59d65169872a0bf.jpg",
        "画面正前方是否有一个白色盒状物体，近距离占据画面大部分？",
        "yes",
        "同 16:56 的白盒，角度略异",
    ),
    (
        "17:23 left_wrist",
        "left_wrist-02cfe216b4a0285fb17f19cb6e117f97af176a3f72fbca32a59d65169872a0bf.jpg",
        "画面中能否看到机器人的夹爪手指？",
        "yes",
        "同 16:56：黑色手指在画面下方左右两侧",
    ),
    (
        "12:22 left_wrist (负样本)",
        "left_wrist-ffa923787d232bad2781331f7ea7719c0538182640829d1dec45160ef0f278f6.jpg",
        "画面中是否有木质柜子或木板？",
        "yes",
        "左侧木柜",
    ),
    (
        "12:22 left_wrist",
        "left_wrist-ffa923787d232bad2781331f7ea7719c0538182640829d1dec45160ef0f278f6.jpg",
        "画面中是否有一个近距离的白色盒状可抓取物体？",
        "no",
        "该帧是腕部相机拍到机械臂自身：白面板上有明显螺栓、黑支架，底部两个深色锥形才是夹爪手指。"
        "可见的紧固件本应提示这是机械结构而非被抓物体",
    ),
    (
        "12:22 left_wrist",
        "left_wrist-ffa923787d232bad2781331f7ea7719c0538182640829d1dec45160ef0f278f6.jpg",
        "画面中是否出现不锈钢/金属材质的壶或杯？",
        "no",
        "该帧无金属壶",
    ),
    (
        "16:56 head (双目)",
        "head-ae1ae64ca285715a2bfc2b62a51dc5021e28d6985136cc6302016732decaa206.jpg",
        "这张图是否由左右两幅并排构成（双目）？",
        "yes",
        "1280x480 两个 640x480 并排",
    ),
    (
        "16:56 head",
        "head-ae1ae64ca285715a2bfc2b62a51dc5021e28d6985136cc6302016732decaa206.jpg",
        "左右两幅的清晰度是否明显不同（其中一幅更模糊）？",
        None,
        "不计分。该设备问题已修复：09-08 帧左/右边缘能量比中位 2.06（右半明显模糊），"
        "09-10 起降到 0.91–0.94（两半相当）。本帧是修复后的，'明显不同'已不是它的真实属性，"
        "模型答 no 正确。保留此题用于将来回归——若哪天比值回到 >1.5 即说明缺陷复现",
    ),
    (
        "16:56 head",
        "head-ae1ae64ca285715a2bfc2b62a51dc5021e28d6985136cc6302016732decaa206.jpg",
        "画面中是否出现人的手？",
        "yes",
        "人物抬起的手清晰可见",
    ),
    (
        "17:23 head (双目)",
        "head-b02e827dc7ba8ae4959defe7654ccf941e850f3df8ac8ac942c2b39ab174419e.jpg",
        "这张图是否由左右两幅并排构成（双目）？",
        "yes",
        "同型号双目",
    ),
    (
        "17:23 head",
        "head-b02e827dc7ba8ae4959defe7654ccf941e850f3df8ac8ac942c2b39ab174419e.jpg",
        "画面中是否有人正在操作笔记本电脑？",
        "yes",
        "人物俯身打字",
    ),
    (
        "17:23 head",
        "head-b02e827dc7ba8ae4959defe7654ccf941e850f3df8ac8ac942c2b39ab174419e.jpg",
        "画面中是否能辨认出机械臂的关节结构？",
        "yes",
        "左下可见机械臂各关节",
    ),
]

PROMPT = (
    "Answer only from what you can actually see in the image. "
    'Reply with one JSON object and nothing else: {"answer": "yes" | "no" | "uncertain", '
    '"evidence": "<= 20 words describing the region you looked at"}'
)


def ask(client, base, key, model, path, question, max_tokens):
    data = base64.b64encode(path.read_bytes()).decode()
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{question}\n\n{PROMPT}"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{data}"},
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    t0 = time.perf_counter()
    r = client.post(
        f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body
    )
    elapsed = time.perf_counter() - t0
    if r.status_code != 200:
        return {"answer": f"HTTP{r.status_code}", "evidence": r.text[:80], "elapsed": elapsed}
    payload = r.json()
    choice = payload["choices"][0]
    text = (choice["message"].get("content") or "").strip()
    usage = payload.get("usage", {})
    answer, evidence = "unparseable", text[:80]
    if "{" in text:
        try:
            parsed = json.loads(text[text.index("{") : text.rindex("}") + 1])
            answer = str(parsed.get("answer", "?")).lower().strip()
            evidence = str(parsed.get("evidence", ""))[:80]
        except Exception:
            pass
    return {
        "answer": answer,
        "evidence": evidence,
        "elapsed": elapsed,
        "prompt": usage.get("prompt_tokens"),
        "completion": usage.get("completion_tokens"),
        "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "finish": choice.get("finish_reason"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="defaults to the configured verifier model")
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--image-dir", default="workspace/g1d_snapshots")
    args = parser.parse_args()

    config = json.loads((ROOT / ".paos-instance/config.json").read_text(encoding="utf-8"))
    key = config["providers"]["deepseek"]["apiKey"]
    base = config["providers"]["deepseek"]["apiBase"].rstrip("/")
    model = args.model or config["agents"]["verification"]["model"]
    image_dir = Path(args.image_dir)

    print(f"模型: {model}   max_tokens={args.max_tokens}   题目: {len(CASES)}\n")
    rows = []
    with httpx.Client(timeout=300, trust_env=False) as client:
        for label, filename, question, expected, note in CASES:
            path = image_dir / filename
            if not path.is_file():
                print(f"  跳过（缺文件）: {filename}")
                continue
            result = ask(client, base, key, model, path, question, args.max_tokens)
            correct = None if expected is None else result["answer"] == expected
            rows.append((label, question, expected, result, correct, note))
            mark = "–" if correct is None else ("✓" if correct else "✗")
            shown = "不计分" if expected is None else expected
            print(
                f"  {mark} [{label}] {question}\n"
                f"      期望 {shown:9s} 实得 {result['answer']:9s} "
                f"{result['elapsed']:.1f}s tok={result.get('prompt')}/"
                f"{result.get('completion')} | {result['evidence']}"
            )

    scored = [r for r in rows if r[4] is not None]
    correct_n = sum(1 for r in scored if r[4])
    total = len(scored)
    print(
        f"\n{'=' * 78}\n计分 {correct_n}/{total} = {correct_n * 100 // max(total, 1)}%"
        f"   （另有 {len(rows) - total} 题因 ground truth 不可验证而不计分）"
    )
    lenses = [r[3]["elapsed"] for r in rows]
    if lenses:
        print(f"单题延迟 中位数 {statistics.median(lenses):.1f}s  最慢 {max(lenses):.1f}s")
    print("\n错题：")
    wrong = [r for r in scored if not r[4]]
    if not wrong:
        print("  无")
    for label, question, expected, result, _, note in wrong:
        print(f"  [{label}] {question}")
        print(f"      期望 {expected} / 实得 {result['answer']} — 实况：{note}")
        print(f"      模型说：{result['evidence']}")
    unscored = [r for r in rows if r[4] is None]
    if unscored:
        print("\n不计分题（ground truth 无法确认，模型回答仅供参考）：")
        for label, question, _, result, _, note in unscored:
            print(f"  [{label}] {question}")
            print(f"      模型答 {result['answer']}：{result['evidence']}")
            print(f"      {note}")


if __name__ == "__main__":
    sys.exit(main())
