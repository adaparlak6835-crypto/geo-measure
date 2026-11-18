#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
geo-measure: 最小可用的 GEO 测量工具

用法:
    export DEEPSEEK_API_KEY=sk-xxx
    python measure.py --prompts prompts.json --engine deepseek --out result.json

    # 计算指标
    python measure.py --report result.json
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

# 各引擎的 API 配置
ENGINES = {
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "qwen": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "key_env": "DASHSCOPE_API_KEY",
        "model": "qwen-plus",
    },
    "moonshot": {
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "key_env": "MOONSHOT_API_KEY",
        "model": "moonshot-v1-8k",
    },
}


def call_engine(engine: str, question: str, timeout: int = 120) -> dict:
    """调用一个引擎，返回 {answer, error, latencyMs, usage}"""
    cfg = ENGINES.get(engine)
    if not cfg:
        return {"answer": "", "error": f"unknown engine: {engine}", "latencyMs": 0}

    key = os.environ.get(cfg["key_env"])
    if not key:
        return {"answer": "", "error": f"missing {cfg['key_env']}", "latencyMs": 0}

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "请基于你的知识回答，如果使用了联网搜索，请保留引用标记。"},
            {"role": "user", "content": question},
        ],
        "temperature": 0.7,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"],
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        answer = ""
        if "choices" in body:
            answer = body["choices"][0]["message"].get("content", "")
        elif "output" in body:
            answer = json.dumps(body["output"], ensure_ascii=False)
        return {
            "answer": answer,
            "error": None,
            "latencyMs": int((time.time() - t0) * 1000),
            "usage": body.get("usage"),
        }
    except Exception as e:
        return {
            "answer": "",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "latencyMs": int((time.time() - t0) * 1000),
        }


def detect_mentions(answer: str, brand_names: list) -> dict:
    """检测答案里是否提到目标品牌"""
    if not answer:
        return {"mentioned": False, "mentionCount": 0}

    count = 0
    for name in brand_names:
        count += answer.count(name)
    return {"mentioned": count > 0, "mentionCount": count}


def extract_citations(answer: str) -> list:
    """从答案里提取 URL"""
    urls = re.findall(r"https?://[^\s\)\]\"'，。；]+", answer or "")
    out = []
    seen = set()
    for u in urls:
        u = u.rstrip(".,;:)]}")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def run_measure(prompts_file: str, engine: str, brand_names: list, out_file: str):
    with open(prompts_file, encoding="utf-8") as f:
        cfg = json.load(f)

    prompts = cfg.get("prompts") or cfg.get("questions") or []
    if not prompts:
        print("问题集为空")
        return 1

    print(f"问题集: {cfg.get('name', '-')} | {len(prompts)} 题 | 引擎: {engine}")
    results = []
    t0 = time.time()

    for i, p in enumerate(prompts, 1):
        q = p.get("question") if isinstance(p, dict) else str(p)
        branded = bool(p.get("branded")) if isinstance(p, dict) else False
        print(f"  [{i}/{len(prompts)}] {q[:40]} ...", end=" ", flush=True)

        r = call_engine(engine, q)
        m = detect_mentions(r["answer"], brand_names)
        cits = extract_citations(r["answer"])

        own = 0
        domains = []
        for u in cits:
            try:
                h = urlparse(u).netloc.replace("www.", "")
                domains.append(h)
                if any(b in h for b in brand_names):
                    own += 1
            except Exception:
                pass

        rec = {
            "question": q,
            "branded": branded,
            "answer": r["answer"],
            "error": r["error"],
            "latencyMs": r["latencyMs"],
            "mentioned": m["mentioned"],
            "mentionCount": m["mentionCount"],
            "citations": cits,
            "citationDomains": domains,
            "ownCitationCount": own,
        }
        results.append(rec)
        status = "✅" if m["mentioned"] else "—"
        print(f"{status} ({r['latencyMs']}ms, {len(cits)} 引用)")

    out = {
        "engine": engine,
        "brandNames": brand_names,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "durationSec": round(time.time() - t0, 1),
        "results": results,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n已保存: {out_file}")
    return 0


def print_report(path: str):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)

    results = d.get("results") or []
    if not results:
        print("无数据")
        return 1

    brand = [r for r in results if r.get("branded")]
    natural = [r for r in results if not r.get("branded")]

    def rate(lst):
        if not lst:
            return 0.0
        return round(sum(1 for x in lst if x.get("mentioned")) / len(lst) * 100, 1)

    total_cit = sum(len(r.get("citations") or []) for r in results)
    own_cit = sum(r.get("ownCitationCount") or 0 for r in results)

    print(f"\n{'='*50}")
    print(f"引擎: {d.get('engine')} | {len(results)} 题 | 耗时 {d.get('durationSec')}s")
    print(f"{'='*50}")
    print(f"提及率        {rate(results)}%")
    print(f"品牌题        {rate(brand)}%   ({len(brand)} 题)")
    print(f"自然题        {rate(natural)}%   ({len(natural)} 题)  ← 核心")
    print(f"自有引用      {own_cit} / {total_cit}")

    dom = {}
    for r in results:
        for h in (r.get("citationDomains") or []):
            dom[h] = dom.get(h, 0) + 1
    if dom:
        print(f"\n引用来源 Top 10:")
        for k, v in sorted(dom.items(), key=lambda x: -x[1])[:10]:
            print(f"  {v:3d}  {k}")

    errors = [r for r in results if r.get("error")]
    if errors:
        print(f"\n失败 {len(errors)} 题:")
        for e in errors[:3]:
            print(f"  {e['question'][:30]}: {e['error'][:60]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", help="问题集 JSON")
    ap.add_argument("--engine", default="deepseek", choices=list(ENGINES.keys()))
    ap.add_argument("--brand", default="", help="品牌名，逗号分隔")
    ap.add_argument("--out", default="result.json")
    ap.add_argument("--report", help="直接输出报告")
    args = ap.parse_args()

    if args.report:
        sys.exit(print_report(args.report))

    if not args.prompts:
        ap.print_help()
        sys.exit(1)

    brands = [b.strip() for b in args.brand.split(",") if b.strip()]
    if not brands:
        print("请用 --brand 指定品牌名")
        sys.exit(1)

    sys.exit(run_measure(args.prompts, args.engine, brands, args.out))


if __name__ == "__main__":
    main()
