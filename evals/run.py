"""Periodic LLM eval suite for the classifier and summarizer prompts.

Paid lane (real `claude -p` calls). Run before shipping a prompt or model change:

    uv run python -m evals.run                 # configured model
    uv run python -m evals.run --model claude-haiku-4-5-20251001
    uv run python -m evals.run --only classify

Graders are deterministic: primary-domain accuracy (threshold 85%), off-topic items
must land in `unsorted`, and per-summary checks for English output, numbers kept
verbatim, no invented numbers, no invented timestamps, size bounds and key entities.
Results are written to evals/results/<timestamp>.json (git-ignored) and the exit
code is non-zero when a threshold is missed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from kb_llm import ClaudeCodeClient, LLMError

from kb.config import load_settings
from kb.models import FetchedItem
from kb.summarizer import render_body, summarize
from kb.tagger import UNSORTED, tag

from .cases import CLASSIFY, SUMMARY, ClassifyCase, SummaryCase

CLASSIFY_THRESHOLD = 0.85
SUMMARY_THRESHOLD = 1.0  # every summary case must pass every check
RESULTS = Path(__file__).parent / "results"

_NUM = re.compile(r"\d[\d.,]*\d%?|\d%?")
_TS = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
_SPANISH_WORDS = re.compile(r"\b(el|los|las|que|del|una|para|con|por|más|fue|está)\b", re.IGNORECASE)


def norm_num(s: str) -> str:
    """'18.000' / '18,000' / '18000' all compare equal; the % sign is kept."""
    pct = s.endswith("%")
    digits = re.sub(r"[.,](?=\d{3}\b)", "", s.rstrip("%"))
    return digits + ("%" if pct else "")


def numbers(text: str) -> set[str]:
    text = _TS.sub(" ", text)
    return {norm_num(m.group(0).rstrip(".,")) for m in _NUM.finditer(text)}


def grade_summary(case: SummaryCase, text: str, key_points: int) -> dict[str, object]:
    src_nums = numbers(case.body)
    out_nums = numbers(text)
    missing = [n for n in case.must_keep_numbers if norm_num(n) not in out_nums]
    invented = sorted(
        n
        for n in out_nums - src_nums
        if n.rstrip("%").replace(".", "").isdigit() and float(n.rstrip("%").replace(",", "")) > 10
    )
    src_ts = set(_TS.findall(case.body))
    out_ts = _TS.findall(text)
    fake_ts = sorted(set(out_ts) - src_ts)
    checks = {
        "english": len(_SPANISH_WORDS.findall(text)) <= 2,
        "numbers_kept": not missing,
        "no_invented_numbers": not invented,
        "no_invented_timestamps": not fake_ts,
        "timestamps_cited": (len(out_ts) >= 2) if case.expect_timestamps else True,
        "size_bounds": case.min_key_points <= key_points <= case.max_key_points,
        "mentions": all(m.lower() in text.lower() for m in case.must_mention),
    }
    return {"checks": checks, "missing_numbers": missing, "invented_numbers": invented, "fake_timestamps": fake_ts}


def run_classify(settings, client, case: ClassifyCase) -> dict[str, object]:
    item = FetchedItem(source_type="web", url="https://example.com", title=case.title, body=case.body)
    t0 = time.time()
    try:
        r = tag(settings, client, item, case.hint_tags)
    except LLMError as e:
        return {"case": case.name, "ok": False, "error": str(e)}
    primary = r.domains[0]
    ok = primary == case.primary or primary in case.also_ok
    return {
        "case": case.name,
        "ok": ok,
        "expected": case.primary,
        "got": r.domains,
        "confidence": r.confidence,
        "method": r.method,
        "seconds": round(time.time() - t0, 1),
    }


def run_summary(settings, client, case: SummaryCase) -> dict[str, object]:
    t0 = time.time()
    try:
        s, _ = summarize(settings, client, case.meta, case.body)
    except LLMError as e:
        return {"case": case.name, "ok": False, "error": str(e)}
    text = render_body(s)
    g = grade_summary(case, text, len(s.key_points))
    return {
        "case": case.name,
        "ok": all(g["checks"].values()),
        **g,
        "summary": text,
        "seconds": round(time.time() - t0, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="override llm.model")
    ap.add_argument("--only", choices=["classify", "summary"])
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    settings = load_settings()
    model = args.model or settings.llm.model
    client = ClaudeCodeClient(model, binary=settings.llm.binary)
    report: dict[str, object] = {"model": model, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    ok = True

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        if args.only in (None, "classify"):
            res = list(pool.map(lambda c: run_classify(settings, client, c), CLASSIFY))
            acc = sum(r["ok"] for r in res) / len(res)
            offtopic = [r for r in res if r.get("expected") == UNSORTED]
            report["classify"] = {"accuracy": acc, "threshold": CLASSIFY_THRESHOLD, "cases": res}
            print(
                f"classifier: {acc:.0%} primary accuracy ({sum(r['ok'] for r in res)}/{len(res)}), threshold {CLASSIFY_THRESHOLD:.0%}"
            )
            for r in res:
                if not r["ok"]:
                    print(f"  FAIL {r['case']}: expected {r.get('expected')}, got {r.get('got')} {r.get('error', '')}")
            print(f"  off-topic -> unsorted: {sum(r['ok'] for r in offtopic)}/{len(offtopic)}")
            ok &= acc >= CLASSIFY_THRESHOLD
        if args.only in (None, "summary"):
            res = list(pool.map(lambda c: run_summary(settings, client, c), SUMMARY))
            rate = sum(r["ok"] for r in res) / len(res)
            report["summary"] = {"pass_rate": rate, "threshold": SUMMARY_THRESHOLD, "cases": res}
            print(f"summarizer: {sum(r['ok'] for r in res)}/{len(res)} cases pass all checks")
            for r in res:
                if not r["ok"]:
                    failed = [k for k, v in r.get("checks", {}).items() if not v]
                    print(
                        f"  FAIL {r['case']}: {failed or r.get('error')} missing={r.get('missing_numbers')} invented={r.get('invented_numbers')} fake_ts={r.get('fake_timestamps')}"
                    )
            ok &= rate >= SUMMARY_THRESHOLD

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{model.replace('/', '_')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"results: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
